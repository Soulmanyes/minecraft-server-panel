# Minecraft Server Panel

A [Noctalia](https://github.com/noctalia-dev) bar plugin for watching and running Minecraft servers on a Linux box over ssh. The box can be a VPS, a home server or a spare PC. The bar shows how many players are online, and the panel has a card per server with Start, Stop, backups, alerts and more. Server packs can be added from the panel too.

## Features

- **Bar widget:** a server glyph plus the number of players online. It turns primary while someone plays, and shows an error colour when the box can't be reached. The tooltip lists the running servers, their players, and the box's CPU and RAM.
- **Panel:** CPU, RAM and disk meters, load and uptime, then one card per server. Each card shows the server's state (running, starting, stopping, not answering, stopped), port, version, uptime, RAM, CPU and players with names.
- **Install agent** button: when the agent isn't on the box yet, the panel offers to copy it there.
- **Rename** a server with the pencil next to its name (empty = the name made from the folder). Only the shown name changes, not the folder. Its tmux session takes the new name on the next start or restart.
- **Start, Stop and Restart** per server. Stop and Restart need a second click. A terminal button opens the server's tmux console (`ssh -t HOST tmux attach`).
- When the box can't be reached, the panel keeps showing the last data it got, with its age.
- **Alerts** (desktop notifications) when a server goes down or stops answering without a Stop from the panel ("crashed" when it wrote a crash report), and when the disk passes 85 % (sent again after it drops below 80 %). They only arrive while this PC is on and Noctalia runs.
- **Join/leave notices** per server: the bell on a server's card turns on a notification for each player who joins or leaves (by name; on a big server that lists only some names, by count). Off by default; the choice is kept in `~/.config/minecraft-server-panel/config.json`.
- **Back up** per server: zips the world to `~/backups/minecraft/<id>/` on the box and keeps the newest 10. On a running server it types `save-off` and `save-all flush`, waits for "Saved the game", zips, then types `save-on`, so players stay on. The card lists the backups and shows the pack's own backup mod's backups (Simple Backups, FTB Backups, Advanced Backups) next to them.
- **Lag alerts**: the agent reads new lines of each running server's `logs/latest.log` and counts `Can't keep up!` warnings. Three within 5 minutes send a notification (at most one per server every 30 minutes), and the card shows the last lag for an hour.
- **Restore** a backup from the card's backup list (two clicks). A running server is stopped first and started again after. The current world is renamed to `world.before-restore-<date>` and never deleted: remove those folders by hand once you're sure.
- **Auto-restart** per server (the circular-arrow button on its card). It runs on the box: switching it on for the first server adds one line to the box's crontab that runs `agent.py watch` every minute, and switching off the last one removes it. A server that goes down without `Stopping the server` in its log (a `/stop`), or that wrote a new crash report, is started again. After 3 restarts in 30 minutes it gives up and stays stopped. A Stop from the panel or a `/stop` in the console never triggers it. The desktop gets a notification for each auto-restart and when it gives up.
- **RAM** per server: the card shows the min and max heap (`-Xms` / `-Xmx`), and the pencil opens 1 GB steppers. Save edits the flags in place in the file `run.sh` takes them from (`user_jvm_args.txt` for Forge/NeoForge, else the `java` line in `run.sh`; `variables.txt` is kept the same for `start.sh` packs). The old file is copied to `~/minecraft-server-panel/file-backups/<id>/` first. Max can be at most the box's RAM minus 2 GB. A running server uses the new values from its next start, and the card says so.
- **Pack and mods** (read-only): each card shows the pack version (from a CurseForge `manifest.json` or the folder name), the mod loader (from the start scripts) and the mod count. The arrow opens the mod list with names and versions read from each jar, with a filter box. The list is cached on the box until the `mods` folder changes.
- **Add a server** (the + in the header): pick a server pack zip from the newest `*server*.zip` files in `~/Downloads`, choose one with a file picker (`zenity`), or give a direct download link for the box to fetch itself.
  - The upload uses `rsync` over the shared ssh connection with progress in the panel. A broken upload resumes when you upload the same zip again. Before it starts, it checks that the box has room for the zip, the unpacked pack and 2 GB to spare.
  - The agent unzips the pack into a new folder in the home directory: the zip's single top-level folder, else the zip's name. It refuses when that folder exists, so nothing is overwritten. Paths outside the folder are refused, and `.sh` files are made executable.
  - Zips already in the box's home directory are listed too, with "Unpack" (the zip stays where it is).
  - The new server gets a setup card, and the panel converts or runs nothing by itself. A pack without `run.sh` offers "Make run.sh from run.bat…" (a side-by-side preview of the `.bat` and the suggested `run.sh`; lines it can't translate are marked `CHECK`), "Run <installer>.jar" (`java -jar … --installServer` in the server's tmux session) and "Use start.sh" (Start runs that script instead), and for packs that only ship a server jar (old Forge), "Make run.sh for <jar>" (`java -jar <jar> nogui`).
  - **Java:** the card lists the Java installs on the box (`/usr/lib/jvm`) and marks the one the pack's Minecraft version needs (8 up to 1.16, 17 up to 1.20.4, then 21). Picking one writes its full path into the start script.
  - Pick a port from the ports open to the internet (25565–25575; the card says which other servers use it), set the RAM, and tick the EULA box (it writes `eula=true`). "Add to servers" then turns it into a normal card. "Delete folder" removes a pack whose setup isn't finished.

## Requirements

**On your PC:** Noctalia, `python3`, `ssh`, `rsync` and `zenity` (the file picker for adding server packs).

**On the server box:**

- Linux with Python 3.9 or newer, `tmux` and Java. `crontab` is only needed for auto-restart.
- ssh login with a key, without a password prompt.
- Each server is a folder in the home directory of the ssh user, containing `server.properties` and `run.sh`. That's the normal layout of a Forge, NeoForge or Fabric server pack.
- Servers are started in tmux, which the panel does for you. A server that runs another way (systemd, Docker, a hosting panel) shows up, but Stop and Back up refuse it.

## Install

Run this on your PC, with Noctalia running:

```
curl -fsSL https://raw.githubusercontent.com/Soulmanyes/minecraft-server-panel/main/install.sh | sh
```

It asks for the server's address, your user name there and the ssh port, then the server password once. After that it:

- checks the tools on your PC, and that the box has Python 3.9+, tmux and Java,
- makes an ssh key only for the panel (`~/.ssh/minecraft-server-panel`), copies it to the box and adds the alias `mcpanel` to `~/.ssh/config` (backup: `~/.ssh/config.bak-minecraft-server-panel`),
- adds this repo as a Noctalia plugin source, enables the plugin and copies the agent to the box.

Then add the "Minecraft Server Panel" widget to your bar in Noctalia's settings, and click it.

To read the script before running it: clone the repo and run `./install.sh`. It's safe to run again; finished steps are skipped. If you already have an ssh alias for the box, set **SSH host** in the plugin's settings to it instead.

**Updating:** run the install command again. It updates the plugin and copies the new agent to the box.

**Firewall:** players need the server's port open on the box (`firewalld`, `ufw`), at your cloud provider (security list, security group) or on your router. The panel doesn't open ports.

## Settings

| Setting | Default | What it does |
|---|---|---|
| SSH host | `mcpanel` | The box: an ssh alias or `user@host`. The installer makes the `mcpanel` alias. |
| Agent folder | `minecraft-server-panel` | Folder in the box's home directory for the agent and its state. If you change it, click Install agent again. |
| Open ports | `25565-25575` | Ports a new server can pick from. Only list ports that the box's firewall (and your cloud provider's firewall or your router) lets in. |
| Panel title | `Minecraft servers` | Shown on the panel and on notifications. |

## How it works

- `server/agent.py` runs on the box (Python 3.9+, standard library only):
  - A server is any folder in the home directory that has `server.properties` and `run.sh`. A folder added from the panel has a `.server-panel.json` marker. It stays a setup card until it has a start script, a port and the EULA, and you click "Add to servers".
  - Player counts come from the normal Minecraft server-list ping, so RCON and query can stay off.
  - **Start** types `bash run.sh` into the server's own tmux session, named after it (`my-pack-3`), and creates it when needed. On a Restart, the session the server ran in is renamed to that name when it holds only that server, so the console keeps its scrollback. Other tmux sessions are never touched.
  - Start refuses when the port is already taken, `eula.txt` isn't accepted, or the server's `-Xmx` plus 1 GB is more than the free RAM.
  - **Stop** types `stop` into the server's console and waits up to 5 minutes for it to save and exit. It never kills anything.
  - **Back up** refuses when the server isn't running in tmux (saving can't be paused) or the disk lacks the world's size plus 2 GB.
  - Actions run in the background. Their progress is in `~/minecraft-server-panel/state/`.
- `client.py` runs locally:
  - It calls the agent over ssh and caches the status for 20 s, so the bars on every monitor share one call.
  - It keeps one ssh connection open (ControlMaster) to make polling cheap.

## License

CC0 1.0, see `LICENSE`.
