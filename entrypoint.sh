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
if [ -n "${OPENCODE_GO_API_KEY:-}" ]; then
    mkdir -p "$HOME/.pi/agent"
    (
        umask 077
        python3 -c 'import json, os, pathlib

path = pathlib.Path.home() / ".pi/agent/auth.json"
auth = json.loads(path.read_text()) if path.exists() else {}
auth["opencode-go"] = {"type": "api_key", "key": os.environ["OPENCODE_GO_API_KEY"]}
path.write_text(json.dumps(auth, indent=2))
'
    )
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
