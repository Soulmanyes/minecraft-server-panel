#!/bin/sh
# Minecraft Server Panel installer, run on your PC:
#   curl -fsSL https://raw.githubusercontent.com/Soulmanyes/minecraft-server-panel/main/install.sh | sh
#
# 1. checks the tools on this PC
# 2. makes an ssh key just for the panel, copies it to the server box (asks for
#    the box's password once) and adds the ssh alias "mcpanel" to ~/.ssh/config
# 3. checks the box has Python 3.9+, tmux and Java
# 4. adds this repo as a Noctalia plugin source, enables the plugin and copies
#    the agent to the box
#
# Safe to run again: steps that are already done are skipped. To skip the
# questions, set MCPANEL_ADDRESS, MCPANEL_USER and MCPANEL_PORT.
set -eu

REPO=https://github.com/Soulmanyes/minecraft-server-panel
SOURCE=minecraft-server-panel
PLUGIN=soulmanyes/minecraft-server-panel
ALIAS=mcpanel
KEY="$HOME/.ssh/minecraft-server-panel"

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*"; }
die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }
# ask VAR "question" [default]: reads from the terminal, so it works under curl | sh.
ask() {
	eval "current=\${$1:-}"
	if [ -z "$current" ]; then
		printf '%s' "$2" > /dev/tty
		read -r current < /dev/tty
		[ -n "$current" ] || current=${3:-}
		[ -n "$current" ] || die "no answer"
	fi
	eval "$1=\$current"
}

say "Checking this PC"
missing=""
for c in noctalia python3 ssh ssh-keygen ssh-copy-id rsync git; do
	command -v "$c" > /dev/null 2>&1 || missing="$missing $c"
done
[ -z "$missing" ] || die "missing:$missing. Install them (on Arch: sudo pacman -S --needed openssh rsync python git) and run this again."
command -v zenity > /dev/null 2>&1 || warn "zenity is missing: the panel's \"Choose a file\" button for adding server packs needs it."
noctalia msg plugins source list > /dev/null 2>&1 || die "can't talk to Noctalia: start it first."

if ssh -G "$ALIAS" 2> /dev/null | grep -qx "hostname $ALIAS"; then
	say "Setting up ssh to your server box"
	ask MCPANEL_ADDRESS "Server address (IP or domain): "
	ask MCPANEL_USER "User name on the server: "
	ask MCPANEL_PORT "SSH port [22]: " 22
	mkdir -p "$HOME/.ssh"
	chmod 700 "$HOME/.ssh"
	if [ ! -f "$KEY" ]; then
		ssh-keygen -q -t ed25519 -N "" -C minecraft-server-panel -f "$KEY"
		echo "Made the key $KEY (no passphrase, so the panel can log in by itself)."
	fi
	echo "Copying the key to the server. It asks for the server password once."
	ssh-copy-id -i "$KEY.pub" -p "$MCPANEL_PORT" -o StrictHostKeyChecking=accept-new \
		"$MCPANEL_USER@$MCPANEL_ADDRESS"
	if [ -f "$HOME/.ssh/config" ]; then
		cp "$HOME/.ssh/config" "$HOME/.ssh/config.bak-minecraft-server-panel"
	fi
	cat >> "$HOME/.ssh/config" <<EOF

# Added by the Minecraft Server Panel installer
Host $ALIAS
    HostName $MCPANEL_ADDRESS
    User $MCPANEL_USER
    Port $MCPANEL_PORT
    IdentityFile $KEY
    IdentitiesOnly yes
EOF
	chmod 600 "$HOME/.ssh/config"
	echo "Added \"Host $ALIAS\" to ~/.ssh/config."
else
	say "ssh alias \"$ALIAS\" already exists, keeping it"
fi
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$ALIAS" true \
	|| die "\"ssh $ALIAS\" doesn't log in without a password. Check the address, user and port in ~/.ssh/config."

say "Checking the server box"
found=$(ssh -o BatchMode=yes "$ALIAS" 'python3 -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>/dev/null && echo python;
	for c in tmux java crontab; do command -v $c >/dev/null 2>&1 && echo $c; done' || true)
echo "$found" | grep -qx python || die "the box needs Python 3.9 or newer (python3)."
echo "$found" | grep -qx tmux || warn "the box has no tmux: Start, Stop and backups of running servers need it."
echo "$found" | grep -qx java || warn "the box has no java on the PATH: servers can't start."
echo "$found" | grep -qx crontab || warn "the box has no crontab: auto-restart after a crash needs it."

say "Adding the plugin to Noctalia"
if noctalia msg plugins source list | grep -q "^$SOURCE "; then
	noctalia msg plugins update "$SOURCE" > /dev/null
else
	noctalia msg plugins source add "$SOURCE" git "$REPO" > /dev/null
fi
# Noctalia fetches the repo in the background, which can take a minute or two.
printf 'Waiting for Noctalia to download it'
tries=0
until noctalia msg plugins list 2> /dev/null | grep -q "^$PLUGIN "; do
	tries=$((tries + 1))
	[ "$tries" -le 60 ] || die "Noctalia hasn't downloaded the plugin after 3 minutes. Restart Noctalia and run this again."
	printf '.'
	sleep 3
done
echo
noctalia msg plugins enable "$PLUGIN" > /dev/null
echo "Enabled $PLUGIN."

say "Copying the agent to the box"
client=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
	client=$(ls "$HOME"/.local/state/noctalia/plugins/materialized/*/minecraft-server-panel/client.py 2> /dev/null | head -n 1)
	[ -n "$client" ] && break
	sleep 2
done
if [ -n "$client" ] && SERVER_PANEL_HOST="$ALIAS" python3 -I "$client" deploy > /dev/null; then
	echo "Done."
else
	warn "couldn't copy it from here: click \"Install agent on the box\" in the panel instead."
fi

say "Last step"
echo "Add the \"Minecraft Server Panel\" widget to your bar in Noctalia's settings, then click it."
