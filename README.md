# pi-sandbox

Run the [Pi coding agent](https://pi.dev) in a Docker container that can see one
project and nothing else on your machine. It is for untrusted or
lightly-trusted models, such as Chinese-hosted ones reached through OpenCode.

https://github.com/user-attachments/assets/af0dbce4-7b3f-4417-9e72-bc65415c8f1d

One shell script, one Dockerfile and an entrypoint. Nothing to install or
configure.

The container gets the project you launched from, read-write, and a private
volume for Pi's own state. It does not get your home directory, your SSH or
cloud credentials, the Docker socket, your Herdr socket, or any other project.
It does not make the checkout safe to run afterwards, and the API keys are
readable inside it. [SECURITY.md](SECURITY.md) says what does and does not
count as a vulnerability.

## Quick start

```sh
cd ~/any/project
# needs Pi installed on the host, see Requirements for other ways
export OPENCODE_GO_API_KEY=$(pi auth print-api-key --provider opencode-go)
/path/to/pi-sandbox/pi
```

The first run builds the image, which takes a few minutes. After that it starts
straight into Pi, on `deepseek-v4.1-flash` from the opencode-go subscription.
That model is not on OpenCode Zen, so the wrapper names the provider alongside
it. Pass `--model` for another model on that subscription, for example `pi
--model kimi-k3`. Any other Pi flag goes straight through. Requirements covers
where the key comes from, and Images and rebuilds the rest.

An alias is convenient:

```sh
alias pis='/path/to/pi-sandbox/pi'
```

Do not put the script on your `PATH` as `pi`, or it will shadow a host Pi
install for every project.

## Requirements

Docker. The default model needs `OPENCODE_GO_API_KEY` in your environment. The
command in Quick start gets that value from a host Pi install with `pi auth
print-api-key --provider opencode-go`, so host Pi is needed for that form.
Without it, export the same value from wherever you keep it, such as a password
manager or a secret store.

That key is needed for the default model only. Naming another provider with
`--provider` or a `provider/model` string runs without it, and Pi then uses
that provider's own key, forwarded through `OPENCODE_API_KEY` or
`PI_SANDBOX_ENV`:

```sh
export OPENCODE_API_KEY=your-opencode-key
pi --model opencode/glm-5.3
```

pi-sandbox is developed and verified on macOS with Docker Desktop. Windows with
Docker Desktop works the same way: both map bind-mount ownership to the
container user, so the agent can write to `/workspace`. On native Linux the
mounted files keep their host UID and GID, so the wrapper builds the image with
your own, and the container user matches you. Each pair of ids gets its own
image tag. Do not run it as root on Linux: that would make the container user
UID 0 and leave root-owned files in the project. Rootless Docker and Podman are
untested.

## Images and rebuilds

Edits to `Dockerfile.pi` or `entrypoint.sh` rebuild automatically on the next
launch, and so does a new Pi release on npm. If the npm lookup fails, an
existing image under the current tag is used. With none, as on the first
launch or the first after an edit to those files, the wrapper stops and says
so. A failed rebuild falls back to the image the tag already held.

After the session the wrapper removes every other `pi-sandbox` image it can,
and the build cache their Pi installs left, so nothing accumulates. That
includes images from another checkout or, on Linux, another host user, whose
next launch then rebuilds.

The container is removed however the session ends: Pi exiting, a closed pane,
a SIGHUP, SIGTERM or SIGKILL to the wrapper, or the docker client dying.
`e2e.py` checks every one of those paths on real Docker. A SIGKILL to the
wrapper's whole process group leaves the container running until the next
launch in any project removes it, where `ps` works.
[docs/design.md](docs/design.md#removing-the-container) explains how, and how
sessions of other users or machines are left alone.

## What the container can and cannot see

Mounted:

- the project you launched from, at `/workspace`, read-write. Inside a Git
  repository this is the repository root, found from the nearest `.git` up the
  tree. With no `.git` above it, the launch directory itself is mounted, with
  a warning naming it.
- a per-project Docker volume at `/home/agent` for Pi's own state.
- inside a Git repository, `.git` itself is bind-mounted over the project, and
  `.git/config`, `.git/config.worktree`, `.git/hooks`, `.git/worktrees`,
  `.git/modules` and `.git/commondir` are mounted read-only inside it. Git
  below says what that means in practice.

Not mounted, and unreachable: your home directory, `~/.ssh`, `~/.aws`,
`~/.config`, the system keychain, every other project, the Docker socket, your
Herdr socket, and `~/.pi` on the host, which holds Pi's provider OAuth tokens.
The script refuses to start if the directory it would mount is your home
directory, contains it, is `/`, or has a comma in its path, which Docker's
`--mount` syntax would read as another field.

The container runs as non-root (`agent`, your own uid and gid on Linux and
1001 on Docker Desktop) with `--cap-drop ALL`,
`--security-opt no-new-privileges` and `--pids-limit 512`, and may use half
the CPUs Docker has (at least one), so a spinning agent cannot hold every core.
Its terminal output is not written to a Docker log file (`--log-driver none`),
which would otherwise grow without limit. No `--privileged`, and no host PID,
IPC or network namespace. Outbound networking is on, since the agent has to
reach the model API. No port is published.

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

`OPENCODE_GO_API_KEY` is the one the default model needs, and the wrapper
checks for it whenever the run reaches opencode-go. The entrypoint writes it to
`~/.pi/agent/auth.json` in the state volume, mode 0600, because Pi reads that
provider from there, so the key outlives the container until you remove the
volume. Set `OPENCODE_API_KEY` as well if you want the models that are on Zen
instead. Providers that need an OAuth flow cannot work here: nothing mounts
your host `~/.pi`, and the sandbox has no browser to complete one.

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
`pi --resume` work without exposing your host `~/.pi`. The volume also holds
`UV_PROJECT_ENVIRONMENT`, so a Linux `uv sync` in the sandbox cannot overwrite a
macOS or Windows virtualenv in the mounted checkout. uv's cache stays in the
container's `/tmp` and goes with it. uv uses Debian's Python 3.11 for a
project that accepts it and the image's Python 3.14 for one that needs 3.12 or
newer, so either keeps only its venv in the volume. A project that pins
another Python, in `.python-version` or `requires-python`, makes uv download
that one into its volume, about 90 MB. A volume created by an earlier
pi-sandbox, one that kept uv's cache in `~/uv-cache`, keeps that cache and its
uv-downloaded Python until you reset it.

```sh
docker volume ls --filter name=pi-sandbox-     # list
docker system df -v | grep pi-sandbox-         # sizes
docker volume rm pi-sandbox-<project>-<hash>   # reset one project
```

Every worktree is a project of its own, so volumes pile up as worktrees come
and go. Each volume is labelled with its project's path, and this removes the
ones whose directory no longer exists (a volume in use is refused and kept):

```sh
docker volume ls -q --filter label=pi-sandbox.project | while read -r v; do [ -d "$(docker volume inspect -f '{{index .Labels "pi-sandbox.project"}}' "$v")" ] || docker volume rm "$v"; done
```

Run it on the machine whose Docker created the volumes: it checks the paths
there, and a project on a disk that is not mounted counts as gone, as does
a worktree inside another project whose agent renamed its directory. Volumes
created before
the label existed are not listed by it. Remove those by name, or every unused
one at once with
`docker volume ls -q --filter name=pi-sandbox- | xargs docker volume rm`, which
also drops the sessions of projects you still have.

### What an agent installs

Whether a tool survives the container exiting depends only on where it writes.

Kept, because it lands in the volume under `/home/agent`: `rustup` and
`cargo install`, `uv tool install`, `pip install --user`, npm's cache, and Pi's
own sessions. `~/.local/bin` and `~/.cargo/bin` are on `PATH`, so these are
found again on the next run rather than reinstalled.

Lost, because `--rm` deletes the container's writable layer: anything in
`/usr/local`, `/usr/bin` or `/opt`, and anything in `/tmp`, uv's cache among
it. A venv already synced needs no cache, so only a changed lock downloads
again. The agent cannot
install system packages at all, since it is not root and there is no `sudo`, so
`apt-get install` fails. Put anything system-level in `Dockerfile.pi` instead.

These volumes grow. A project whose agent installed a Rust toolchain reached
2.6 GB.

### Careful with a repo where a build writes into the checkout

Build output under `/workspace`, such as Cargo's `target/`, is written into
your real checkout, not the volume, and will be Linux binaries sitting next to
your host build artifacts.

Containers carry the label `pi-sandbox=1` and are removed when the session
ends, as Images and rebuilds describes. To get a shell in a running sandbox:

```sh
docker exec -it "$(docker ps -q --filter label=pi-sandbox=1 | head -1)" bash
```

## Extensions

The entrypoint installs four Pi extensions from npm at their newest release:

- `npm:pi-subagents`, delegation to subagents and scripted multi-agent
  workflows, under `/subagents`.
- `npm:@tintinweb/pi-subagents`, a separate project with the same base name,
  offering a fleet view and mid-run steering, under `/agents`.
- `npm:pi-background-tasks`, durable background shell tasks and read-only
  delegated agents, under `/bg`.
- `npm:pi-extension-manager`, an interactive manager for the above, under
  `/extensions`.

The subagent and background-task extensions let the agent start work that
keeps running while you are not watching, with the same key, open network,
`--pids-limit` and CPU share as the foreground session. Everything still dies
with the container.

They live in the agent's home, so they reach a project through its state
volume. A new project's first start downloads them. Offline, that start
proceeds without them, and the next start with a network installs them. A
start installs any of the four that is missing and updates every unpinned
extension to its newest release. After an update that worked, this is
skipped for 24 hours unless one of the four is missing. Delete
`~/.pi-sandbox-extensions-updated` in the volume to force it on the next
start. An extension you added yourself
as `npm:<package>@<version>` stays at that version, while a pin on one of the
four is undone at the next update.

Pi's own subcommands run against the volume rather than your host Pi, so
`pi list` shows what a project actually has and `pi install npm:<package>` adds
one. `install`, `remove`, `uninstall`, `list`, `config` and `auth` all work this
way. So does `pi update --extensions`, but bare `pi update` targets Pi itself,
which lives outside the volume in a root-owned directory the agent cannot
write. Resetting the volume, as under State and reset, is the other way to pick
up a change.

Change the set by editing the four names in `entrypoint.sh`, which is where the
install list lives. To drop one for good, run `pi remove npm:<package>` and
delete its name from that list, then relaunch, which rebuilds the image.
pi-sandbox runs the newest releases rather than reviewed pins, so a new
upstream release reaches the sandbox, and the API key it holds, without review.
[docs/design.md](docs/design.md#extension-updates) has the details.

## Git

`git status`, `diff`, `add` and `commit` work in `/workspace`, and write
straight through to your real checkout, so a commit made in the sandbox is
immediately in your local repository. `/workspace` is marked a safe directory
because the mounted files belong to the host user, and commits use the identity
`Pi Sandbox <pi-sandbox@localhost>` rather than yours. Override it per commit
with `git -c user.name=... -c user.email=...`.

What fails, because the protected parts of `.git` are read-only: `git config`,
`git remote add`, installing a hook, most `git submodule` operations and `git
worktree add`. Do those on the host. `git add`, `git commit` and the rest still
work, because they write to `.git/index`, `.git/objects` and `.git/refs`, which
stay writable. Launched from a linked worktree or a submodule checkout, the
sandbox gives its Git directory no protection, and Git does not work there
anyway. No credentials are mounted,
so `git push` fails inside the sandbox by design. Push from the host after
reviewing the diff.

The wrapper refuses to start on a symlink at `.git` or at any of the
read-only paths, a `.git/config.worktree` or `.git/commondir` that is not an
ordinary file, and a `.git/commondir` that holds anything but `.`.

Warned at exit, not prevented, when created or changed during the session:

- a repository the agent made inside the project, such as `git init sub`, or a
  root `.git` in a project that was not a repository at launch.
- a planted rebase, merge or cherry-pick: `.git/rebase-merge`,
  `.git/rebase-apply`, `.git/sequencer`, `.git/MERGE_HEAD` or
  `.git/CHERRY_PICK_HEAD`.

Host Git would run a command named in either. The wrapper only warns, and
deletes nothing. Inspect a nested repository before running Git in it, and
abort a planted operation rather than continuing it.

[docs/design.md](docs/design.md#protecting-git) explains why each of these is
needed.

## Herdr

The script is named `pi` on purpose. [Herdr](https://herdr.dev) identifies a
pane's agent from the foreground job's process arguments, so running this
script in a pane makes it a first-class Pi agent, with the agent list, idle and
working detection, `herdr agent prompt` and Ctrl-C all behaving as they do for
a host agent.

```sh
herdr pane split --current --direction right --cwd ~/any/project --no-focus
herdr pane run <pane-id> '/path/to/pi-sandbox/pi'
```

The script must not `exec docker`, or Herdr would no longer see a `pi`
process. The wrapper ignores a pane's own `HERDR_SOCKET_PATH`, since `herdr
pane run` through that socket would execute on the host.
[docs/design.md](docs/design.md#herdr) says why.

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
  files and the code itself, all of which your host may execute later. The
  same goes for what your own coding agents trust in a checkout: hooks in
  `.claude/settings.json`, MCP servers in `.mcp.json`, and instructions in
  `CLAUDE.md` or `AGENTS.md`. Review the diff before running anything, or
  starting another agent, in a checkout the agent has touched.
- The agent can write escape sequences to the terminal you launched it from.
  The container's terminal device is owned by the user Pi and its tools run
  as, so any command the agent runs can open it. A sequence can retitle the
  pane, draw text that looks like your shell, add links, and on a terminal
  that honours OSC 52, replace your clipboard. Turn off OSC 52 clipboard
  writes in your terminal, and do not paste from a clipboard you did not fill
  yourself after a session.
- Nested repositories, and the rebase, merge or cherry-pick state the agent
  can plant in `.git`, are detected at exit, not prevented. The checks run
  after `docker run` returns, so they do not run at all if the wrapper itself
  is killed, and anything they find has already been written to your checkout.
  They warn rather than fixing.
- If the script lives inside the project it mounts, the agent can edit the
  script that defines its own sandbox, which would take effect on the next
  launch. Keeping it in its own directory, as here, avoids that.
- A container is not a virtual machine. A container escape defeats this
  boundary. On macOS and Windows, Docker Desktop's own VM is a second layer.
- The agent can start other agents through the extensions, and they spend the
  same key on work nobody is watching. That is the point of the feature, and
  also the cost of it. A child can start children of its own, and nothing
  enforces a limit beyond `--pids-limit` and the container's 8 GB memory
  limit, plus as much again in swap where the host has it, which stops a
  runaway fan-out before it takes the machine's memory. Docker Desktop's VM may
  have less than that, in which case its own limit is the one reached first.
- The image is roughly 1.2 GB, mostly Pi's npm dependency tree. It carries
  Node 24, Python 3.11 and 3.14, uv, Git, ripgrep and fd.

These are accepted limits, and [SECURITY.md](SECURITY.md) defines what does
count as a vulnerability here and how to report it privately.

## Tests

`e2e.py` runs the real wrapper on real Docker, in CI on Linux and by hand
with `python3 e2e.py` on Docker Desktop. It starts Pi in a pseudo-terminal in a
throwaway repository, checks from inside the container that `/workspace` is
writable while `.git/config`, `config.worktree`, `commondir`, `hooks`,
`worktrees` and `modules` are not and `.git` cannot be moved, that a file the
agent writes is owned by you, that the memory and CPU limits are set and that
the terminal output is not logged, then ends a session each way one can end and
checks that no container is left. Last it puts an image of an older Pi under
the tag, starts a session, which rebuilds it, and checks that no Pi install
build cache is left unused. It writes its findings to `e2e-report.txt`, which
CI keeps as an artifact. It also checks
that uv picks Debian's Python 3.11 for a project that accepts it, syncs a
Python project and fails if the state volume grows more than 30 MB. No API key
and no model call. `python3 e2e.py --storage` runs 30 sessions in turn and then 10 at
once in one project, all writing the key, and checks that
what pi-sandbox keeps on disk stays flat. It is not run in CI.

The tests in `test_pi.py` use a fake `docker` on `PATH`, so they neither build
an image nor start a container. They assert the isolation properties: the expected mounts
and no others, the keys forwarded by name and never by value, the sandboxing
flags present and no privileged or host namespace flags, the home-directory
refusal, and that a Herdr pane's socket stays on the host. The rest run
`entrypoint.sh` against stub binaries and a throwaway home, for the default
model, where the subscription key is written, what happens when the agent has
ruined the file it is written to, and when the extensions are updated. Two
check that `Dockerfile.pi` pins its base image and uv by digest, a supply chain
property no behaviour test can see.

## Contributing

Tests run with `uv run --with pytest pytest -q`, and `python3 e2e.py` runs the
real wrapper on Docker. The shell scripts must pass `shellcheck` and run under
dash. [AGENTS.md](AGENTS.md) holds the security
lessons a change here has to follow, including that every behaviour gets a
test proven to fail when the fix is reverted and that the wrapper stays POSIX
sh which does not `exec docker`. Report a vulnerability privately as
[SECURITY.md](SECURITY.md) describes rather than in a public issue.

## License

Copyright 2026 pi-sandbox contributors. Licensed under Apache-2.0, see
[LICENSE](LICENSE).
