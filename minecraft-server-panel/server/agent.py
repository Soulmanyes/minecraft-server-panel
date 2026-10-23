#!/usr/bin/env python3
"""Server panel agent: runs on the server box, called over ssh by client.py.

  agent.py status                 JSON: host load/RAM/disk and every Minecraft server
  agent.py action start|stop|restart|backup ID
  agent.py action restore ID BACKUP.zip
  agent.py auto-restart ID on|off  restart this server by itself after a crash
  agent.py watch                   run by cron every minute while any server has auto-restart
  agent.py mods ID                 JSON list of a server's mods (read-only)
  agent.py ram ID MIN_MB MAX_MB    set -Xms / -Xmx (used from the next start)
  agent.py free                    JSON: free disk space in bytes
  agent.py unpack UPLOAD NAME      unzip APP/uploads/UPLOAD into a new ~/<folder>
  agent.py fetch URL               download a server pack zip and unpack it (in the background)
  agent.py unpack-home NAME        unpack ~/NAME.zip already on the box (in the background; kept)
  agent.py setup KIND ID [ARG]     a step of a new server's setup: port N, eula, script NAME,
                                   convert, save-run, make-run JAR, java PATH, finish, remove
                                  starts the action in the background and returns at once;
                                  its progress shows up in `status`

A server is any folder in $HOME with server.properties and run.sh. A folder
unpacked from the panel carries a marker file (MARKER) and stays in the
status's "setup" list until its setup is finished: a start script, a port
picked and the EULA accepted. Servers run
in tmux like they were started by hand: start types `bash run.sh` into the
server's own tmux session, named after it (my-pack), creating it when
needed; stop types `stop` into the server's console and waits for it to save
and exit. Nothing is ever killed.
Backup zips the world to ~/backups/minecraft/<id>/ (on a running server
between save-off / save-all flush and save-on) and keeps the newest KEEP.
Restore stops the server if it runs, renames the world to
<world>.before-restore-<date> (never deleted), unzips the backup and starts
the server again if it was running.

Auto-restart (opt-in per server, APP/config.json) is done by
`watch` from cron, so it works while the desktop is off. It restarts a server
that went down without "Stopping the server" in its log (a /stop) or that
wrote a new crash report, and gives up after AUTO_MAX restarts in AUTO_WINDOW.

APP is the folder this script was deployed to (~/minecraft-server-panel by
default); it also holds state/, uploads/ and file-backups/.

Python 3.9, stdlib only.
"""
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

HOME = os.path.expanduser("~")
APP = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(APP, "state")
STOP_WAIT = 300  # a big modpack can take minutes to save
START_WAIT = 60  # until the java process shows up
BOOT_GRACE = 900  # java up this long without answering pings counts as unresponsive, not starting
TICK = os.sysconf("SC_CLK_TCK")
BUSY_MAX = 900  # an action file older than this that never finished is stale
BACKUPS = os.path.join(HOME, "backups", "minecraft")
KEEP = 10
SAVE_WAIT = 120  # for "Saved the game" after save-all flush
LAG = re.compile(r"Can't keep up!.*?Running (\d+)ms")
LAG_KEEP = 600  # seconds of lag warnings remembered per server
LAG_BOOT = 300  # a pack lags while it loads; don't count that
CONFIG = os.path.join(APP, "config.json")
AUTO_MAX, AUTO_WINDOW = 3, 1800
CRON_TAG = f"# {os.path.basename(APP)} watch"
# Folders the packs' own backup mods write to (Simple Backups, FTB Backups, Advanced Backups).
MOD_BACKUP_DIRS = ("simplebackups", "backups")
MARKER = ".server-panel.json"  # in a folder unpacked from the panel
UPLOADS = os.path.join(APP, "uploads")
FETCH = os.path.join(STATE, "_fetch.json")  # progress of `fetch`
INSTALL_WAIT = 1800  # for a Forge/NeoForge installer to finish


# Discovery -----------------------------------------------------------------

def slug(name):
	return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def pretty(folder):
	"""My_Pack_3-13.3_server_pack -> My Pack 3, ABC2 1.8 - Server -> ABC2."""
	name = folder.replace("_", " ")
	name = re.split(r"(?i)[\s-]*server", name)[0] or name
	name = re.sub(r"[\s-]+\d+\.[\d.]*$", "", name).strip(" -")
	# "Server 1.3.5.2" would come out as just "Server": keep the folder name then.
	return folder.replace("_", " ") if name.lower() in ("", "server") else name


def properties(path):
	out = {}
	try:
		with open(path, errors="replace") as f:
			for line in f:
				if "=" in line and not line.lstrip().startswith("#"):
					k, v = line.rstrip("\n").split("=", 1)
					out[k.strip()] = v.strip()
	except OSError:
		pass
	return out


HEAP_FLAG = re.compile(r"-Xm([sx])(\d+)([kKmMgG])")
UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}


def ram_file(folder):
	"""The file run.sh takes its JVM flags from: user_jvm_args.txt when run.sh
	reads it (Forge/NeoForge), else run.sh itself (java -Xmx... -jar). A
	start.sh (ServerStarter) pack without run.sh keeps them in variables.txt."""
	try:
		with open(os.path.join(folder, "run.sh"), errors="replace") as f:
			if "@user_jvm_args.txt" in f.read():
				return "user_jvm_args.txt"
	except FileNotFoundError:
		if os.path.isfile(os.path.join(folder, "variables.txt")):
			return "variables.txt"
	except OSError:
		pass
	return "run.sh"


def heap(folder):
	"""{min, max, file} in bytes from the flags run.sh uses; 0 when not set."""
	name = ram_file(folder)
	out = {"min": 0, "max": 0, "file": name}
	try:
		with open(os.path.join(folder, name), errors="replace") as f:
			text = "\n".join(l for l in f if not l.lstrip().startswith("#"))
	except OSError:
		return out
	for kind, n, unit in HEAP_FLAG.findall(text):
		out["min" if kind == "s" else "max"] = int(n) * UNITS[unit.lower()]
	return out


def heap_bytes(folder):
	return heap(folder)["max"]


def last_crash(folder):
	"""mtime of the newest crash report or JVM crash log in a server folder, or 0."""
	paths = [os.path.join(folder, n) for n in os.listdir(folder) if n.startswith("hs_err_pid") and n.endswith(".log")]
	reports = os.path.join(folder, "crash-reports")
	if os.path.isdir(reports):
		paths += [os.path.join(reports, n) for n in os.listdir(reports) if n.endswith(".txt")]
	return max((int(os.path.getmtime(p)) for p in paths), default=0)


def marker(folder):
	"""The marker of a folder unpacked from the panel ({} if unreadable), or None."""
	try:
		with open(os.path.join(folder, MARKER)) as f:
			data = json.load(f)
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def start_script(folder, mark):
	"""The script Start runs: the one picked in the panel, else run.sh, else None."""
	for name in ((mark or {}).get("start"), "run.sh"):
		if name and os.path.isfile(os.path.join(folder, name)):
			return name
	return None


def describe(entry, folder, mark):
	props = properties(os.path.join(folder, "server.properties"))
	port = props.get("server-port", "")
	return {
		"id": slug(entry),
		"name": pretty(entry),
		"folder": entry,
		"dir": folder,
		"port": int(port) if port.isdigit() else 25565,
		"motd": props.get("motd", ""),
		"max_players": int(props["max-players"]) if props.get("max-players", "").isdigit() else 0,
		"heap": heap_bytes(folder),
		"ip": props.get("server-ip") or "127.0.0.1",
		"level": props.get("level-name") or "world",
		"script": start_script(folder, mark),
		"mark": mark,
	}


def scan():
	"""(servers, setup): the servers, and the folders unpacked from the panel
	whose setup isn't finished yet."""
	ready, setup = [], []
	for entry in sorted(os.listdir(HOME)):
		folder = os.path.join(HOME, entry)
		if not os.path.isdir(folder):
			continue
		mark = marker(folder)
		if mark is not None and not mark.get("ready"):
			setup.append(describe(entry, folder, mark))
		elif os.path.isfile(os.path.join(folder, "server.properties")) and start_script(folder, mark):
			ready.append(describe(entry, folder, mark))
	return ready, setup


def servers():
	return scan()[0]


# Processes and tmux --------------------------------------------------------

def proc_stat(pid):
	"""(ppid, cpu ticks, start ticks) of a process, or None."""
	try:
		with open(f"/proc/{pid}/stat") as f:
			fields = f.read().rsplit(")", 1)[1].split()
	except OSError:
		return None
	return int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[19])


def java_by_dir():
	"""{folder: pid} for every java process, by its working directory."""
	out = {}
	for pid in os.listdir("/proc"):
		if not pid.isdigit():
			continue
		try:
			with open(f"/proc/{pid}/comm") as f:
				if f.read().strip() != "java":
					continue
			out[os.readlink(f"/proc/{pid}/cwd")] = int(pid)
		except OSError:
			continue
	return out


def rss_bytes(pid):
	try:
		with open(f"/proc/{pid}/status") as f:
			for line in f:
				if line.startswith("VmRSS:"):
					return int(line.split()[1]) * 1024
	except OSError:
		pass
	return 0


def tmux_panes():
	"""[{id, session, pid, command, cwd}] of every tmux pane, [] without tmux."""
	try:
		out = subprocess.run(["tmux", "list-panes", "-a", "-F",
			"#{pane_id}\t#{session_name}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"],
			capture_output=True, text=True, timeout=5).stdout
	except (OSError, subprocess.TimeoutExpired):
		return []
	panes = []
	for line in out.splitlines():
		parts = line.split("\t")
		if len(parts) == 5:
			panes.append({"id": parts[0], "session": parts[1], "pid": int(parts[2]),
				"command": parts[3], "cwd": parts[4]})
	return panes


def pane_of(pid, panes):
	"""The tmux pane a process runs in (its pane shell is an ancestor), or None."""
	by_pid = {p["pid"]: p for p in panes}
	for _ in range(20):
		if pid in by_pid:
			return by_pid[pid]
		stat = proc_stat(pid)
		if stat is None or stat[0] <= 1:
			return None
		pid = stat[0]
	return None


def listening_ports():
	ports = set()
	for name in ("/proc/net/tcp", "/proc/net/tcp6"):
		try:
			with open(name) as f:
				next(f)
				for line in f:
					parts = line.split()
					if parts[3] == "0A":  # LISTEN
						ports.add(int(parts[1].rsplit(":", 1)[1], 16))
		except OSError:
			pass
	return ports


# Minecraft server list ping ------------------------------------------------

def varint(n):
	out = b""
	while True:
		b = n & 0x7F
		n >>= 7
		out += bytes([b | (0x80 if n else 0)])
		if not n:
			return out


def read_varint(sock):
	n = shift = 0
	while True:
		b = sock.recv(1)
		if not b:
			raise OSError("closed")
		n |= (b[0] & 0x7F) << shift
		shift += 7
		if not b[0] & 0x80:
			return n


def ping(port, ip="127.0.0.1", timeout=2.0):
	"""The status JSON a Minecraft client sees in its server list, or None."""
	try:
		with socket.create_connection((ip, port), timeout=timeout) as s:
			host = b"localhost"
			handshake = varint(0) + varint(767) + varint(len(host)) + host + struct.pack(">H", port) + varint(1)
			s.sendall(varint(len(handshake)) + handshake + varint(1) + varint(0))
			read_varint(s)  # packet length
			read_varint(s)  # packet id
			size = read_varint(s)
			data = b""
			while len(data) < size:
				chunk = s.recv(size - len(data))
				if not chunk:
					return None
				data += chunk
		return json.loads(data.decode("utf-8", "replace"))
	except (OSError, ValueError):
		return None


def motd_text(desc):
	if isinstance(desc, str):
		return re.sub(r"§.", "", desc)
	if isinstance(desc, dict):
		return motd_text(desc.get("text", "")) + "".join(motd_text(x) for x in desc.get("extra", []))
	if isinstance(desc, list):
		return "".join(motd_text(x) for x in desc)
	return ""


# Host ----------------------------------------------------------------------

def cpu_total():
	with open("/proc/stat") as f:
		fields = [int(x) for x in f.readline().split()[1:]]
	idle = fields[3] + fields[4]
	return sum(fields), idle


def meminfo():
	out = {}
	with open("/proc/meminfo") as f:
		for line in f:
			k, v = line.split(":", 1)
			out[k] = int(v.split()[0]) * 1024
	return out


def host(cpu_pct):
	mem = meminfo()
	disk = os.statvfs(HOME)
	with open("/proc/uptime") as f:
		uptime = float(f.read().split()[0])
	return {
		"cpu": cpu_pct,
		"cores": os.cpu_count(),
		"load": list(os.getloadavg()),
		"mem_total": mem["MemTotal"],
		"mem_used": mem["MemTotal"] - mem["MemAvailable"],
		"disk_total": disk.f_blocks * disk.f_frsize,
		"disk_used": (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
		"uptime": int(uptime),
	}


# Lag -----------------------------------------------------------------------

def lag(s, uptime):
	"""{recent, last, behind_ms} from "Can't keep up!" lines that appeared in
	latest.log since the previous status call, remembered for LAG_KEEP seconds.
	The first call for a log starts at its end, so old warnings don't count,
	and lines from the first LAG_BOOT seconds (world loading) are skipped."""
	log = os.path.join(s["dir"], "logs", "latest.log")
	path = os.path.join(STATE, s["id"] + ".lag.json")
	try:
		with open(path) as f:
			mem = json.load(f)
	except (OSError, ValueError):
		mem = {}
	try:
		st = os.stat(log)
	except OSError:
		return None
	now = time.time()
	events = [e for e in mem.get("events", []) if now - e[0] < LAG_KEEP]
	offset = mem.get("offset")
	if mem.get("inode") != st.st_ino or offset is None or offset > st.st_size:
		offset = st.st_size if offset is None else 0  # new log after a restart: read it all
	if st.st_size > offset:
		with open(log, "rb") as f:
			f.seek(offset)
			chunk = f.read(4 << 20).decode("utf-8", "replace")
			offset = f.tell()
		if uptime >= LAG_BOOT:
			events += [[int(now), int(m.group(1))] for m in LAG.finditer(chunk)]
	os.makedirs(STATE, exist_ok=True)
	with open(path + ".tmp", "w") as f:
		json.dump({"inode": st.st_ino, "offset": offset, "events": events}, f)
	os.replace(path + ".tmp", path)
	if not events:
		return None
	recent = [e for e in events if now - e[0] < 300]
	return {"recent": len(recent), "last": events[-1][0], "behind_ms": max(e[1] for e in recent or events[-1:])}


# RAM ------------------------------------------------------------------------

FILE_BACKUPS = os.path.join(APP, "file-backups")


def heap_flag(kind, mb):
	return f"-Xm{kind}{mb // 1024}G" if mb % 1024 == 0 else f"-Xm{kind}{mb}M"


def set_flags(text, flags, add):
	"""text with its -Xms/-Xmx replaced by flags ({"s": "-Xms4G", "x": ...}) on
	lines that aren't comments; add(text, missing) puts in flags it lacked."""
	seen = set()

	def sub(m):
		seen.add(m.group(1))
		return flags[m.group(1)]
	lines = [l if l.lstrip().startswith("#") else HEAP_FLAG.sub(sub, l) for l in text.split("\n")]
	text = "\n".join(lines)
	missing = [flags[k] for k in ("s", "x") if k not in seen]
	return add(text, missing) if missing else text


def append_line(text, missing):
	return text.rstrip("\n") + "\n" + " ".join(missing) + "\n"


def after_java(text, missing):
	"""For run.sh: put the flags right after `java` on the line that starts it."""
	out = []
	done = False
	for line in text.split("\n"):
		if not done and not line.lstrip().startswith("#") and JAVA_LINE.search(line):
			# After `java` or a full path to it (set by the panel's Java choice).
			line = JAVA_LINE.sub(lambda m: m.group(1) + m.group(2) + " " + " ".join(missing) + m.group(3), line, count=1)
			done = True
		out.append(line)
	if not done:
		raise RuntimeError("couldn't find the java line in run.sh")
	return "\n".join(out)


def no_flags(text, missing):
	raise RuntimeError("variables.txt has no -Xms / -Xmx to change")


def set_ram(s, min_mb, max_mb):
	total_mb = meminfo()["MemTotal"] >> 20
	if not 512 <= min_mb <= max_mb:
		raise RuntimeError("min must be at least 512 MB and not above max")
	if max_mb > total_mb - 2048:
		raise RuntimeError(f"max can be at most {(total_mb - 2048) / 1024:.1f} GB (the box has {total_mb / 1024:.1f} GB)")
	flags = {"s": heap_flag("s", min_mb), "x": heap_flag("x", max_mb)}
	name = ram_file(s["dir"])
	edits = [(name, append_line if name == "user_jvm_args.txt" else after_java)]
	# start.sh (ServerStarter) packs keep a copy in variables.txt: keep it the same.
	variables = os.path.join(s["dir"], "variables.txt")
	if name == "variables.txt":
		edits = [(name, no_flags)]
	elif os.path.exists(variables) and HEAP_FLAG.search(open(variables, errors="replace").read()):
		edits.append(("variables.txt", lambda text, missing: text))
	stamp = time.strftime("%Y%m%d-%H%M%S")
	folder = os.path.join(FILE_BACKUPS, s["id"])
	for fname, add in edits:
		path = os.path.join(s["dir"], fname)
		with open(path, errors="surrogateescape") as f:
			old = f.read()
		new = set_flags(old, flags, add)
		if new != old:
			os.makedirs(folder, exist_ok=True)
			with open(os.path.join(folder, f"{fname}.{stamp}"), "w", errors="surrogateescape") as f:
				f.write(old)
			with open(path + ".tmp", "w", errors="surrogateescape") as f:
				f.write(new)
			os.chmod(path + ".tmp", os.stat(path).st_mode)
			os.replace(path + ".tmp", path)
	return heap(s["dir"])


# Pack and mods (read-only) -------------------------------------------------

LOADER = re.compile(r"(neoforge|forge|fabric)[/-](?:[a-z]+/)?(\d[\w.+-]*?)(?:/|\s|$)")


def pack_info(s):
	"""{version, loader, mods}: the pack version from a CurseForge manifest.json or
	the folder name, the mod loader from the start scripts, and the mod count."""
	version = ""
	try:
		with open(os.path.join(s["dir"], "manifest.json")) as f:
			manifest = json.load(f)
		if isinstance(manifest, dict) and manifest.get("version"):
			version = str(manifest["version"])
	except (OSError, ValueError):
		pass
	if not version:
		found = re.findall(r"\d+(?:\.\d+)+", s["folder"])
		version = found[-1] if found else ""
	loader = ""
	for name in ("run.sh", "user_jvm_args.txt"):
		try:
			with open(os.path.join(s["dir"], name), errors="replace") as f:
				m = LOADER.search(f.read())
		except OSError:
			continue
		if m:
			loader = {"neoforge": "NeoForge", "forge": "Forge", "fabric": "Fabric"}[m.group(1)] + " " + \
				re.sub(r"(-universal|-server)?\.jar$", "", m.group(2))
			break
	mods = os.path.join(s["dir"], "mods")
	count = len([n for n in os.listdir(mods) if n.endswith(".jar")]) if os.path.isdir(mods) else 0
	return {"version": version, "loader": loader, "mods": count}


def toml_value(text, key):
	m = re.search(r"^\s*" + key + r"\s*=\s*[\"']([^\"'\n]*)[\"']", text, re.MULTILINE)
	return m.group(1) if m else ""


def jar_info(path):
	"""{file, id, name, version} from a mod jar's metadata, best effort."""
	info = {"file": os.path.basename(path), "id": "", "name": "", "version": ""}
	manifest = ""
	try:
		with zipfile.ZipFile(path) as z:
			names = set(z.namelist())
			manifest = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace") if "META-INF/MANIFEST.MF" in names else ""
			for meta in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
				if meta in names:
					text = z.read(meta).decode("utf-8", "replace")
					block = text.split("[[mods]]", 1)[-1]
					info.update(id=toml_value(block, "modId"), name=toml_value(block, "displayName"),
						version=toml_value(block, "version"))
					break
			else:
				if "fabric.mod.json" in names:
					data = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"))
					info.update(id=data.get("id", ""), name=data.get("name", ""), version=data.get("version", ""))
	except (OSError, ValueError, zipfile.BadZipFile, KeyError):
		pass
	if not info["version"] or "${" in info["version"]:
		m = re.search(r"^Implementation-Version:\s*(\S+)", manifest, re.MULTILINE)
		info["version"] = m.group(1) if m else ""
	return info


def mod_list(s):
	"""All mods of a server, sorted by name; cached until the mods folder changes."""
	mods = os.path.join(s["dir"], "mods")
	if not os.path.isdir(mods):
		return []
	jars = sorted(n for n in os.listdir(mods) if n.endswith(".jar"))
	key = [int(os.path.getmtime(mods)), len(jars)]
	cache = os.path.join(STATE, s["id"] + ".mods.json")
	try:
		with open(cache) as f:
			cached = json.load(f)
		if cached.get("key") == key:
			return cached["mods"]
	except (OSError, ValueError):
		pass
	out = sorted((jar_info(os.path.join(mods, n)) for n in jars), key=lambda m: (m["name"] or m["file"]).lower())
	save_json(cache, {"key": key, "mods": out})
	return out


# Auto-restart --------------------------------------------------------------

def load_config():
	try:
		with open(CONFIG) as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def save_json(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path + ".tmp", "w") as f:
		json.dump(data, f)
	os.replace(path + ".tmp", path)


def set_cron(enabled):
	"""Adds or removes the one crontab line that runs `watch` every minute."""
	current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
	lines = [l for l in current.splitlines() if CRON_TAG not in l]
	if enabled:
		lines.append(f"* * * * * {sys.executable} -I {os.path.abspath(__file__)} watch >/dev/null 2>&1 {CRON_TAG}")
	subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def set_auto(sid, on):
	find(sid)
	config = load_config()
	ids = [i for i in config.get("auto_restart", []) if i != sid]
	if on:
		ids.append(sid)
	config["auto_restart"] = ids
	save_json(CONFIG, config)
	set_cron(bool(ids))
	return ids


def stopped_on_purpose(s):
	"""A /stop in the current log and no newer crash report."""
	try:
		with open(os.path.join(s["dir"], "logs", "latest.log"), "rb") as f:
			f.seek(max(0, os.path.getsize(f.name) - (256 << 10)))
			tail = f.read().decode("utf-8", "replace")
	except OSError:
		return False
	return "Stopping the server" in tail


def watch():
	ids = set(load_config().get("auto_restart", []))
	path = os.path.join(STATE, "watch.json")
	try:
		with open(path) as f:
			mem = json.load(f)
	except (OSError, ValueError):
		mem = {}
	javas = java_by_dir()
	now = time.time()
	for s in servers():
		if s["id"] not in ids:
			mem.pop(s["id"], None)
			continue
		m = mem.setdefault(s["id"], {"up": False, "crash": last_crash(s["dir"]), "restarts": []})
		m["restarts"] = [t for t in m["restarts"] if now - t < AUTO_WINDOW]
		up = bool(javas.get(s["dir"]))
		crash = last_crash(s["dir"])
		action = read_action(s["id"]) or {}
		busy = action.get("phase") not in (None, "done", "failed") and now - action.get("updated", 0) < BUSY_MAX
		# A Stop from the panel that finished after the server was last seen up.
		panel_stop = action.get("action") == "stop" and action.get("phase") == "done" \
			and action.get("updated", 0) >= m.get("seen_up", 0) - 90
		if m["up"] and not up and not busy and not panel_stop:
			if crash <= m["crash"] and stopped_on_purpose(s):
				pass  # someone typed /stop in the console
			elif len(m["restarts"]) >= AUTO_MAX:
				m["gave_up"] = int(now)
			else:
				m["restarts"].append(int(now))
				m["last"] = int(now)
				write_action(s["id"], action="start", phase="queued", started=int(now), auto=True)
				subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "_run", "start", s["id"], "auto"],
					stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
				up = True  # don't count the same crash twice while it boots
		if javas.get(s["dir"]):
			m["seen_up"] = int(now)
			m.pop("gave_up", None)  # running again, so it was started by hand
		m["up"], m["crash"] = up, crash
	save_json(path, mem)


def auto_info(sid, mem):
	m = mem.get(sid) or {}
	now = time.time()
	return {"restarts": len([t for t in m.get("restarts", []) if now - t < AUTO_WINDOW]),
		"last": m.get("last", 0), "gave_up": m.get("gave_up", 0)}


# Status --------------------------------------------------------------------

def read_action(sid):
	try:
		with open(os.path.join(STATE, sid + ".json")) as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def write_action(sid, **fields):
	os.makedirs(STATE, exist_ok=True)
	tmp = os.path.join(STATE, sid + ".json.tmp")
	with open(tmp, "w") as f:
		json.dump(dict(fields, updated=int(time.time())), f)
	os.replace(tmp, os.path.join(STATE, sid + ".json"))


def read_fetch():
	try:
		with open(FETCH) as f:
			fetch = json.load(f)
	except (OSError, ValueError):
		return None
	if fetch.get("phase") not in ("done", "failed") and time.time() - fetch.get("updated", 0) > BUSY_MAX:
		fetch = dict(fetch, phase="failed", error="the download stopped without finishing")
	return fetch


def setup_entry(s, procs, panes):
	"""What a folder unpacked from the panel still needs before it can start."""
	d, mark = s["dir"], s["mark"]
	top = sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))
	low = {n: n.lower() for n in top}
	entry = {k: s[k] for k in ("id", "name", "folder")}
	entry.update({
		"state": "installing" if procs.get(d) else "setup",
		"script": s["script"] or "",
		"bats": [n for n in top if low[n].endswith(".bat") and ("run" in low[n] or "start" in low[n])],
		"installers": [n for n in top if low[n].endswith(".jar") and "installer" in low[n]],
		"starters": [n for n in top if n.endswith(".sh") and n != "run.sh" and "start" in low[n]],
		# A server jar to run directly (old Forge, Fabric's launcher), not the vanilla jar or an installer.
		"jars": [n for n in top if low[n].endswith(".jar") and "installer" not in low[n]
			and not low[n].startswith("minecraft_server") and ("forge" in low[n] or "server" in low[n] or "fabric" in low[n])],
		"port": mark.get("port", 0),
		"eula": properties(os.path.join(d, "eula.txt")).get("eula", "").lower() == "true",
		"source": mark.get("source", ""),
		"installed": mark.get("installed", 0),
	})
	entry["mc"], entry["java_needed"] = mc_java(top + [s["folder"]])
	entry["javas"] = [{"major": k, "path": v} for k, v in sorted(javas().items())]
	entry["java"] = script_java(d, s["script"]) if s["script"] else ""
	if s["script"]:
		h = heap(d)
		entry["ram"] = {"min": h["min"], "max": h["max"], "file": h["file"]}
	pane = next((p for p in panes if p["session"] == session_name(s)), None)
	entry["tmux"] = pane["session"] if pane else ""
	action = read_action(s["id"])
	if action and action.get("phase") not in ("done", "failed") and time.time() - action.get("updated", 0) > BUSY_MAX:
		action = dict(action, phase="failed", error="the action stopped without finishing")
	if action:
		entry["action"] = action
	return entry


def status():
	found, setup = scan()
	auto = set(load_config().get("auto_restart", []))
	try:
		with open(os.path.join(STATE, "watch.json")) as f:
			watch_mem = json.load(f)
	except (OSError, ValueError):
		watch_mem = {}
	javas = java_by_dir()
	panes = tmux_panes()
	with open("/proc/uptime") as f:
		boot = time.time() - float(f.read().split()[0])

	# One short sample for host and per-server CPU.
	pids = {s["id"]: javas.get(s["dir"]) for s in found}
	t0, i0 = cpu_total()
	p0 = {sid: proc_stat(pid) for sid, pid in pids.items() if pid}
	time.sleep(0.4)
	t1, i1 = cpu_total()
	p1 = {sid: proc_stat(pid) for sid, pid in pids.items() if pid}
	span = max(1, t1 - t0)
	out = []
	for s in found:
		pid = pids[s["id"]]
		entry = {k: s[k] for k in ("id", "name", "folder", "port", "heap")}
		entry["crash"] = last_crash(s["dir"])
		entry["backups"] = own_backups(s["id"])
		entry["mod_backups"] = mod_backups(s["dir"])
		entry["pack"] = pack_info(s)
		h = heap(s["dir"])
		entry["ram"] = {"min": h["min"], "max": h["max"], "file": h["file"]}
		if pid and p1.get(s["id"]):
			# Edited after this java started: it only applies from the next start.
			started = boot + p1[s["id"]][2] / TICK
			try:
				entry["ram"]["pending"] = os.path.getmtime(os.path.join(s["dir"], h["file"])) > started
			except OSError:
				entry["ram"]["pending"] = False  # e.g. a start.sh pack before it made run.sh
		entry["auto_restart"] = s["id"] in auto
		if entry["auto_restart"]:
			entry["auto"] = auto_info(s["id"], watch_mem)
		entry["state"] = "stopped"
		if pid and p0.get(s["id"]) and p1.get(s["id"]):
			stat = p1[s["id"]]
			entry.update({
				"pid": pid,
				# share of the whole machine, like top's per-process % divided by cores
				"cpu": round(100.0 * (stat[1] - p0[s["id"]][1]) / span, 1),
				"rss": rss_bytes(pid),
				"uptime": int(time.time() - (boot + stat[2] / TICK)),
			})
			pane = pane_of(pid, panes)
			entry["tmux"] = pane["session"] if pane else ""
			entry["lag"] = lag(s, entry["uptime"])
			info = ping(s["port"], s["ip"])
			if info:
				players = info.get("players") or {}
				entry.update({
					# Old Forge answers pings while it still loads, with no player slots yet.
					"state": "running" if players.get("max") else "starting",
					"online": players.get("online", 0),
					"max": players.get("max", s["max_players"]),
					"names": [p.get("name", "") for p in players.get("sample") or [] if p.get("name")],
					"version": (info.get("version") or {}).get("name", ""),
					"motd": motd_text(info.get("description")) or s["motd"],
				})
			else:
				entry["state"] = "starting" if entry["uptime"] < BOOT_GRACE else "unresponsive"
		else:
			own = session_name(s)
			pane = next((p for p in panes if p["session"] == own), None) or \
				next((p for p in panes if p["cwd"] == s["dir"]), None)
			entry["tmux"] = pane["session"] if pane else ""
		action = read_action(s["id"])
		if action and action.get("phase") not in ("done", "failed") and time.time() - action.get("updated", 0) > BUSY_MAX:
			action = dict(action, phase="failed", error="the action stopped without finishing")
		if action:
			entry["action"] = action
			phase = action.get("phase")
			if phase == "stopping" and pid:
				entry["state"] = "stopping"
			elif not pid and (phase == "starting" or (phase == "queued" and action.get("action") == "start")):
				entry["state"] = "starting"
		out.append(entry)
	return {"time": int(time.time()), "host": host(round(100.0 * (1 - (i1 - i0) / span), 1)), "servers": out,
		"setup": [setup_entry(s, javas, panes) for s in setup], "fetch": read_fetch(), "box_zips": box_zips()}


# Actions -------------------------------------------------------------------

def find(sid):
	ready, setup = scan()
	for s in ready + setup:
		if s["id"] == sid:
			return s
	raise SystemExit(f"no server {sid}")


def tmux(*args):
	subprocess.run(["tmux", *args], check=True, timeout=10)


def session_name(s):
	"""A short tmux session name for a server: my-pack-3, abc2."""
	return slug(s["name"])[:24].strip("-") or s["id"][:24]


def do_stop(s):
	"""Stops s; returns the tmux session it ran in (or None)."""
	pid = java_by_dir().get(s["dir"])
	if not pid:
		return None
	pane = pane_of(pid, tmux_panes())
	if pane is None:
		raise RuntimeError("it isn't running in tmux, stop it by hand")
	tmux("send-keys", "-t", pane["id"], "stop", "Enter")
	deadline = time.time() + STOP_WAIT
	while time.time() < deadline:
		if proc_stat(pid) is None:
			return pane["session"]
		time.sleep(2)
	raise RuntimeError(f"still running {STOP_WAIT // 60} min after `stop`")


def start_problem(s):
	"""Why s can't start now, or None."""
	if java_by_dir().get(s["dir"]):
		return "it is already running"
	if s["port"] in listening_ports():
		other = next((o for o in servers() if o["port"] == s["port"] and java_by_dir().get(o["dir"])), None)
		return f"port {s['port']} is taken by " + (other["name"] if other else "another program")
	eula = properties(os.path.join(s["dir"], "eula.txt"))
	if eula.get("eula", "").lower() != "true":
		return "eula.txt isn't accepted"
	free = meminfo()["MemAvailable"]
	if s["heap"] and s["heap"] + (1 << 30) > free:
		return f"it needs {s['heap'] / (1 << 30):.1f} GB RAM plus 1 GB to spare, {free / (1 << 30):.1f} GB is free"
	return None


SHELLS = ("bash", "sh", "zsh")


def console(s, old_session=None):
	"""The tmux target to type s's commands into: its own session
	(session_name), created when needed. After a restart, old_session is the
	session it ran in: when it holds just that one pane it is renamed, so the
	console keeps its scrollback under the new name."""
	name = session_name(s)
	sessions = {}
	for p in tmux_panes():
		sessions.setdefault(p["session"], []).append(p)
	if old_session and old_session != name and name not in sessions and len(sessions.get(old_session, [])) == 1:
		tmux("rename-session", "-t", old_session, name)
		sessions[name] = sessions.pop(old_session)
	target, n = None, 1
	while target is None:
		candidate = name if n == 1 else f"{name}-{n}"
		panes = sessions.get(candidate)
		if panes is None:
			tmux("new-session", "-d", "-s", candidate, "-c", s["dir"])
			target = candidate
		elif len(panes) == 1 and panes[0]["cwd"] == s["dir"] and panes[0]["command"] in SHELLS:
			target = panes[0]["id"]  # its own idle session from an earlier run
		n += 1
	return target


def do_start(s, old_session=None):
	"""Starts s by typing `bash <its start script>` into its console."""
	problem = start_problem(s)
	if problem:
		raise RuntimeError(problem)
	tmux("send-keys", "-t", console(s, old_session), "bash " + shlex.quote(s["script"]), "Enter")
	deadline = time.time() + START_WAIT
	while time.time() < deadline:
		if java_by_dir().get(s["dir"]):
			return
		time.sleep(2)
	raise RuntimeError("java didn't start; look at its tmux console")


# Backups -------------------------------------------------------------------

def own_backups(sid):
	"""[{name, size, time}] of this app's backups of a server, newest first."""
	folder = os.path.join(BACKUPS, sid)
	out = []
	if os.path.isdir(folder):
		for n in os.listdir(folder):
			if n.endswith(".zip"):
				st = os.stat(os.path.join(folder, n))
				out.append({"name": n, "size": st.st_size, "time": int(st.st_mtime)})
	return sorted(out, key=lambda b: b["time"], reverse=True)


def mod_backups(folder):
	"""{count, size, newest} of the backups a pack's own mod made, or None."""
	count = size = newest = 0
	for d in MOD_BACKUP_DIRS:
		path = os.path.join(folder, d)
		if not os.path.isdir(path):
			continue
		for n in os.listdir(path):
			if n.endswith(".zip"):
				st = os.stat(os.path.join(path, n))
				count, size, newest = count + 1, size + st.st_size, max(newest, int(st.st_mtime))
	return {"count": count, "size": size, "newest": newest} if count else None


def dir_size(path):
	total = 0
	for root, _dirs, files in os.walk(path):
		for n in files:
			try:
				total += os.lstat(os.path.join(root, n)).st_size
			except OSError:
				pass
	return total


def wait_for_save(log, offset):
	"""Waits until latest.log shows "Saved the game" after offset."""
	deadline = time.time() + SAVE_WAIT
	while time.time() < deadline:
		try:
			with open(log, errors="replace") as f:
				f.seek(offset)
				if "Saved the game" in f.read():
					return
		except OSError:
			pass
		time.sleep(1)
	raise RuntimeError(f"no \"Saved the game\" in the log after {SAVE_WAIT} s")


def zip_world(world, target):
	tmp = target + ".partial"
	base = os.path.dirname(world)
	with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
		for root, _dirs, files in os.walk(world):
			for n in files:
				path = os.path.join(root, n)
				if n == "session.lock":
					continue  # held open by the server, and never needed in a backup
				try:
					z.write(path, os.path.relpath(path, base))
				except OSError:
					pass  # a file removed while zipping (e.g. a temp region file)
	os.replace(tmp, target)


def do_backup(s):
	world = os.path.join(s["dir"], s["level"])
	if not os.path.isdir(world):
		raise RuntimeError(f"no {s['level']}/ folder")
	disk = os.statvfs(HOME)
	if dir_size(world) + (2 << 30) > disk.f_bavail * disk.f_frsize:
		raise RuntimeError("not enough free disk space")
	folder = os.path.join(BACKUPS, s["id"])
	os.makedirs(folder, exist_ok=True)
	target = os.path.join(folder, time.strftime("%Y-%m-%d_%H-%M-%S") + ".zip")
	pid = java_by_dir().get(s["dir"])
	pane = pane_of(pid, tmux_panes()) if pid else None
	if pid and pane is None:
		raise RuntimeError("it isn't running in tmux, so saving can't be paused")
	if pane:
		log = os.path.join(s["dir"], "logs", "latest.log")
		offset = os.path.getsize(log) if os.path.exists(log) else 0
		tmux("send-keys", "-t", pane["id"], "save-off", "Enter")
		try:
			tmux("send-keys", "-t", pane["id"], "save-all flush", "Enter")
			wait_for_save(log, offset)
			zip_world(world, target)
		finally:
			tmux("send-keys", "-t", pane["id"], "save-on", "Enter")
	else:
		zip_world(world, target)
	for old in own_backups(s["id"])[KEEP:]:
		os.remove(os.path.join(folder, old["name"]))


def do_restore(s, name, write):
	"""write(phase) reports progress. The current world is kept, renamed."""
	if name not in [b["name"] for b in own_backups(s["id"])]:
		raise RuntimeError(f"no backup {name}")
	archive = os.path.join(BACKUPS, s["id"], name)
	level = s["level"]
	with zipfile.ZipFile(archive) as z:
		names = z.namelist()
		bad = [n for n in names if not n.startswith(level + "/") or ".." in n.split("/") or n.startswith("/")]
		if bad or not names:
			raise RuntimeError(f"unexpected paths in the backup: {bad[:1] or 'empty'}")
		need = sum(i.file_size for i in z.infolist())
	disk = os.statvfs(HOME)
	if need + (2 << 30) > disk.f_bavail * disk.f_frsize:
		raise RuntimeError("not enough free disk space")
	was_running = bool(java_by_dir().get(s["dir"]))
	if was_running:
		write("stopping")
		do_stop(s)
	write("restoring")
	world = os.path.join(s["dir"], level)
	staging = os.path.join(s["dir"], ".restore-" + time.strftime("%Y%m%d-%H%M%S"))
	with zipfile.ZipFile(archive) as z:
		z.extractall(staging)
	kept = world + ".before-restore-" + time.strftime("%Y-%m-%d_%H-%M-%S")
	if os.path.exists(world):
		os.rename(world, kept)
	try:
		os.rename(os.path.join(staging, level), world)
	except OSError:
		if os.path.exists(kept):
			os.rename(kept, world)
		raise
	os.rmdir(staging)
	if was_running:
		write("starting")
		do_start(s)


# Installing a server pack ---------------------------------------------------

def free_bytes():
	disk = os.statvfs(HOME)
	return disk.f_bavail * disk.f_frsize


def folder_name(name):
	"""A folder name for a new server: no slashes, not hidden, not empty."""
	name = re.sub(r"[/\\\0]", "_", name).strip().lstrip(".")
	return name or "server-pack"


def write_marker(folder, **fields):
	mark = marker(folder) or {}
	mark.update(fields)
	save_json(os.path.join(folder, MARKER), mark)


def unpack(upload, name, path=None):
	"""Unzips uploads/<upload> (or the zip at path) into a new ~/<folder> and
	returns the folder name: the zip's one top-level folder, else name. Nothing
	is overwritten. An upload is removed afterwards, whatever happens; a zip
	given by path is kept."""
	keep = path is not None
	path = path or os.path.join(UPLOADS, os.path.basename(upload))
	staging = None
	try:
		with zipfile.ZipFile(path) as z:
			infos = [i for i in z.infolist() if not i.filename.startswith("__MACOSX/")]
			names = [i.filename for i in infos]
			bad = [n for n in names if n.startswith("/") or ".." in n.split("/") or "\\" in n]
			if bad or not names:
				raise RuntimeError(f"unexpected paths in the zip: {bad[:1] or 'empty'}")
			top = names[0].split("/")[0]
			single = all(n.startswith(top + "/") for n in names)
			folder = folder_name(top if single else name)
			target = os.path.join(HOME, folder)
			if os.path.lexists(target):
				raise RuntimeError(f"~/{folder} already exists")
			taken = next((o for o in sum(scan(), []) if o["id"] == slug(folder)), None)
			if taken:
				raise RuntimeError(f"its name is too close to ~/{taken['folder']}")
			if sum(i.file_size for i in infos) + (2 << 30) > free_bytes():
				raise RuntimeError("not enough free disk space")
			staging = os.path.join(HOME, ".server-panel-unpack-" + time.strftime("%Y%m%d-%H%M%S"))
			os.makedirs(staging)
			for info in infos:
				out = z.extract(info, staging)
				mode = info.external_attr >> 16
				if mode & 0o111 and not info.is_dir():
					os.chmod(out, os.stat(out).st_mode | 0o111)
		src = os.path.join(staging, top) if single else staging
		for n in os.listdir(src):
			if n.endswith(".sh") and os.path.isfile(os.path.join(src, n)):
				os.chmod(os.path.join(src, n), 0o755)
		write_marker(src, installed=int(time.time()), source=name)
		os.rename(src, target)
		return folder
	finally:
		if staging and os.path.isdir(staging):
			shutil.rmtree(staging, ignore_errors=True)
		if not keep:
			try:
				os.remove(path)
			except OSError:
				pass


def box_zips():
	"""[{name, size, time}] of the zips in the box's home directory, newest first."""
	out = []
	for n in os.listdir(HOME):
		path = os.path.join(HOME, n)
		if n.lower().endswith(".zip") and os.path.isfile(path):
			st = os.stat(path)
			out.append({"name": n, "size": st.st_size, "time": int(st.st_mtime)})
	return sorted(out, key=lambda z: z["time"], reverse=True)


def unpack_home(name):
	"""Unpacks ~/<name> like an upload, reporting to FETCH; the zip stays."""
	started = int(time.time())

	def report(phase, **fields):
		save_json(FETCH, dict(fields, phase=phase, name=name, started=started, updated=int(time.time())))
	report("unpacking")
	try:
		folder = unpack(None, re.sub(r"(?i)\.zip$", "", name), path=os.path.join(HOME, name))
		report("done", folder=folder)
	except Exception as e:  # noqa: BLE001 - reported to the panel
		report("failed", error=str(e))


# The Java a Minecraft version needs: up to 1.16 Java 8, up to 1.20.4 Java 17, then 21.
MC_VERSION = re.compile(r"(?:minecraft_server\.|forge-|minecraft-|neoforge-|server-)1\.(\d+)(?:\.(\d+))?")


def javas():
	"""{major: path} of the Java installs on the box, plus "" for the default java."""
	out = {}
	for root in ("/usr/lib/jvm", "/usr/lib64/jvm", "/opt"):
		try:
			names = sorted(os.listdir(root))
		except OSError:
			continue
		for n in names:
			m = re.search(r"(?:java|jdk|jre|temurin|openjdk)[-_]?(?:1\.)?(\d+)", n)
			if not m or os.path.islink(os.path.join(root, n)):
				continue
			for path in (os.path.join(root, n, "bin", "java"), os.path.join(root, n, "jre", "bin", "java")):
				if os.access(path, os.X_OK):
					out.setdefault(int(m.group(1)), path)
					break
	return out


def mc_java(names):
	"""(minecraft version, java major it needs) guessed from file names, or ("", 0)."""
	for n in names:
		m = MC_VERSION.search(n)
		if m:
			minor, patch = int(m.group(1)), int(m.group(2) or 0)
			java = 8 if minor <= 16 else 17 if (minor, patch) < (20, 5) else 21
			return f"1.{minor}" + (f".{patch}" if m.group(2) else ""), java
	for n in names:
		m = re.search(r"neoforge-(2\d)\.(\d+)", n)  # NeoForge 21.1.x is for Minecraft 1.21.1
		if m:
			minor, patch = int(m.group(1)), int(m.group(2))
			return f"1.{minor}" + (f".{patch}" if patch else ""), 17 if (minor, patch) < (20, 5) else 21
	return "", 0


JAVA_LINE = re.compile(r"^(\s*(?:exec\s+)?)(\S*java)(\s)", re.MULTILINE)


def script_java(folder, script):
	"""The java command the start script runs: "java" or a full path, or ""."""
	try:
		with open(os.path.join(folder, script), errors="replace") as f:
			text = "\n".join(l for l in f.read().split("\n") if not l.lstrip().startswith("#"))
	except OSError:
		return ""
	m = JAVA_LINE.search(text)
	return m.group(2) if m else ""


def fetch(url):
	"""Downloads a server pack zip into UPLOADS and unpacks it, reporting to FETCH."""
	started = int(time.time())

	def report(phase, **fields):
		save_json(FETCH, dict(fields, phase=phase, url=url, started=started, updated=int(time.time())))
	report("downloading", got=0, total=0)
	try:
		request = urllib.request.Request(url, headers={"User-Agent": "minecraft-server-panel"})
		with urllib.request.urlopen(request, timeout=30) as r:
			name = ""
			m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", r.headers.get("Content-Disposition", ""))
			if m:
				name = urllib.parse.unquote(m.group(1))
			name = os.path.basename(name or urllib.parse.unquote(urllib.parse.urlparse(r.geturl()).path)) or "server-pack.zip"
			total = int(r.headers.get("Content-Length") or 0)
			if total and total + (2 << 30) > free_bytes():
				raise RuntimeError("not enough free disk space")
			os.makedirs(UPLOADS, exist_ok=True)
			upload = re.sub(r"[^A-Za-z0-9._-]", "_", name)
			got, last = 0, 0.0
			with open(os.path.join(UPLOADS, upload), "wb") as f:
				while True:
					chunk = r.read(1 << 20)
					if not chunk:
						break
					f.write(chunk)
					got += len(chunk)
					if time.time() - last > 2:
						report("downloading", name=name, got=got, total=total)
						last = time.time()
		report("unpacking", name=name, got=got, total=total)
		folder = unpack(upload, re.sub(r"(?i)\.zip$", "", name))
		report("done", name=name, folder=folder)
	except Exception as e:  # noqa: BLE001 - reported to the panel
		report("failed", error=str(e))


def bat_to_sh(text):
	"""(suggested run.sh, [numbers of the .bat lines it couldn't translate]).
	Drops @echo off, pause, cls and title, turns REM / :: into comments,
	joins ^ continuations, and on the java line turns win_args.txt into
	unix_args.txt and %* into "$@". Anything else is kept as a marked comment."""
	joined, buf, first = [], "", 0
	for n, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
		line = raw.rstrip()
		if not buf:
			first = n
		if line.endswith("^"):
			buf += line[:-1]
			continue
		joined.append((first, buf + line))
		buf = ""
	if buf:
		joined.append((first, buf))
	out, flagged = ["#!/usr/bin/env sh"], []
	java = False
	for n, line in joined:
		s = line.strip()
		low = s.lower().lstrip("@")
		if not s:
			if out[-1] != "":
				out.append("")
		elif low in ("echo off", "pause", "cls") or low.startswith(("title ", "pause ", "pause>")):
			continue
		elif low == "rem" or low.startswith("rem "):
			out.append("#" + s.lstrip("@")[3:].replace("%*", '"$@"'))
		elif s.startswith("::"):
			out.append("#" + s[2:].replace("%*", '"$@"'))
		elif re.match(r'(?i)cd\s+(/d\s+)?"?%~dp0"?\s*$', s):
			out.append('cd "$(dirname "$0")"')
		elif re.match(r'(?i)"?java(\.exe)?"?\s', low):
			cmd = re.sub(r'(?i)^@?"?java(\.exe)?"?', "java", s)
			cmd = cmd.replace("win_args.txt", "unix_args.txt").replace("%*", '"$@"').replace("\\", "/")
			if "%" in cmd:
				flagged.append(n)
				out.append("# CHECK, not translated: " + s)
			else:
				out.append(cmd)
				java = True
		else:
			flagged.append(n)
			out.append("# CHECK, not translated: " + s)
	while out[-1] == "":
		out.pop()
	if not java:
		flagged.append(0)  # no java line at all
	return "\n".join(out) + "\n", flagged


def pick_bat(s):
	top = sorted(n for n in os.listdir(s["dir"]) if n.lower().endswith(".bat"))
	for want in ("run.bat", "start.bat"):
		for n in top:
			if n.lower() == want:
				return n
	starts = [n for n in top if "run" in n.lower() or "start" in n.lower()]
	if not starts:
		raise RuntimeError("no run.bat or start.bat")
	return starts[0]


def set_property(folder, key, value):
	"""Sets key=value in server.properties, creating it when missing."""
	path = os.path.join(folder, "server.properties")
	try:
		with open(path, errors="surrogateescape") as f:
			lines = f.read().rstrip("\n").split("\n")
	except FileNotFoundError:
		lines = []
	for i, line in enumerate(lines):
		if not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() == key:
			lines[i] = f"{key}={value}"
			break
	else:
		lines.append(f"{key}={value}")
	with open(path + ".tmp", "w", errors="surrogateescape") as f:
		f.write("\n".join(l for l in lines if l) + "\n")
	os.replace(path + ".tmp", path)


def setup_step(kind, s, arg):
	"""One step of a new server's setup; returns extra fields for the answer."""
	if s["mark"] is None or s["mark"].get("ready"):
		raise RuntimeError("only for a server added from the panel that isn't set up yet")
	d = s["dir"]
	if kind == "port":
		if not arg.isdigit() or not 1 <= int(arg) <= 65535:
			raise RuntimeError("a port is a number from 1 to 65535")
		set_property(d, "server-port", arg)
		write_marker(d, port=int(arg))
	elif kind == "eula":
		with open(os.path.join(d, "eula.txt"), "w") as f:
			f.write(f"# Accepted in the server panel {time.strftime('%Y-%m-%d %H:%M')}: https://aka.ms/MinecraftEULA\neula=true\n")
	elif kind == "script":
		if "/" in arg or not arg.endswith(".sh") or not os.path.isfile(os.path.join(d, arg)):
			raise RuntimeError(f"no {arg} in the folder")
		os.chmod(os.path.join(d, arg), 0o755)
		write_marker(d, start=arg)
	elif kind in ("convert", "save-run"):
		bat = pick_bat(s)
		with open(os.path.join(d, bat), errors="replace") as f:
			text = f.read()
		suggestion, flagged = bat_to_sh(text)
		if kind == "convert":
			return {"bat": bat, "bat_text": text.replace("\r\n", "\n"), "run_sh": suggestion, "flagged": flagged}
		path = os.path.join(d, "run.sh")
		if os.path.exists(path):
			raise RuntimeError("run.sh already exists")
		with open(path, "w") as f:
			f.write(suggestion)
		os.chmod(path, 0o755)
	elif kind == "make-run":
		if "/" in arg or not arg.endswith(".jar") or not os.path.isfile(os.path.join(d, arg)):
			raise RuntimeError(f"no {arg} in the folder")
		path = os.path.join(d, "run.sh")
		if os.path.exists(path):
			raise RuntimeError("run.sh already exists")
		with open(path, "w") as f:
			f.write(f"#!/usr/bin/env sh\njava -jar {shlex.quote(arg)} nogui \"$@\"\n")
		os.chmod(path, 0o755)
	elif kind == "java":
		found = javas()
		if arg not in ("java", *found.values()):
			raise RuntimeError(f"no Java at {arg}")
		if not s["script"]:
			raise RuntimeError("make a start script first")
		path = os.path.join(d, s["script"])
		with open(path, errors="surrogateescape") as f:
			text = f.read()
		lines, done = text.split("\n"), False
		for i, line in enumerate(lines):
			if not done and not line.lstrip().startswith("#") and JAVA_LINE.search(line):
				lines[i] = JAVA_LINE.sub(lambda m: m.group(1) + arg + m.group(3), line, count=1)
				done = True
		if not done:
			raise RuntimeError(f"no java line in {s['script']}")
		with open(path + ".tmp", "w", errors="surrogateescape") as f:
			f.write("\n".join(lines))
		os.chmod(path + ".tmp", os.stat(path).st_mode)
		os.replace(path + ".tmp", path)
	elif kind == "finish":
		missing = [what for what, ok in (("a start script", s["script"]), ("a port", s["mark"].get("port")),
			("the EULA", properties(os.path.join(d, "eula.txt")).get("eula", "").lower() == "true")) if not ok]
		if missing:
			raise RuntimeError("still needs " + ", ".join(missing))
		write_marker(d, ready=True)
	elif kind == "remove":
		if java_by_dir().get(d):
			raise RuntimeError("java is running in this folder")
		shutil.rmtree(d)
		try:
			os.remove(os.path.join(STATE, s["id"] + ".json"))
		except OSError:
			pass
	else:
		raise RuntimeError(f"unknown step {kind}")
	return {}


def do_installer(s, jar, beat):
	"""Runs a Forge/NeoForge installer in s's console; beat() is called while it runs."""
	if "/" in jar or not jar.endswith(".jar") or not os.path.isfile(os.path.join(s["dir"], jar)):
		raise RuntimeError(f"no {jar} in the folder")
	if java_by_dir().get(s["dir"]):
		raise RuntimeError("java is already running in this folder")
	beat()
	tmux("send-keys", "-t", console(s), "java -jar " + shlex.quote(jar) + " --installServer", "Enter")
	deadline = time.time() + START_WAIT
	pid = None
	while pid is None:
		pid = java_by_dir().get(s["dir"])
		if pid is None:
			if time.time() > deadline:
				raise RuntimeError("the installer didn't start; look at its tmux console")
			time.sleep(2)
	deadline = time.time() + INSTALL_WAIT
	last = time.time()
	while proc_stat(pid) is not None:
		if time.time() > deadline:
			raise RuntimeError(f"the installer still runs after {INSTALL_WAIT // 60} min")
		if time.time() - last > 60:
			beat()
			last = time.time()
		time.sleep(2)
	if not start_script(s["dir"], marker(s["dir"])):
		raise RuntimeError("the installer finished without making run.sh; look at its tmux console")


def run_action(kind, sid, arg=None):
	s = find(sid)
	started = int(time.time())
	# An auto-restart by watch carries a flag, so the desktop can tell it from a click.
	extra = {"auto": True} if arg == "auto" else {}

	def report(phase, **fields):
		write_action(sid, action=kind, phase=phase, started=started, **extra, **fields)
	try:
		old_session = None
		if kind in ("stop", "restart"):
			report("stopping")
			old_session = do_stop(s)
		if kind in ("start", "restart"):
			report("starting")
			do_start(s, old_session)
		if kind == "backup":
			report("backing-up")
			do_backup(s)
		if kind == "restore":
			do_restore(s, arg, lambda phase: report(phase, backup=arg))
		if kind == "installer":
			do_installer(s, arg, lambda: report("installing", installer=arg))
		report("done", **({"backup": arg} if kind == "restore" else {}))
	except Exception as e:  # noqa: BLE001 - reported to the panel
		report("failed", error=str(e))


def main(argv):
	if argv[:1] == ["status"]:
		print(json.dumps(status()))
	elif (len(argv) == 3 and argv[0] == "action" and argv[1] in ("start", "stop", "restart", "backup")) or \
			(len(argv) == 4 and argv[0] == "action" and argv[1] in ("restore", "installer")):
		s = find(argv[2])
		if (argv[1] == "installer") != (s["mark"] is not None and not s["mark"].get("ready")):
			print(json.dumps({"ok": False, "error": "finish the server's setup first" if argv[1] != "installer"
				else "only for a server that isn't set up yet"}))
			return 1
		if argv[1] == "restore" and argv[3] not in [b["name"] for b in own_backups(s["id"])]:
			print(json.dumps({"ok": False, "error": f"no backup {argv[3]}"}))
			return 1
		if argv[1] == "start":
			problem = start_problem(s)
			if problem:
				print(json.dumps({"ok": False, "error": problem}))
				return 1
		action = read_action(s["id"])
		if action and action.get("phase") not in ("done", "failed") and time.time() - action.get("updated", 0) < BUSY_MAX:
			print(json.dumps({"ok": False, "error": f"already busy: {action['action']}"}))
			return 1
		write_action(s["id"], action=argv[1], phase="queued", started=int(time.time()))
		subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "_run", argv[1], s["id"], *argv[3:]],
			stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		print(json.dumps({"ok": True}))
	elif len(argv) == 3 and argv[0] == "auto-restart" and argv[2] in ("on", "off"):
		print(json.dumps({"ok": True, "auto_restart": set_auto(argv[1], argv[2] == "on")}))
	elif len(argv) == 4 and argv[0] == "ram" and argv[2].isdigit() and argv[3].isdigit():
		try:
			print(json.dumps({"ok": True, "ram": set_ram(find(argv[1]), int(argv[2]), int(argv[3]))}))
		except (RuntimeError, OSError) as e:
			print(json.dumps({"ok": False, "error": str(e)}))
			return 1
	elif len(argv) == 2 and argv[0] == "mods":
		print(json.dumps({"ok": True, "mods": mod_list(find(argv[1]))}))
	elif argv[:1] == ["free"]:
		print(json.dumps({"ok": True, "free": free_bytes()}))
	elif len(argv) == 3 and argv[0] == "unpack":
		try:
			print(json.dumps({"ok": True, "folder": unpack(argv[1], argv[2])}))
		except (RuntimeError, OSError, zipfile.BadZipFile) as e:
			print(json.dumps({"ok": False, "error": str(e)}))
			return 1
	elif len(argv) == 2 and argv[0] == "fetch":
		if not re.match(r"https?://", argv[1]):
			print(json.dumps({"ok": False, "error": "the link must start with http:// or https://"}))
			return 1
		fetching = read_fetch()
		if fetching and fetching.get("phase") not in ("done", "failed"):
			print(json.dumps({"ok": False, "error": "a download is already running"}))
			return 1
		save_json(FETCH, {"phase": "downloading", "url": argv[1], "started": int(time.time()), "updated": int(time.time())})
		subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "_fetch", argv[1]],
			stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		print(json.dumps({"ok": True}))
	elif len(argv) == 2 and argv[0] == "unpack-home":
		if argv[1] not in [z["name"] for z in box_zips()]:
			print(json.dumps({"ok": False, "error": f"no ~/{argv[1]}"}))
			return 1
		fetching = read_fetch()
		if fetching and fetching.get("phase") not in ("done", "failed"):
			print(json.dumps({"ok": False, "error": "a download or unpack is already running"}))
			return 1
		save_json(FETCH, {"phase": "unpacking", "name": argv[1], "started": int(time.time()), "updated": int(time.time())})
		subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "_unpack-home", argv[1]],
			stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		print(json.dumps({"ok": True}))
	elif len(argv) == 2 and argv[0] == "_unpack-home":
		unpack_home(argv[1])
	elif len(argv) == 2 and argv[0] == "_fetch":
		fetch(argv[1])
	elif len(argv) in (3, 4) and argv[0] == "setup":
		try:
			s = find(argv[2])
			action = read_action(s["id"])
			if action and action.get("phase") not in ("done", "failed") and time.time() - action.get("updated", 0) < BUSY_MAX:
				raise RuntimeError(f"busy: {action['action']}")
			print(json.dumps(dict(setup_step(argv[1], s, argv[3] if len(argv) == 4 else ""), ok=True)))
		except (RuntimeError, OSError) as e:
			print(json.dumps({"ok": False, "error": str(e)}))
			return 1
	elif argv[:1] == ["watch"]:
		watch()
	elif len(argv) in (3, 4) and argv[0] == "_run":
		run_action(argv[1], argv[2], *argv[3:])
	else:
		print(__doc__, file=sys.stderr)
		return 2
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
