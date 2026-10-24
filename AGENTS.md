# AGENTS.md

Notes for agents and contributors changing this repo, from the security work on
`open-source-readiness` (2026-09-21).

## Testing

- Never write unit tests after the code. If a component must be tested in isolation, FIRST write down every way it could fail, THEN write the code.
- Prefer end-to-end tests as the only testing mechanism, and use them to prove complex features work. Every E2E run ends by producing a verifiable, repeatable artifact (a report, screenshot, log or output file that can be regenerated and checked).
- For E2E verification, do not pick the simplest scenario that could pass. Pick a medium-to-hard one.
- Tautological tests (they restate the implementation or only exercise their own mocks) and change-detector tests (they fail on any refactor with no behavior change) are harmful. Do not write them. When adding tests, check that none are tautological or self-testing.
- Do not add a regression test for a bug fix unless it closes a real gap in behavior testing.

## Security notes

- Threat model: everything under the mounted project, `.git` included, is
  attacker-controlled on the next launch and to host tools.
- Host git trusts files the container writes, because Docker Desktop maps
  ownership to the host user. Any `.git` path host git reads that can name a
  command (`config`, `config.worktree`, `hooks`, `modules`,
  `worktrees/*/commondir`) must be read-only in the container or reported at
  exit. Found this way: renaming `.git` away, nested repos, `commondir`
  redirection, planted rebase todos. Git reads `.git/commondir` in a plain
  repository too, not only in a worktree, and a planted one made host
  `git status` run a command (2026-09-23), so it is mounted read-only too.
  Its content check was bypassed twice in review (`.\n\nevil`, then
  `.\n\0`, since the shell drops NUL bytes and Git does not). The two-byte
  limit was then checked against every file of one or two bytes: only four
  pass, and host Git resolves all four to the repository's own `.git`.
  A read-only mount protects a path, not the inode. A link cannot be made
  from inside one: `ln` to the writable project fails with `Invalid
  cross-device link` (Docker Desktop, 2026-09-23). But one made while `.git`
  was unprotected writes straight through: a project file hard-linked to
  `.git/hooks/post-checkout` changed the host hook under the read-only mount
  (Docker Desktop, 2026-10-24). So any symlink there, and a second link to a
  config or hook, is refused. A name filter applied before the type let a
  symlinked submodule git directory through (round 2 review).
- A check of the project is only good while no container can write it.
  Another session's container, live or left by a SIGKILLed wrapper, of this
  project or of a directory above it, could swap `.git` for a symlink between
  the checks and `docker run`. So the stale sweep runs before the checks and
  such a live session refuses the launch: one session per project.
- Never follow a project path the agent could have planted. Symlinks at the
  mounted `.git` paths are refused, because a planted `.git` symlink made a
  later launch mount a host directory read-write (round 3).
- Never mount the wrapper's own directory. Launched on its own checkout, the
  agent could edit `pi`, `Dockerfile.pi` or `entrypoint.sh`, and the host ran
  the edit at the next launch in any project (2026-10-24). The wrapper now
  refuses it by physical path, so run the sandbox on pi-sandbox from a clone
  with another checkout's `pi`.
- Never ask host `git` where the project is. `git rev-parse --show-toplevel`
  honours `core.worktree`, and a planted `core.worktree=/etc` made a later
  launch mount `/etc` (round 4). The wrapper walks up to the nearest `.git`.
- Opening a FIFO for writing blocks, and `set -C` does not prevent it. Refuse
  non-regular files before writing, and wrap FIFO experiments in `timeout`.
- The agent reaches the host terminal. `/dev/pts/0` is owned by the user Pi
  runs as, and Pi's own stdout is reachable through `/proc`, so a tool wrote
  an OSC title that host Herdr showed (2026-09-22). Only a host-side filter
  between docker and the terminal could stop it, so it is a documented limit.
- The container reaches the host's network. On Docker Desktop a host server
  bound to 127.0.0.1 answered a container through `host.docker.internal` and
  its IP 192.168.65.254 (2026-10-24). Docker has no flag that keeps the model
  API and blocks the host, so this is a documented limit, and SECURITY.md
  counts only reach through what pi-sandbox itself mounts, forwards or exposes.
- macOS file systems ignore case, and host Git opens `.GIT` for `.git` and
  `head` for `HEAD`, so any check of a Git name the agent can choose must
  ignore case. Docker Desktop also resolves a case variant through a writable
  parent mount: `/workspace/.git/CONFIG` reached the read-only-mounted
  `.git/config` on the host (2026-10-24, file mounts only, `hooks` held).
- Docker Desktop turns a mode 000 directory the agent makes into 0600 on the
  host, and macOS find lists it but silently skips what is inside with status
  0 (2026-10-24). A host-side scan must treat a directory without its search
  bit as a failure. An agent tool shell can alias `find` to bfs,
  which behaves differently, so test with `/usr/bin/find`.
- Test security fixes end to end on real Docker, not only with the fake docker
  in `test_pi.py`, because the fake cannot show what a mount does. Keep its
  style: every behaviour gets a test proven to fail when the fix is reverted.
- The wrapper must stay POSIX sh that runs under dash, must not `exec docker`
  (Herdr identifies the pane by the `pi` process), and keeps docker's exit
  status. Its body stays in one brace group so an edit or `git pull` during a
  session cannot change what the running wrapper executes.

## Image and volume notes

- `Dockerfile.pi` sets `HOME=/home/agent` before its root `RUN` steps, so
  anything a root step writes under `$HOME` lands root-owned in the image and
  is copied into every new state volume, where the agent cannot write it. The
  Pi install left a root-owned `~/.npm` that made `pi install` inside the
  sandbox fail with EACCES (found 2026-10-24). That step now removes it, and
  `ENV npm_config_cache` keeps npm out of it in older volumes. Remove what a
  root step leaves under `$HOME`.
- The four extensions are pinned in `entrypoint.sh`. `pi install
  npm:<package>@<version>` moves an installed one down as well as up and
  replaces its entry in Pi's settings, and an install without a version
  unpins it (measured 2026-10-24), which is why the entrypoint also checks
  Pi's settings for the pinned entry.
