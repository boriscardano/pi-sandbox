# pi-sandbox

Run the [Pi coding agent](https://pi.dev) in a Docker container that can see one
project and nothing else on your machine. It is for untrusted or
lightly-trusted models, such as Chinese-hosted ones reached through OpenCode.

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
launch. The image is tagged by the contents of those files, so a changed file
produces a tag that does not exist yet and the wrapper builds it before
starting. It also rebuilds when npm answers with a newer release than the
image's label records. When that lookup fails it keeps the existing image and
says so. With no image there is nothing to fall back to and the Dockerfile
carries no default version, so the wrapper stops and says the newest Pi could
not be looked up. Any launch with no image under the current tag, the first one
or the first after an edit to `Dockerfile.pi` or `entrypoint.sh`, therefore
needs that lookup to answer. A rebuild that fails keeps the image the tag
already held and starts it, so a registry blip does not leave you without Pi,
while a first build that fails stops with docker's status. It removes the older
images after the session, so nothing accumulates. That removal reaches every
other `pi-sandbox` image it can, including one built by another checkout of
this repository or, on Linux, by another host user, whose next launch then
rebuilds. The container itself is removed by `--rm` when Pi exits.

## What the container can and cannot see

Mounted:

- the project you launched from, at `/workspace`, read-write. Inside a Git
  repository this is the repository root, even when you launch from a
  subdirectory. It is found from the nearest `.git` up the tree, not from
  `git rev-parse --show-toplevel`, whose answer a planted `core.worktree` or
  `.git` file can point at any host directory.
- a per-project Docker volume at `/home/agent` for Pi's own state.
- inside a Git repository, `.git` itself is bind-mounted over the project, so
  it cannot be renamed away, and the parts of it that can name a command are
  then mounted read-only inside it: `.git/config`, `.git/config.worktree`,
  `.git/hooks`, `.git/worktrees`, `.git/modules` and `.git/commondir`.

Not mounted, and unreachable: your home directory, `~/.ssh`, `~/.aws`,
`~/.config`, the system keychain, every other project, the Docker socket, your
Herdr socket, and `~/.pi` on the host, which holds Pi's provider OAuth tokens.
The script refuses to start if the directory it would mount is your home
directory, contains it, is `/`, or has a comma in its path, which Docker's
`--mount` syntax would read as another field.

The container runs as non-root (`agent`, your own uid and gid on Linux and
1001 on Docker Desktop) with `--cap-drop ALL`,
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

`OPENCODE_GO_API_KEY` is the one the default model needs, and the wrapper
checks for it whenever the run reaches opencode-go. Pi reads that provider from
`~/.pi/agent/auth.json` rather than from the environment, so the entrypoint
writes the forwarded key there, mode 0600, leaving any other provider in the
file alone. Set `OPENCODE_API_KEY` as well if you want the models that are on
Zen instead.

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
keeps running while you are not watching, with the same key, open network and
`--pids-limit` as the foreground session. Everything still dies with the
container.

They live in the agent's home, so they reach a project through its state
volume. A new project's first start downloads them. Offline, that start
proceeds without them, and the next start with a network installs them. On
every start the entrypoint installs any of the four that is missing and then
runs `pi update --extensions`, so an existing volume ends up at their newest
releases even though an image rebuild does not reach it. That update also moves
any extension you added without a version, while one installed as
`npm:<package>@<version>` stays at it. A `pi remove npm:<package>` of one of the
four therefore lasts only until the next start, which installs it again.
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
pi-sandbox always runs the newest releases rather than reviewed pins, which
means a new upstream release reaches the sandbox, and the API key it holds,
without review. Lifecycle scripts stay off, which limits what a release can run
at install time but not what the extension code does once it is loaded.

## Git

`git status`, `diff`, `add` and `commit` work in `/workspace`, and write
straight through to your real checkout, so a commit made in the sandbox is
immediately in your local repository. `/workspace` is marked a safe directory
because the mounted files belong to the host user, and commits use the identity
`Pi Sandbox <pi-sandbox@localhost>` rather than yours. Override it per commit
with `git -c user.name=... -c user.email=...`.

The bind mounts listed above are what protect this. The hooks, worktrees and
modules directories are created first if they are missing, and so is the
worktree config, as an empty file. Git ignores an empty `.git/config.worktree`
while `extensions.worktreeConfig` is off, which is the default, so creating it
changes nothing for the host, and an existing one is left untouched. Git reads
`.git/commondir` in any repository and takes its config and hooks from the
directory it names, so the wrapper creates it holding `.`, which names the same
directory and leaves Git behaving as before, and refuses to run when one names
anything else. A symlink at `.git` or at any of the six read-only paths is
refused before the wrapper creates or mounts anything, because Git, the mount
and the `mkdir` would all follow it outside the project. A
`.git/config.worktree` or `.git/commondir` that is not an ordinary file, such
as a FIFO, is refused for the same reason, since creating or mounting it as a
file would block or fail. Git runs commands named in those
places, through `core.fsmonitor`, `core.pager`, `core.hooksPath` and
`filter.<name>.clean`, so leaving them writable would let the agent leave a
command behind that you run yourself with the next `git status`. A linked
worktree keeps its git directory at `.git/worktrees/<name>`, and `commondir`,
`gitdir` and `config.worktree` there redirect Git to the config and hooks it
reads, so the whole directory is read-only too. `git worktree add` therefore
fails in the sandbox, which costs nothing because the paths in those files are
host paths.

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

A planted rebase, merge or cherry-pick is not prevented either. The agent can
write `.git/rebase-merge/git-rebase-todo` with `exec` lines in it, or the
files the other backends read, so the next host `git status` reports an
operation in progress and `git rebase --continue` would run what the file
names. An empty directory there cannot be blocked, because Git reads the empty
directory as a rebase in progress, so the wrapper warns instead: at exit it
names `.git/rebase-merge`, `.git/rebase-apply`, `.git/sequencer`,
`.git/MERGE_HEAD` or `.git/CHERRY_PICK_HEAD` when its ctime is newer than the
marker. Inspect those and abort the operation rather than continuing it.

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

For the same reason the script must not `exec docker`: that would replace the
`pi`-named process with `docker` and Herdr would see a plain shell. `test_pi.py`
pins that, and that the wrapper ignores the pane's own `HERDR_SOCKET_PATH`.
Mounting your socket would undo the sandbox rather than extend it, since
`herdr pane run` executes on the host, outside the container.

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
  enforces a limit. `docker run` sets no memory limit, so a runaway fan-out
  reaches the machine's memory before it reaches `--pids-limit`.
- The image is roughly 1.2 GB, mostly Pi's npm dependency tree. It carries
  Node 24, Python 3.11, uv, Git, ripgrep and fd-find.

These are accepted limits, and [SECURITY.md](SECURITY.md) defines what does
count as a vulnerability here and how to report it privately.

## Tests

The tests use a fake `docker` on `PATH`, so they neither build an image nor
start a container. They assert the isolation properties: the expected mounts
and no others, the keys forwarded by name and never by value, the sandboxing
flags present and no privileged or host namespace flags, the home-directory
refusal, and that a Herdr pane's socket stays on the host. The rest read
`Dockerfile.pi` or run `entrypoint.sh` against stub binaries and a throwaway
home, for the default model, where the subscription key is written, and what
happens when the agent has ruined the file it is written to.

## Contributing

Tests run with `uv run --with pytest pytest -q`. The shell scripts must pass
`shellcheck` and run under dash. [AGENTS.md](AGENTS.md) holds the security
lessons a change here has to follow, including that every behaviour gets a
test proven to fail when the fix is reverted and that the wrapper stays POSIX
sh which does not `exec docker`. Report a vulnerability privately as
[SECURITY.md](SECURITY.md) describes rather than in a public issue.

## License

Copyright 2026 pi-sandbox contributors. Licensed under Apache-2.0, see
[LICENSE](LICENSE).
