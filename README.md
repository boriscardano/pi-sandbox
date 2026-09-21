# pi-sandbox

Run the [Pi coding agent](https://pi.dev) in a Docker container that can see one
project and nothing else on your machine. Intended for untrusted or
lightly-trusted models, such as Chinese-hosted ones reached through OpenCode.

One shell script, one Dockerfile, an entrypoint and a skill file. Nothing to
install or configure.

## Requirements

Docker, and the opencode-go subscription key exported in your shell:

```sh
export OPENCODE_GO_API_KEY=$(pi auth print-api-key --provider opencode-go)
```

The wrapper requires it even when you mean to use a model from somewhere else,
since it is what the default needs.

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
straight into Pi, on `deepseek-v4.1-flash` from the opencode-go subscription.
Pass `--model` for another on that subscription, for example `pi --model
kimi-k3`, or name a provider yourself with `--provider` or a `provider/model`
string. Any other Pi flag goes straight through.

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
- inside a Git repository, `.git` itself is bind-mounted over the project, so
  it cannot be renamed away, and the parts of it that can name a command are
  then mounted read-only inside it: `.git/config`, `.git/hooks` and, when it
  exists, `.git/modules`.

Not mounted, and unreachable: your home directory, `~/.ssh`, `~/.aws`,
`~/.config`, the system keychain, every other project, the Docker socket, your
Herdr socket, and `~/.pi` on the host, which holds Pi's provider OAuth tokens.
The script refuses to start if the directory it would mount is your home
directory, contains it, is `/`, or has a comma in its path, which Docker's
`--mount` syntax would read as another field.

The container runs as non-root (`agent`, uid 1001) with `--cap-drop ALL`,
`--security-opt no-new-privileges` and `--pids-limit 512`. No `--privileged`,
and no host PID, IPC or network namespace. Outbound networking is on, since the
agent has to reach the model API. No port is published.

## The API keys

Both are forwarded by name, never by value:

```sh
--env OPENCODE_GO_API_KEY
--env OPENCODE_API_KEY
```

Docker takes the values from your environment at run time, so they are never
written into the image, the command line, or any file in this repository. A
name whose variable is unset is dropped rather than passed empty, so the second
one is optional. Nothing else from your environment is passed in: the container
starts from a clean environment and gets these, `TERM` and `COLORTERM`.

`OPENCODE_GO_API_KEY` is the one the default model needs, since
`deepseek-v4.1-flash` is on the opencode-go subscription and not on OpenCode
Zen. Pi reads that provider from `~/.pi/agent/auth.json` rather than from the
environment, so the entrypoint writes the forwarded key there, mode 0600,
leaving any other provider in the file alone. Set `OPENCODE_API_KEY` as well if
you want the models that are on Zen instead.

That file is in the state volume, so unlike the environment the key outlives
the container. `docker volume rm` is what removes it, as under State and reset.
Providers that need an OAuth flow still cannot work here: nothing mounts your
host `~/.pi`, and the sandbox has no browser to complete one.

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
The Herdr fleet below is a fourth way to do that, with whole Pi sessions
instead of subagents. Everything still dies with the container.

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

`.git` itself is bind-mounted over the project, and then `.git/config`,
`.git/hooks` and, when the repository has them, `.git/modules` are mounted
read-only inside it. The hooks directory is created first if it is missing.
Git runs commands named in those places, through `core.fsmonitor`,
`core.pager`, `core.hooksPath` and `filter.<name>.clean`, so leaving them
writable would let the agent leave a command behind that you run yourself with
the next `git status`.

Mounting `.git` matters on its own. Read-only mounts on the files inside it do
not stop `mv .git .git-old`, which succeeds while `.git` is still an ordinary
directory of the project, and the agent can then build a fresh `.git` with its
own config. A bind mount makes `.git` a mount point, which cannot be renamed
or removed, so that move fails with `Device or resource busy`.

The cost is that anything writing there fails in the sandbox: `git config`,
`git remote add`, installing a hook and most `git submodule` operations. Do
those on the host. `git add`, `git commit` and the rest still work, because
they write to `.git/index`, `.git/objects` and `.git/refs`, which stay
writable. A linked worktree or a submodule checkout keeps its real Git
directory outside the mount and gets no such protection, and Git does not work
in the sandbox for it anyway.

A repository the agent creates inside the project is not covered by any mount.
`git init sub` leaves `sub/.git/config` in your checkout, and on Docker Desktop
the host user owns the files the agent writes, so host Git trusts them and
would run a command named there. The wrapper cannot prevent that, so it warns
instead: when the container exits it looks for `.git` entries, `.git/config`
files and `.git/hooks` entries under the project whose ctime is newer than a
marker made before the container started, and prints what it found. It only
warns, and deletes nothing.

No credentials are mounted, so `git push` fails inside the sandbox by design.
Push from the host after reviewing the diff.

## Herdr

Two Herdrs are involved, and they never meet.

Yours, on the host: the script is named `pi` on purpose.
[Herdr](https://herdr.dev) identifies a pane's agent from the foreground job's
process arguments, so running this script in a pane makes it a first-class Pi
agent, with the agent list, idle and working detection, `herdr agent prompt`
and Ctrl-C all behaving as they do for a host agent.

```sh
herdr pane split --current --direction right --cwd ~/any/project --no-focus
herdr pane run <pane-id> '/path/to/pi-sandbox/pi'
```

For the same reason the script must not `exec docker`: that would replace the
`pi`-named process with `docker` and Herdr would see a plain shell. `test_pi.py`
pins both properties, and that the wrapper ignores the pane's own
`HERDR_SOCKET_PATH`. Mounting your socket would undo the sandbox rather than
extend it, since `herdr pane run` executes on the host, outside the container.

The container's own, for the agent: the image carries the `herdr` binary and
the entrypoint starts a server inside the container before Pi. That server
manages panes in the container and nothing else, so the agent can run a fleet
of Pi children of its own.

```sh
herdr workspace create --cwd /workspace --label review --no-focus
herdr agent start reviewer --kind pi --pane <pane-id> \
    -- --provider opencode-go --model deepseek-v4.1-flash
herdr agent prompt reviewer "Review the diff on this branch" --wait
herdr pane read <pane-id>
```

The provider and model flags are not optional: only the host wrapper applies
the default, and `deepseek-v4.1-flash` is on the opencode-go subscription
rather than on OpenCode Zen, so the provider has to be named with it.
Two skills in the image teach the agent all this: Herdr's own, printed by the
pinned binary at build time, and `herdr-fleet.md` from this repository, which
covers what is different here, including telling a child agent not to start a
fleet of its own. Like the extensions they live in the agent's home, so a
project whose state volume predates this image has the fleet but not the
instructions until you reset the volume.

You cannot see these panes from your own Herdr, so ask the container:

```sh
container=$(docker ps -q --filter label=pi-sandbox=1 | head -1)
docker exec "$container" herdr agent list
docker exec "$container" herdr pane read <pane-id>
```

`docker exec -it "$container" herdr` attaches a real client instead, which puts
a Herdr TUI inside your Herdr pane and gives the prefix key two owners.

The server keeps its state in `/home/agent/.config/herdr`, inside the project's
volume, and checks `herdr.dev` for updates on a timer like any other Herdr.

## Limitations

- The keys are inside the container, in the environment and, for the
  subscription, in `auth.json` in the state volume. Anything running there can
  read them and, since outbound network is open, send them elsewhere. This is
  inherent to running the agent in the container rather than proxying its
  traffic. Use keys you are willing to rotate.
- The mounted project is fully readable and writable by the agent. Only launch
  it from a project whose contents you are willing to send to the model
  provider, and keep a remote you can restore from.
- Protecting `.git` does not make the checkout safe to run. The agent can still
  write `.envrc` for direnv, a `Makefile`, `package.json` scripts, editor task
  files and the code itself, all of which your host may execute later. Review
  the diff before running anything from a checkout the agent has touched.
- Nested repositories the agent creates inside the project are detected at
  exit, not prevented. The check runs after `docker run` returns, so it does
  not run at all if the wrapper itself is killed, and anything it finds has
  already been written to your checkout. It also warns rather than fixing.
- If the script lives inside the project it mounts, the agent can edit the
  script that defines its own sandbox, which would take effect on the next
  launch. Keeping it in its own directory, as here, avoids that.
- A container is not a virtual machine. A container escape defeats this
  boundary. On macOS and Windows, Docker Desktop's own VM is a second layer.
- The agent can start other agents, through the extensions or the Herdr server
  in the container, and they spend the same key on work nobody is watching.
  That is the point of the feature, and also the cost of it. A child can start
  children of its own: the skill tells it not to, and nothing enforces that.
  Measured, a Pi session costs about fifteen of the container's 512 processes
  and a few hundred megabytes, and `docker run` sets no memory limit, so a
  runaway fleet reaches the machine's memory before it reaches `--pids-limit`.
- The image is roughly 1.3 GB, mostly Pi's npm dependency tree and the
  extensions. It carries Node 24, Python 3.11, uv, Git, ripgrep, fd and the
  23 MB Herdr binary.

## Tests

```sh
uv run --with pytest pytest
```

The tests use a fake `docker` on `PATH`, so they neither build an image nor
start a container. They assert the isolation properties: the expected mounts
and no others, the keys forwarded by name and never by value, the sandboxing
flags present and no privileged or host namespace flags, the home-directory
refusal, and that a Herdr pane's socket stays on the host. The rest read
`Dockerfile.pi` or run `entrypoint.sh` against stub binaries and a throwaway
home, for the default model, where the subscription key is written, and what
happens when the agent has ruined the file it is written to.

## License

Copyright 2026 pi-sandbox contributors. Licensed under Apache-2.0, see
[LICENSE](LICENSE).
