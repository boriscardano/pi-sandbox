"""Verify that the host Pi sandbox wrapper builds an isolating docker run."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
WRAPPER = ROOT / "pi"
FAKE_KEY = "opencode-test-key-do-not-leak"

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
exit 0
"""


def _run(
    tmp_path: Path,
    *args: str,
    key: str | None = FAKE_KEY,
    inspect_status: str = "0",
    cwd: Path | None = None,
    home: Path | None = None,
    forward: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"

    project = cwd if cwd is not None else tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home if home is not None else tmp_path / "home"),
        "PI_SANDBOX_FAKE_LOG": str(log),
        "PI_SANDBOX_FAKE_INSPECT": inspect_status,
    }
    if key is not None:
        env["OPENCODE_API_KEY"] = key
    if forward is not None:
        env["PI_SANDBOX_ENV"] = forward
        env["TYPESAFE_API_KEY"] = "typesafe-test-key-do-not-leak"

    completed = subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        cwd=project,
        env=env,
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


def test_wrapper_is_named_pi_so_herdr_identifies_the_pane_agent() -> None:
    assert WRAPPER.name == "pi"
    assert os.access(WRAPPER, os.X_OK)


def test_wrapper_fails_closed_without_the_opencode_key(tmp_path: Path) -> None:
    completed, invocations = _run(tmp_path, key=None)

    assert completed.returncode == 2
    assert "OPENCODE_API_KEY is required" in completed.stderr
    assert invocations == []


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

    assert argv[argv.index("--env") + 1] == "OPENCODE_API_KEY"
    assert FAKE_KEY not in " ".join(argv)
    assert FAKE_KEY not in output
    assert "--env-file" not in argv
    # Nothing beyond the key, TERM and COLORTERM is forwarded by default.
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--env"] == [
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


def test_wrapper_runs_pi_in_workspace_with_the_caller_arguments(
    tmp_path: Path,
) -> None:
    _, invocations = _run(tmp_path, "--model", "opencode/glm-5.3")
    argv = _docker_run(invocations)

    assert argv[argv.index("--workdir") + 1] == "/workspace"
    assert argv[-3:] == [
        "pi-sandbox:local",
        "--model",
        "opencode/glm-5.3",
    ]
    assert "--rm" in argv
    assert "--init" in argv


def test_wrapper_defaults_to_a_chinese_hosted_model(tmp_path: Path) -> None:
    _, invocations = _run(tmp_path)
    argv = _docker_run(invocations)
    image = "pi-sandbox:local"

    assert argv[argv.index(image) + 1 :] == [
        "--provider",
        "opencode",
        "--model",
        "deepseek-v4.1-flash",
    ]


def test_wrapper_keeps_a_model_the_caller_asked_for(tmp_path: Path) -> None:
    _, chosen = _run(tmp_path, "--model", "kimi-k3")
    _, other_flag = _run(tmp_path / "b", "--models", "glm-5.3,kimi-k3")

    image = "pi-sandbox:local"
    chosen_argv = _docker_run(chosen)
    other_argv = _docker_run(other_flag)

    assert chosen_argv[chosen_argv.index(image) + 1 :] == ["--model", "kimi-k3"]
    assert "deepseek-v4.1-flash" not in " ".join(chosen_argv)
    # --models is a different flag, so the default model still applies.
    assert "deepseek-v4.1-flash" in other_argv


def test_wrapper_builds_the_image_only_when_it_is_missing(tmp_path: Path) -> None:
    _, present = _run(tmp_path)
    _, missing = _run(tmp_path / "missing", inspect_status="1")

    assert [argv[0] for argv in present] == ["image", "run"]
    assert [argv[0] for argv in missing] == ["image", "build", "run"]
    assert any(arg.endswith("/Dockerfile.pi") for arg in missing[1])


def test_wrapper_stays_in_the_process_tree_so_herdr_can_identify_pi(
    tmp_path: Path,
) -> None:
    """An `exec docker ...` would replace the only `pi`-named process."""

    _run(tmp_path)

    assert "/pi" in _docker_run_parent(tmp_path / "docker.log")
