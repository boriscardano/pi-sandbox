"""Verify that the host Pi sandbox wrapper builds an isolating docker run."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

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
case "$1" in
    image) exit "${PI_SANDBOX_FAKE_INSPECT:-0}" ;;
esac
if [ "$1" = run ]; then
    if [ -n "${PI_SANDBOX_FAKE_AGENT:-}" ]; then
        sh -c "$PI_SANDBOX_FAKE_AGENT"
    fi
    exit "${PI_SANDBOX_FAKE_RUN_STATUS:-0}"
fi
exit 0
"""

# Simulates what an agent can leave behind in the mounted project. A nested
# repository cannot be prevented by a mount, so the wrapper looks for it at
# exit instead.
NESTED_GIT = (
    "mkdir -p sub/.git && printf '[core]\\n\\tfsmonitor = true\\n' > sub/.git/config"
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
    uname: str | None = None,
    fake_id: tuple[str, str] | None = None,
    fake_find: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
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

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home if home is not None else tmp_path / "home"),
        "PI_SANDBOX_FAKE_LOG": str(log),
        "PI_SANDBOX_FAKE_INSPECT": inspect_status,
        "PI_SANDBOX_FAKE_RUN_STATUS": run_status,
    }
    if agent is not None:
        env["PI_SANDBOX_FAKE_AGENT"] = agent
    if key is not None:
        env["OPENCODE_GO_API_KEY"] = key
    if forward is not None:
        env["PI_SANDBOX_ENV"] = forward
        env["TYPESAFE_API_KEY"] = "typesafe-test-key-do-not-leak"
    env.update(also or {})

    completed = subprocess.run(
        [str(wrapper or WRAPPER), *args],
        capture_output=True,
        text=True,
        cwd=project,
        env=env,
        check=False,
    )
    invocations = [
        [line for line in block.splitlines() if line and not line.startswith("PARENT=")]
        for block in (log.read_text() if log.exists() else "").split("---")
        if block.strip()
    ]
    return completed, invocations


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
    for name in ("pi", "Dockerfile.pi", "entrypoint.sh", "herdr-fleet.md"):
        shutil.copy(ROOT / name, directory / name)
    wrapper = directory / "pi"
    wrapper.chmod(0o755)
    return wrapper


def test_wrapper_is_named_pi_so_herdr_identifies_the_pane_agent() -> None:
    assert WRAPPER.name == "pi"
    assert os.access(WRAPPER, os.X_OK)


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


def test_wrapper_mounts_only_the_chosen_project_and_a_state_volume(
    tmp_path: Path,
) -> None:
    _, invocations = _run(tmp_path)
    argv = _docker_run(invocations)

    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]
    assert mounts[0] == f"type=bind,source={tmp_path / 'project'},target=/workspace"
    assert mounts[1].startswith("type=volume,source=pi-sandbox-project-")
    assert mounts[1].endswith(",target=/home/agent")
    assert len(mounts) == 2
    assert "--volume" not in argv
    assert "-v" not in argv


def test_wrapper_mounts_the_git_root_when_run_from_a_subdirectory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path, cwd=repo / "pkg" / "deep")

    assert f"type=bind,source={repo},target=/workspace" in _docker_run(invocations)


def test_wrapper_mounts_git_config_and_hooks_read_only(tmp_path: Path) -> None:
    """A writable .git/config or hook is a command the host runs the next time
    the owner touches the checkout: `git status` and `git commit` honour
    core.fsmonitor, core.pager, core.hooksPath and filter.<name>.clean."""

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
        f"type=bind,source={repo}/.git/hooks,target=/workspace/.git/hooks,readonly"
    )
    assert mounts[4] == (
        f"type=bind,source={repo}/.git/worktrees,"
        "target=/workspace/.git/worktrees,readonly"
    )
    # A fresh repository has no modules, and the state volume is still last.
    assert len(mounts) == 6
    assert mounts[5].startswith("type=volume,source=pi-sandbox-")
    assert mounts[5].endswith(",target=/home/agent")


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
    assert mounts[3].endswith(",target=/workspace/.git/hooks,readonly")


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


def test_wrapper_mounts_git_modules_only_when_present(tmp_path: Path) -> None:
    """Submodule configs carry the same keys, so they get the same treatment,
    and a repository without the directory gets no mount that would fail."""

    with_modules = tmp_path / "with-modules"
    without = tmp_path / "without"
    for repo in (with_modules, without):
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    (with_modules / ".git/modules").mkdir()

    _, present = _run(tmp_path / "a", cwd=with_modules)
    _, absent = _run(tmp_path / "b", cwd=without)
    present_argv = _docker_run(present)
    absent_argv = _docker_run(absent)
    present_mounts = [
        present_argv[index + 1]
        for index, arg in enumerate(present_argv)
        if arg == "--mount"
    ]
    absent_mounts = [
        absent_argv[index + 1]
        for index, arg in enumerate(absent_argv)
        if arg == "--mount"
    ]

    modules = (
        f"type=bind,source={with_modules}/.git/modules,"
        "target=/workspace/.git/modules,readonly"
    )
    assert modules in present_mounts
    # Layered over .git and under the state volume, like the other read-only
    # parts.
    assert present_mounts.index(modules) < present_mounts.index(
        next(mount for mount in present_mounts if mount.startswith("type=volume"))
    )
    assert not any(".git/modules" in mount for mount in absent_mounts)


def test_wrapper_mounts_config_worktree_read_only_when_git_reads_it(
    tmp_path: Path,
) -> None:
    """`git sparse-checkout init --cone` sets extensions.worktreeConfig, and
    git then reads .git/config.worktree, which names a command through
    core.fsmonitor just as .git/config does. It is writable through the .git
    mount unless it is covered too, so the host would run what the agent put
    there."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "sparse-checkout", "init", "--cone"], cwd=repo, check=True)

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


def test_wrapper_refuses_a_config_worktree_symlink(tmp_path: Path) -> None:
    """Git would follow the symlink and run what it names, and the mount would
    follow it too, so a link the agent left is refused rather than trusted."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "sparse-checkout", "init", "--cone"], cwd=repo, check=True)
    (repo / ".git/config.worktree").unlink()
    (repo / ".git/config.worktree").symlink_to(repo / "evil")

    completed, invocations = _run(tmp_path / "a", cwd=repo)

    assert completed.returncode == 2
    assert "symlink" in completed.stderr
    assert not any(argv[0] == "run" for argv in invocations)


def test_wrapper_leaves_config_worktree_alone_without_worktree_config(
    tmp_path: Path,
) -> None:
    """Git ignores .git/config.worktree when extensions.worktreeConfig is off,
    so no mount is added and no file is created in the repository."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    _, invocations = _run(tmp_path / "a", cwd=repo)
    argv = _docker_run(invocations)
    mounts = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--mount"]

    assert not any("config.worktree" in mount for mount in mounts)
    assert not (repo / ".git/config.worktree").exists()


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
        f"type=bind,source={repo}/.git/worktrees,"
        "target=/workspace/.git/worktrees,readonly"
    ) in mounts
    assert len(mounts) == 6
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
    config or hook there. Detect it at exit instead of preventing it."""

    completed, _ = _run(tmp_path, agent=NESTED_GIT)

    assert completed.returncode == 0
    assert "nested git metadata" in completed.stderr
    assert str(tmp_path / "project/sub/.git") in completed.stderr
    assert "inspect it" in completed.stderr


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


def test_wrapper_keeps_the_container_unprivileged(tmp_path: Path) -> None:
    """Asserted rather than assumed: dropping one of these costs nothing that
    the rest of the suite would notice."""

    _, invocations = _run(tmp_path)
    argv = _docker_run(invocations)

    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--pids-limit") + 1] == "512"


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

    assert [argv[0] for argv in present] == ["image", "run"]
    assert [argv[0] for argv in missing] == ["image", "build", "run"]
    assert any(arg.endswith("/Dockerfile.pi") for arg in missing[1])


def test_wrapper_and_readme_agree_on_how_to_remove_old_images(
    tmp_path: Path,
) -> None:
    """`docker image prune` without -a removes only dangling images, and every
    old pi-sandbox tag is still referenced, so the printed command did
    nothing. The README and the wrapper have to give the same working one."""

    completed, _ = _run(tmp_path, inspect_status="1")
    command = "docker image prune -a --filter label=pi-sandbox.image=1"

    assert command in completed.stderr
    assert command in (ROOT / "README.md").read_text()
    assert "docker image prune --filter" not in completed.stderr
    assert "docker image prune --filter" not in (ROOT / "README.md").read_text()


def test_wrapper_tags_the_image_by_content_and_uses_it_for_inspect_and_run(
    tmp_path: Path,
) -> None:
    _, invocations = _run(tmp_path, inspect_status="1")

    assert [argv[0] for argv in invocations] == ["image", "build", "run"]
    inspected = invocations[0][2]
    built = invocations[1][invocations[1].index("--tag") + 1]
    ran = _image(invocations[2])

    assert re.fullmatch(r"pi-sandbox:[0-9a-f]{12}", inspected)
    assert inspected == built == ran


def test_wrapper_changes_the_image_tag_when_a_build_input_changes(
    tmp_path: Path,
) -> None:
    """A pull that touches the Dockerfile, the entrypoint or the fleet skill
    has to produce a tag that does not exist yet, or the build-if-missing
    check keeps running the image built from the old files."""

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
    for name in ("Dockerfile.pi", "entrypoint.sh", "herdr-fleet.md"):
        assert tag(tmp_path / name.replace(".", "-"), changed=name) != unchanged


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
    assert args == [f"AGENT_UID={real_uid}", f"AGENT_GID={real_gid}"]


def test_wrapper_builds_without_the_host_ids_off_linux(tmp_path: Path) -> None:
    """Docker Desktop maps the mounted files to the container user, so the
    defaults, and the state volumes already owned as 1001, still fit."""

    _, invocations = _run(tmp_path, inspect_status="1", uname="Darwin")

    assert "--build-arg" not in invocations[1]


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


def test_image_installs_the_extensions_after_becoming_the_agent_user() -> None:
    """Above `USER agent` they land root-owned in the layer that seeds the
    state volume, leaving the agent unable to write its own Pi config."""

    lines = (ROOT / "Dockerfile.pi").read_text().splitlines()
    installs = [
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("&& pi install npm:")
    ]

    assert installs
    assert min(installs) > lines.index("USER agent")


def test_image_builds_the_agent_user_with_build_argument_ids() -> None:
    """The wrapper passes the host uid and gid on Linux, so the defaults stay
    at the Docker Desktop value and the base image's node user, which sits at
    the common host uid, is removed before agent takes those ids."""

    dockerfile = (ROOT / "Dockerfile.pi").read_text()
    lines = dockerfile.splitlines()

    assert "ARG AGENT_UID=1001" in dockerfile
    assert "ARG AGENT_GID=1001" in dockerfile
    removed = next(
        index
        for index, line in enumerate(lines)
        if "userdel" in line and "node" in line
    )
    created = next(index for index, line in enumerate(lines) if "useradd" in line)
    assert removed < created
    # One groupadd, always named agent, so the COPY --chown below resolves for
    # any gid without a second branch or a getent lookup.
    assert "getent" not in dockerfile
    assert 'groupadd --non-unique --gid "$AGENT_GID" agent' in dockerfile
    assert "--gid agent agent" in dockerfile


def test_image_pins_herdr_and_checks_it_against_a_digest() -> None:
    """An unpinned or unverified download would put unreviewed code in a
    container that holds the API key."""

    dockerfile = (ROOT / "Dockerfile.pi").read_text()

    digests = re.findall(r"target=(linux-\S+); \\\n\s+sha=([0-9a-f]{64})", dockerfile)

    assert sorted(target for target, _ in digests) == ["linux-aarch64", "linux-x86_64"]
    assert len({digest for _, digest in digests}) == 2
    assert "releases/download/v$version/herdr-$target" in dockerfile
    assert "sha256sum --check" in dockerfile


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
    assert "/uv /uvx /usr/local/bin/" in dockerfile
    assert "install.sh" not in dockerfile


def test_image_pins_the_node_base_image_by_tag_and_digest() -> None:
    """A digest with no tag gives Dependabot no version to compare, so it
    never proposes an update. The tag in front of the same digest keeps the
    pin and lets the version move."""

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


def _run_entrypoint(tmp_path: Path, **env: str) -> _Entrypoint:
    """Run the entrypoint with stubs for the two programs it launches."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv_log = tmp_path / "pi.argv"
    env_log = tmp_path / "pi.env"
    (bin_dir / "herdr").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "pi").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" >{argv_log}\nenv >{env_log}\n'
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    completed = subprocess.run(
        [shutil.which("dash") or "sh", str(ENTRYPOINT), "--model", "kimi-k3"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), **env},
    )

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
    )


def test_the_entrypoint_is_a_valid_shell_script() -> None:
    """It runs as `/bin/sh` in the image, which is dash. On macOS `sh` is bash,
    which accepts bashisms the image would reject, so use dash where it is
    installed."""

    checked = subprocess.run(
        [shutil.which("dash") or "sh", "-n", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert ENTRYPOINT.read_text().startswith("#!/bin/sh\n")
    assert checked.returncode == 0, checked.stderr


def test_the_entrypoint_starts_a_herdr_server_and_then_becomes_pi() -> None:
    """Herdr's API commands do not start a server, so without this the agent's
    first herdr call is told `server_not_running`. The server has to be
    backgrounded and Pi has to replace the script: a server left in the
    foreground means Pi never starts at all, and running Pi as a child means
    the container's foreground process is a shell, which stops Herdr on the
    host from seeing a Pi agent in the pane."""

    script = ENTRYPOINT.read_text()
    dockerfile = (ROOT / "Dockerfile.pi").read_text()
    start = script[script.index("herdr server") :].split("\n")[0]

    assert 'ENTRYPOINT ["pi-sandbox-entrypoint"]' in dockerfile
    assert "COPY --chmod=0755 entrypoint.sh" in dockerfile
    assert start.rstrip().endswith(" &")
    assert script.index("herdr server") < script.index('exec pi "$@"')
    # A HERDR_SOCKET_PATH that survived would move this server's socket, and a
    # HERDR_PANE_ID would make Pi read itself as one of its own children.
    assert script.index("unset HERDR_SOCKET_PATH HERDR_PANE_ID") < script.index(
        "herdr server"
    )


def test_the_entrypoint_hands_pi_a_clean_herdr_environment(tmp_path: Path) -> None:
    """Read from Pi's own environment, since a variable that survived would
    point its herdr calls at the host pane's socket."""

    started = _run_entrypoint(
        tmp_path,
        HERDR_SOCKET_PATH="/tmp/elsewhere/custom.sock",
        HERDR_PANE_ID="hostpane:p9",
        HERDR_TAB_ID="hostpane:t1",
        HERDR_WORKSPACE_ID="hostpane",
    )

    assert started.argv == ["--model", "kimi-k3"]
    assert started.env["HERDR_ENV"] == "1"
    assert [name for name in started.env if name.startswith("HERDR_")] == ["HERDR_ENV"]


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


def test_image_ships_both_herdr_skills_as_the_agent_user() -> None:
    """Herdr's own skill for the CLI, and ours for what is different here. They
    land in the home directory, so they follow the extensions' ownership rule,
    and the copied one has to arrive owned by the user that reads it."""

    lines = (ROOT / "Dockerfile.pi").read_text().splitlines()
    skills = [
        index
        for index, line in enumerate(lines)
        if "/home/agent/.pi/agent/skills/" in line
    ]

    assert min(skills) > lines.index("USER agent")
    assert any("herdr --skill >" in line for line in lines)
    assert any(
        line.startswith("COPY --chown=agent:agent herdr-fleet.md") for line in lines
    )


def test_the_fleet_skill_tells_a_child_not_to_start_its_own_fleet() -> None:
    """Children load the same global skills directory, so the file has to be
    true for them too. A pane Herdr creates has HERDR_PANE_ID set; the
    container's foreground Pi does not."""

    skill = (ROOT / "herdr-fleet.md").read_text()

    assert skill.startswith("---\nname: herdr-fleet\n")
    assert "HERDR_PANE_ID" in skill.split("## ")[1]
    assert "do not start a fleet of your" in skill
