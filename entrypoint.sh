#!/bin/sh
# The container's entrypoint: prepare the sandbox, then become Pi.
#
# Installed as /usr/local/bin/pi-sandbox-entrypoint. It is a file rather than a
# heredoc in Dockerfile.pi so that it can be read, quoted and shellchecked like
# any other script.
set -eu

# Pi reads the opencode-go subscription from auth.json rather than from the
# environment, so the key arrives as a variable and is written out here, with
# any other provider the agent authenticated left alone. It lands in the state
# volume, so it outlives the container: `docker volume rm` is what removes it.
#
# The file is the agent's own between runs, so nothing in it is trusted: bad
# JSON is replaced rather than allowed to stop the write, and the new file is
# renamed over the old one, which is atomic, replaces a symlink instead of
# following it, and cannot leave a mode the umask would not have set. Only a
# regular file is read, since read_text() on a FIFO the agent left would block
# forever and the atomic replace would never get the chance to repair it.
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
    auth = json.loads(path.read_text()) if path.is_file() else {}
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

# Extensions live in /home/agent, which is the project's state volume, so an
# image rebuild does not reach a project whose volume already exists. Bring
# them up to date on every start, best effort: a registry that is down or
# slow, or a home the agent has ruined, must cost the update rather than the
# session.
#
# The four packages are listed here and nowhere else. `pi install` without a
# version adds a missing one and unpins a pinned one, but leaves an installed
# one at its version, so `pi update --extensions` is what moves an existing
# volume to the newest releases (measured: 0.69.0 stayed 0.69.0 until it ran).
# Lifecycle scripts stay off, so a new release cannot run one in a sandbox that
# holds the API key. `timeout` bounds the whole step so a hung registry cannot
# stop Pi from starting. Pi's own subcommands are passed through to it
# untouched, so they must not trigger an install of their own.
# Keep this list in step with the same list in pi.
case "${1:-}" in
    install | remove | uninstall | update | list | config | auth) ;;
    *)
        # shellcheck disable=SC2016  # $package expands when sh runs the script, not here
        if ! timeout 120 sh -c '
            export npm_config_ignore_scripts=true
            failed=0
            for package in \
                pi-subagents \
                @tintinweb/pi-subagents \
                pi-background-tasks \
                pi-extension-manager
            do
                pi install "npm:$package" || failed=1
            done
            pi update --extensions || failed=1
            exit "$failed"
        ' >/dev/null 2>&1; then
            printf '%s\n' "pi-sandbox: could not update the extensions" >&2
        fi
        ;;
esac

# Volumes made by earlier images still hold the two Herdr skills, which now
# describe a tool this image does not carry. Remove them, best effort, so a
# skill listing cannot point at a missing binary. This can go once the old
# volumes are gone.
rm -rf "$HOME/.pi/agent/skills/herdr" "$HOME/.pi/agent/skills/herdr-fleet" 2>/dev/null || true

# Pi replaces this script, so the container's foreground process is Pi and a
# Herdr pane on the host still sees a Pi agent.
exec pi "$@"
