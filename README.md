# pi-sandbox

Run the [Pi coding agent](https://pi.dev) in a Docker container that can see one
project and nothing else on your machine. Intended for untrusted or
lightly-trusted models, such as Chinese-hosted ones reached through OpenCode.

One shell script and one Dockerfile. Nothing to install or configure.

## Requirements

Docker, and `OPENCODE_API_KEY` exported in your shell.

Developed and verified on macOS with Docker Desktop. It should work on Windows
with Docker Desktop for the same reason: both map bind-mount ownership to the
container user, so the agent can write to `/workspace`. On native Linux there
is no such mapping, the mounted files keep their host UID, and the container
user (1001) would find `/workspace` read-only. Linux needs UID handling that
this script does not yet do, so treat it as unsupported for now.

## Use

```sh
cd ~/any/project
/path/to/pi-sandbox/pi
```

The first run builds the image, which takes a few minutes. After that it starts
straight into Pi, on `deepseek-v4.1-flash` through OpenCode. Pass `--model` for
another, for example `pi --model kimi-k3`, or any other Pi flag.

An alias is convenient:

```sh
alias pis='/path/to/pi-sandbox/pi'
```

Do not put the script on your `PATH` as `pi`, or it will shadow a host Pi
install for every project.

Rebuild after editing the Dockerfile:

```sh
docker build --file Dockerfile.pi --tag pi-sandbox:local .
```

## What the container can and cannot see

Mounted:

- the project you launched from, at `/workspace`, read-write. Inside a Git
  repository this is the repository root, even when you launch from a
  subdirectory.
- a per-project Docker volume at `/home/agent` for Pi's own state.

Not mounted, and unreachable: your home directory, `~/.ssh`, `~/.aws`,
`~/.config`, the system keychain, every other project, the Docker socket, and
`~/.pi` on the host, which holds Pi's provider OAuth tokens. The script refuses
to start if the directory it would mount is your home directory, contains it,
or is `/`.

The container runs as non-root (`agent`, uid 1001) with `--cap-drop ALL`,
`--security-opt no-new-privileges` and `--pids-limit 512`. No `--privileged`,
and no host PID, IPC or network namespace. Outbound networking is on, since the
agent has to reach the model API. No port is published.

## The API key

`OPENCODE_API_KEY` is forwarded by name:

```sh
--env OPENCODE_API_KEY
```

Docker takes the value from your environment at run time, so it is never
written into the image, the command line, or any file in this repository.
Nothing else from your environment is passed in: the container starts from a
clean environment and gets only this key, `TERM` and `COLORTERM`.

Only OpenCode Zen (provider `opencode`) works in the sandbox, because it is the
provider that authenticates from this variable. Providers that read
`~/.pi/agent/auth.json`, including `opencode-go`, cannot work here by design.

### Forwarding other variables

Name them in `PI_SANDBOX_ENV`, space or comma separated, values never included:

```sh
PI_SANDBOX_ENV='TYPESAFE_API_KEY' pi
```

Or bake it into your alias:

```sh
alias pis='PI_SANDBOX_ENV=TYPESAFE_API_KEY /path/to/pi-sandbox/pi'
```

Nothing is forwarded unless named, and names are validated so a value cannot
smuggle in another `docker` flag. Every variable you add is readable by whatever
runs in the container, which has open outbound network, so add only what the
agent genuinely needs and use credentials you are willing to rotate.

## State and reset

Pi's config and sessions live in a Docker volume named
`pi-sandbox-<project>-<hash>`, one per project, so `pi --continue` and
`pi --resume` work without exposing your host `~/.pi`. The volume also holds the
uv cache and `UV_PROJECT_ENVIRONMENT`, so a Linux `uv sync` in the sandbox
cannot overwrite a macOS or Windows virtualenv in the mounted checkout.

```sh
docker volume ls --filter name=pi-sandbox-     # list
docker system df -v | grep pi-sandbox-         # sizes
docker volume rm pi-sandbox-<project>-<hash>   # reset one project
```

### What an agent installs

Whether a tool survives the container exiting depends only on where it writes.

Kept, because it lands in the volume under `/home/agent`: `rustup` and
`cargo install`, `uv tool install`, `pip install --user`, npm's cache, and Pi's
own sessions. `~/.local/bin` and `~/.cargo/bin` are on `PATH`, so these are
found again on the next run rather than reinstalled.

Lost, because `--rm` deletes the container's writable layer: anything in
`/usr/local`, `/usr/bin` or `/opt`, and anything in `/tmp`. The agent cannot
install system packages at all, since it is not root and there is no `sudo`, so
`apt-get install` fails. Put anything system-level in `Dockerfile.pi` instead.

These volumes grow. A project whose agent installed a Rust toolchain reached
2.6 GB.

### Careful with a repo where a build writes into the checkout

Build output under `/workspace`, such as Cargo's `target/`, is written into
your real checkout, not the volume, and will be Linux binaries sitting next to
your host build artifacts.

Containers run with `--rm` and carry the label `pi-sandbox=1`, so nothing is
left behind. To get a shell in a running sandbox:

```sh
docker exec -it "$(docker ps -q --filter label=pi-sandbox=1 | head -1)" bash
```

## Extensions

The image ships four Pi extensions, pinned in `Dockerfile.pi`:

- `npm:pi-subagents`, delegation to subagents and scripted multi-agent
  workflows, under `/subagents`.
- `npm:@tintinweb/pi-subagents`, a separate project with the same base name,
  offering a fleet view and mid-run steering, under `/agents`.
- `npm:pi-background-tasks`, durable background shell tasks and read-only
  delegated agents, under `/bg`.
- `npm:pi-extension-manager`, an interactive manager for the above, under
  `/extensions`.

The subagent and background-task extensions let the agent start work that
keeps running while you are not watching the pane, with the same key and the
same open network as the foreground session, and sharing its `--pids-limit`.
Everything still dies with the container.

They live in the agent's home, so they reach a project through that project's
state volume and one whose volume predates them will not have them. Pi's own
subcommands run against that volume rather than your host Pi, so `pi list`
shows what a project actually has and `pi install npm:<package>@<version>`
adds to it. Pin the version there too, for the reason below. `install`,
`remove`, `uninstall`, `list`, `config` and `auth` all work this way. So does
`pi update --extensions`, but bare `pi update` targets Pi itself, which lives
outside the volume in a root-owned directory the agent cannot write.
Resetting the volume, as under State and reset, is the other way to pick up a
change.

Change the set by editing the `pi install` lines in `Dockerfile.pi` and
rebuilding. Versions are pinned there on purpose, for the reason in the
comment beside them.

## Git

`git status`, `diff`, `add` and `commit` work in `/workspace`, and write
straight through to your real checkout, so a commit made in the sandbox is
immediately in your local repository. `/workspace` is marked a safe directory
because the mounted files belong to the host user, and commits use the identity
`Pi Sandbox <pi-sandbox@localhost>` rather than yours. Override it per commit
with `git -c user.name=... -c user.email=...`.

No credentials are mounted, so `git push` fails inside the sandbox by design.
Push from the host after reviewing the diff.

## Herdr

The script is named `pi` on purpose. [Herdr](https://herdr.dev) identifies a
pane's agent from the foreground job's process arguments, so running this script
in a pane makes it a first-class Pi agent: the agent list, idle and working
detection, `herdr agent prompt` and Ctrl-C all behave as they do for a host
agent.

```sh
herdr pane split --current --direction right --cwd ~/any/project --no-focus
herdr pane run <pane-id> '/path/to/pi-sandbox/pi'
```

For the same reason the script must not `exec docker`: that would replace the
`pi`-named process with `docker` and Herdr would see a plain shell. `test_pi.py`
pins both properties.

## Limitations

- The key is inside the container. Anything running there can read
  `OPENCODE_API_KEY` and, since outbound network is open, send it elsewhere.
  This is inherent to running the agent in the container rather than proxying
  its traffic. Use a key you are willing to rotate.
- The mounted project is fully readable and writable by the agent. Only launch
  it from a project whose contents you are willing to send to the model
  provider, and keep a remote you can restore from.
- If the script lives inside the project it mounts, the agent can edit the
  script that defines its own sandbox, which would take effect on the next
  launch. Keeping it in its own directory, as here, avoids that.
- A container is not a virtual machine. A container escape defeats this
  boundary. On macOS and Windows, Docker Desktop's own VM is a second layer.
- The image is roughly 1.3 GB, mostly Pi's npm dependency tree and the
  extensions. It carries Node 24, Python 3.11, uv, Git, ripgrep and fd.

## Tests

```sh
uv run --with pytest pytest
```

The tests use a fake `docker` on `PATH`, so they neither build an image nor
start a container. They assert the isolation properties: only the two expected
mounts, the key forwarded by name and never by value, no privileged or host
namespace flags, the home-directory refusal, and the two Herdr requirements
above.

## License

Copyright 2026 pi-sandbox contributors. Licensed under Apache-2.0, see
[LICENSE](LICENSE).
