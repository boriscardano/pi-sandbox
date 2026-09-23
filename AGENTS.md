# AGENTS.md

Notes for agents and contributors changing this repo, from the security work on
`open-source-readiness` (2026-09-21).

- Threat model: everything under the mounted project, `.git` included, is
  attacker-controlled on the next launch and to host tools.
- Host git trusts files the container writes, because Docker Desktop maps
  ownership to the host user. Any `.git` path host git reads that can name a
  command (`config`, `config.worktree`, `hooks`, `modules`,
  `worktrees/*/commondir`) must be read-only in the container or reported at
  exit. Found this way: renaming `.git` away, nested repos, `commondir`
  redirection, planted rebase todos.
- Never follow a project path the agent could have planted. Symlinks at the
  mounted `.git` paths are refused, because a planted `.git` symlink made a
  later launch mount a host directory read-write (round 3).
- Never ask host `git` where the project is. `git rev-parse --show-toplevel`
  honours `core.worktree`, and a planted `core.worktree=/etc` made a later
  launch mount `/etc` (round 4). The wrapper walks up to the nearest `.git`.
- Opening a FIFO for writing blocks, and `set -C` does not prevent it. Refuse
  non-regular files before writing, and wrap FIFO experiments in `timeout`.
- Test security fixes end to end on real Docker, not only with the fake docker
  in `test_pi.py`, because the fake cannot show what a mount does. Keep its
  style: every behaviour gets a test proven to fail when the fix is reverted.
- The wrapper must stay POSIX sh that runs under dash, must not `exec docker`
  (Herdr identifies the pane by the `pi` process), and keeps docker's exit
  status.
