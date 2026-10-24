"""End-to-end check of the `pi` wrapper on real Docker.

Starts the real wrapper in a pseudo-terminal in a throwaway repository, the
way a Herdr pane does, and proves from inside the container it started that
the project is writable and the parts of `.git` that can name a command are
not. It checks that a copy of the wrapper refuses the project it sits in,
that uv still picks Debian's Python for a project that accepts it, syncs a
Python project and checks that the state volume grows by little more than
the venv, which the next session imports from offline, and records whether
the container reaches a host service on 127.0.0.1.
Then it ends sessions every way one can end and checks that no container
is left behind, and rebuilds the image for a new Pi release and checks that
the build cache the old one left is pruned. No API key and no model call: Pi
starts its interface and waits for input.

    python3 e2e.py [report-file]
    python3 e2e.py --storage [report-file]

`--storage` runs 30 sessions in turn and then 10 at once in one project and
checks that pi-sandbox keeps its image and no new unused build cache. It also
reports the state volume's size. This script itself deletes only the
project, volume and containers it created, but the wrapper it runs removes
every other pi-sandbox image and unused Pi install build cache when a session
ends, as the README says, so run it where no session needs an older image.
Both modes count build cache across the whole host, so run them without other
sessions building. The report is printed and written to the file (default
e2e-report.txt or e2e-storage.txt).
"""

import contextlib
import http.server
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent / "pi"
READY_TIMEOUT = 900  # a first launch builds the image
GONE_TIMEOUT = 30
# Any release other than the newest, standing in for the one the image was
# built with before npm moved on.
OLDER_PI = "1.2.0"
PI_INSTALL_STEP = "the wrapper must pass --build-arg PI_VERSION"
KEY_LOST = b"could not write the subscription key"

# Run in the container by `docker exec`, as the user Pi runs as. Each line
# prints one result, so a check that fails names itself.
CHECKS = r"""
cd /workspace
can() { if sh -c "$1" 2>/dev/null; then echo "WRITABLE $2"; else echo "readonly $2"; fi; }
can 'touch probe-file' workspace
can 'printf x >> .git/config' .git/config
can 'printf x >> .git/config.worktree' .git/config.worktree
can 'printf x >> .git/commondir' .git/commondir
can 'touch .git/hooks/pre-commit' .git/hooks
can 'mkdir .git/worktrees/x' .git/worktrees
can 'mkdir .git/modules/x' .git/modules
can 'mv .git moved-git' 'mv .git'
"""
EXPECTED_CHECKS = [
    "WRITABLE workspace",
    "readonly .git/config",
    "readonly .git/config.worktree",
    "readonly .git/commondir",
    "readonly .git/hooks",
    "readonly .git/worktrees",
    "readonly .git/modules",
    "readonly mv .git",
]

# What an agent does first in a Python project: install its dependencies. The
# project asks for a recent Python without pinning one, as most do, which the
# image's Python must satisfy without uv downloading another into the volume.
# Prints the state volume's size in KB before and after, then what is in it.
UV_SYNC = r"""
cd /workspace
cat > pyproject.toml <<'EOF'
[project]
name = "e2e-probe"
version = "0"
requires-python = ">=3.12"
dependencies = ["httpx", "pydantic"]
EOF
du -sk "$HOME" | cut -f1
timeout 300 uv sync -q >&2
du -sk "$HOME" | cut -f1
du -sk "$HOME"/* "$HOME"/.[!.]* 2>/dev/null | sort -rn
"""
# The venv the sync left, used by the next session with nothing downloaded.
UV_REUSE = "timeout 60 uv run --offline python -c 'import httpx, pydantic'"
# Per project, so dozens of worktrees must not add up to gigabytes. A Python
# uv downloaded (about 90 MB) or uv's cache in the volume fails it, the venv
# alone (9 MB measured) does not.
VOLUME_GROWTH_KB = 30 * 1024
# A project that accepts Debian's Python must still get it, as before the image
# carried 3.14: one with a dependency that has no 3.14 wheel synced on 3.11
# and fails on 3.14, since there is no compiler.
UV_FIND = "uv python find '>=3.10'"
# A documented limit, recorded rather than asserted: Docker Desktop forwards
# host.docker.internal to the host's own 127.0.0.1. Linux has no such name.
HOST_PROBE = """
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://host.docker.internal:{sys.argv[1]}/", timeout=5)
    print("reached")
except Exception as error:
    print(f"not reached ({type(error).__name__})")
"""


class Answer(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass

# The extensions the volume holds, from Pi's settings and the version npm
# installed, and a downgrade of one by hand, which the next start must undo.
INSTALLED = r"""python3 -I -c 'import json, pathlib, re
agent = pathlib.Path.home() / ".pi/agent"
for spec in json.loads((agent / "settings.json").read_text())["packages"]:
    name = re.sub(r"^npm:|@[0-9][^/]*$", "", spec)
    package = agent / "npm/node_modules" / name / "package.json"
    print(spec, json.loads(package.read_text())["version"])
'"""
DOWNGRADE = "pi install npm:pi-background-tasks@2.6.9 >/dev/null 2>&1"


def pinned_extensions() -> list[str]:
    """What the entrypoint pins, each as Pi's settings and npm should hold it."""

    for line in (WRAPPER.parent / "entrypoint.sh").read_text().splitlines():
        if line.strip().startswith("packages="):
            specs = line.split("=", 1)[1].strip("'").split()
            return [f"npm:{spec} {spec.rpartition('@')[2]}" for spec in specs]
    raise SystemExit("no packages= line in entrypoint.sh")


def docker(*args: str, check: bool = False) -> str:
    """docker's output. Pass check where a failed call reading as empty would
    pass a check, so it raises instead."""

    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
    ).stdout.strip()


def children(parent: int, name: str = "") -> list[str]:
    return subprocess.run(
        ["pgrep", "-P", str(parent), *([name] if name else [])],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()


# Every session not yet ended, so a failed run still closes them all.
OPEN_SESSIONS: list["Session"] = []
# Its write end is held only by this script, so a read of it returns when the
# script dies, however it dies.
SCRIPT_ALIVE, _SCRIPT_ALIVE_WRITE = os.pipe()


class Session:
    """One wrapper launch in a pseudo-terminal, as a Herdr pane runs it."""

    def __init__(self, project: Path) -> None:
        self.project = project
        # The terminal's session leader stands in for the pane's shell, and
        # runs the wrapper as its foreground job in a process group of its
        # own, as a shell with job control does. Signalling the wrapper then
        # reaches only the wrapper, and closing the terminal hangs up both.
        self.shell, self.fd = pty.fork()
        if self.shell == 0:
            # Neither child may return into this script, whatever fails.
            try:
                # A forked copy keeps every other session's terminal open,
                # and a process exiting with unread output on one waits for
                # it forever.
                os.closerange(3, SCRIPT_ALIVE)
                os.closerange(SCRIPT_ALIVE + 1, 1024)
                job = os.fork()
                if job == 0:
                    try:
                        os.setpgid(0, 0)
                        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                        os.tcsetpgrp(0, os.getpgrp())
                        signal.signal(signal.SIGTTOU, signal.SIG_DFL)
                        os.chdir(project)
                        # Python ignores SIGPIPE, and a shell would not.
                        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                        os.execv(
                            WRAPPER,
                            [str(WRAPPER), "--provider", "opencode", "--model", "x"],
                        )
                    finally:
                        os._exit(127)
                os.waitpid(job, 0)
                # A shell outlives its job, and its exit would hang up
                # whatever the job left in the foreground group. It goes when
                # the script does, so a killed run leaves nothing running.
                os.read(SCRIPT_ALIVE, 1)
            finally:
                os._exit(0)
        self.output = b""
        self.container = ""
        self.pid = 0
        self.started = time.time()
        OPEN_SESSIONS.append(self)

    def read(self, seconds: float) -> None:
        if self.fd < 0:
            time.sleep(seconds)
            return
        end = time.time() + seconds
        while time.time() < end and self.fd >= 0:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    self.output += os.read(self.fd, 65536)
                except OSError:
                    return

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def type(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def docker_client(self) -> int:
        return int(children(self.pid, "docker")[0])

    def close_pane(self) -> None:
        """Closing the terminal hangs up everything in its foreground group,
        which is what closing a Herdr pane does."""

        os.close(self.fd)
        self.fd = -1

    def end(self) -> None:
        """Close the pane for good, once the result has been read."""

        if self not in OPEN_SESSIONS:
            return
        OPEN_SESSIONS.remove(self)
        # Closed first: a process exiting with unread terminal output waits
        # for it to drain, so the wait below would never return.
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        os.kill(self.shell, signal.SIGKILL)
        os.waitpid(self.shell, 0)


def wait_ready(sessions: list[Session]) -> None:
    """Pi's footer names the working directory and its branch once its
    interface is up, which the build log does not. Every terminal is read
    meanwhile, so no session stalls writing to one nobody reads."""

    end = time.time() + READY_TIMEOUT
    pending = list(sessions)
    while pending:
        for session in sessions:
            session.read(0.05)
        for session in list(pending):
            tail = session.output[-2000:].decode(errors="replace")
            if time.time() > end:
                raise TimeoutError(f"Pi did not start:\n{tail}")
            if not session.pid:
                session.pid = int((children(session.shell) or ["0"])[0])
                if not session.pid and time.time() - session.started > 10:
                    raise RuntimeError(f"the wrapper exited at once:\n{tail}")
            elif not session.alive():
                raise RuntimeError(f"the wrapper exited before Pi started:\n{tail}")
            elif b"/workspace (" in session.output:
                session.container = find_container(session.project, session.pid)
                if session.container:
                    pending.remove(session)
    # The footer comes up before Pi takes keys: a Ctrl-C sent at once was
    # missed and Pi never quit.
    for _ in range(30):
        for session in sessions:
            session.read(0.1)


def find_container(project: Path, wrapper: int = 0) -> str:
    """The container whose /workspace is this project, and when sessions
    share it, the one the wrapper named after its own process id. Docker
    Desktop shows the source under /host_mnt, so match the end of the path."""

    listed = docker("ps", "-aq", "--filter", "label=pi-sandbox=1", check=True)
    for container in listed.split():
        found = json.loads(
            docker("inspect", "--format", "{{json .}}", container) or "{}"
        )
        if not found or (wrapper and not found["Name"].endswith(f"-{wrapper}")):
            continue
        if any(
            m["Destination"] == "/workspace" and m["Source"].endswith(str(project))
            for m in found["Mounts"]
        ):
            return container
    return ""


def container_left(project: Path) -> str:
    """Wait for the container to go, and say what is left if it does not."""

    end = time.time() + GONE_TIMEOUT
    while time.time() < end:
        container = find_container(project)
        if not container:
            return "none"
        time.sleep(1)
    return docker("inspect", "--format", "{{.State.Status}}", container) or "none"


def kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def orphaned_clients(project: Path) -> list[int]:
    found = subprocess.run(
        ["pgrep", "-f", f"docker run .*{project}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return [int(pid) for pid in found]


def unshared_pi_installs() -> set[str]:
    """Build cache records of Pi installs that no image or build uses, as the
    wrapper lists them. Docker prints `.Shared` as the string true or false,
    and a template docker cannot run raises rather than listing nothing."""

    lines = docker(
        "system",
        "df",
        "-v",
        "--format",
        '{{range .BuildCache}}{{if eq .Shared "false"}}{{if eq .InUse "false"}}'
        "{{println .ID .Description}}{{end}}{{end}}{{end}}",
        check=True,
    ).splitlines()
    return {
        line.split()[0]
        for line in lines
        # The same guard as the wrapper's, before an id reaches a filter.
        if PI_INSTALL_STEP in line
        and line.split()[0].isalnum()
        and len(line.split()[0]) >= 20
    }


EXITS: dict[str, Callable[[Session], object]] = {
    "pi quit (/quit)": lambda s: s.type(b"/quit\r"),
    "ctrl-c twice": lambda s: (s.type(b"\x03"), s.read(0.3), s.type(b"\x03")),
    "ctrl-d": lambda s: s.type(b"\x04"),
    "pane closed": Session.close_pane,
    "SIGHUP to wrapper": lambda s: os.kill(s.pid, signal.SIGHUP),
    "SIGTERM to wrapper": lambda s: os.kill(s.pid, signal.SIGTERM),
    "SIGKILL to wrapper": lambda s: os.kill(s.pid, signal.SIGKILL),
    "docker client killed": lambda s: os.kill(s.docker_client(), signal.SIGKILL),
}
# Takes the watcher with the wrapper, so the container is left until the next
# launch removes it.
GROUP_KILL = "SIGKILL to the whole process group"


def start(project: Path) -> Session:
    session = Session(project)
    wait_ready([session])
    return session


def wrapper_finished(session: Session) -> bool:
    """Whether the wrapper exits within the timeout. Its cleanup after the
    container goes, the exit checks and the image and cache removal, must not
    be cut short by the pane closing under it."""

    end = time.time() + GONE_TIMEOUT
    while session.alive():
        if time.time() > end:
            return False
        session.read(0.5)
    return True


def quit_pi(session: Session) -> None:
    session.type(b"/quit\r")
    if not wrapper_finished(session):
        raise RuntimeError("the wrapper did not exit after /quit")
    session.end()


@contextlib.contextmanager
def throwaway_project() -> Iterator[Path]:
    """A fresh repository, and afterwards, however the run went, no session,
    container, docker client, state volume or directory left of it."""

    project = Path(tempfile.mkdtemp(prefix="pi-e2e-")).resolve()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    try:
        yield project
    finally:
        while OPEN_SESSIONS:
            OPEN_SESSIONS[0].end()
        for pid in orphaned_clients(project):
            kill(pid)
        for _ in range(10):
            # Best effort, so a docker hiccup here still lets the volume and
            # the directory go.
            container = ""
            with contextlib.suppress(subprocess.CalledProcessError):
                container = find_container(project)
            if not container:
                break
            docker("rm", "-f", container)
            time.sleep(1)
        for volume in docker(
            "volume", "ls", "-q", "--filter", f"name=pi-sandbox-{project.name}-"
        ).split():
            docker("volume", "rm", volume)
        shutil.rmtree(project, ignore_errors=True)


def self_mount(lines: list[str]) -> bool:
    """A copy of the wrapper launched on the project that holds it must stop
    before Docker, or the agent could rewrite the launcher the host runs."""

    with throwaway_project() as project:
        for name in ("pi", "Dockerfile.pi", "entrypoint.sh"):
            shutil.copy2(WRAPPER.parent / name, project / name)
        # The refusal comes before anything slow, so a run still going after
        # a minute has got past it and is building or starting a session.
        try:
            refused = subprocess.run(
                [str(project / "pi"), "--provider", "opencode", "--model", "x"],
                cwd=project,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            status, stderr = str(refused.returncode), refused.stderr
        except subprocess.TimeoutExpired:
            status, stderr = "none, still running after 60 s", ""
        container = find_container(project)
    lines.append(
        f"wrapper inside its own project: exit {status}, "
        f"container {container or 'none'}"
    )
    lines += [f"  {line}" for line in stderr.splitlines()]
    return status != "2" or "it contains this wrapper" not in stderr or bool(container)


def checks(lines: list[str]) -> bool:
    failed = self_mount(lines)
    lines.append("")
    with throwaway_project() as project:
        session = start(project)
        image = docker("inspect", "--format", "{{.Config.Image}}", session.container)
        results = docker("exec", session.container, "sh", "-c", CHECKS).splitlines()
        lines.append("mounts, checked from inside the container:")
        lines += [f"  {line}" for line in results]
        if results != EXPECTED_CHECKS:
            failed = True
            lines.append(f"  FAIL: expected {EXPECTED_CHECKS}")
        # The file the container wrote belongs to the host user, so the agent
        # cannot leave root-owned files in the project.
        owner = (project / "probe-file").stat().st_uid
        lines.append(f"  probe-file owner uid {owner}, host uid {os.getuid()}")
        failed |= owner != os.getuid()
        memory = docker(
            "inspect", "--format", "{{.HostConfig.Memory}}", session.container
        )
        lines.append(f"  memory limit {memory} bytes")
        failed |= memory != str(8 * 1024**3)
        # Attach works without a log driver: Pi's footer was read through it above.
        log_driver = docker(
            "inspect", "--format", "{{.HostConfig.LogConfig.Type}}", session.container
        )
        lines.append(f"  log driver {log_driver}")
        failed |= log_driver != "none"
        nano_cpus = docker(
            "inspect", "--format", "{{.HostConfig.NanoCpus}}", session.container
        )
        ncpu = int(docker("info", "--format", "{{.NCPU}}", check=True))
        lines.append(f"  cpu limit {nano_cpus} nanocpus of the daemon's {ncpu} cpus")
        failed |= nano_cpus != str(max(1, ncpu // 2) * 10**9)
        found = docker("exec", session.container, "sh", "-c", UV_FIND)
        lines.append(f"  uv's Python for a >=3.10 project: {found}")
        failed |= found != "/usr/bin/python3"
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                reach = docker(
                    "exec",
                    session.container,
                    "timeout",
                    "30",
                    "python3",
                    "-c",
                    HOST_PROBE,
                    str(server.server_address[1]),
                )
            finally:
                server.shutdown()
        lines.append(
            f"  host service on 127.0.0.1, from the container: {reach or 'probe failed'}"
        )

        pinned = pinned_extensions()
        installed = docker("exec", session.container, "sh", "-c", INSTALLED)
        lines.append("  extensions a fresh volume got:")
        lines += [f"    {line}" for line in installed.splitlines()]
        if installed.splitlines() != pinned:
            failed = True
            lines.append(f"    FAIL: expected {pinned}")
        docker("exec", session.container, "sh", "-c", DOWNGRADE, check=True)

        before, after, *contents = docker(
            "exec", session.container, "sh", "-c", UV_SYNC, check=True
        ).splitlines()
        grown = int(after) - int(before)
        lines.append("")
        lines.append(
            f"state volume after uv sync of a Python >=3.12 project: "
            f"{int(before) // 1024} MB -> {int(after) // 1024} MB, "
            f"grew {grown // 1024} MB (allowed {VOLUME_GROWTH_KB // 1024} MB)"
        )
        lines += [f"  {line}" for line in contents]
        failed |= grown > VOLUME_GROWTH_KB
        quit_pi(session)

        session = start(project)
        reuse = subprocess.run(
            ["docker", "exec", session.container, "sh", "-c", UV_REUSE],
            capture_output=True,
            text=True,
            check=False,
        )
        lines.append(
            f"  next session imports from that venv offline: {reuse.returncode == 0}"
            + (f"\n{reuse.stderr}" if reuse.returncode else "")
        )
        failed |= reuse.returncode != 0
        installed = docker("exec", session.container, "sh", "-c", INSTALLED)
        lines.append(
            "  next start moved a hand downgrade back to the pins: "
            f"{installed.splitlines() == pinned}"
        )
        failed |= installed.splitlines() != pinned
        quit_pi(session)

        lines.append("")
        lines.append("exit path -> container left behind")
        for name, end_session in [
            *EXITS.items(),
            (GROUP_KILL, lambda s: os.killpg(s.pid, signal.SIGKILL)),
        ]:
            session = start(project)
            end_session(session)
            session.read(2)
            left = container_left(project)
            if name == GROUP_KILL and left == "running":
                stale = find_container(project)
                session.end()
                session = start(project)
                if not docker("ps", "-aq", "--filter", f"id={stale}", check=True):
                    left = "none"
                    name = f"{name}, then a new launch"
                quit_pi(session)
            # A wrapper still running means the session never ended, which is
            # a failure of its own rather than a container left behind.
            if not wrapper_finished(session):
                left = f"session did not end, container {left}"
            # A client the wrapper's end killed can take a moment to go.
            for _ in range(5):
                orphans = orphaned_clients(project)
                if not orphans:
                    break
                time.sleep(1)
            for pid in orphans:
                kill(pid)
            container = find_container(project)
            if container:
                docker("rm", "-f", container)
            session.end()
            note = f", docker client left running {orphans}" if orphans else ""
            lines.append(f"  {name:40} {left}{note}")
            print(lines[-1], file=sys.stderr, flush=True)
            failed |= left != "none" or bool(orphans)

        # The tag now holds an image of an older Pi, as if npm had moved on
        # since it was built, so the next launch rebuilds it. Unused Pi
        # install cache an earlier run left is not counted. This script no
        # longer prunes it first, so a build that reuses one can hide a prune
        # that no longer works, which a fresh CI runner, with none, catches.
        cache_before = unshared_pi_installs()
        ids = []
        if sys.platform.startswith("linux"):
            ids = [f"--build-arg=AGENT_UID={os.getuid()}"]
            ids += [f"--build-arg=AGENT_GID={os.getgid()}"]
        subprocess.run(
            ["docker", "build", "--file", str(WRAPPER.parent / "Dockerfile.pi")]
            + ids
            + ["--build-arg", f"PI_VERSION={OLDER_PI}", "--tag", image]
            + [str(WRAPPER.parent)],
            check=True,
        )
        older = docker("image", "inspect", "--format", "{{.Id}}", image, check=True)
        quit_pi(start(project))
        version = docker(
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "pi-sandbox.version"}}',
            image,
            check=True,
        )
        kept = bool(docker("image", "inspect", "--format", "{{.Id}}", older))
        left = unshared_pi_installs() - cache_before
        lines.append("")
        lines.append(
            f"a new Pi release: {OLDER_PI} rebuilt as {version}, "
            f"old image kept {kept}, Pi install build cache left unused {len(left)}"
        )
        failed |= version in ("", OLDER_PI) or kept or bool(left)
    return failed


def storage_figures(volume: str, image: str) -> dict[str, str]:
    """What pi-sandbox holds on disk for this run: its image, the Pi install
    build cache no image uses, and the project's state volume."""

    volumes = docker(
        "system",
        "df",
        "-v",
        "--format",
        "{{range .Volumes}}{{println .Name .Size}}{{end}}",
    ).splitlines()
    return {
        "image present": str(bool(docker("images", "-q", image))),
        "unused Pi install build cache": str(len(unshared_pi_installs())),
        "state volume": next(
            (line.split()[1] for line in volumes if line.split()[0] == volume), "?"
        ),
    }


def storage(lines: list[str]) -> bool:
    failed = False
    # Forwarded by the wrapper, so every start writes auth.json. The provider
    # the sessions name is another one, so it is never sent anywhere.
    os.environ["OPENCODE_GO_API_KEY"] = "e2e-not-a-real-key"
    exits = list(EXITS.values())
    with throwaway_project() as project:
        session = start(project)
        volume = docker(
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}',
            session.container,
        )
        image = docker("inspect", "--format", "{{.Config.Image}}", session.container)
        quit_pi(session)
        figures = [("after the first start", storage_figures(volume, image))]

        for index in range(30):
            session = start(project)
            exits[index % len(exits)](session)
            wrapper_finished(session)
            session.end()
            if container_left(project) != "none":
                failed = True
                lines.append(f"  session {index + 1} left a container")
        figures.append(("after 30 sessions in turn", storage_figures(volume, image)))

        # Ten sessions starting at once, all writing the key. The image is
        # kept, since this run deletes only what it created, so they do not
        # race a build.
        sessions = [Session(project) for _ in range(10)]
        wait_ready(sessions)
        lost = sum(KEY_LOST in s.output for s in sessions)
        lines.append(f"  starts that lost the key among 10 concurrent: {lost}")
        failed |= lost > 0
        for index, session in enumerate(sessions):
            exits[index % len(exits)](session)
        for session in sessions:
            wrapper_finished(session)
            session.end()
        if container_left(project) != "none":
            failed = True
            lines.append("  a concurrent session left a container")
        figures.append(("after 10 sessions at once", storage_figures(volume, image)))

    lines.append("")
    lines.append(" | ".join(["", *figures[0][1]]))
    for label, row in figures:
        lines.append(" | ".join([label, *row.values()]))
    for key in ("image present", "unused Pi install build cache"):
        failed |= len({row[key] for _, row in figures}) != 1
    return failed


def main() -> int:
    run, default = (
        (storage, "e2e-storage.txt")
        if "--storage" in sys.argv
        else (checks, "e2e-report.txt")
    )
    paths = [arg for arg in sys.argv[1:] if arg != "--storage"]
    lines: list[str] = []
    failed = True
    # A CI step that times out is sent SIGTERM, which would skip the report.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    # A run that fails partway, or is interrupted, still ends in its report.
    try:
        failed = run(lines)
    except BaseException:
        lines.append(traceback.format_exc())
        raise
    finally:
        lines += ["", "FAIL" if failed else "PASS"]
        report = "\n".join(lines) + "\n"
        Path(paths[0] if paths else default).write_text(report)
        print(report, end="")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
