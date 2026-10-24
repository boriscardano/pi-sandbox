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
# regular file, or a link to one, is read, since read_text() on a FIFO the
# agent left would block forever and the atomic replace would never get the
# chance to repair it.
#
# Sessions in one project share the volume, so starts can overlap. A lock on
# the directory keeps one start from unlinking or renaming another's pending
# file, which lost the key for some of twelve concurrent starts. The agent in
# another session can hold that lock for good, so `timeout` bounds the wait,
# and O_DIRECTORY refuses a FIFO swapped in for the directory rather than
# blocking on it. `-I` keeps Python from importing a module the agent left in
# /workspace, the working directory, before Pi has even started. Both bounded
# steps read nothing, so their stdin is /dev/null rather than the terminal,
# which `timeout` puts them in the background of.
#
# The whole thing is best effort. Pi is what this container is for, and a home
# directory the agent has ruined must cost it the subscription key, not the
# session: `set -e` would otherwise turn `mkdir ~/.pi/agent/auth.json` into a
# sandbox that never starts again until the volume is deleted.
if [ -n "${OPENCODE_GO_API_KEY:-}" ] && ! (
    mkdir -p "$HOME/.pi/agent" && timeout 10 python3 -I -c 'import fcntl, json, os, pathlib

directory = pathlib.Path(os.environ["HOME"]) / ".pi/agent"
fcntl.flock(os.open(directory, os.O_RDONLY | os.O_DIRECTORY), fcntl.LOCK_EX)
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
' </dev/null
); then
    printf '%s\n' "pi-sandbox: could not write the subscription key to auth.json" >&2
fi

# Extensions live in /home/agent, which is the project's state volume, so an
# image rebuild does not reach a project whose volume already exists. Install
# any of the four that is missing or not at its pinned version, best effort: a
# registry that is down or slow, or a home the agent has ruined, must cost the
# install rather than the session.
#
# The four are listed here and nowhere else, each at an exact version that is
# bumped by hand in a reviewed change, and docs/design.md says why. `pi install`
# with a version moves an installed one up or down to it, and nothing runs
# `pi update`, so no release reaches the sandbox unreviewed. The installed
# version is read from the package.json under Pi's npm directory, and only from
# a regular file, since a FIFO the agent left there would block the read. An
# install costs about a second and every Herdr pane is a start, so a volume at
# the pins makes no call. Lifecycle scripts stay off, so a release cannot run
# one in a sandbox that holds the API key. npm's cache goes to /tmp, which goes
# with the container, since in the volume it kept every release it downloaded.
# `timeout` bounds the whole step so a hung registry cannot stop Pi from
# starting. Pi's own subcommands are passed through to it untouched, so they
# must not trigger an install of their own.
# Keep this list in step with the same list in pi.
case "${1:-}" in
    install | remove | uninstall | update | list | config | auth) ;;
    *)
        packages='pi-subagents@0.81.0 @tintinweb/pi-subagents@0.22.0 pi-background-tasks@2.6.10 pi-extension-manager@0.8.2'
        # shellcheck disable=SC2016,SC2086  # $package expands in the inner sh, and the list splits into one argument each
        if ! timeout 120 sh -c '
            export npm_config_ignore_scripts=true
            export npm_config_cache="${TMPDIR:-/tmp}/pi-sandbox-npm-cache"
            failed=0
            for package in "$@"; do
                installed=$HOME/.pi/agent/npm/node_modules/${package%@*}/package.json
                if [ -f "$installed" ] && grep -Eq "\"version\": *\"${package##*@}\"" "$installed"; then
                    continue
                fi
                pi install "npm:$package" || failed=1
            done
            exit "$failed"
        ' sh $packages </dev/null >/dev/null 2>&1; then
            printf '%s\n' "pi-sandbox: could not install the extensions" >&2
        fi
        ;;
esac

# Pi replaces this script, so the container's foreground process is Pi and a
# Herdr pane on the host still sees a Pi agent.
exec pi "$@"
