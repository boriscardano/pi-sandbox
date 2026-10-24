"""Verify that the host Pi sandbox wrapper builds an isolating docker run."""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).parent
WRAPPER = ROOT / "pi"
FAKE_KEY = "opencode-go-test-key-do-not-leak"

FAKE_DOCKER = """#!/bin/sh
{
    printf '%s\\n' '---'
    printf 'PARENT=%s\\n' "$(ps -o command= -p "$PPID" 2>/dev/null)"
    for arg in "$@"; do
        printf '%s\\n' "$arg"
    done
} >>"$PI_SANDBOX_FAKE_LOG"
# A container that runs on, a live session's or one whose wrapper is gone,
# can change the project at any moment until it is removed.
if [ -n "${PI_SANDBOX_FAKE_LIVE:-}" ] && [ ! -e "$PI_SANDBOX_FAKE_LOG.removed" ]; then
    if [ "$*" = "rm -f $PI_SANDBOX_FAKE_LIVE" ]; then
        : >"$PI_SANDBOX_FAKE_LOG.removed"
    else
        sh -c "$PI_SANDBOX_FAKE_LIVE_AGENT" sh "$@"
    fi
fi
case "$1" in
    image)
        case "$2" in
            inspect)
                # One inspect both checks that the image exists and reads
                # the version label with --format. A non-zero status means
                # the image is missing, and a successful inspect of an image
                # without the label prints nothing.
                if [ "${PI_SANDBOX_FAKE_INSPECT:-0}" -ne 0 ]; then
                    exit "${PI_SANDBOX_FAKE_INSPECT}"
                fi
                if [ "$3" = "--format" ]; then
                    printf '%s\\n' "${PI_SANDBOX_FAKE_LABEL:-}"
                    exit 0
                fi
                ;;
            prune) exit 0 ;;
        esac
        ;;
    images)
        printf '%s\\n' "${PI_SANDBOX_FAKE_TAGS:-}" | tr ' ' '\\n'
        exit 0
        ;;
    rmi) exit "${PI_SANDBOX_FAKE_RMI_STATUS:-0}" ;;
    rm) exit "${PI_SANDBOX_FAKE_RM_STATUS:-0}" ;;
    # The build cache listing, as the wrapper's template prints it.
    system) printf '%s' "${PI_SANDBOX_FAKE_CACHE:-}"; exit 0 ;;
    ps)
        printf '%s' "${PI_SANDBOX_FAKE_CONTAINERS:-}"
        exit "${PI_SANDBOX_FAKE_PS_STATUS:-0}"
        ;;
    # The daemon's CPU count, as `docker info --format '{{.NCPU}}'` prints it,
    # and an unreachable daemon when there is none.
    info)
        [ -n "${PI_SANDBOX_FAKE_NCPU-8}" ] || exit 1
        printf '%s\\n' "${PI_SANDBOX_FAKE_NCPU-8}"
        exit 0
        ;;
esac
if [ "$1" = build ]; then
    exit "${PI_SANDBOX_FAKE_BUILD_STATUS:-0}"
fi
if [ "$1" = run ]; then
    if [ -n "${PI_SANDBOX_FAKE_AGENT:-}" ]; then
        sh -c "$PI_SANDBOX_FAKE_AGENT"
    fi
    exit "${PI_SANDBOX_FAKE_RUN_STATUS:-0}"
fi
exit 0
"""

# The wrapper looks up the newest Pi release before deciding to rebuild. This
# answers that endpoint from an env var, and can fail, so no test touches the
# network.
FAKE_CURL = """#!/bin/sh
url=''
while [ $# -gt 0 ]; do
    case "$1" in
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
case "$url" in
    *pi-coding-agent/latest)
        [ "${PI_SANDBOX_FAKE_CURL_PI:-ok}" = fail ] && exit 22
        body='{"version":"'"${PI_SANDBOX_FAKE_PI_VERSION:-0.87.0}"'"}'
        ;;
    *) exit 22 ;;
esac
printf '%s\\n' "$body"
exit 0
"""

# Simulates what an agent can leave behind in the mounted project. A nested
# repository cannot be prevented by a mount, so the wrapper looks for it at
# exit instead.
NESTED_GIT = (
    "mkdir -p sub/.git/hooks && "
    "printf '[core]\\n\\tfsmonitor = true\\n' > sub/.git/config && "
    "printf '#!/bin/sh\\n' > sub/.git/hooks/pre-commit.sample && "
    "printf '#!/bin/sh\\n' > sub/.git/hooks/post-commit.sample"
)

# Simulates a rebase the agent can plant. `exec` lines in the todo run when the
# host continues the rebase this makes git report.
PLANTED_REBASE = (
    "mkdir -p .git/rebase-merge && "
    "printf 'exec true\\n' > .git/rebase-merge/git-rebase-todo"
)


def _run(
    tmp_path: Path,
    *args: str,
    key: str | None = FAKE_KEY,
    inspect_status: str = "0",
    cwd: Path | None = None,
    home: Path | None = None,
    forward: str | None = None,
    also: dict[str, str] | None = None,
    agent: str | None = None,
    run_status: str = "0",
    wrapper: Path | None = None,
    interpreter: str | None = None,
    uname: str | None = None,
    fake_id: tuple[str, str] | None = None,
    fake_find: str | None = None,
    pi_version: str = "0.87.0",
    curl_pi: str = "ok",
    label: str | None = None,
    tags: str = "",
    rmi_status: str = "0",
    build_status: str = "0",
    cache: str = "",
    containers: str = "",
    terminal: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bin_dir = tmp_path / "fakebin"
    # A test can put a fake of its own here first.
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    # Every test gets the fake registry lookup, so the real curl never runs.
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    # Only when a test asks for a platform, so every other test sees the host.
    if uname is not None:
        fake = bin_dir / "uname"
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' '{uname}'\n")
        fake.chmod(0o755)
    if fake_id is not None:
        uid, gid = fake_id
        fake = bin_dir / "id"
        fake.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"    -u) printf '%s\\n' '{uid}' ;;\n"
            f"    -g) printf '%s\\n' '{gid}' ;;\n"
            "esac\n"
        )
        fake.chmod(0o755)
    if fake_find is not None:
        fake = bin_dir / "find"
        fake.write_text(fake_find)
        fake.chmod(0o755)
    log = tmp_path / "docker.log"

    project = cwd if cwd is not None else tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    if label is None:
        label = pi_version
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home if home is not None else tmp_path / "home"),
        "PI_SANDBOX_FAKE_LOG": str(log),
        "PI_SANDBOX_FAKE_INSPECT": inspect_status,
        "PI_SANDBOX_FAKE_RUN_STATUS": run_status,
        "PI_SANDBOX_FAKE_PI_VERSION": pi_version,
        "PI_SANDBOX_FAKE_CURL_PI": curl_pi,
        "PI_SANDBOX_FAKE_LABEL": label,
        "PI_SANDBOX_FAKE_TAGS": tags,
        "PI_SANDBOX_FAKE_RMI_STATUS": rmi_status,
        "PI_SANDBOX_FAKE_BUILD_STATUS": build_status,
        "PI_SANDBOX_FAKE_CACHE": cache,
        "PI_SANDBOX_FAKE_CONTAINERS": containers,
    }
    if agent is not None:
        env["PI_SANDBOX_FAKE_AGENT"] = agent
    if key is not None:
        env["OPENCODE_GO_API_KEY"] = key
    if forward is not None:
        env["PI_SANDBOX_ENV"] = forward
        env["TYPESAFE_API_KEY"] = "typesafe-test-key-do-not-leak"
    env.update(also or {})

    command = [str(wrapper or WRAPPER), *args]
    # The bug this test file proves is about how sh reads a script, and the
    # shells differ, so a test can name the interpreter to run the wrapper
    # with. The wrapper itself stays POSIX.
    if interpreter is not None:
        command = [interpreter, *command]
    if terminal:
        # A pane: stdin, stdout and stderr are one terminal, whose bytes come
        # back as stdout, escape sequences included.
        leader, follower = os.openpty()
        process = subprocess.Popen(
            command,
            stdin=follower,
            stdout=follower,
            stderr=follower,
            cwd=project,
            env=env,
        )
        os.close(follower)
        output = b""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not select.select([leader], [], [], 1)[0]:
                continue
            try:
                chunk = os.read(leader, 65536)
            except OSError:  # EIO on Linux once every writer has closed
                break
            if not chunk:
                break
            output += chunk
        os.close(leader)
        process.kill()
        returncode = process.wait()
        return subprocess.CompletedProcess(
            command, returncode, output.decode(errors="replace"), ""
        ), _invocations(log)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=project,
        env=env,
        check=False,
    )
    return completed, _invocations(log)


def _invocations(log: Path) -> list[list[str]]:
    return [
        [line for line in block.splitlines() if line and not line.startswith("PARENT=")]
        for block in (log.read_text() if log.exists() else "").split("---")
        if block.strip()
    ]


def _docker_run_parent(log: Path) -> str:
    blocks = [block for block in log.read_text().split("---") if block.strip()]
    for block in blocks:
        lines = block.splitlines()
        if any(line == "run" for line in lines):
            return next(line for line in lines if line.startswith("PARENT="))
    raise AssertionError("no docker run invocation")


def _docker_run(invocations: list[list[str]]) -> list[str]:
    for argv in invocations:
        if argv and argv[0] == "run":
            return argv
    raise AssertionError(f"no docker run invocation in {invocations}")


def _image(argv: list[str]) -> str:
    """The image is the argument right after the last --mount value."""

    last_mount = max(index for index, arg in enumerate(argv) if arg == "--mount")
    return argv[last_mount + 2]


def _wrapper_with_inputs(directory: Path) -> Path:
    """Copy the wrapper and the files it hashes into a fresh directory, so a
    test can edit one input without touching the checkout."""

    directory.mkdir(parents=True, exist_ok=True)
    for name in ("pi", "Dockerfile.pi", "entrypoint.sh"):
        shutil.copy(ROOT / name, directory / name)
    wrapper = directory / "pi"
    wrapper.chmod(0o755)
    return wrapper


def test_wrapper_fails_closed_without_the_opencode_key(tmp_path: Path) -> None:
    completed, invocations = _run(tmp_path, key=None)

    assert completed.returncode == 2
    assert "OPENCODE_GO_API_KEY is required" in completed.stderr
    assert invocations == []


def test_wrapper_requires_the_opencode_key_only_for_opencode_go(
    tmp_path: Path,
) -> None:
    """The wrapper supplies the default provider, so that run needs the key.
    Another provider is Pi's to authenticate, and the wrapper still forwards
    both keys by name for docker to drop when they are unset."""

    other, other_calls = _run(
        tmp_path, "--provider", "opencode", "--model", "kimi-k3", key=None
    )
    slashed, slashed_calls = _run(
        tmp_path / "b", "--model", "opencode/glm-5.3", key=None
    )
    subcommand, subcommand_calls = _run(tmp_path / "c", "list", key=None)

    for completed in (other, slashed, subcommand):
        assert completed.returncode == 0
        assert "OPENCODE_GO_API_KEY is required" not in completed.stderr
    for calls in (other_calls, slashed_calls, subcommand_calls):
        argv = _docker_run(calls)
        assert argv[argv.index("--env") + 1] == "OPENCODE_GO_API_KEY"
        assert "OPENCODE_API_KEY" in argv


def test_wrapper_requires_the_key_for_an_opencode_go_model_or_provider(
    tmp_path: Path,
) -> None:
    model, model_calls = _run(
        tmp_path, "--model", "opencode-go/deepseek-v4.1-flash", key=None
    )
    flag, flag_calls = _run(
        tmp_path / "b", "--provider=opencode-go", "--model", "x", key=None
    )

    assert model.returncode == 2
    assert "OPENCODE_GO_API_KEY is required" in model.stderr
    assert model_calls == []
    assert flag.returncode == 2
    assert "OPENCODE_GO_API_KEY is required" in flag.stderr
    assert flag_calls == []


def test_wrapper_does_not_require_the_key_for_pi_help_or_version(
    tmp_path: Path,
) -> None:
    """Pi prints these and exits without a model call, so requiring the key
    would make the wrapper's own help and version unreachable without one.
    The default provider and model are still prepended, as for any prompt."""

    for index, flag in enumerate(("--help", "-h", "--version", "-v")):
        completed, invocations = _run(tmp_path / f"run-{index}", flag, key=None)

        assert completed.returncode == 0, flag
        assert "OPENCODE_GO_API_KEY is required" not in completed.stderr, flag
        argv = _docker_run(invocations)
        image = _image(argv)
        assert argv[argv.index(image) + 1 :] == [
            "--provider",
            "opencode-go",
            "--model",
            "deepseek-v4.1-flash",
            flag,
        ]


def test_wrapper_mounts_only_the_chosen_project_and_a_state_volume(
    tmp_path: Path,
) -> None:
    _, invocations = _run(tmp_path)
    argv = _docker_run(invocations)

    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]
    assert mounts[0] == f"type=bind,source={tmp_path / 'project'},target=/workspace"
    assert mounts[1].startswith("type=volume,source=pi-sandbox-project-")
    assert mounts[1].endswith(
        f",target=/home/agent,volume-label=pi-sandbox.project={tmp_path / 'project'}"
    )
    assert len(mounts) == 2
    assert "--volume" not in argv
    assert "-v" not in argv


def test_wrapper_warns_when_no_repository_names_the_mount(tmp_path: Path) -> None:
    """Outside a repository the launch directory itself is mounted, which may
    be far more than the user meant, so say which directory that is."""

    plain, _ = _run(tmp_path / "plain", cwd=tmp_path / "Downloads")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    inside, _ = _run(tmp_path / "inside", cwd=repo)

    assert plain.returncode == 0
    assert "no .git" in plain.stderr
    assert str(tmp_path / "Downloads") in plain.stderr
    assert "no .git" not in inside.stderr


def test_wrapper_mounts_the_git_root_when_run_from_a_subdirectory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path, cwd=repo / "pkg" / "deep")

    assert f"type=bind,source={repo},target=/workspace" in _docker_run(invocations)


def test_wrapper_mounts_the_launch_directory_not_a_planted_worktree(
    tmp_path: Path,
) -> None:
    """The repository is found from the nearest `.git`, not from git.
    `git rev-parse --show-toplevel` honours `core.worktree`, which lives in
    .git/config and so can be written by the container. It was reproduced:
    after the agent set `core.worktree = /etc`, the next launch bind-mounted
    the host's /etc at /workspace with no warning. An ancestor with no `.git`
    of its own is the same escape with a narrower target, and containment
    alone would not stop it."""

    home = tmp_path / "home"
    outer = tmp_path / "outer"
    for index, target in enumerate(("/etc", str(outer))):
        repo = outer / f"repo-{index}"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.worktree", target],
            check=True,
        )

        completed, invocations = _run(tmp_path / f"run-{index}", cwd=repo, home=home)
        argv = _docker_run(invocations)
        mounts = [
            argv[position + 1] for position, arg in enumerate(argv) if arg == "--mount"
        ]

        assert completed.returncode == 0, target
        assert f"type=bind,source={repo},target=/workspace" in mounts, target
        assert not any(f"source={target}," in mount for mount in mounts), target


def test_wrapper_ignores_a_dot_git_file_that_redirects_the_worktree(
    tmp_path: Path,
) -> None:
    """A `.git` file names a git directory the container can build inside the
    project, and `core.worktree` there makes git report a host directory. The
    launch directory's own `.git` names the repository instead."""

    project = tmp_path / "project"
    project.mkdir()
    gitdir = project / "built"
    subprocess.run(["git", "init", "--quiet", str(gitdir)], check=True)
    subprocess.run(
        ["git", "-C", str(gitdir), "config", "core.worktree", "/etc"],
        check=True,
    )
    (project / ".git").write_text(f"gitdir: {gitdir}/.git\n")

    completed, invocations = _run(tmp_path / "run", cwd=project)
    argv = _docker_run(invocations)
    mounts = [
        argv[position + 1] for position, arg in enumerate(argv) if arg == "--mount"
    ]

    assert completed.returncode == 0
    assert f"type=bind,source={project},target=/workspace" in mounts
    assert not any("source=/etc," in mount for mount in mounts)


def test_wrapper_mounts_git_config_and_hooks_read_only(tmp_path: Path) -> None:
    """A writable .git/config, config.worktree or hook is a command the host
    runs the next time the owner touches the checkout: `git status` and `git
    commit` honour core.fsmonitor, core.pager, core.hooksPath and
    filter.<name>.clean."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path, cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    assert mounts[0] == f"type=bind,source={repo},target=/workspace"
    assert mounts[1] == f"type=bind,source={repo}/.git,target=/workspace/.git"
    assert mounts[2] == (
        f"type=bind,source={repo}/.git/config,target=/workspace/.git/config,readonly"
    )
    assert mounts[3] == (
        f"type=bind,source={repo}/.git/config.worktree,"
        "target=/workspace/.git/config.worktree,readonly"
    )
    assert mounts[4] == (
        f"type=bind,source={repo}/.git/hooks,target=/workspace/.git/hooks,readonly"
    )
    assert mounts[5] == (
        f"type=bind,source={repo}/.git/worktrees,"
        "target=/workspace/.git/worktrees,readonly"
    )
    assert mounts[6] == (
        f"type=bind,source={repo}/.git/modules,target=/workspace/.git/modules,readonly"
    )
    assert mounts[7] == (
        f"type=bind,source={repo}/.git/commondir,"
        "target=/workspace/.git/commondir,readonly"
    )
    # A fresh repository has no modules, they are created empty, and the
    # state volume is still last.
    assert len(mounts) == 9
    assert mounts[8].startswith("type=volume,source=pi-sandbox-")
    assert mounts[8].endswith(
        f",target=/home/agent,volume-label=pi-sandbox.project={repo}"
    )


def test_wrapper_mounts_git_worktrees_read_only(tmp_path: Path) -> None:
    """A linked worktree keeps its git directory at .git/worktrees/<name>,
    where `commondir`, `gitdir` and `config.worktree` can redirect the host's
    Git at a config the agent wrote, so the whole directory is read-only."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path / "a", cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    worktrees = (
        f"type=bind,source={repo}/.git/worktrees,"
        "target=/workspace/.git/worktrees,readonly"
    )
    assert worktrees in mounts
    # Layered over .git and before the state volume, like the hooks.
    assert mounts.index(worktrees) > mounts.index(
        f"type=bind,source={repo}/.git,target=/workspace/.git"
    )
    assert mounts.index(worktrees) < mounts.index(
        next(mount for mount in mounts if mount.startswith("type=volume"))
    )


def test_wrapper_creates_a_missing_git_worktrees_directory(tmp_path: Path) -> None:
    """Skipping the mount because the directory is absent would leave the
    agent free to create it and point the host's Git at a config it wrote,
    the same as a missing hooks directory."""

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git/config").write_text("[core]\n\trepositoryformatversion = 0\n")

    _, invocations = _run(tmp_path, cwd=project)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    assert (project / ".git/worktrees").is_dir()
    assert (
        f"type=bind,source={project}/.git/worktrees,"
        "target=/workspace/.git/worktrees,readonly"
    ) in mounts


def test_wrapper_mounts_dot_git_itself_so_it_cannot_be_replaced(
    tmp_path: Path,
) -> None:
    """Read-only mounts on .git/config and .git/hooks do not stop `mv .git
    .git-old`: nothing is mounted at .git itself, so the rename succeeds and
    the agent builds a fresh .git with its own config. Mounting .git makes it
    a mount point, which cannot be renamed or removed."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path, cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    # After the workspace mount and before the read-only parts, so they layer
    # over it rather than the other way round.
    assert mounts[1] == f"type=bind,source={repo}/.git,target=/workspace/.git"
    assert "readonly" not in mounts[1]
    assert mounts[2].endswith(",target=/workspace/.git/config,readonly")
    assert mounts[3].endswith(",target=/workspace/.git/config.worktree,readonly")
    assert mounts[4].endswith(",target=/workspace/.git/hooks,readonly")


def test_wrapper_creates_a_missing_git_hooks_directory(tmp_path: Path) -> None:
    """Skipping the mount because the directory is absent would leave the
    agent free to create it and put a hook there for the host to run."""

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git/config").write_text("[core]\n\trepositoryformatversion = 0\n")

    _, invocations = _run(tmp_path, cwd=project)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    assert (project / ".git/hooks").is_dir()
    assert (
        f"type=bind,source={project}/.git/hooks,target=/workspace/.git/hooks,readonly"
    ) in mounts


def test_wrapper_mounts_git_modules_read_only(tmp_path: Path) -> None:
    """A `core.fsmonitor` or hook planted at .git/modules/<name>/config runs
    when the host's Git reaches that git directory through the submodule's
    `.git` file. The protected `.git` is pruned at exit, so nothing there is
    reported, and a missing directory can no longer be left for the agent to
    fill: it is created and mounted read-only like the hooks."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path / "a", cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    modules = (
        f"type=bind,source={repo}/.git/modules,target=/workspace/.git/modules,readonly"
    )
    assert (repo / ".git/modules").is_dir()
    assert modules in mounts
    # Layered over .git and under the state volume, like the other read-only
    # parts.
    assert mounts.index(modules) > mounts.index(
        f"type=bind,source={repo}/.git,target=/workspace/.git"
    )
    assert mounts.index(modules) < mounts.index(
        next(mount for mount in mounts if mount.startswith("type=volume"))
    )


def test_wrapper_mounts_config_worktree_read_only(tmp_path: Path) -> None:
    """`git sparse-checkout init --cone` sets extensions.worktreeConfig, and
    git then reads .git/config.worktree, which names a command through
    core.fsmonitor just as .git/config does. It is writable through the .git
    mount unless it is covered too, so it is created empty when missing and
    mounted read-only like the hooks, whether or not the extension is on. Git
    ignores the empty file while it is off, so the host sees no change."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path / "a", cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    worktree = (
        f"type=bind,source={repo}/.git/config.worktree,"
        "target=/workspace/.git/config.worktree,readonly"
    )
    assert worktree in mounts
    # Layered over .git like the other read-only parts.
    assert mounts.index(worktree) > mounts.index(
        f"type=bind,source={repo}/.git,target=/workspace/.git"
    )
    assert (repo / ".git/config.worktree").is_file()
    assert (repo / ".git/config.worktree").read_text() == ""


def test_wrapper_keeps_an_existing_config_worktree(tmp_path: Path) -> None:
    """`set -C` must create the file only when it is missing: truncating a
    config.worktree git really reads would drop the host's own settings."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "sparse-checkout", "init", "--cone"], cwd=repo, check=True)
    (repo / ".git/config.worktree").write_text("[core]\n\tsparseCheckout = true\n")

    _, invocations = _run(tmp_path / "a", cwd=repo)

    assert (
        f"type=bind,source={repo}/.git/config.worktree,"
        "target=/workspace/.git/config.worktree,readonly"
    ) in _docker_run(invocations)
    assert (repo / ".git/config.worktree").read_text() == (
        "[core]\n\tsparseCheckout = true\n"
    )


def test_wrapper_refuses_a_config_worktree_symlink(tmp_path: Path) -> None:
    """Git would follow the symlink and run what it names, and the mount would
    follow it too, so a link the agent left is refused rather than trusted."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    (repo / ".git/config.worktree").symlink_to(repo / "evil")

    completed, invocations = _run(tmp_path / "a", cwd=repo)

    assert completed.returncode == 2
    assert "symlink" in completed.stderr
    assert not any(argv[0] == "run" for argv in invocations)


def test_wrapper_refuses_a_non_regular_config_worktree(tmp_path: Path) -> None:
    """Creating the file with `: >` blocks on a FIFO, which dash opens and
    waits on for a reader, so a non-regular file there is refused before the
    write and before Docker mounts it as a file."""

    for index, make in enumerate((os.mkfifo, lambda path: path.mkdir())):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        make(repo / ".git/config.worktree")

        completed, invocations = _run(tmp_path / f"run-{index}", cwd=repo)

        assert completed.returncode == 2, index
        assert "not a regular file" in completed.stderr, index
        assert not any(argv[0] == "run" for argv in invocations), index


def test_wrapper_creates_commondir_as_dot_and_git_still_works(tmp_path: Path) -> None:
    """Git reads `commondir` in any git directory and takes the config and
    hooks from the directory it names, so a writable `.git/commondir` let the
    agent make the host's next `git status` run its command. The file is
    created holding `.`, which Git reads as this same directory, and mounted
    read-only. Host Git must behave as before."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path / "a", cwd=repo)

    assert (
        f"type=bind,source={repo}/.git/commondir,"
        "target=/workspace/.git/commondir,readonly"
    ) in _docker_run(invocations)
    assert (repo / ".git/commondir").read_text() == ".\n"
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr


def test_wrapper_refuses_a_planted_commondir(tmp_path: Path) -> None:
    """A `.git/commondir` naming another directory was planted, possibly by a
    session from before the file was protected, and host Git would run the
    commands in that directory's config. It is refused before anything runs,
    and the file is left for the user to inspect."""

    # The last three pass a check the shell can make on its own: Git uses the
    # whole file, and the shell drops the NUL that makes Git follow `.\n`.
    contents = (
        "/tmp/evil\n",
        "",
        "../other\n",
        ".\n\nevil",
        ".\n\n\nevil\n",
        ".\n\0",
    )
    for index, content in enumerate(contents):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        (repo / ".git/commondir").write_text(content)

        completed, invocations = _run(tmp_path / f"run-{index}", cwd=repo)

        assert completed.returncode == 2, content
        assert "commondir" in completed.stderr, content
        assert not any(argv[0] == "run" for argv in invocations), content
        assert (repo / ".git/commondir").read_text() == content


def test_wrapper_refuses_a_non_regular_commondir(tmp_path: Path) -> None:
    """Writing the file would block on a FIFO and Docker cannot mount a
    directory as a file, so either is refused before the write."""

    for index, make in enumerate((os.mkfifo, lambda path: path.mkdir())):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        make(repo / ".git/commondir")

        completed, invocations = _run(tmp_path / f"run-{index}", cwd=repo)

        assert completed.returncode == 2, index
        assert "not a regular file" in completed.stderr, index
        assert not any(argv[0] == "run" for argv in invocations), index


def test_wrapper_refuses_a_symlinked_git_path(tmp_path: Path) -> None:
    """Git, the `mkdir` below and the bind mounts all follow a symlink, so the
    agent can make the next launch create directories outside the project or
    mount a host directory read-write. The `.git` case was reproduced: the
    target's id_rsa was readable from the second session. Every path is
    refused before anything is created or run."""

    for index, relative in enumerate(
        (
            ".git",
            ".git/config",
            ".git/hooks",
            ".git/modules",
            ".git/worktrees",
            ".git/config.worktree",
            ".git/commondir",
        )
    ):
        project = tmp_path / f"project-{index}"
        project.mkdir()
        if relative != ".git":
            (project / ".git").mkdir()
        target = tmp_path / f"target-{index}"
        target.mkdir()
        (target / "keep").write_text("unchanged")
        (project / relative).symlink_to(target)

        completed, invocations = _run(tmp_path / f"run-{index}", cwd=project)

        assert completed.returncode == 2, relative
        assert "symlink" in completed.stderr, relative
        assert str(project / relative) in completed.stderr, relative
        assert not any(argv[0] == "run" for argv in invocations), relative
        # Nothing was created through the link.
        assert sorted(path.name for path in target.iterdir()) == ["keep"], relative


def test_wrapper_refuses_a_second_path_to_the_protected_git_files(
    tmp_path: Path,
) -> None:
    """A read-only mount protects a path, not the file behind it. In a
    session where `.git` was not protected, the agent can make a hook or
    config a hard link or a symlink to a project file, and later rewrite the
    hook through that file while every read-only mount is in place."""

    for index, relative in enumerate(
        (
            ".git/config",
            ".git/config.worktree",
            ".git/commondir",
            ".git/hooks/post-checkout",
            ".git/modules/sub/config",
            ".git/modules/sub/hooks/pre-commit",
            ".git/worktrees/wt/commondir",
        )
    ):
        for kind in ("hard", "symbolic"):
            repo = tmp_path / f"repo-{index}-{kind}"
            repo.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            (repo / "x").write_text(".\n")
            if kind == "hard":
                os.link(repo / "x", path)
            else:
                path.symlink_to(repo / "x")

            completed, invocations = _run(tmp_path / f"run-{index}-{kind}", cwd=repo)

            assert completed.returncode == 2, (relative, kind)
            assert "link" in completed.stderr, (relative, kind)
            assert str(path) in completed.stderr, (relative, kind)
            assert not any(argv[0] == "run" for argv in invocations), (relative, kind)


def test_wrapper_refuses_a_symlinked_directory_under_the_git_paths(
    tmp_path: Path,
) -> None:
    """A submodule's git directory, or its hooks, linked to a project
    directory gives the agent a config and hooks the host's Git reads through
    the submodule, writable in every later session. Its name is not one Git
    reads a command from, so every symlink counts, not only those names."""

    for index, relative in enumerate(
        (".git/modules/x", ".git/modules/y/hooks", ".git/worktrees/wt")
    ):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        (repo / "evil").mkdir()
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(repo / "evil")

        completed, invocations = _run(tmp_path / f"run-{index}", cwd=repo)

        assert completed.returncode == 2, relative
        assert str(path) in completed.stderr, relative
        assert not any(argv[0] == "run" for argv in invocations), relative


def test_wrapper_prints_no_control_bytes_from_a_refused_name(
    tmp_path: Path,
) -> None:
    """The agent names the file, so an escape sequence in its name would
    reach the terminal from the wrapper itself and could hide the refusal."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    (repo / "x").write_text("#!/bin/sh\n")
    os.link(repo / "x", repo / ".git/hooks/a\x1b]0;title\x07\x1b[2Kb")

    completed, _ = _run(tmp_path / "run", cwd=repo)

    assert completed.returncode == 2
    assert "hard link" in completed.stderr
    assert "\x1b" not in completed.stderr
    assert "\x07" not in completed.stderr


def test_wrapper_says_when_it_could_not_check_for_links(tmp_path: Path) -> None:
    """A directory the check cannot read is refused, and said to be
    unreadable rather than reported as a link that is not there."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    locked = repo / ".git/modules/sub"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        completed, invocations = _run(tmp_path / "run", cwd=repo)
    finally:
        locked.chmod(0o755)

    assert completed.returncode == 2
    assert "could not check" in completed.stderr
    assert "hard link" not in completed.stderr
    assert not any(argv[0] == "run" for argv in invocations)


def test_wrapper_runs_a_repository_whose_objects_are_hard_links(
    tmp_path: Path,
) -> None:
    """`git clone` of a local path hard-links the objects, a submodule's under
    .git/modules too. They name no command, so they do not count, also where
    a directory above the project is called `hooks`."""

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    git += ["-c", "protocol.file.allow=always"]
    sub = tmp_path / "sub"
    sub.mkdir()
    subprocess.run(["git", "init", "--quiet", str(sub)], check=True)
    (sub / "file").write_text("x")
    subprocess.run([*git, "-C", str(sub), "add", "file"], check=True)
    subprocess.run([*git, "-C", str(sub), "commit", "-qm", "x"], check=True)
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(
        [*git, "-C", str(source), "submodule", "add", "-q", str(sub), "sub"],
        check=True,
    )
    subprocess.run([*git, "-C", str(source), "commit", "-qm", "x"], check=True)
    clone = tmp_path / "hooks" / "clone"
    subprocess.run(
        [*git, "clone", "-q", "--recurse-submodules", str(source), str(clone)],
        check=True,
    )
    for objects in (clone / ".git/objects", clone / ".git/modules/sub/objects"):
        assert any(
            path.stat().st_nlink > 1 for path in objects.rglob("*") if path.is_file()
        ), objects

    completed, invocations = _run(tmp_path / "run", cwd=clone)

    assert completed.returncode == 0, completed.stderr
    assert _docker_run(invocations)


def test_wrapper_still_runs_a_repository_without_symlinked_git_paths(
    tmp_path: Path,
) -> None:
    """The refusal is only for symlinks: an ordinary repository still runs."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, invocations = _run(tmp_path / "run", cwd=repo)

    assert completed.returncode == 0
    assert _docker_run(invocations)


def test_wrapper_leaves_a_git_file_and_a_plain_directory_alone(
    tmp_path: Path,
) -> None:
    """A linked worktree or a submodule checkout has .git as a file pointing
    outside the mount, so there is nothing inside it to protect, and a
    non-repository gets no extra mounts at all."""

    linked = tmp_path / "linked"
    plain = tmp_path / "plain"
    linked.mkdir()
    plain.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n")

    _, linked_calls = _run(tmp_path / "a", cwd=linked)
    _, plain_calls = _run(tmp_path / "b", cwd=plain)

    for invocations in (linked_calls, plain_calls):
        argv = _docker_run(invocations)
        mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]
        assert len(mounts) == 2


def test_wrapper_keeps_a_project_path_with_a_space_in_one_mount(
    tmp_path: Path,
) -> None:
    """An unquoted mount variable split `/Users/x/my project` into broken
    --mount arguments, so the tail of the command is built with `set --`."""

    repo = tmp_path / "my project"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path, cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    assert f"type=bind,source={repo},target=/workspace" in mounts
    assert f"type=bind,source={repo}/.git,target=/workspace/.git" in mounts
    assert (
        f"type=bind,source={repo}/.git/config,target=/workspace/.git/config,readonly"
    ) in mounts
    assert (
        f"type=bind,source={repo}/.git/config.worktree,"
        "target=/workspace/.git/config.worktree,readonly"
    ) in mounts
    assert (
        f"type=bind,source={repo}/.git/worktrees,"
        "target=/workspace/.git/worktrees,readonly"
    ) in mounts
    assert (
        f"type=bind,source={repo}/.git/modules,target=/workspace/.git/modules,readonly"
    ) in mounts
    assert len(mounts) == 9
    assert all("target=" in mount for mount in mounts)


def test_wrapper_refuses_a_project_path_with_a_comma(tmp_path: Path) -> None:
    """Docker parses --mount as comma-separated fields, so a comma in the path
    splits the source. That breaks the original mounts as much as the git
    ones, so the path is refused rather than mounted wrongly."""

    completed, invocations = _run(tmp_path, cwd=tmp_path / "a,b")

    assert completed.returncode == 2
    assert "comma" in completed.stderr
    assert invocations == []


def test_wrapper_warns_about_nested_git_metadata_the_container_left(
    tmp_path: Path,
) -> None:
    """A nested `git init sub` cannot be stopped by a mount, and on Docker
    Desktop the host user owns what the agent writes, so host git trusts a
    config or hook there. Detect it at exit instead of preventing it, and
    print each repository once: its .git, not its config and every hook."""

    completed, _ = _run(tmp_path, agent=NESTED_GIT)

    assert completed.returncode == 0
    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1] == str(tmp_path / "project/sub/.git")
    assert lines[start + 2].startswith("your host git trusts it")
    assert "sub/.git/config" not in completed.stderr
    assert "sub/.git/hooks" not in completed.stderr


def test_wrapper_warns_about_a_root_git_the_container_created(
    tmp_path: Path,
) -> None:
    """In a project that is not a repository at launch nothing is mounted over
    `.git`, so the container can `git init .` and leave a root `.git` whose
    config or hooks the host would trust. Pruning the root `.git`
    unconditionally, as when it was protected, hid it. It is reported like a
    nested one instead."""

    completed, _ = _run(
        tmp_path,
        agent="git init -q . && git config core.fsmonitor true",
    )

    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1] == str(tmp_path / "project/.git")
    assert lines[start + 2].startswith("your host git trusts it")


def test_wrapper_does_not_report_the_protected_root_git(tmp_path: Path) -> None:
    """The repository's own `.git` is mounted over at launch, so its ctime
    moving when the agent commits is expected and must not be reported as
    nested metadata the container left."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(tmp_path / "run", cwd=repo, agent="touch .git")

    assert completed.returncode == 0
    assert "nested git metadata" not in completed.stderr


def test_wrapper_prints_one_line_per_nested_repository(tmp_path: Path) -> None:
    """Deduplicating by `.git` path keeps two repositories to two lines even
    though find reports each one's config and hooks too."""

    completed, _ = _run(
        tmp_path,
        agent=(
            "mkdir -p one/.git/hooks two/.git/hooks && "
            "touch one/.git/config two/.git/config "
            "one/.git/hooks/pre-commit.sample two/.git/hooks/pre-commit.sample"
        ),
    )

    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1 : start + 3] == [
        str(tmp_path / "project/one/.git"),
        str(tmp_path / "project/two/.git"),
    ]
    assert lines[start + 3].startswith("your host git trusts it")


def test_wrapper_stays_quiet_when_the_container_leaves_no_nested_git(
    tmp_path: Path,
) -> None:
    completed, _ = _run(tmp_path)

    assert completed.returncode == 0
    assert "nested git metadata" not in completed.stderr


def test_wrapper_does_not_call_the_root_git_nested_when_the_path_has_globs(
    tmp_path: Path,
) -> None:
    """`-path` takes a glob, so an unescaped project path with `[x]`, `*` or
    `?` fails to prune the protected root `.git`, whose ctime changes on every
    commit, and reports it as a nested repository the container left."""

    for index, name in enumerate(("a[x]b", "a*b", "a?b")):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        # The root .git is a genuine git directory, not one the agent made.
        completed, _ = _run(tmp_path / f"clean-{index}", cwd=repo, agent="touch .git")

        assert completed.returncode == 0
        assert "nested git metadata" not in completed.stderr


def test_wrapper_still_finds_nested_git_when_the_path_has_a_glob(
    tmp_path: Path,
) -> None:
    """The escape is not only about false positives: an unescaped `*` matches
    across the separator, so `-path` prunes a real nested `.git` and a
    repository the agent created goes unreported."""

    repo = tmp_path / "a*b"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(tmp_path / "run", cwd=repo, agent=NESTED_GIT + " && touch .git")

    assert completed.returncode == 0
    assert f"{repo}/sub/.git" in completed.stderr
    assert "nested git metadata" in completed.stderr


def test_wrapper_reports_when_find_cannot_check_for_nested_git(
    tmp_path: Path,
) -> None:
    """The check must not fail open: BusyBox find has no -cnewer and prints
    its usage, which must not reach the terminal in place of a warning."""

    completed, _ = _run(
        tmp_path,
        run_status="7",
        fake_find=(
            "#!/bin/sh\n"
            "printf '%s\\n' 'find: unrecognized: -cnewer' >&2\n"
            "printf '%s\\n' 'Usage: find [-HL] [PATH]...' >&2\n"
            "exit 1\n"
        ),
    )

    assert "nested-git check could not run" in completed.stderr
    assert "Usage: find" not in completed.stderr
    assert "unrecognized" not in completed.stderr
    # The check is warn only, so docker's status still reaches the caller.
    assert completed.returncode == 7


def test_wrapper_warns_about_a_bare_repository_the_container_left(
    tmp_path: Path,
) -> None:
    """Host git takes any directory holding HEAD, objects and refs for a
    repository, with no `.git` in sight, and trusts the config there. A new
    HEAD outside the protected root `.git` is reported by the directory it is
    in."""

    completed, _ = _run(
        tmp_path,
        agent=(
            "mkdir -p src/objects src/refs && "
            "printf 'ref: refs/heads/main\\n' > src/HEAD && "
            "printf '[core]\\n\\tfsmonitor = true\\n' > src/config"
        ),
    )

    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1] == str(tmp_path / "project/src")
    assert lines[start + 2].startswith("your host git trusts it")


def test_wrapper_does_not_report_a_directory_named_head(tmp_path: Path) -> None:
    """Git only takes a file or a symlink for HEAD, and a `components/Head`
    directory is ordinary code. Reporting it would teach the reader to skip
    the warning."""

    completed, _ = _run(tmp_path, agent="mkdir -p components/Head src/head")

    assert "nested git metadata" not in completed.stderr


def test_wrapper_warns_when_the_project_itself_becomes_a_bare_repository(
    tmp_path: Path,
) -> None:
    """With no `.git` at launch nothing protects the root, and host git takes
    the project itself for a bare repository once it holds HEAD, objects and
    refs. Its HEAD is named by the project path."""

    completed, _ = _run(tmp_path, agent="mkdir objects refs && touch HEAD")

    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1] == str(tmp_path / "project")


def test_wrapper_warns_about_git_metadata_named_in_another_case(
    tmp_path: Path,
) -> None:
    """macOS file systems ignore case by default, so host git there opens
    `SUB/.GIT` for `.git` and takes a bare repository's `head` for `HEAD`. The
    report matches the names in any case."""

    completed, _ = _run(
        tmp_path,
        agent=(
            "mkdir -p SUB/.GIT src/objects src/refs && touch SUB/.GIT/config && "
            "printf 'ref: refs/heads/main\\n' > src/head"
        ),
    )

    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1 : start + 3] == [
        str(tmp_path / "project/SUB/.GIT"),
        str(tmp_path / "project/src"),
    ]


def test_wrapper_scans_a_project_reached_through_a_symlink(
    tmp_path: Path,
) -> None:
    """The project path keeps a symlink it was reached through, and find does
    not follow one named on its command line, so it scanned nothing and the
    repository the agent planted went unreported."""

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    # A shell launched in the link keeps it in PWD, which `pwd` then prints.
    completed, _ = _run(
        tmp_path / "run", cwd=link, agent=NESTED_GIT, also={"PWD": str(link)}
    )

    assert f"{link}/sub/.git" in completed.stderr


def test_wrapper_reports_a_git_beside_the_root_under_another_case(
    tmp_path: Path,
) -> None:
    """On a case-sensitive file system `.GIT` is not the protected `.git`, so
    pruning the root in any case hid a repository the agent built under it."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    if (repo / ".GIT").exists():
        pytest.skip("needs a case-sensitive file system")

    completed, _ = _run(
        tmp_path / "run",
        cwd=repo,
        agent="mkdir -p .GIT/sub/.git && touch .GIT/sub/.git/config",
    )

    assert str(repo / ".GIT/sub/.git") in completed.stderr


def test_wrapper_prints_the_project_path_as_it_is(tmp_path: Path) -> None:
    """Only the part of a path the agent chose is escaped. The project path is
    the user's own, so a letter outside ASCII or a `.git` in it prints as it
    is and the warning still names the repository inside the project."""

    repo = tmp_path / "Časopis/.git/proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(
        tmp_path / "run",
        cwd=repo,
        agent="mkdir -p src/objects src/refs && touch src/HEAD",
    )

    assert f"{repo}/src\n" in completed.stderr


def test_wrapper_warns_about_an_edit_to_a_nested_commondir_or_worktree_config(
    tmp_path: Path,
) -> None:
    """Editing a file in place leaves its directory's ctime alone, and host
    git follows a nested repository's commondir and reads its config.worktree
    as it reads its config."""

    project = tmp_path / "project"
    (project / "a/.git").mkdir(parents=True)
    (project / "b/.git").mkdir(parents=True)
    (project / "a/.git/commondir").write_text(".\n")
    (project / "b/.git/config.worktree").write_text("")

    completed, _ = _run(
        tmp_path,
        agent=(
            "printf '../../evil\\n' > a/.git/commondir && "
            "printf '[core]\\n\\tfsmonitor = true\\n' > b/.git/config.worktree"
        ),
    )

    assert str(project / "a/.git") in completed.stderr
    assert str(project / "b/.git") in completed.stderr


def test_wrapper_keeps_what_find_found_when_a_directory_is_unreadable(
    tmp_path: Path,
) -> None:
    """One directory the agent made unreadable makes find fail. The scan of
    everything else still ran, and dropping its result hid the repository
    planted next to it behind a warning that read as noise."""

    if os.geteuid() == 0:
        pytest.skip("root reads a mode 000 directory")

    completed, _ = _run(
        tmp_path, agent=NESTED_GIT + " && mkdir locked && chmod 000 locked"
    )
    (tmp_path / "project/locked").chmod(0o755)

    lines = completed.stderr.splitlines()
    start = lines.index(
        "pi-sandbox: warning: the container left nested git metadata in the project:"
    )
    assert lines[start + 1] == str(tmp_path / "project/sub/.git")
    assert "nested-git check could not run" in completed.stderr


def test_wrapper_notes_a_directory_it_can_list_but_not_enter(
    tmp_path: Path,
) -> None:
    """A mode 000 directory the agent makes on Docker Desktop reaches the host
    as 0600. macOS find lists it, skips what is inside without an error, and
    the repository planted there went unreported with no note at all. GNU
    find already fails there, so only macOS shows the fix."""

    if os.geteuid() == 0:
        pytest.skip("root enters a mode 0600 directory")

    completed, _ = _run(
        tmp_path, agent=NESTED_GIT + " && mkdir hide && mv sub hide && chmod 600 hide"
    )
    (tmp_path / "project/hide").chmod(0o755)

    assert "nested-git check could not run" in completed.stderr


def test_wrapper_prints_no_control_bytes_from_a_name_the_agent_chose(
    tmp_path: Path,
) -> None:
    """The agent names the paths the exit warning prints, and an escape
    sequence in a name could erase the warning or rewrite it once it reaches
    the terminal. The wrapper prints a `?` for every byte that is not
    printable ASCII instead."""

    completed, _ = _run(
        tmp_path,
        agent=(
            "d=$(printf 'sub\\033[1A\\033[2K') && mkdir -p \"$d/.git\" && "
            'touch "$d/.git/config"'
        ),
    )

    assert "\x1b" not in completed.stderr
    assert str(tmp_path / "project/sub?[1A?[2K/.git") in completed.stderr


def test_wrapper_prints_a_newline_in_a_name_as_a_question_mark(
    tmp_path: Path,
) -> None:
    """The report is one path per line, so a newline in a name the agent
    chose would add a line of its choosing under the warning, one that reads
    as the wrapper's own."""

    completed, _ = _run(
        tmp_path,
        agent=(
            "d=$(printf 'x\\npi-sandbox: this is safe to ignore\\ny') && "
            'mkdir -p "$d/.git" && touch "$d/.git/config"'
        ),
    )

    assert "pi-sandbox: this is safe to ignore" not in completed.stderr.splitlines()
    assert (
        str(tmp_path / "project/x?pi-sandbox: this is safe to ignore?y/.git")
        in completed.stderr
    )


def test_wrapper_reports_many_planted_files_quickly(tmp_path: Path) -> None:
    """Escaping each name in a process of its own let the agent stall the exit
    for as long as it liked with empty files, and a user who closed the pane
    thinking it hung lost the warning."""

    started = time.monotonic()
    completed, _ = _run(
        tmp_path,
        agent="mkdir -p s/.git/hooks && cd s/.git/hooks && seq 20000 | xargs touch",
    )

    assert str(tmp_path / "project/s/.git") in completed.stderr
    assert time.monotonic() - started < 10


def test_wrapper_resets_the_terminal_before_its_exit_warnings(
    tmp_path: Path,
) -> None:
    """The agent can leave the terminal drawing black on black, concealed, or
    with the default colours swapped, and kill Pi so nothing undoes it. The
    exit warnings would then be invisible, so the wrapper resets the terminal
    (DECSTR, and the palette and default colours) once docker returns and
    before it prints anything."""

    hide = "\\033[30;40m\\033[8m\\033]10;#010203\\007\\033]11;#010203\\007"
    completed, _ = _run(
        tmp_path,
        agent=f"{NESTED_GIT} && printf '{hide}'",
        terminal=True,
    )

    output = completed.stdout
    hidden = output.index("\x1b]11;#010203\x07")
    warning = output.index("pi-sandbox: warning: the container left", hidden)
    between = output[hidden:warning]
    for reset in ("\x1b[!p\x1b[m", "\x1b]104\x07", "\x1b]110\x07", "\x1b]111\x07"):
        assert reset in between, reset


def test_wrapper_leaves_the_terminal_alone_without_a_warning(
    tmp_path: Path,
) -> None:
    """The reset also undoes colours the user set on purpose, such as a theme
    a shell sets at startup, so a session that leaves nothing to warn about
    must not send it."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(tmp_path / "run", cwd=repo, terminal=True)

    assert "pi-sandbox: warning" not in completed.stdout
    assert "\x1b[!p" not in completed.stdout
    assert "\x1b]110" not in completed.stdout


def test_wrapper_ends_without_a_job_notice_for_its_watcher(tmp_path: Path) -> None:
    """The wrapper SIGKILLs its watcher once docker returns, and bash, which
    is macOS /bin/sh, then printed `Killed: 9` and the watcher's source on
    every exit, which teaches the reader to skip what the wrapper prints
    there."""

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash, which prints the notice")

    completed, _ = _run(tmp_path, interpreter=bash)

    assert "Killed" not in completed.stderr


def test_wrapper_keeps_docker_status_through_the_nested_git_check(
    tmp_path: Path,
) -> None:
    """`set -e` would otherwise end the script on docker's non-zero status
    before the check runs and change the status the caller sees."""

    warning, _ = _run(tmp_path, agent=NESTED_GIT, run_status="7")
    quiet, _ = _run(tmp_path / "b", run_status="7")

    assert warning.returncode == 7
    assert "nested git metadata" in warning.stderr
    assert quiet.returncode == 7
    assert "nested git metadata" not in quiet.stderr


def test_wrapper_warns_about_a_git_operation_left_in_progress(
    tmp_path: Path,
) -> None:
    """A planted rebase makes the host's git report one in progress, and
    `git rebase --continue` then runs the `exec` lines its todo holds. An
    empty rebase-merge directory cannot be mounted over, because git would
    read the empty directory as a rebase in progress, so the wrapper names it
    at exit instead."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(tmp_path / "run", cwd=repo, agent=PLANTED_REBASE)

    assert completed.returncode == 0
    assert "git operation in progress" in completed.stderr
    assert str(repo / ".git/rebase-merge") in completed.stderr
    assert "abort" in completed.stderr


def test_wrapper_warns_about_a_planted_merge_or_cherry_pick(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(
        tmp_path / "run",
        cwd=repo,
        agent="touch .git/MERGE_HEAD .git/CHERRY_PICK_HEAD",
    )

    assert completed.returncode == 0
    assert str(repo / ".git/MERGE_HEAD") in completed.stderr
    assert str(repo / ".git/CHERRY_PICK_HEAD") in completed.stderr


def test_wrapper_stays_quiet_about_a_clean_git_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    completed, _ = _run(tmp_path / "run", cwd=repo)

    assert completed.returncode == 0
    assert "git operation in progress" not in completed.stderr


def test_wrapper_gives_each_project_its_own_state_volume(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    _, first = _run(tmp_path / "a", cwd=one)
    _, second = _run(tmp_path / "b", cwd=two)

    def volume(invocations: list[list[str]]) -> str:
        argv = _docker_run(invocations)
        return next(arg for arg in argv if arg.startswith("type=volume"))

    assert volume(first) != volume(second)


def test_wrapper_refuses_to_mount_the_home_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "work" / "repo").mkdir(parents=True)

    at_home, home_calls = _run(tmp_path, cwd=home, home=home)
    above_home, above_calls = _run(tmp_path / "b", cwd=tmp_path, home=home)
    inside, inside_calls = _run(tmp_path / "c", cwd=home / "work" / "repo", home=home)

    assert at_home.returncode == 2
    assert "home directory" in at_home.stderr
    assert home_calls == []
    assert above_home.returncode == 2
    assert above_calls == []
    assert inside.returncode == 0
    assert _docker_run(inside_calls)


def test_wrapper_refuses_a_project_that_contains_it(tmp_path: Path) -> None:
    """The wrapper runs on the host for every project, and Dockerfile.pi and
    entrypoint.sh build the image every project runs in. Launched on its own
    checkout, the project mount made all three writable by the agent, so its
    edit ran on the host at the next launch anywhere. Paths are compared
    physically, so reaching either one through a symlink does not dodge it."""

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "sub").mkdir()
    wrapper = _wrapper_with_inputs(checkout)
    nested = _wrapper_with_inputs(checkout / "tools")
    (tmp_path / "link").symlink_to(checkout)

    launches = {
        "root": _run(tmp_path / "a", cwd=checkout, wrapper=wrapper),
        "subdirectory": _run(tmp_path / "b", cwd=checkout / "sub", wrapper=wrapper),
        "wrapper below the project": _run(tmp_path / "e", cwd=checkout, wrapper=nested),
        "wrapper by symlink": _run(
            tmp_path / "c", cwd=checkout, wrapper=tmp_path / "link" / "pi"
        ),
        "project by symlink": _run(
            tmp_path / "d",
            cwd=tmp_path / "link",
            wrapper=wrapper,
            also={"PWD": str(tmp_path / "link")},
        ),
    }

    for name, (completed, invocations) in launches.items():
        assert completed.returncode == 2, name
        assert "it contains this wrapper" in completed.stderr, name
        assert invocations == [], name


def test_wrapper_forwards_the_key_by_name_and_never_its_value(
    tmp_path: Path,
) -> None:
    completed, invocations = _run(tmp_path)
    argv = _docker_run(invocations)
    output = completed.stdout + completed.stderr

    assert argv[argv.index("--env") + 1] == "OPENCODE_GO_API_KEY"
    assert FAKE_KEY not in " ".join(argv)
    assert FAKE_KEY not in output
    assert "--env-file" not in argv
    # Nothing beyond the two keys, TERM and COLORTERM is forwarded by default,
    # and an unset name is dropped by docker rather than passed empty.
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--env"] == [
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "TERM=xterm-256color",
        "COLORTERM=truecolor",
    ]


def test_wrapper_forwards_extra_variables_only_when_named(tmp_path: Path) -> None:
    _, invocations = _run(tmp_path, forward="TYPESAFE_API_KEY")
    argv = _docker_run(invocations)

    assert argv[argv.index("TYPESAFE_API_KEY") - 1] == "--env"
    assert "typesafe-test-key-do-not-leak" not in " ".join(argv)


def test_wrapper_rejects_a_bogus_variable_name(tmp_path: Path) -> None:
    sneaky, calls = _run(tmp_path, forward="--privileged")
    spaced, spaced_calls = _run(tmp_path / "b", forward="BAD-NAME")

    assert sneaky.returncode == 2
    assert "invalid variable name" in sneaky.stderr
    assert calls == []
    assert spaced.returncode == 2
    assert spaced_calls == []


def test_wrapper_does_not_glob_the_variable_names_it_forwards(
    tmp_path: Path,
) -> None:
    """The names are word-split from PI_SANDBOX_ENV, and an unquoted expansion
    is also pathname-expanded, so a value like `*` was replaced by the
    project's filenames. A file named after a secret then forwarded that host
    variable, which the README says cannot happen: nothing is forwarded unless
    named."""

    project = tmp_path / "project"
    project.mkdir()
    (project / "LEAKED_SECRET").write_text("")

    completed, invocations = _run(
        tmp_path,
        cwd=project,
        forward="*",
        also={"LEAKED_SECRET": "leaked-value"},
    )

    assert completed.returncode == 2
    assert "invalid variable name" in completed.stderr
    assert invocations == []


def test_wrapper_keeps_the_container_unprivileged(tmp_path: Path) -> None:
    """Asserted rather than assumed: dropping one of these costs nothing that
    the rest of the suite would notice."""

    _, invocations = _run(tmp_path)
    argv = _docker_run(invocations)

    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    # A runaway subagent fan-out must hit this before it takes the host.
    assert argv[argv.index("--memory") + 1] == "8g"
    # Without it dockerd copies the terminal stream into a log with no size
    # limit, which the agent can fill the disk through.
    assert argv[argv.index("--log-driver") + 1] == "none"


@pytest.mark.parametrize(
    ("ncpu", "cpus"), [("8", "4"), ("3", "1"), ("1", "1"), ("", "1")]
)
def test_wrapper_lets_one_session_use_half_the_cpus(
    tmp_path: Path, ncpu: str, cpus: str
) -> None:
    """A few spinning processes stay far under the pids and memory limits and
    would hold every core the daemon has. Docker refuses `--cpus` above its own
    count, so the bound comes from the daemon, and a one-CPU machine or a
    failed `docker info` still gets a bound that starts."""

    _, invocations = _run(tmp_path, also={"PI_SANDBOX_FAKE_NCPU": ncpu})
    argv = _docker_run(invocations)

    assert argv[argv.index("--cpus") + 1] == cpus


def _container_name(invocations: list[list[str]]) -> str:
    run = _docker_run(invocations)
    return run[run.index("--name") + 1]


def _removals(invocations: list[list[str]]) -> list[str]:
    return [argv[2] for argv in invocations if argv[:2] == ["rm", "-f"] and argv[2:]]


def test_wrapper_removes_the_container_when_the_docker_client_dies(
    tmp_path: Path,
) -> None:
    """`--rm` only fires when the container stops. A docker client killed
    under a running session leaves the container running with nobody attached,
    still able to write the project after the exit checks have looked."""

    _, invocations = _run(tmp_path, run_status="137")
    commands = [argv[0] for argv in invocations]

    # Removed before the nested-git check reads the project, not after.
    after_run = invocations[commands.index("run") + 1]
    assert after_run == ["rm", "-f", _container_name(invocations)]


@pytest.mark.parametrize("signal_name", ["KILL", "TERM", "HUP"])
def test_wrapper_removes_the_container_when_it_is_killed(
    tmp_path: Path, signal_name: str
) -> None:
    """A closed Herdr pane hangs the wrapper up, and a SIGKILL cannot be
    trapped, so neither may leave the container behind. The agent here kills
    the wrapper while `docker run` is still in progress."""

    killed, _ = _run(tmp_path, agent=f'kill -{signal_name} "$(ps -o ppid= -p "$PPID")"')
    # Otherwise the wrapper's own removal after `docker run` would pass this.
    assert killed.returncode == -getattr(signal, f"SIG{signal_name}")
    log = tmp_path / "docker.log"
    for _ in range(100):
        if _removals(_invocations(log)):
            break
        time.sleep(0.1)
    invocations = _invocations(log)

    assert set(_removals(invocations)) == {_container_name(invocations)}


# A pid no system hands out, for a wrapper that is certainly gone.
GONE_PID = 99999999


def _origin(tmp_path: Path) -> str:
    """The hash of where the wrapper runs, from the name it gives a
    container: pi-sandbox-<project>-<origin>-<pid>."""

    _, invocations = _run(tmp_path / "origin")
    return _container_name(invocations).split("-")[3]


def test_wrapper_removes_containers_whose_wrapper_is_gone(tmp_path: Path) -> None:
    """A SIGKILL to the whole process group takes the watcher with the
    wrapper, so the container outlives both. The next launch removes it, read
    from the wrapper pid in its name. A container whose wrapper still runs is
    a live session. So is one started elsewhere, by another user, a WSL
    distro, a devcontainer or a second machine on the same daemon, whose pid
    this host's `ps` cannot see, and anything not named the way the wrapper
    names."""

    origin = _origin(tmp_path)
    alive = os.getpid()
    _, invocations = _run(
        tmp_path,
        containers=(
            f"pi-sandbox-0a1b2c3d-{origin}-{GONE_PID}\n"
            f"pi-sandbox-0a1b2c3d-{origin}-{alive}\n"
            f"pi-sandbox-0a1b2c3d-ffffff-{GONE_PID}\n"
            f"pi-sandbox-0a1b2c3d-{origin}-notapid\n"
            "festive_turing\n"
        ),
    )
    removed = _removals(invocations)
    commands = [argv[0] for argv in invocations]

    assert f"pi-sandbox-0a1b2c3d-{origin}-{GONE_PID}" in removed
    assert f"pi-sandbox-0a1b2c3d-{origin}-{alive}" not in removed
    assert f"pi-sandbox-0a1b2c3d-ffffff-{GONE_PID}" not in removed
    assert f"pi-sandbox-0a1b2c3d-{origin}-notapid" not in removed
    assert "festive_turing" not in removed
    # Before this launch's own container exists.
    assert commands.index("rm") < commands.index("run")


def test_wrapper_names_another_user_apart(tmp_path: Path) -> None:
    """Two users on one host share the daemon and can each be hidden from the
    other's `ps`, so the user is part of where a wrapper runs."""

    def origin(uid: str) -> str:
        _, invocations = _run(tmp_path / uid, fake_id=(uid, "1000"))
        return _container_name(invocations).split("-")[3]

    assert origin("1000") != origin("1001")


def test_wrapper_clears_its_own_name_before_running(tmp_path: Path) -> None:
    """A container a dead session left under a pid this launch now has would
    make `docker run --name` fail. Only this origin's sessions use the name,
    and only this launch can hold it now."""

    _, invocations = _run(tmp_path)
    name = _container_name(invocations)
    commands = [argv[0] for argv in invocations]

    assert name in _removals(invocations)
    assert invocations.index(["rm", "-f", name]) < commands.index("run")


def test_wrapper_does_not_sweep_when_ps_cannot_see_processes(
    tmp_path: Path,
) -> None:
    """A `ps` that is missing, or cannot see processes at all, would make
    every wrapper look gone and remove every live session."""

    origin = _origin(tmp_path)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "ps").write_text("#!/bin/sh\nexit 1\n")
    (fake / "ps").chmod(0o755)
    stale = f"pi-sandbox-0a1b2c3d-{origin}-{GONE_PID}"
    _, invocations = _run(tmp_path, containers=f"{stale}\n")

    assert stale not in _removals(invocations)


def _prefix(tmp_path: Path, project: Path) -> str:
    """The start of every container name this project gets from here:
    pi-sandbox-<project>-<origin>-."""

    _, invocations = _run(tmp_path / "prefix", cwd=project)
    return _container_name(invocations).rsplit("-", 1)[0] + "-"


def test_wrapper_checks_the_project_after_a_dead_sessions_container_is_gone(
    tmp_path: Path,
) -> None:
    """A container whose wrapper was SIGKILLed with its watcher still runs,
    and its agent can turn `.git` into a symlink at any moment. Checked before
    that container was removed, a plain project passed, then the symlink made
    the wrapper create directories through it and mount a host directory. The
    checks must see a project no container can still change."""

    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("unchanged")
    stale = _prefix(tmp_path, project) + str(GONE_PID)

    completed, invocations = _run(
        tmp_path / "run",
        cwd=project,
        containers=f"{stale}\n",
        also={
            "PI_SANDBOX_FAKE_LIVE": stale,
            # As late as it can: while the wrapper lists the containers.
            "PI_SANDBOX_FAKE_LIVE_AGENT": f'[ "$1" = ps ] || exit 0; '
            f"rm -rf '{project}/.git'; ln -s '{target}' '{project}/.git'",
        },
    )

    assert completed.returncode == 2, completed.stderr
    assert "symlink" in completed.stderr
    assert not any(argv[0] == "run" for argv in invocations)
    assert sorted(path.name for path in target.iterdir()) == ["keep"]


def test_wrapper_refuses_while_another_session_of_the_project_runs(
    tmp_path: Path,
) -> None:
    """A live session's agent can change the project while this launch checks
    it, so a second session of one project is refused. The running one is
    left alone, and a live session of another project does not count."""

    project = tmp_path / "project"
    project.mkdir()
    live = _prefix(tmp_path, project) + str(os.getpid())
    other = f"pi-sandbox-0a1b2c3d-{live.split('-')[3]}-{os.getpid()}"

    completed, invocations = _run(
        tmp_path / "run", cwd=project, containers=f"{other}\n{live}\n"
    )

    assert completed.returncode == 2
    assert "still running" in completed.stderr
    # A wrapper pid reused by another process looks live too.
    assert f"docker rm -f {live}" in completed.stderr
    assert not any(argv[0] == "run" for argv in invocations)
    assert live not in _removals(invocations)

    started, invocations = _run(
        tmp_path / "other", cwd=project, containers=f"{other}\n"
    )
    assert started.returncode == 0
    assert _docker_run(invocations)


def test_wrapper_refuses_when_it_cannot_tell_a_session_is_gone(
    tmp_path: Path,
) -> None:
    """With a `ps` that sees nothing, or a container listing that failed, a
    container of this project may be a live session's, so the launch is
    refused rather than assumed safe."""

    project = tmp_path / "project"
    project.mkdir()
    stale = _prefix(tmp_path, project) + str(GONE_PID)
    fake = tmp_path / "blind" / "fakebin"
    fake.mkdir(parents=True)
    (fake / "ps").write_text("#!/bin/sh\nexit 1\n")
    (fake / "ps").chmod(0o755)

    blind = _run(tmp_path / "blind", cwd=project, containers=f"{stale}\n")
    failed = _run(
        tmp_path / "failed", cwd=project, also={"PI_SANDBOX_FAKE_PS_STATUS": "1"}
    )

    for (completed, invocations), says in (
        (blind, "still running"),
        (failed, "could not list"),
    ):
        assert completed.returncode == 2, completed.stderr
        assert says in completed.stderr
        assert not any(argv[0] == "run" for argv in invocations)


def test_wrapper_refuses_while_a_session_of_an_enclosing_directory_runs(
    tmp_path: Path,
) -> None:
    """A session of a directory above this project mounts it whole, `.git`
    unprotected, so its agent can swap this project's `.git` for a symlink
    during the whole session. A session of a directory below cannot reach this
    project's `.git`, so it does not count."""

    outer = tmp_path / "outer"
    project = outer / "project"
    inner = project / "inner"
    inner.mkdir(parents=True)
    above = _prefix(tmp_path / "above", outer) + str(os.getpid())
    below = _prefix(tmp_path / "below", inner) + str(os.getpid())

    refused, invocations = _run(tmp_path / "run", cwd=project, containers=f"{above}\n")
    started, _ = _run(tmp_path / "other", cwd=project, containers=f"{below}\n")

    assert refused.returncode == 2
    assert above in refused.stderr
    assert not any(argv[0] == "run" for argv in invocations)
    assert started.returncode == 0, started.stderr


def test_wrapper_refuses_when_a_dead_sessions_container_will_not_go(
    tmp_path: Path,
) -> None:
    """A container the daemon would not remove may still be running and
    writing the project, so it counts as live."""

    project = tmp_path / "project"
    project.mkdir()
    stale = _prefix(tmp_path, project) + str(GONE_PID)

    completed, invocations = _run(
        tmp_path / "run",
        cwd=project,
        containers=f"{stale}\n",
        also={"PI_SANDBOX_FAKE_RM_STATUS": "1"},
    )

    assert completed.returncode == 2
    assert stale in completed.stderr
    assert not any(argv[0] == "run" for argv in invocations)


def test_wrapper_checks_the_project_after_the_image_build(tmp_path: Path) -> None:
    """A build can take minutes, during which another launch of the project
    can start a container whose agent swaps `.git` for a symlink. The checks
    must come after it, so they see that container."""

    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("unchanged")

    completed, invocations = _run(
        tmp_path / "run",
        cwd=project,
        inspect_status="1",
        also={
            "PI_SANDBOX_FAKE_LIVE": "never-removed",
            "PI_SANDBOX_FAKE_LIVE_AGENT": f'[ "$1" = build ] || exit 0; '
            f"ln -s '{target}' '{project}/.git'",
        },
    )

    assert any(argv[0] == "build" for argv in invocations)
    assert completed.returncode == 2, completed.stderr
    assert "symlink" in completed.stderr
    assert sorted(path.name for path in target.iterdir()) == ["keep"]


def test_wrapper_names_each_container_apart(tmp_path: Path) -> None:
    """A session's watcher removes its container by name for seconds after
    the session ends, so a name shared between sessions of one project would
    let it remove the next session's container."""

    _, first = _run(tmp_path / "a", cwd=tmp_path / "project")
    _, second = _run(tmp_path / "b", cwd=tmp_path / "project")

    assert _container_name(first) != _container_name(second)


def test_wrapper_never_grants_host_namespaces_or_privileges(tmp_path: Path) -> None:
    _, invocations = _run(tmp_path)
    joined = " ".join(_docker_run(invocations))

    for forbidden in (
        "--privileged",
        "--network=host",
        "--network host",
        "--pid=host",
        "--pid host",
        "--ipc=host",
        "--ipc host",
        "/var/run/docker.sock",
        "herdr.sock",
    ):
        assert forbidden not in joined


def test_wrapper_ignores_the_herdr_pane_it_was_launched_from(
    tmp_path: Path,
) -> None:
    """The usual way to start this is from a Herdr pane, which exports the
    session's socket. Picking it up would put `herdr pane run` on the host
    inside the container, which is the one thing the sandbox must not allow."""

    socket = tmp_path / "herdr.sock"
    socket.write_text("")
    _, invocations = _run(
        tmp_path,
        also={
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": str(socket),
            "HERDR_PANE_ID": "w1:p1",
        },
    )
    argv = _docker_run(invocations)

    assert len([arg for arg in argv if arg == "--mount"]) == 2
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--env"] == [
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "TERM=xterm-256color",
        "COLORTERM=truecolor",
    ]
    assert not any(arg.endswith(".sock") for arg in argv)


def test_wrapper_runs_pi_in_workspace_with_the_caller_arguments(
    tmp_path: Path,
) -> None:
    _, invocations = _run(tmp_path, "--model", "opencode/glm-5.3")
    argv = _docker_run(invocations)

    assert argv[argv.index("--workdir") + 1] == "/workspace"
    image = _image(argv)
    assert argv[argv.index(image) + 1 :] == [
        "--model",
        "opencode/glm-5.3",
    ]
    assert "--rm" in argv
    assert "--init" in argv


def test_wrapper_defaults_to_a_chinese_hosted_model(tmp_path: Path) -> None:
    _, invocations = _run(tmp_path)
    argv = _docker_run(invocations)
    image = _image(argv)

    assert argv[argv.index(image) + 1 :] == [
        "--provider",
        "opencode-go",
        "--model",
        "deepseek-v4.1-flash",
    ]


def test_wrapper_keeps_a_model_the_caller_asked_for(tmp_path: Path) -> None:
    """The provider still comes along: `--model kimi-k3` alone is ambiguous
    across the providers Pi knows, and it refuses to run rather than guess."""

    _, chosen = _run(tmp_path, "--model", "kimi-k3")
    _, other_flag = _run(tmp_path / "b", "--models", "glm-5.3,kimi-k3")

    chosen_argv = _docker_run(chosen)
    other_argv = _docker_run(other_flag)
    image = _image(chosen_argv)

    assert chosen_argv[chosen_argv.index(image) + 1 :] == [
        "--provider",
        "opencode-go",
        "--model",
        "kimi-k3",
    ]
    # --models is a different flag, so the default model still applies.
    assert "deepseek-v4.1-flash" in " ".join(other_argv)


def test_wrapper_leaves_a_provider_the_caller_named_alone(tmp_path: Path) -> None:
    """Adding the flag to a `provider/model` string overrides it, and Pi then
    sends the rest of the name to the wrong API as a custom model id."""

    _, slashed = _run(tmp_path, "--model", "opencode/glm-5.3")
    _, joined = _run(tmp_path / "b", "--model=opencode/glm-5.3")
    _, explicit = _run(tmp_path / "c", "--provider", "opencode", "--model", "kimi-k3")

    for invocations in (slashed, joined):
        argv = _docker_run(invocations)
        assert "opencode-go" not in " ".join(argv)

    explicit_argv = _docker_run(explicit)
    image = _image(explicit_argv)
    assert explicit_argv[explicit_argv.index(image) + 1 :] == [
        "--provider",
        "opencode",
        "--model",
        "kimi-k3",
    ]


def test_wrapper_names_the_model_even_when_the_provider_is_given(
    tmp_path: Path,
) -> None:
    """Observed: with both keys forwarded, `--provider opencode-go` on its own
    answered from Zen, because Pi resolves the model it picks against every
    provider it can authenticate."""

    _, invocations = _run(tmp_path, "--provider", "opencode-go")
    argv = _docker_run(invocations)
    image = _image(argv)

    assert argv[argv.index(image) + 1 :] == [
        "--model",
        "deepseek-v4.1-flash",
        "--provider",
        "opencode-go",
    ]


def test_wrapper_reads_model_as_a_flag_not_as_text(tmp_path: Path) -> None:
    """Matching the flattened arguments would read a prompt that mentions the
    flag as picking a model, and leave no provider the sandbox can reach."""

    _, asking = _run(tmp_path, "what does --model do?")
    _, after_ddash = _run(tmp_path / "b", "--", "--model", "foo")

    for invocations in (asking, after_ddash):
        assert "deepseek-v4.1-flash" in _docker_run(invocations)


def test_wrapper_passes_pi_subcommands_through_untouched(tmp_path: Path) -> None:
    """Pi recognises `install` and friends only as the first argument, so the
    prepended model default would chat them at the model instead."""

    _, subcommand = _run(tmp_path, "install", "npm:example")
    _, prompt = _run(tmp_path / "b", "installed?")

    subcommand_argv = _docker_run(subcommand)
    prompt_argv = _docker_run(prompt)
    subcommand_image = _image(subcommand_argv)
    prompt_image = _image(prompt_argv)

    assert subcommand_argv[subcommand_argv.index(subcommand_image) + 1 :] == [
        "install",
        "npm:example",
    ]
    # A word that merely looks like one is still an ordinary prompt.
    assert prompt_argv[prompt_argv.index(prompt_image) + 1 :] == [
        "--provider",
        "opencode-go",
        "--model",
        "deepseek-v4.1-flash",
        "installed?",
    ]


def test_wrapper_builds_the_image_only_when_it_is_missing(tmp_path: Path) -> None:
    _, present = _run(tmp_path)
    _, missing = _run(tmp_path / "missing", inspect_status="1")

    assert [argv for argv in present if argv[0] == "build"] == []
    builds = [argv for argv in missing if argv[0] == "build"]
    assert len(builds) == 1
    assert any(arg.endswith("/Dockerfile.pi") for arg in builds[0])


def test_wrapper_removes_old_images_after_the_session(tmp_path: Path) -> None:
    """Each build leaves an older pi-sandbox tag behind. Removing them here
    keeps storage bounded without the user running anything, and the current
    tag has to survive so this launch's image is not deleted."""

    # The fake image list does not carry the current tag, so compute it from a
    # first launch and put it among the older tags the second run sees.
    _, first = _run(tmp_path)
    current = _image(_docker_run(first)).removeprefix("pi-sandbox:")

    completed, invocations = _run(tmp_path / "second", tags=f"aaaa {current} bbbb")
    removed = [argv[1] for argv in invocations if argv[0] == "rmi"]
    prunes = [argv for argv in invocations if argv[:2] == ["image", "prune"]]

    assert f"pi-sandbox:{current}" not in removed
    assert "pi-sandbox:aaaa" in removed
    assert "pi-sandbox:bbbb" in removed
    assert len(prunes) == 1
    assert "label=pi-sandbox.image=1" in prunes[0]
    assert "removed old images:" in completed.stderr
    assert "aaaa" in completed.stderr and "bbbb" in completed.stderr


PI_INSTALL_STEP = (
    'mount / from exec /bin/sh -c : "${PI_VERSION:?the wrapper must pass '
    '--build-arg PI_VERSION}" && npm install --global'
)


def _pruned(invocations: list[list[str]]) -> list[str]:
    return [argv[-1] for argv in invocations if argv[:2] == ["builder", "prune"]]


def test_wrapper_prunes_the_build_cache_an_old_pi_install_left(
    tmp_path: Path,
) -> None:
    """A rebuild for a new Pi leaves the old install's build cache, about
    190 MB a release, once the old image is gone. Only that step's records
    are pruned: another project's cache is not the wrapper's to touch, and a
    listed id that is not a plain id must not reach the filter, where docker
    reads it as a pattern that `.` or a short word would match widely."""

    record = "igt3lb4ht80nil0l0udexm8pw"
    cache = (
        f"{record} {PI_INSTALL_STEP}\n"
        "8m0kku6gs3u932l9gziv7tpaa mount / from exec /bin/sh -c pip install -e .\n"
        f". {PI_INSTALL_STEP}\n"
        f"npm {PI_INSTALL_STEP}\n"
    )
    _, rebuilt = _run(tmp_path / "a", inspect_status="1", cache=cache)
    _, removed = _run(tmp_path / "b", tags="old", cache=cache)
    _, quiet = _run(tmp_path / "c", cache=cache)

    assert _pruned(rebuilt) == [f"id={record}"]
    assert _pruned(removed) == [f"id={record}"]
    # Nothing was rebuilt or removed, so nothing new can be stale, and the
    # listing, which sizes every volume, is not paid for at every exit.
    assert _pruned(quiet) == []
    assert not any(argv[:1] == ["system"] for argv in quiet)


def test_wrapper_keeps_the_exit_status_when_image_removal_fails(
    tmp_path: Path,
) -> None:
    """Another running session uses an image, so `docker rmi` fails there and
    the wrapper has to stay quiet about it and return docker's own status."""

    completed, invocations = _run(tmp_path, tags="aaaa", rmi_status="1", run_status="7")

    assert completed.returncode == 7
    assert "pi-sandbox:aaaa" not in completed.stderr
    assert "removed old images" not in completed.stderr
    assert any(argv[0] == "image" and argv[1] == "prune" for argv in invocations)


def test_wrapper_tags_the_image_by_content_and_uses_it_for_inspect_and_run(
    tmp_path: Path,
) -> None:
    _, invocations = _run(tmp_path, inspect_status="1")

    inspected = invocations[0][-1]
    build = next(argv for argv in invocations if argv[0] == "build")
    built = build[build.index("--tag") + 1]
    ran = _image(_docker_run(invocations))

    assert re.fullmatch(r"pi-sandbox:[0-9a-f]{12}", inspected)
    assert inspected == built == ran


def test_wrapper_changes_the_image_tag_when_a_build_input_changes(
    tmp_path: Path,
) -> None:
    """A pull that touches the Dockerfile or the entrypoint has to produce a
    tag that does not exist yet, or the build-if-missing check keeps running
    the image built from the old files."""

    def tag(directory: Path, changed: str | None = None) -> str:
        wrapper = _wrapper_with_inputs(directory)
        if changed is not None:
            with (directory / changed).open("a") as handle:
                handle.write("\nchanged by the test\n")
        _, invocations = _run(directory, wrapper=wrapper)
        return _image(_docker_run(invocations))

    unchanged = tag(tmp_path / "unchanged")
    # Names and mtimes are not hashed, so the same contents give the same tag.
    assert tag(tmp_path / "again") == unchanged
    for name in ("Dockerfile.pi", "entrypoint.sh"):
        assert tag(tmp_path / name.replace(".", "-"), changed=name) != unchanged


def test_wrapper_reads_the_whole_script_before_running_it(tmp_path: Path) -> None:
    """A pull or edit during a session replaces the wrapper while it is still
    alive for `docker run`. sh reads a script as it goes, so without the brace
    group the shell resumed at the old byte offset in the new file, ran
    whatever text was there, and never reached the checks and cleanup after
    `docker run`. bash is where that was seen, as macOS /bin/sh, and it still
    reads that way, so the copy is run under bash."""

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash to reproduce a script replaced while it runs")

    wrapper = _wrapper_with_inputs(tmp_path / "checkout")
    replacement = tmp_path / "newer-pi"
    # Longer than the wrapper and not shell, so the offset the shell resumes
    # at lands inside a word that cannot resolve, which is the loud failure
    # the owner saw. A shorter file only reads as end of input and stops
    # silently.
    replacement.write_text("not-a-command = not shell\n" * 5000)

    completed, invocations = _run(
        tmp_path / "run",
        wrapper=wrapper,
        interpreter=bash,
        run_status="7",
        tags="latest deadbeef",
        agent=f'cat "{replacement}" > "{wrapper}"',
    )

    # The rewrite is the whole point, so fail here rather than pass vacuously
    # if the fake docker's agent never replaced the wrapper.
    assert wrapper.read_text() == replacement.read_text()

    # Docker's status still comes back, and the cleanup after `docker run`
    # still asked docker for the old tags: neither happens when the shell
    # dies on the text the replacement left at its old offset.
    assert completed.returncode == 7
    assert "command not found" not in completed.stderr
    assert any(argv and argv[0] == "images" for argv in invocations)


def test_wrapper_builds_with_the_host_ids_on_linux(tmp_path: Path) -> None:
    """A bind mount keeps the host uid and gid on Linux, so the image has to
    be built with them for the agent to write /workspace."""

    _, invocations = _run(tmp_path, inspect_status="1", uname="Linux")
    build = invocations[1]
    real_uid = subprocess.run(
        ["id", "-u"], capture_output=True, text=True, check=True
    ).stdout.strip()
    real_gid = subprocess.run(
        ["id", "-g"], capture_output=True, text=True, check=True
    ).stdout.strip()

    args = [build[index + 1] for index, arg in enumerate(build) if arg == "--build-arg"]
    assert f"AGENT_UID={real_uid}" in args
    assert f"AGENT_GID={real_gid}" in args


def test_wrapper_builds_without_the_host_ids_off_linux(tmp_path: Path) -> None:
    """Docker Desktop maps the mounted files to the container user, so the
    defaults, and the state volumes already owned as 1001, still fit. The
    version arguments are platform independent and still arrive."""

    _, invocations = _run(tmp_path, inspect_status="1", uname="Darwin")
    build = invocations[1]
    args = [build[index + 1] for index, arg in enumerate(build) if arg == "--build-arg"]

    assert not any(arg.startswith("AGENT_") for arg in args)
    assert "PI_VERSION=0.87.0" in args


def test_wrapper_rebuilds_the_same_tag_when_npm_has_a_newer_pi(
    tmp_path: Path,
) -> None:
    """The content tag does not change when npm publishes a release, so the
    wrapper has to notice the image's label differs and rebuild under the same
    tag with the new version as a build argument."""

    _, invocations = _run(tmp_path, pi_version="0.99.0", label="0.87.0")
    inspect = invocations[0]
    build = next(argv for argv in invocations if argv[0] == "build")
    args = [build[index + 1] for index, arg in enumerate(build) if arg == "--build-arg"]

    assert build[build.index("--tag") + 1] == inspect[-1]
    assert "PI_VERSION=0.99.0" in args


def test_wrapper_does_not_rebuild_when_the_versions_match(tmp_path: Path) -> None:
    """The image the content tag names already carries the newest version, so
    the launch goes straight to docker run."""

    _, invocations = _run(tmp_path, pi_version="0.87.0")

    assert [argv for argv in invocations if argv[0] == "build"] == []


def test_wrapper_treats_a_failed_lookup_as_unknown(tmp_path: Path) -> None:
    """Offline, the existing image is used as it is, and no build argument the
    wrapper could not verify is passed."""

    _, invocations = _run(tmp_path, curl_pi="fail")

    assert [argv for argv in invocations if argv[0] == "build"] == []
    assert _docker_run(invocations)


def test_wrapper_treats_an_invalid_version_as_unknown(tmp_path: Path) -> None:
    """A registry answer that is not a plain semver must not become a build
    argument or a tag the Dockerfile would use. The label differs from the
    valid versions, so without the check this would rebuild."""

    _, invocations = _run(tmp_path, pi_version="not a version", label="0.87.0")

    assert [argv for argv in invocations if argv[0] == "build"] == []


def _assert_lookup_failure_stops_the_first_launch(
    completed: subprocess.CompletedProcess[str], invocations: list[list[str]]
) -> None:
    """No image plus a failed lookup must stop the launch rather than build a
    tag the Dockerfile has no defaults for or start an image that is missing."""

    assert completed.returncode == 1
    assert [argv for argv in invocations if argv[0] == "build"] == []
    assert [argv for argv in invocations if argv[0] == "run"] == []
    assert "could not look up the newest Pi" in completed.stderr
    assert "no image to fall back to" in completed.stderr


def test_wrapper_refuses_to_build_when_the_lookup_fails_and_no_image(
    tmp_path: Path,
) -> None:
    """The Dockerfile has no default version, so a build without one would
    fail at once. Offline with no image there is nothing to fall back to, so
    the wrapper must stop instead of building or running."""

    completed, invocations = _run(tmp_path, inspect_status="1", curl_pi="fail")

    _assert_lookup_failure_stops_the_first_launch(completed, invocations)


def test_wrapper_stops_when_the_build_fails(tmp_path: Path) -> None:
    """A build that fails half-way leaves no image under the tag. `set -e`
    has to end the wrapper with docker's status rather than starting the
    missing image or a stale one the same tag used to hold."""

    completed, invocations = _run(
        tmp_path, inspect_status="1", build_status="9", run_status="0"
    )

    assert completed.returncode == 9
    assert not any(argv[0] == "run" for argv in invocations)


def test_wrapper_starts_the_existing_image_when_a_rebuild_fails(
    tmp_path: Path,
) -> None:
    """A same-tag build that fails leaves the image the tag already held
    intact, so availability wins: warn once and start that image rather than
    leaving the user with no Pi at all. The exit status is docker run's."""

    completed, invocations = _run(
        tmp_path,
        pi_version="0.99.0",
        label="0.87.0",
        build_status="9",
        run_status="7",
    )

    assert completed.returncode == 7
    assert "starting the existing image" in completed.stderr
    assert _docker_run(invocations)


def test_wrapper_gives_linux_host_ids_their_own_image_tag(tmp_path: Path) -> None:
    """A changed uid, or a second host user, must not reuse the image built
    for the first one."""

    _, linux = _run(tmp_path / "linux", uname="Linux")
    _, darwin = _run(tmp_path / "darwin", uname="Darwin")

    assert _image(_docker_run(linux)) != _image(_docker_run(darwin))


def test_wrapper_refuses_to_run_as_root_on_linux(tmp_path: Path) -> None:
    """A root uid would make the container user uid 0 and leave root-owned
    files in the project it mounts."""

    completed, invocations = _run(tmp_path, uname="Linux", fake_id=("0", "0"))

    assert completed.returncode == 2
    assert "root" in completed.stderr
    assert invocations == []


def test_wrapper_stays_in_the_process_tree_so_herdr_can_identify_pi(
    tmp_path: Path,
) -> None:
    """An `exec docker ...` would replace the only `pi`-named process."""

    _run(tmp_path)

    assert str(WRAPPER) in _docker_run_parent(tmp_path / "docker.log")


def test_image_pins_uv_to_a_digest_instead_of_piping_install_sh() -> None:
    """An install script piped into a shell runs whatever the network returns
    at build time, which must not happen in a container that holds the API
    key. uv's official distroless image comes pinned by the digest of its
    multi-arch index instead."""

    dockerfile = (ROOT / "Dockerfile.pi").read_text()

    assert re.search(
        r"COPY --from=ghcr\.io/astral-sh/uv:[^@\s]+@sha256:[0-9a-f]{64}",
        dockerfile,
    )


def test_image_pins_the_node_base_image_by_tag_and_digest() -> None:
    """The digest keeps the build on the image it was reviewed at, and the tag
    in front of it says which version that is, so the two can be bumped
    together."""

    dockerfile = (ROOT / "Dockerfile.pi").read_text()

    assert re.search(
        r"^FROM node:[^@\s]+@sha256:[0-9a-f]{64}$",
        dockerfile,
        re.MULTILINE,
    )


ENTRYPOINT = ROOT / "entrypoint.sh"


class _Entrypoint(NamedTuple):
    auth: Path
    argv: list[str]
    env: dict[str, str]
    stderr: str
    calls: list[list[str]]
    scripts: list[str]
    timeouts: list[str]


def _run_entrypoint(
    tmp_path: Path,
    args: tuple[str, ...] = ("--model", "kimi-k3"),
    timeout: float | None = None,
    **env: str,
) -> _Entrypoint:
    """Run the entrypoint with stubs for the programs it launches."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv_log = tmp_path / "pi.argv"
    env_log = tmp_path / "pi.env"
    calls_log = tmp_path / "pi.calls"
    timeout_log = tmp_path / "timeout.log"
    # Every pi call is appended, with the lifecycle-script setting it saw, so a
    # test can tell the extension installs from the final exec. An install can
    # be made to fail to check the best-effort path.
    (bin_dir / "pi").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "---" >>{calls_log}\n'
        f'printf "%s\\n" "$@" >>{calls_log}\n'
        f'printf "SCRIPTS=%s\\n" "${{npm_config_ignore_scripts:-}}" >>{calls_log}\n'
        f'printf "%s\\n" "$@" >{argv_log}\n'
        f"env >{env_log}\n"
        # Fails the install of the one package named, at any version.
        'case "${PI_SANDBOX_FAKE_PI_INSTALL_FAIL:-}" in\n'
        '    ?*) case "${2:-}" in "$PI_SANDBOX_FAKE_PI_INSTALL_FAIL" | "$PI_SANDBOX_FAKE_PI_INSTALL_FAIL"@*) exit 1 ;; esac ;;\n'
        "esac\n"
        'if [ "$1" = install ]; then\n'
        '    if [ "${PI_SANDBOX_FAKE_PI_INSTALL_STATUS:-0}" != 0 ]; then\n'
        '        exit "${PI_SANDBOX_FAKE_PI_INSTALL_STATUS}"\n'
        "    fi\n"
        # Where Pi puts an installed npm package, with the version asked for
        # (none for a bare name, scoped or not), and where npm caches it.
        '    spec=${2#npm:} name=${2#npm:} version=""\n'
        '    case "${spec#@}" in *@*) name=${spec%@*} version=${spec##*@} ;; esac\n'
        '    mkdir -p "$HOME/.pi/agent/npm/node_modules/$name"\n'
        '    rm -f "$HOME/.pi/agent/npm/node_modules/$name/package.json"\n'
        '    printf \'{"name": "%s", "version": "%s"}\\n\' "$name" "$version" >"$HOME/.pi/agent/npm/node_modules/$name/package.json"\n'
        # Pi's settings list each install as it was named, one per line here,
        # which is all a fixed-string search of the real JSON needs.
        '    printf \'"%s"\\n\' "$2" >>"$HOME/.pi/agent/settings.json"\n'
        "fi\n"
        "exit 0\n"
    )
    # Log each use of `timeout`, then run the command it wraps under a real
    # limit, since macOS has no `timeout` of its own.
    (bin_dir / "timeout").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >>{timeout_log}\n'
        "exec perl -e 'alarm shift; exec @ARGV or die' \"$@\"\n"
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    completed = subprocess.run(
        [shutil.which("dash") or "sh", str(ENTRYPOINT), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            # The container's /tmp, which goes with it.
            "TMPDIR": str(tmp_path),
            **env,
        },
        timeout=timeout,
    )

    calls = []
    scripts = []
    for block in calls_log.read_text().split("---"):
        if not block.strip():
            continue
        lines = block.strip().splitlines()
        scripts.append(lines[-1].removeprefix("SCRIPTS="))
        calls.append(lines[:-1])

    # Whatever it does, it must not print the key on the way.
    assert FAKE_KEY not in completed.stdout + completed.stderr
    return _Entrypoint(
        stderr=completed.stderr,
        auth=home / ".pi/agent/auth.json",
        argv=argv_log.read_text().split(),
        env=dict(
            line.split("=", 1)
            for line in env_log.read_text().splitlines()
            if "=" in line
        ),
        calls=calls,
        scripts=scripts,
        timeouts=timeout_log.read_text().splitlines() if timeout_log.exists() else [],
    )


PINNED = re.compile(r"npm:(@[a-z0-9-]+/)?[a-z0-9-]+@[0-9]+\.[0-9]+\.[0-9]+")


def _unpinned(spec: str) -> str:
    """The package a spec names, without the version it is pinned to."""

    return re.sub(r"@[0-9][^/]*$", "", spec)


def test_the_entrypoint_installs_the_pinned_extensions_on_start(
    tmp_path: Path,
) -> None:
    """The extensions run with the forwarded keys in every project, so they
    are pinned to exact versions, bumped in a reviewed change, and nothing
    moves them to a newer release on its own. Lifecycle scripts stay off and
    the step is bounded so a hung registry cannot stop Pi."""

    started = _run_entrypoint(tmp_path)
    installs = [call[1] for call in started.calls[:-1]]

    assert [_unpinned(spec) for spec in installs] == [
        "npm:pi-subagents",
        "npm:@tintinweb/pi-subagents",
        "npm:pi-background-tasks",
        "npm:pi-extension-manager",
    ]
    assert all(PINNED.fullmatch(spec) for spec in installs), installs
    assert all(call[0] == "install" for call in started.calls[:-1])
    assert all(scripts == "true" for scripts in started.scripts[:-1])
    # The final exec is Pi itself, which must not inherit the setting.
    assert started.calls[-1] == ["--model", "kimi-k3"]
    assert started.scripts[-1] == ""
    assert started.timeouts


def test_the_entrypoint_skips_the_extension_install_for_subcommands(
    tmp_path: Path,
) -> None:
    """`pi install` and the rest are passed through to Pi, so the entrypoint
    must not run an install of its own for them."""

    started = _run_entrypoint(tmp_path, args=("list",))

    assert started.calls == [["list"]]
    assert started.timeouts == []


def test_the_entrypoint_starts_pi_when_the_extension_install_fails(
    tmp_path: Path,
) -> None:
    """A registry that is down or a volume the agent has ruined must cost the
    install, not the session."""

    started = _run_entrypoint(tmp_path, PI_SANDBOX_FAKE_PI_INSTALL_STATUS="1")

    assert started.argv == ["--model", "kimi-k3"]
    assert "could not install the extensions" in started.stderr


def test_the_entrypoint_reports_a_partial_extension_install_failure(
    tmp_path: Path,
) -> None:
    """One package failing must be reported even though the other three
    succeed, and the rest must still be attempted rather than skipped."""

    started = _run_entrypoint(
        tmp_path, PI_SANDBOX_FAKE_PI_INSTALL_FAIL="npm:pi-subagents"
    )
    installs = [call for call in started.calls if call[:1] == ["install"]]

    assert started.argv == ["--model", "kimi-k3"]
    assert len(installs) == 4
    assert "could not install the extensions" in started.stderr


def _restart(tmp_path: Path, **env: str) -> list[list[str]]:
    """Start the entrypoint again on the same home, and return the calls this
    start made to pi other than the final exec."""

    (tmp_path / "pi.calls").unlink()
    return _run_entrypoint(tmp_path, **env).calls[:-1]


def test_the_entrypoint_makes_no_call_at_the_pins(tmp_path: Path) -> None:
    """Every Herdr pane is a start, and an install costs about a second
    each, so a volume that already holds the pins makes no call."""

    _run_entrypoint(tmp_path)

    assert _restart(tmp_path) == []


def test_the_entrypoint_moves_an_existing_volume_to_the_pinned_versions(
    tmp_path: Path,
) -> None:
    """An image rebuild does not reach an existing volume, so a bump in the
    list has to, in either direction: an older release from before the bump
    and a newer one the agent installed both go back to the pin."""

    first = [call[1] for call in _run_entrypoint(tmp_path).calls[:-1]]
    modules = tmp_path / "home/.pi/agent/npm/node_modules"
    (modules / "pi-subagents/package.json").write_text('{"version": "0.0.1"}')
    (modules / "@tintinweb/pi-subagents/package.json").write_text(
        '{"version":"99.0.0"}'
    )

    assert [call[1] for call in _restart(tmp_path)] == first[:2]


def test_the_entrypoint_pins_an_extension_pi_lists_without_a_version(
    tmp_path: Path,
) -> None:
    """A volume from before the pins, or a bare `pi install` of one of the
    four, leaves Pi's settings naming it without a version while npm holds
    the pinned one. `pi update --extensions` moves exactly those entries, so
    the next start must pin it again."""

    first = [call[1] for call in _run_entrypoint(tmp_path).calls[:-1]]
    settings = tmp_path / "home/.pi/agent/settings.json"
    settings.write_text(
        settings.read_text().replace(first[1], "npm:@tintinweb/pi-subagents")
    )

    assert [call[1] for call in _restart(tmp_path)] == [first[1]]


def test_the_entrypoint_installs_a_missing_extension_on_any_start(
    tmp_path: Path,
) -> None:
    """An extension the agent removed is put back on the next start, at its
    pinned version."""

    first = [call[1] for call in _run_entrypoint(tmp_path).calls[:-1]]
    shutil.rmtree(tmp_path / "home/.pi/agent/npm/node_modules/pi-background-tasks")

    assert [call[1] for call in _restart(tmp_path)] == [first[2]]


def test_the_entrypoint_does_not_block_on_a_fifo_the_agent_left(
    tmp_path: Path,
) -> None:
    """The installed version is read from the volume, which the agent owns.
    A FIFO in place of a package.json would block that read forever, and
    the session with it."""

    _run_entrypoint(tmp_path)
    package = tmp_path / "home/.pi/agent/npm/node_modules/pi-subagents/package.json"
    package.unlink()
    os.mkfifo(package)
    (tmp_path / "pi.calls").unlink()

    started = _run_entrypoint(tmp_path, timeout=20)

    assert started.argv == ["--model", "kimi-k3"]


def test_the_entrypoint_writes_the_subscription_key_where_pi_reads_it(
    tmp_path: Path,
) -> None:
    """Pi reads opencode-go from auth.json, not from the environment, so the
    forwarded key has to be written out before Pi starts."""

    started = _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)
    written = json.loads(started.auth.read_text())

    assert written["opencode-go"] == {"type": "api_key", "key": FAKE_KEY}
    # It is a credential at rest in the state volume.
    assert started.auth.stat().st_mode & 0o077 == 0


def test_the_entrypoint_keeps_other_providers_the_agent_authenticated(
    tmp_path: Path,
) -> None:
    """The file is in the state volume, so it outlives the container and may
    hold providers the agent logged into itself."""

    auth = tmp_path / "home/.pi/agent/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"anthropic": {"type": "oauth", "access": "keep-me"}}))
    auth.chmod(0o644)

    _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)
    written = json.loads(auth.read_text())

    assert written["anthropic"] == {"type": "oauth", "access": "keep-me"}
    assert written["opencode-go"]["key"] == FAKE_KEY
    # A mode the file already had, which umask alone would not have corrected.
    assert auth.stat().st_mode & 0o077 == 0


def test_the_entrypoint_runs_pi_without_a_key_too(tmp_path: Path) -> None:
    """The wrapper requires the key, but the image is also used directly, and
    an absent key must not stop Pi from starting."""

    started = _run_entrypoint(tmp_path)

    assert not started.auth.exists()
    assert started.argv == ["--model", "kimi-k3"]


def test_the_entrypoint_survives_an_auth_file_the_agent_ruined(
    tmp_path: Path,
) -> None:
    """That file is the agent's own between runs. Letting a parse error out of
    the entrypoint would let it wedge the sandbox shut with a one-byte write,
    recoverable only by deleting the project's volume."""

    auth = tmp_path / "home/.pi/agent/auth.json"
    auth.parent.mkdir(parents=True)

    for ruined in ("not json at all", "[]", '"a string"'):
        auth.write_text(ruined)
        started = _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)

        assert started.argv == ["--model", "kimi-k3"]
        assert json.loads(auth.read_text())["opencode-go"]["key"] == FAKE_KEY


def test_the_entrypoint_survives_a_fifo_left_at_the_auth_file(
    tmp_path: Path,
) -> None:
    """The file is the agent's own between runs. `read_text()` on a FIFO it
    left there blocks until a writer appears, so the entrypoint would never
    reach Pi and the project's volume would have to be deleted. It is only
    read when it is a regular file, so the atomic replace repairs it."""

    auth = tmp_path / "home/.pi/agent/auth.json"
    auth.parent.mkdir(parents=True)
    os.mkfifo(auth)

    # With the old unconditional read the write blocks until the entrypoint's
    # own 10 second bound ends it, and the key is not written, which the
    # asserts below catch. The timeout here only keeps the suite from hanging.
    started = _run_entrypoint(tmp_path, timeout=20, OPENCODE_GO_API_KEY=FAKE_KEY)

    assert started.argv == ["--model", "kimi-k3"]
    assert auth.is_file()
    assert json.loads(auth.read_text())["opencode-go"]["key"] == FAKE_KEY


def test_the_entrypoint_starts_pi_even_when_it_cannot_write_the_key(
    tmp_path: Path,
) -> None:
    """The agent owns its home between runs. `mkdir auth.json` there, or a
    directory it made unwritable, must cost it the key rather than the
    session, which `set -eu` would otherwise turn into a sandbox that never
    starts again until the volume is deleted."""

    agent_dir = tmp_path / "home/.pi/agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "auth.json").mkdir()

    started = _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)

    assert started.argv == ["--model", "kimi-k3"]
    assert "could not write" in started.stderr


def test_the_entrypoint_keeps_the_old_auth_file_when_the_write_fails(
    tmp_path: Path,
) -> None:
    """It is renamed into place, so a failed write cannot destroy the
    providers the agent authenticated itself."""

    agent_dir = tmp_path / "home/.pi/agent"
    agent_dir.mkdir(parents=True)
    auth = agent_dir / "auth.json"
    auth.write_text(json.dumps({"anthropic": {"type": "oauth", "access": "keep-me"}}))
    agent_dir.chmod(0o500)

    try:
        started = _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)
    finally:
        agent_dir.chmod(0o700)

    assert started.argv == ["--model", "kimi-k3"]
    assert json.loads(auth.read_text()) == {
        "anthropic": {"type": "oauth", "access": "keep-me"}
    }


def test_the_entrypoint_clears_a_half_written_file_from_a_killed_run(
    tmp_path: Path,
) -> None:
    """The container is killed rather than stopped, so the file it renames
    from can be left behind. Without the unlink, every later run would fail to
    create it and quietly lose the key."""

    agent_dir = tmp_path / "home/.pi/agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "auth.json.new").write_text("half a file")

    started = _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)

    assert json.loads(started.auth.read_text())["opencode-go"]["key"] == FAKE_KEY
    assert not (agent_dir / "auth.json.new").exists()


def test_concurrent_starts_in_one_project_all_write_the_key(tmp_path: Path) -> None:
    """Sessions in one project share its state volume, and every one writes
    auth.json through the same pending file as it starts. One start must not
    unlink or rename another's half-written file. The race is narrow, so five
    bursts of twelve starts make it near certain to show without the lock
    (4 of 5 single bursts failed)."""

    home = tmp_path / "home"
    home.mkdir()
    with ThreadPoolExecutor(max_workers=12) as pool:
        started = list(
            pool.map(
                lambda index: _run_entrypoint(
                    tmp_path / str(index),
                    HOME=str(home),
                    OPENCODE_GO_API_KEY=FAKE_KEY,
                ),
                range(60),
            )
        )

    assert [s.stderr for s in started if "auth.json" in s.stderr] == []


def test_a_session_holding_the_auth_lock_cannot_stop_another_starting(
    tmp_path: Path,
) -> None:
    """The lock is in the shared volume, so the agent in one session can take
    it and never let go. Another session must still start, at the cost of the
    key write, which it reports."""

    agent_dir = tmp_path / "home/.pi/agent"
    agent_dir.mkdir(parents=True)
    held = os.open(agent_dir, os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY, timeout=60)
    finally:
        os.close(held)

    assert started.argv == ["--model", "kimi-k3"]
    assert "could not write the subscription key" in started.stderr


def test_the_entrypoint_does_not_write_the_key_through_a_symlink(
    tmp_path: Path,
) -> None:
    """`/workspace` is the host's real checkout, and a symlink left in the
    volume would put the key there."""

    auth = tmp_path / "home/.pi/agent/auth.json"
    auth.parent.mkdir(parents=True)
    elsewhere = tmp_path / "workspace-file"
    elsewhere.write_text("")
    auth.symlink_to(elsewhere)

    _run_entrypoint(tmp_path, OPENCODE_GO_API_KEY=FAKE_KEY)

    assert elsewhere.read_text() == ""
    assert not auth.is_symlink()
    assert json.loads(auth.read_text())["opencode-go"]["key"] == FAKE_KEY
