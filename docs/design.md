# Design notes

Why the wrapper does what the [README](../README.md) describes. The README says
what works, what fails and what is warned. This file holds the reasoning and
the mechanics behind it. [AGENTS.md](../AGENTS.md) holds the lessons a change
has to follow.

## Finding the project

Inside a Git repository the mounted project is the repository root, even when
you launch from a subdirectory. It is found from the nearest `.git` up the
tree, not from `git rev-parse --show-toplevel`, whose answer a planted
`core.worktree` or `.git` file can point at any host directory. With no `.git`
anywhere above it, the launch directory itself is mounted, and the wrapper
prints a warning naming it, since that may be far more than you meant to share.

## Images and rebuilds

The image is tagged by the contents of `Dockerfile.pi` and `entrypoint.sh`, so
a changed file produces a tag that does not exist yet and the wrapper builds it
before starting. It also rebuilds when npm answers with a newer release than
the image's label records. When that lookup fails it keeps the existing image
and says so. With no image there is nothing to fall back to and the Dockerfile
carries no default version, so the wrapper stops and says the newest Pi could
not be looked up. Any launch with no image under the current tag, the first one
or the first after an edit to `Dockerfile.pi` or `entrypoint.sh`, therefore
needs that lookup to answer. A rebuild that fails keeps the image the tag
already held and starts it, so a registry blip does not leave you without Pi,
while a first build that fails stops with docker's status.

It removes the older images after the session, so nothing accumulates. That
removal reaches every other `pi-sandbox` image it can, including one built by
another checkout of this repository or, on Linux, by another host user, whose
next launch then rebuilds. It also prunes the build cache an old image's Pi
install left, about 190 MB per Pi release, picked by that Dockerfile step's own
text so no other project's cache is touched. Pi is the last layer of the image,
so a new release rebuilds only that layer.

## Removing the container

The container is removed however the session ends. `--rm` covers Pi exiting,
but not the wrapper or the docker client dying under it, which left the
container running with nobody attached. So each container is named after the
project, where the wrapper runs and its process id. The wrapper removes it as
soon as `docker run` returns, and a small watcher it starts removes it once
the wrapper is gone, which covers a closed pane, a SIGHUP, a SIGTERM and a
SIGKILL to the wrapper. `e2e.py` checks every one of those paths on real
Docker.

The watcher is in the wrapper's process group, so a SIGKILL to the whole group
takes it too and leaves the container running until the next launch, in any
project: where `ps` works, every launch removes the pi-sandbox containers whose
wrapper, named by the process id at the end of the container's name, no longer
runs. The name also carries a hash of where the wrapper runs: the machine's id
(Linux's machine-id, macOS's kern.uuid), the user and the process namespace.
Only containers from the same place are removed by name. So a session
started by another user, from a container with its own process namespace, or
on another machine using the same Docker daemon is not taken for a dead one.
WSL distros are told apart only by their process namespaces, which has not been
checked on WSL.

## The subscription key

Pi reads the opencode-go provider from `~/.pi/agent/auth.json` rather than from
the environment, so the entrypoint writes the forwarded `OPENCODE_GO_API_KEY`
there, mode 0600, leaving any other provider in the file alone. That file is in
the state volume, so unlike the environment the key outlives the container.
`docker volume rm` is what removes it.

## Extension updates

The entrypoint installs any of the four extensions that is missing and then
runs `pi update --extensions`, so an existing volume ends up at their newest
releases even though an image rebuild does not reach it. That step took about
3 seconds of a warm start, so after one that worked it is skipped for 24 hours,
unless one of the four is missing. npm's download cache for it goes to `/tmp`,
which is removed with the container, so releases do not pile up in the volume.

That update also moves any extension you added without a version, while one
installed as `npm:<package>@<version>` stays at it. A `pi remove
npm:<package>` of one of the four therefore lasts only until the next start,
which installs it again, and a pin on one of the four is undone, since that
install names no version.

pi-sandbox always runs the newest releases rather than reviewed pins, which
means a new upstream release reaches the sandbox, and the API key it holds,
without review. Lifecycle scripts stay off, which limits what a release can run
at install time but not what the extension code does once it is loaded.

## What the state volume holds

Docker seeds a new volume from the image's `/home/agent`, which is a few
kilobytes. What filled volumes was written at run time: on 2026-10-22 one host
had 94 volumes, 24 GB, mostly uv's cache (253 MB each) and a Python 3.12 that uv
downloaded because Debian ships 3.11 (about 90 MB in a measured run, 184 MB of
`~/.local` in those volumes). Every worktree is a project of its own, so each
got its own copy.

The image therefore carries Python 3.14, installed by uv as root outside the
agent's home, beside Debian's 3.11. uv may still download a Python for any
project that needs one: the image only saves the download, it never limits
what a project can ask for. Its directory comes after `/usr/bin` on `PATH`,
which keeps uv's choice what it was before. A project that accepts 3.11 still
gets 3.11, and one that needs 3.12 or newer gets 3.14 where uv used to
download it. Measured on 2026-10-22 with `uv sync`:

| Project | Before | Now |
| --- | --- | --- |
| `>=3.10`, numpy 1.26.4 (wheels up to 3.12) | 3.11 | 3.11 |
| `>=3.12`, httpx and pydantic | 3.14 downloaded, 102 MB | 3.14 from the image, 10 MB |
| `.python-version` 3.12 | 3.12 downloaded, 101 MB | 3.12 downloaded, 95 MB |

With 3.14 as `python3`, or as `python3.14` in `/usr/local/bin`, uv chose it for
the first project too, and that sync failed: numpy 1.26.4 has no 3.14 wheel
and the sandbox has no compiler. 3.14 rather than 3.15 for the same reason: a
real project that depends on onnxruntime 1.24.3, which has no 3.15 wheel,
failed on a 3.15 image and syncs on 3.14. 3.15 can replace it once wheels
catch up. Baking 3.12 and 3.13 as well would save pinned projects their
download at about 90 MB of image each, which was not worth it.

A volume that already holds a uv-downloaded Python keeps
using it, since uv prefers its own installs to one on `PATH`. uv's cache
lives in the container's `/tmp`, so a session that changes the lock downloads
again. A cache shared across projects would avoid that, but one project's agent
could then plant a package that another project's venv installs. The four
extensions stay in the volume, about 46 MB, because they are updated per volume
as described above.

Each volume carries a `pi-sandbox.project` label with the host path it serves,
set when Docker creates it, so the README's one-liner can remove the volumes
whose project is gone. Nothing in the container can change a volume's labels.

## Protecting `.git`

Git runs its hooks, and commands its config names through `core.fsmonitor`,
`core.pager` and `filter.<name>.clean`, while `core.hooksPath` points it at
other hooks, so leaving these writable would let the agent leave a command
behind that you run yourself with the next `git status`. Host Git trusts those files, because the host user owns
the files the agent writes: Docker Desktop maps ownership to it, and on native
Linux the container user has your own uid. So inside a Git repository,
`.git` itself is bind-mounted over the project, and `.git/config`,
`.git/config.worktree`, `.git/hooks`, `.git/worktrees`, `.git/modules` and
`.git/commondir` are then mounted read-only inside it.

### Why `.git` itself is mounted

Read-only mounts on the files inside `.git` do not stop `mv .git .git-old`,
which succeeds while `.git` is still an ordinary directory of the project, and
the agent can then build a fresh `.git` with its own config. A bind mount makes
`.git` a mount point, which cannot be renamed or removed, so that move fails
with `Device or resource busy`.

### Creating the mount targets

The hooks, worktrees and modules directories are created first if they are
missing, and so is the worktree config, as an empty file. Git ignores an empty
`.git/config.worktree` while `extensions.worktreeConfig` is off, which is the
default, so creating it changes nothing for the host, and an existing one is
left untouched.

Git reads `.git/commondir` in any repository and takes its config and hooks
from the directory it names, so the wrapper creates it holding `.`, which names
the same directory and leaves Git behaving as before, and refuses to run when
one names anything else.

### Symlinks and FIFOs

A symlink at `.git` or at any of the six read-only paths is refused before the
wrapper creates or mounts anything, because Git, the mount and the `mkdir`
would all follow it outside the project. A `.git/config.worktree` or
`.git/commondir` that is not an ordinary file, such as a FIFO, is refused for
the same reason, since creating or mounting it as a file would block or fail.

### Linked worktrees and submodules

A linked worktree keeps its git directory at `.git/worktrees/<name>`, and
`commondir`, `gitdir` and `config.worktree` there redirect Git to the config
and hooks it reads, so the whole directory is read-only too. `git worktree add`
therefore fails in the sandbox, which costs nothing because the paths in those
files are host paths. Launched from a linked worktree or a submodule checkout,
the project's real Git directory is outside the mount and gets no such
protection, and Git does not work in the sandbox there anyway.

### What is warned at exit

A repository the agent creates inside the project is not covered by any mount.
`git init sub` leaves `sub/.git/config` in your checkout, and host Git trusts
it, as it trusts every file the agent writes, and would run a command named
there. The same holds for a root `.git` the agent creates in a project that was
not a repository at launch, since nothing was mounted over it. The wrapper
cannot prevent that, so it warns instead: when the container exits it looks
for `.git` entries, `.git/config` files and `.git/hooks` entries under the
project whose ctime is newer than a marker made before the container started,
and prints what it found. It only warns, and deletes nothing.

A planted rebase, merge or cherry-pick is not prevented either. The agent can
write `.git/rebase-merge/git-rebase-todo` with `exec` lines in it, or the files
the other backends read, so the next host `git status` reports an operation in
progress and `git rebase --continue` would run what the file names. An empty
directory there cannot be blocked, because Git reads the empty directory as a
rebase in progress, so the wrapper warns instead: at exit it names
`.git/rebase-merge`, `.git/rebase-apply`, `.git/sequencer`, `.git/MERGE_HEAD`
or `.git/CHERRY_PICK_HEAD` when its ctime is newer than the marker.

## Herdr

[Herdr](https://herdr.dev) identifies a pane's agent from the foreground job's
process arguments, which is why the script is named `pi`. For the same reason
it must not `exec docker`: that would replace the `pi`-named process with
`docker` and Herdr would see a plain shell. `test_pi.py` pins that, and that
the wrapper ignores the pane's own `HERDR_SOCKET_PATH`. Mounting your socket
would undo the sandbox rather than extend it, since `herdr pane run` executes
on the host, outside the container.
