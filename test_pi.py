"""Verify that the host Pi sandbox wrapper builds an isolating docker run."""

import os
import re
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
    also: dict[str, str] | None = None,
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
    env.update(also or {})

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

    image = "pi-sandbox:local"
    subcommand_argv = _docker_run(subcommand)
    prompt_argv = _docker_run(prompt)

    assert subcommand_argv[subcommand_argv.index(image) + 1 :] == [
        "install",
        "npm:example",
    ]
    # A word that merely looks like one is still an ordinary prompt.
    assert prompt_argv[prompt_argv.index(image) + 1 :] == [
        "--provider",
        "opencode",
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


def test_wrapper_stays_in_the_process_tree_so_herdr_can_identify_pi(
    tmp_path: Path,
) -> None:
    """An `exec docker ...` would replace the only `pi`-named process."""

    _run(tmp_path)

    assert "/pi" in _docker_run_parent(tmp_path / "docker.log")


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


def test_image_pins_herdr_and_checks_it_against_a_digest() -> None:
    """An unpinned or unverified download would put unreviewed code in a
    container that holds the API key."""

    dockerfile = (ROOT / "Dockerfile.pi").read_text()

    digests = re.findall(r"target=(linux-\S+); \\\n\s+sha=([0-9a-f]{64})", dockerfile)

    assert sorted(target for target, _ in digests) == ["linux-aarch64", "linux-x86_64"]
    assert len({digest for _, digest in digests}) == 2
    assert "releases/download/v$version/herdr-$target" in dockerfile
    assert "sha256sum --check" in dockerfile


def _generated_file(path: str) -> str:
    """A file `Dockerfile.pi` writes with printf, reassembled from its
    arguments. Anchored on the redirect, so it cannot pick up another block."""

    lines = (ROOT / "Dockerfile.pi").read_text().splitlines()
    end = next(
        index
        for index, line in enumerate(lines)
        if line.strip().removesuffix(" \\") == f"> {path}"
    )
    start = next(
        index for index in range(end, -1, -1) if "printf '%s\\n'" in lines[index]
    )
    body = [line.strip().removesuffix(" \\")[1:-1] for line in lines[start + 1 : end]]
    return "\n".join(body) + "\n"


ENTRYPOINT = "/usr/local/bin/pi-sandbox-entrypoint"


def test_the_generated_entrypoint_is_a_valid_shell_script() -> None:
    """It is authored as printf arguments, where a lost quote or continuation
    would otherwise surface only at `docker run`."""

    script = _generated_file(ENTRYPOINT)
    checked = subprocess.run(
        ["sh", "-n"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert script.startswith("#!/bin/sh\n")
    assert checked.returncode == 0, checked.stderr


def test_the_entrypoint_starts_a_herdr_server_and_then_becomes_pi() -> None:
    """Herdr's API commands do not start a server, so without this the agent's
    first herdr call is told `server_not_running`. The server has to be
    backgrounded and Pi has to replace the script: a server left in the
    foreground means Pi never starts at all, and running Pi as a child means
    the container's foreground process is a shell, which stops Herdr on the
    host from seeing a Pi agent in the pane."""

    script = _generated_file(ENTRYPOINT)
    start = script[script.index("herdr server") :].split("\n")[0]

    assert (
        'ENTRYPOINT ["pi-sandbox-entrypoint"]' in (ROOT / "Dockerfile.pi").read_text()
    )
    assert start.rstrip().endswith(" &")
    assert script.index("herdr server") < script.index('exec pi "$@"')
    # A HERDR_SOCKET_PATH that survived would move this server's socket, and a
    # HERDR_PANE_ID would make Pi read itself as one of its own children.
    assert script.index("unset HERDR_SOCKET_PATH HERDR_PANE_ID") < script.index(
        "herdr server"
    )


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
