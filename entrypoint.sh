#!/bin/sh
# The container's entrypoint: prepare the sandbox, then become Pi.
#
# Installed as /usr/local/bin/pi-sandbox-entrypoint. It is a file rather than a
# heredoc in Dockerfile.pi so that it can be read, quoted and shellchecked like
# any other script.
set -eu

# A HERDR_SOCKET_PATH inherited from the host pane would move this container's
# Herdr server onto that path, and a stray HERDR_PANE_ID would make Pi read
# itself as one of the children it starts. The wrapper does not forward them,
# but PI_SANDBOX_ENV takes names, so drop them here rather than trusting that.
unset HERDR_SOCKET_PATH HERDR_PANE_ID HERDR_TAB_ID HERDR_WORKSPACE_ID

# Pi reads the opencode-go subscription from auth.json rather than from the
# environment, so the key arrives as a variable and is written out here, with
# any other provider the agent authenticated left alone. It lands in the state
# volume, so it outlives the container: `docker volume rm` is what removes it.
#
# The file is the agent's own between runs, so nothing in it is trusted: bad
# JSON is replaced rather than allowed to stop the write, and the new file is
# renamed over the old one, which is atomic, replaces a symlink instead of
# following it, and cannot leave a mode the umask would not have set.
#
# The whole thing is best effort. Pi is what this container is for, and a home
# directory the agent has ruined must cost it the subscription key, not the
# session: `set -e` would otherwise turn `mkdir ~/.pi/agent/auth.json` into a
# sandbox that never starts again until the volume is deleted.
if [ -n "${OPENCODE_GO_API_KEY:-}" ] && ! (
    mkdir -p "$HOME/.pi/agent" && python3 -c 'import json, os, pathlib

directory = pathlib.Path(os.environ["HOME"]) / ".pi/agent"
path = directory / "auth.json"
try:
    auth = json.loads(path.read_text())
except Exception:
    auth = {}
if not isinstance(auth, dict):
    auth = {}

auth["opencode-go"] = {"type": "api_key", "key": os.environ["OPENCODE_GO_API_KEY"]}

pending = directory / "auth.json.new"
pending.unlink(missing_ok=True)
with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
    json.dump(auth, f, indent=2)
os.replace(pending, path)
'
); then
    printf '%s\n' "pi-sandbox: could not write the subscription key to auth.json" >&2
fi

# Herdr's API commands do not start a server, so the agent's first one would be
# told `server_not_running`. Its socket lives in the agent's home, and no host
# socket is mounted, so the fleet it serves reaches nothing outside this
# container. HERDR_ENV is what the herdr skill checks before it will act.
export HERDR_ENV=1
herdr server >/dev/null 2>&1 &

# Not `herdr server` in the foreground and not Pi as a child: Pi replaces this
# script, so the container's foreground process is Pi and a Herdr pane on the
# host still sees a Pi agent.
exec pi "$@"
