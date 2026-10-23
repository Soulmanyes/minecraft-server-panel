#!/usr/bin/env python3
"""Local side of the server panel: talks to server/agent.py over ssh.

  client.py status                 server status JSON (cached 20 s, so every bar on
                                   every monitor shares one ssh call)
  client.py action KIND ID         start, stop, restart or back up a server
  client.py action restore ID ZIP  put a backup back (the current world is kept)
  client.py notices ID on|off      player join/leave notifications for one server
  client.py auto ID on|off         auto-restart after a crash (runs on the server)
  client.py mods ID                the server's mod list (read-only)
  client.py ram ID MIN_MB MAX_MB   set a server's -Xms / -Xmx
  client.py rename ID NAME         set the name shown in the panel ("" = from the folder)
  client.py candidates             the newest server pack zips in ~/Downloads
  client.py install pick|FILE      upload a server pack zip (pick: choose it with zenity) and
                                   unpack it into a new folder on the server, in the background
  client.py install url URL        let the server download a server pack zip and unpack it
  client.py install box NAME       unpack a zip that is already in the box's home directory
  client.py setup KIND ID [ARG]    a new server's setup step (see agent.py)
  client.py action installer ID JAR  run a Forge/NeoForge installer in the server's console
  client.py deploy                 copy server/agent.py to the server

Settings come from the environment (the plugin passes its settings):
  SERVER_PANEL_HOST   ssh host of the box: an ~/.ssh/config alias or user@host
  SERVER_PANEL_DIR    folder in the box's home for the agent (default
                      minecraft-server-panel); also names the local cache and config
  SERVER_PANEL_TITLE  app name on notifications
Every output is one JSON object; failures come back as {"error": ...}.

Each fresh status is compared with the previous one, and changes worth
knowing about become desktop notifications (notify-send): a server that went
down or stopped answering without a Stop from the panel, and a disk that
filled past DISK_ALERT, a server that logged LAG_WARNINGS "Can't keep up!"
lines within 5 minutes, and players joining or leaving the servers that have
notices switched on. The bars on every monitor share one fetch, so each
alert is sent once.
"""
import fcntl
import json
import re
import shlex
import os
import subprocess
import sys
import time
import zipfile

HOST = os.environ.get("SERVER_PANEL_HOST", "")
APP = os.environ.get("SERVER_PANEL_DIR") or "minecraft-server-panel"
TITLE = os.environ.get("SERVER_PANEL_TITLE") or "Minecraft servers"
HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), APP)
CACHE = os.path.join(RUNTIME, "status.json")
TTL = 20
ALERTS = os.path.join(RUNTIME, "alerts.json")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP, "config.json")
DISK_ALERT, DISK_REARM = 0.85, 0.80
# A Stop or Restart from the panel this recent explains a server going down.
EXPECTED_S = 900
UP = ("running", "starting")
LAG_WARNINGS = 3  # "Can't keep up" lines within 5 min before a lag alert
LAG_QUIET_S = 1800  # at most one lag alert per server this often
REMOTE = f"{APP}/agent.py"
INSTALL = os.path.join(RUNTIME, "install.json")  # progress of `install`
DOWNLOADS = os.path.expanduser("~/Downloads")
# One shared connection, kept for a while, so polling doesn't redo the handshake.
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ControlMaster=auto",
	"-o", f"ControlPath={RUNTIME}/ssh-%C", "-o", "ControlPersist=120"]


def ssh(*remote, timeout=25):
	try:
		r = subprocess.run(SSH + [HOST, *remote], capture_output=True, text=True, timeout=timeout)
	except subprocess.TimeoutExpired:
		return None, "ssh timed out"
	if r.returncode == 255:
		return None, (r.stderr.strip().splitlines() or ["ssh failed"])[-1]
	try:
		return json.loads(r.stdout), None
	except ValueError:
		return None, (r.stderr.strip().splitlines() or [f"agent exited {r.returncode}"])[-1]


def load_config():
	try:
		with open(CONFIG) as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def save_config(config):
	os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
	tmp = CONFIG + ".tmp"
	with open(tmp, "w") as f:
		json.dump(config, f, indent=1)
	os.replace(tmp, CONFIG)


def player_notices(s, before):
	"""Join/leave notifications for one server, from the names in the server-list
	ping. Big servers list only some names; then only the count is told."""
	if s["state"] != "running" or before.get("state") != "running":
		return
	old, new = set(before.get("names", [])), set(s.get("names", []))
	complete = len(new) == s.get("online", 0) and len(old) == before.get("online", 0)
	if complete:
		for name in sorted(new - old):
			notify(f"{name} joined {s['name']}", f"{s['online']} online", "low")
		for name in sorted(old - new):
			notify(f"{name} left {s['name']}", f"{s['online']} online", "low")
	elif s.get("online", 0) != before.get("online", 0):
		diff = s.get("online", 0) - before.get("online", 0)
		what = f"{diff} joined" if diff > 0 else f"{-diff} left"
		notify(f"{what} {s['name']}", f"{s['online']} online", "low")


def notify(title, body, urgency="normal"):
	try:
		subprocess.run(["notify-send", "-a", TITLE, "-i", "network-server", "-u", urgency, title, body],
			timeout=5, check=False)
	except (OSError, subprocess.TimeoutExpired):
		pass


def expected(server):
	"""Whether a panel action explains this server's state change: one still
	running, or a finished Stop (a finished Restart means it should be up)."""
	a = server.get("action") or {}
	if a.get("action") in ("stop", "restart", "restore") and a.get("phase") in ("queued", "stopping", "restoring", "starting"):
		return True
	return a.get("action") == "stop" and a.get("phase") == "done" and time.time() - a.get("started", 0) < EXPECTED_S


def alerts(data):
	"""Notify about changes since the last fresh status. The first status after a
	login only records the state: there is nothing to compare it with."""
	try:
		with open(ALERTS) as f:
			last = json.load(f)
	except (OSError, ValueError):
		last = None
	now = {"servers": {s["id"]: {"state": s["state"], "crash": s.get("crash", 0), "names": s.get("names", []),
		"online": s.get("online", 0)} for s in data["servers"]}}
	for s in data["servers"]:
		lag_alert = ((last or {}).get("servers", {}).get(s["id"]) or {}).get("lag_alert", 0)
		lag = s.get("lag") or {}
		if lag.get("recent", 0) >= LAG_WARNINGS and time.time() - lag_alert > LAG_QUIET_S:
			if last is not None:
				notify(f"{s['name']} is lagging", f"{lag['recent']} \"Can't keep up\" warnings in 5 min, "
					f"up to {lag['behind_ms'] / 1000:.1f} s behind.")
			lag_alert = int(time.time())
		now["servers"][s["id"]]["lag_alert"] = lag_alert
		before = ((last or {}).get("servers", {}).get(s["id"])) or {}
		a, auto = s.get("action") or {}, s.get("auto") or {}
		now["servers"][s["id"]]["auto_start"] = a.get("started", 0) if a.get("auto") else before.get("auto_start", 0)
		now["servers"][s["id"]]["gave_up"] = auto.get("gave_up", 0)
		if last is not None and before:
			if a.get("auto") and a.get("started", 0) > before.get("auto_start", 0):
				notify(f"{s['name']} restarted itself", "It went down and auto-restart started it again.")
			if auto.get("gave_up", 0) > before.get("gave_up", 0):
				notify(f"{s['name']} keeps crashing", f"Auto-restart gave up after {auto.get('restarts', 0)} restarts "
					"in 30 min. It stays stopped.", "critical")
	notices = set(load_config().get("player_notices", []))
	host = data.get("host") or {}
	disk = host.get("disk_used", 0) / max(1, host.get("disk_total", 1))
	now["disk_full"] = disk >= DISK_ALERT or (bool(last and last.get("disk_full")) and disk > DISK_REARM)
	if last is not None:
		for s in data["servers"]:
			before = last.get("servers", {}).get(s["id"])
			if before is None:
				continue
			if s["id"] in notices:
				player_notices(s, before)
			if expected(s):
				continue
			crashed = s.get("crash", 0) > before.get("crash", 0)
			if before["state"] in UP and s["state"] == "stopped":
				if crashed:
					notify(f"{s['name']} crashed", "It wrote a crash report and is stopped now.", "critical")
				else:
					notify(f"{s['name']} stopped", "Not from the panel: it crashed or was stopped by hand.", "critical")
			elif before["state"] != "unresponsive" and s["state"] == "unresponsive":
				notify(f"{s['name']} isn't answering", "Java is running but the server doesn't answer pings.", "critical")
		if now["disk_full"] and not last.get("disk_full"):
			notify("Server disk filling up", f"{disk:.0%} used, {(host['disk_total'] - host['disk_used']) / (1 << 30):.1f} GB free.")
	tmp = ALERTS + ".tmp"
	with open(tmp, "w") as f:
		json.dump(now, f)
	os.replace(tmp, ALERTS)


def read_cache():
	try:
		with open(CACHE) as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def status(fresh=False):
	with open(os.path.join(RUNTIME, "lock"), "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		cached = read_cache()
		if cached and not fresh and time.time() - cached.get("fetched", 0) < TTL:
			return cached
		data, error = ssh("python3", "-I", REMOTE, "status")
		if data is None:
			if error and "No such file" in error:
				error = "agent missing on the server: use Install agent in the panel (or client.py deploy)"
			out = {"error": error, "fetched": int(time.time())}
			if cached:
				out["last"] = {k: v for k, v in cached.items() if k not in ("error", "last")}
			# Keep failures cached too, so an unreachable server isn't retried by every bar at once.
			data = out
		else:
			data["fetched"] = int(time.time())
			alerts(data)
		tmp = CACHE + ".tmp"
		with open(tmp, "w") as f:
			json.dump(data, f)
		os.replace(tmp, CACHE)
		return data


def drop_cache():
	try:
		os.remove(CACHE)
	except OSError:
		pass


def install_state():
	"""The running or last upload, or None. One whose process is gone counts as failed."""
	try:
		with open(INSTALL) as f:
			state = json.load(f)
	except (OSError, ValueError):
		return None
	if state.get("phase") not in ("done", "failed"):
		try:
			os.kill(state.get("pid", 0), 0)
		except (OSError, TypeError):
			state = dict(state, phase="failed", error="the upload stopped without finishing")
	return state


def candidates():
	"""The newest zips with "server" in the name in ~/Downloads and its subfolders."""
	found = []
	for root, dirs, files in os.walk(DOWNLOADS):
		if root.count(os.sep) - DOWNLOADS.count(os.sep) >= 2:
			dirs[:] = []
		for n in files:
			if n.lower().endswith(".zip") and "server" in n.lower():
				path = os.path.join(root, n)
				st = os.stat(path)
				found.append({"path": path, "name": n, "size": st.st_size, "time": int(st.st_mtime)})
	return sorted(found, key=lambda c: c["time"], reverse=True)[:6]


def install(source):
	"""Picks (source "pick") or takes a zip, checks it, uploads it with rsync and
	has the agent unpack it. Progress goes to INSTALL for the panel."""
	state = {"pid": os.getpid(), "started": int(time.time())}

	def report(phase, **fields):
		state.update(fields, phase=phase, updated=int(time.time()))
		with open(INSTALL + ".tmp", "w") as f:
			json.dump(state, f)
		os.replace(INSTALL + ".tmp", INSTALL)
	try:
		if source == "pick":
			report("picking")
			r = subprocess.run(["zenity", "--file-selection", "--title=Server pack zip",
				f"--filename={DOWNLOADS}/", "--file-filter=Zip files | *.zip *.ZIP"], capture_output=True, text=True)
			source = r.stdout.strip()
			if r.returncode != 0 or not source:
				os.remove(INSTALL)  # cancelled
				return
		name = os.path.basename(source)
		report("checking", name=name)
		with zipfile.ZipFile(source) as z:
			unpacked = sum(i.file_size for i in z.infolist())
		size = os.path.getsize(source)
		free, error = ssh("python3", "-I", REMOTE, "free")
		if free is None:
			raise RuntimeError(error)
		if size + unpacked + (2 << 30) > free["free"]:
			raise RuntimeError(f"the server has {free['free'] / (1 << 30):.1f} GB free; the pack needs "
				f"{(size + unpacked) / (1 << 30):.1f} GB plus 2 GB to spare")
		upload = re.sub(r"[^A-Za-z0-9._-]", "_", name)
		report("uploading", name=name, size=size, percent=0)
		subprocess.run(SSH + [HOST, "mkdir", "-p", f"{APP}/uploads"], check=True, timeout=30)
		p = subprocess.Popen(["rsync", "--partial-dir=.rsync-partial", "--info=progress2", "-e", shlex.join(SSH),
			source, f"{HOST}:{APP}/uploads/{upload}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		buf, last = b"", 0.0
		while True:
			chunk = p.stdout.read1(4096)
			if not chunk:
				break
			buf = (buf + chunk)[-200:]
			m = re.findall(rb"(\d+)%", buf)
			if m and time.time() - last > 1:
				report("uploading", percent=int(m[-1]))
				last = time.time()
		if p.wait() != 0:
			raise RuntimeError("rsync failed: " + (p.stderr.read().decode("utf-8", "replace").strip().splitlines() or ["?"])[-1])
		report("unpacking", percent=100)
		result, error = ssh("python3", "-I", REMOTE, "unpack", upload, shlex.quote(re.sub(r"(?i)\.zip$", "", name)),
			timeout=900)
		if result is None or not result.get("ok"):
			raise RuntimeError(error or result.get("error"))
		drop_cache()
		report("done", folder=result["folder"])
	except Exception as e:  # noqa: BLE001 - shown in the panel
		report("failed", error=str(e))


def main(argv):
	os.makedirs(RUNTIME, mode=0o700, exist_ok=True)
	if not HOST or not re.fullmatch(r"[\w.@-]+", HOST) or not re.fullmatch(r"[\w.-]+", APP):
		print(json.dumps({"ok": False, "error": "no ssh host set: open the plugin's settings (or set SERVER_PANEL_HOST)"}))
		return 1
	if argv[:1] == ["status"]:
		data = status(fresh="--fresh" in argv)
		data["notices"] = load_config().get("player_notices", [])
		data["install"] = install_state()
		print(json.dumps(data))
	elif len(argv) == 4 and argv[0] == "ram" and argv[2].isdigit() and argv[3].isdigit():
		data, error = ssh("python3", "-I", REMOTE, "ram", shlex.quote(argv[1]), argv[2], argv[3])
		try:
			os.remove(CACHE)
		except OSError:
			pass
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif len(argv) == 3 and argv[0] == "rename":
		data, error = ssh("python3", "-I", REMOTE, "rename", shlex.quote(argv[1]), shlex.quote(argv[2]))
		drop_cache()
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif len(argv) == 2 and argv[0] == "mods":
		data, error = ssh("python3", "-I", REMOTE, "mods", shlex.quote(argv[1]), timeout=90)
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif len(argv) == 3 and argv[0] == "auto" and argv[2] in ("on", "off"):
		data, error = ssh("python3", "-I", REMOTE, "auto-restart", shlex.quote(argv[1]), argv[2])
		try:
			os.remove(CACHE)
		except OSError:
			pass
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif len(argv) == 3 and argv[0] == "notices" and argv[2] in ("on", "off"):
		config = load_config()
		ids = [i for i in config.get("player_notices", []) if i != argv[1]]
		if argv[2] == "on":
			ids.append(argv[1])
		config["player_notices"] = ids
		save_config(config)
		print(json.dumps({"ok": True, "notices": ids}))
	elif (len(argv) == 3 and argv[0] == "action" and argv[1] in ("start", "stop", "restart", "backup")) or \
			(len(argv) == 4 and argv[0] == "action" and argv[1] in ("restore", "installer")):
		# ssh joins the remote words with spaces, so quote the backup's file name.
		data, error = ssh("python3", "-I", REMOTE, *[shlex.quote(a) for a in argv])
		try:
			os.remove(CACHE)
		except OSError:
			pass
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif argv[:1] == ["candidates"]:
		print(json.dumps({"ok": True, "candidates": candidates()}))
	elif len(argv) == 3 and argv[:2] == ["install", "box"]:
		data, error = ssh("python3", "-I", REMOTE, "unpack-home", shlex.quote(argv[2]))
		drop_cache()
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif len(argv) == 3 and argv[:2] == ["install", "url"]:
		data, error = ssh("python3", "-I", REMOTE, "fetch", shlex.quote(argv[2]))
		drop_cache()
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif len(argv) == 2 and argv[0] == "install":
		state = install_state()
		if state and state.get("phase") not in ("done", "failed"):
			print(json.dumps({"ok": False, "error": "an upload is already running"}))
			return 1
		if argv[1] != "pick" and not os.path.isfile(argv[1]):
			print(json.dumps({"ok": False, "error": f"no file {argv[1]}"}))
			return 1
		source = argv[1] if argv[1] == "pick" else os.path.abspath(argv[1])
		subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "_install", source],
			stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		print(json.dumps({"ok": True}))
	elif len(argv) == 2 and argv[0] == "_install":
		install(argv[1])
	elif len(argv) in (3, 4) and argv[0] == "setup":
		data, error = ssh("python3", "-I", REMOTE, *[shlex.quote(a) for a in argv], timeout=60)
		drop_cache()
		print(json.dumps(data if data is not None else {"ok": False, "error": error}))
	elif argv[:1] == ["deploy"]:
		try:
			subprocess.run(SSH + [HOST, "mkdir", "-p", APP], check=True, timeout=30, capture_output=True)
			subprocess.run(["scp", "-q", "-o", f"ControlPath={RUNTIME}/ssh-%C", os.path.join(HERE, "server", "agent.py"),
				f"{HOST}:{REMOTE}"], check=True, timeout=60, capture_output=True)
		except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
			err = getattr(e, "stderr", b"") or b""
			print(json.dumps({"ok": False, "error": (err.decode("utf-8", "replace").strip().splitlines() or ["deploy failed"])[-1]}))
			return 1
		drop_cache()
		print(json.dumps({"ok": True}))
	else:
		print(__doc__, file=sys.stderr)
		return 2
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
