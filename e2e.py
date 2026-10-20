"""End-to-end check of the `pi` wrapper on real Docker.

Starts the real wrapper in a pseudo-terminal in a throwaway repository, the
way a Herdr pane does, and proves from inside the container it started that
the project is writable and the parts of `.git` that can name a command are
not. Then it ends sessions every way one can end and checks that no container
is left behind. No API key and no model call: Pi starts its interface and
waits for input.

    python3 e2e.py [report-file]

The report is printed and written to the file (default e2e-report.txt).
"""

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent / "pi"
READY_TIMEOUT = 900  # a first launch builds the image
GONE_TIMEOUT = 30

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


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    ).stdout.strip()


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
            # A forked copy keeps every other session's terminal open, and a
            # process exiting with unread output on one waits for it forever.
            os.closerange(3, 1024)
            job = os.fork()
            if job == 0:
                os.setpgid(0, 0)
                signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                os.tcsetpgrp(0, os.getpgrp())
                signal.signal(signal.SIGTTOU, signal.SIG_DFL)
                os.chdir(project)
                os.execv(
                    WRAPPER, [str(WRAPPER), "--provider", "opencode", "--model", "x"]
                )
            os.waitpid(job, 0)
            # A shell outlives its job, and its exit would hang up whatever
            # the job left in the foreground group.
            time.sleep(3600)
            os._exit(0)
        self.output = b""
        self.container = ""
        self.pid = 0

    def read(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and self.fd >= 0:
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    self.output += os.read(self.fd, 65536)
                except OSError:
                    return

    def wait_ready(self) -> None:
        """Pi's footer names the working directory and its branch once its
        interface is up. The build log only names the directory."""

        end = time.time() + READY_TIMEOUT
        while b"/workspace (" not in self.output or not self.container:
            if time.time() > end:
                raise TimeoutError(self.output[-2000:].decode(errors="replace"))
            self.read(1)
            self.pid = int((children(self.shell) or ["0"])[0])
            self.container = find_container(self.project, self.pid)
        # The footer comes up before Pi takes keys: a Ctrl-C sent at once was
        # missed and Pi never quit.
        self.read(3)

    def type(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def docker_client(self) -> int:
        return int(children(self.pid, "docker")[0])

    def wrapper_alive(self) -> bool:
        return str(self.pid) in children(self.shell)

    def close_pane(self) -> None:
        """Closing the terminal hangs up everything in its foreground group,
        which is what closing a Herdr pane does."""

        os.close(self.fd)
        self.fd = -1

    def end(self) -> None:
        """Close the pane for good, once the result has been read."""

        # Closed first: a process exiting with unread terminal output waits
        # for it to drain, so the wait below would never return.
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        os.kill(self.shell, signal.SIGKILL)
        os.waitpid(self.shell, 0)


def children(parent: int, name: str = "") -> list[str]:
    return subprocess.run(
        ["pgrep", "-P", str(parent), *([name] if name else [])],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()


def find_container(project: Path, wrapper: int = 0) -> str:
    """The container whose /workspace is this project, and when sessions
    share it, the one the wrapper named after its own process id. Docker
    Desktop shows the source under /host_mnt, so match the end of the path."""

    ids = docker("ps", "-aq", "--filter", "label=pi-sandbox=1").split()
    for container in ids:
        found = json.loads(
            docker("inspect", "--format", "{{json .}}", container) or "{}"
        )
        if not found:
            continue
        if wrapper and not found["Name"].endswith(f"-{wrapper}"):
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
    return docker("inspect", "--format", "{{.State.Status}}", container)


def orphaned_clients(project: Path) -> list[int]:
    found = subprocess.run(
        ["pgrep", "-f", f"docker run .*{project}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return [int(pid) for pid in found]


EXITS = {
    "pi quit (/quit)": lambda s: s.type(b"/quit\r"),
    "ctrl-c twice": lambda s: (s.type(b"\x03"), s.read(0.3), s.type(b"\x03")),
    "ctrl-d": lambda s: s.type(b"\x04"),
    "pane closed": Session.close_pane,
    "SIGHUP to wrapper": lambda s: os.kill(s.pid, signal.SIGHUP),
    "SIGTERM to wrapper": lambda s: os.kill(s.pid, signal.SIGTERM),
    "SIGKILL to wrapper": lambda s: os.kill(s.pid, signal.SIGKILL),
    "docker client killed": lambda s: os.kill(s.docker_client(), signal.SIGKILL),
}


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "e2e-report.txt")
    project = Path(tempfile.mkdtemp(prefix="pi-e2e-")).resolve()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    lines = []
    failed = False
    volume = ""
    try:
        session = Session(project)
        session.wait_ready()
        volume = docker(
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}',
            session.container,
        )
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
        if owner != os.getuid():
            failed = True
        (project / "probe-file").unlink()
        memory = docker(
            "inspect", "--format", "{{.HostConfig.Memory}}", session.container
        )
        lines.append(f"  memory limit {memory} bytes")
        if memory != str(8 * 1024**3):
            failed = True
        session.type(b"/quit\r")
        session.read(5)
        session.end()

        lines.append("")
        lines.append("exit path -> container left behind")
        for name, end_session in EXITS.items():
            session = Session(project)
            session.wait_ready()
            end_session(session)
            session.read(2)
            left = container_left(project)
            # A wrapper still running means the session never ended, which is
            # a failure of its own rather than a container left behind.
            if session.wrapper_alive():
                left = f"session did not end, container {left}"
            orphans = orphaned_clients(project)
            for pid in orphans:
                os.kill(pid, signal.SIGKILL)
            container = find_container(project)
            if container:
                docker("rm", "-f", container)
            session.end()
            note = f", docker client left running {orphans}" if orphans else ""
            lines.append(f"  {name:24} {left}{note}")
            print(lines[-1], file=sys.stderr, flush=True)
            if left != "none" or orphans:
                failed = True
    finally:
        container = find_container(project)
        if container:
            docker("rm", "-f", container)
        for pid in orphaned_clients(project):
            os.kill(pid, signal.SIGKILL)
        if volume:
            docker("volume", "rm", volume)
        shutil.rmtree(project, ignore_errors=True)

    lines.append("")
    lines.append("FAIL" if failed else "PASS")
    report = "\n".join(lines) + "\n"
    report_path.write_text(report)
    print(report, end="")
    return 1 if failed else 0


def storage_figures(volume: str) -> dict[str, str]:
    """What pi-sandbox holds on disk: its images, containers, build cache
    left by its Pi installs, and the project's state volume."""

    images = docker("images", "--filter", "label=pi-sandbox.image=1", "-q").split()
    volumes = docker(
        "system",
        "df",
        "-v",
        "--format",
        "{{range .Volumes}}{{println .Name .Size}}{{end}}",
    ).splitlines()
    cache = docker(
        "system",
        "df",
        "-v",
        "--format",
        "{{range .BuildCache}}{{println .Shared .Size .Description}}{{end}}",
    ).splitlines()
    pi_cache = [line for line in cache if "must pass --build-arg PI_VERSION" in line]
    return {
        "images": str(len(images)),
        "containers": str(
            len(docker("ps", "-aq", "--filter", "label=pi-sandbox=1").split())
        ),
        "Pi install build cache records": str(len(pi_cache)),
        "unshared ones": str(sum(line.startswith("false") for line in pi_cache)),
        "state volume": next(
            (line.split()[1] for line in volumes if line.split()[0] == volume), "?"
        ),
    }


def storage() -> int:
    """Run sessions in one project, one after another and then side by side,
    and check that what pi-sandbox keeps on disk stays flat."""

    report_path = Path(sys.argv[2] if len(sys.argv) > 2 else "e2e-storage.txt")
    project = Path(tempfile.mkdtemp(prefix="pi-e2e-")).resolve()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    # Forwarded by the wrapper, so every start writes auth.json. The provider
    # the sessions name is another one, so it is never sent anywhere.
    os.environ["OPENCODE_GO_API_KEY"] = "e2e-not-a-real-key"
    lines = []
    failed = False
    volume = ""
    exits = list(EXITS.items())

    def end_with(session: Session, index: int) -> str:
        name, end_session = exits[index % len(exits)]
        end_session(session)
        session.read(1)
        return name

    try:
        first = Session(project)
        first.wait_ready()
        volume = docker(
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}',
            first.container,
        )
        image = docker("inspect", "--format", "{{.Config.Image}}", first.container)
        end_with(first, 0)
        first.end()
        figures = [("after the first start", storage_figures(volume))]

        for index in range(30):
            session = Session(project)
            session.wait_ready()
            name = end_with(session, index)
            session.end()
            if container_left(project) != "none":
                failed = True
                lines.append(f"  cycle {index + 1} ({name}) left a container")
        figures.append(("after 30 sessions in turn", storage_figures(volume)))

        # Without the image, ten sessions starting at once all build it.
        docker("rmi", image)
        sessions = [Session(project) for _ in range(10)]
        for session in sessions:
            session.wait_ready()
        key = docker(
            "run",
            "--rm",
            "--entrypoint",
            "python3",
            "--mount",
            f"type=volume,source={volume},target=/home/agent",
            image,
            "-c",
            "import json; print(json.load(open('/home/agent/.pi/agent/auth.json'))"
            "['opencode-go']['key'])",
        )
        lines.append(f"  auth.json after 10 concurrent starts holds: {key}")
        if key != "e2e-not-a-real-key":
            failed = True
        for index, session in enumerate(sessions):
            end_with(session, index)
        for session in sessions:
            session.end()
        if container_left(project) != "none":
            failed = True
            lines.append("  a concurrent session left a container")
        figures.append(("after 10 sessions at once", storage_figures(volume)))
    finally:
        for pid in orphaned_clients(project):
            os.kill(pid, signal.SIGKILL)
        while container := find_container(project):
            docker("rm", "-f", container)
        if volume:
            docker("volume", "rm", volume)
        shutil.rmtree(project, ignore_errors=True)

    keys = list(figures[0][1])
    lines.append("")
    lines.append(" | ".join(["", *keys]))
    for label, row in figures:
        lines.append(" | ".join([label, *row.values()]))
    for key in ("images", "containers", "unshared ones"):
        if len({row[key] for _, row in figures}) != 1:
            failed = True
    lines.append("")
    lines.append("FAIL" if failed else "PASS")
    report = "\n".join(lines) + "\n"
    report_path.write_text(report)
    print(report, end="")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(storage() if sys.argv[1:2] == ["--storage"] else main())
