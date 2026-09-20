---
name: herdr-fleet
description: Start and steer other Pi agents as children of this session,
  through the Herdr server running in this sandbox. Use when work splits into
  parts worth running at the same time, or when a second agent should review
  what this one did.
---

# Fleet

This container runs its own Herdr server. It manages panes in this container
only and reaches nothing on the host, so there is no other session to inspect
and no human watching the panes.

## Are you the parent or a child?

If `HERDR_PANE_ID` is set, another agent in this container started you. Do the
task you were given, report back in your pane, and do not start a fleet of your
own. The rest of this skill is for the session that has no pane.

## What the herdr skill says that does not hold here

Read the `herdr` skill for the CLI itself. Four of its rules are different in
this sandbox:

- Its opening check is `HERDR_ENV=1`, and that passes: the entrypoint sets it
  because the server here is real. Do not stop over it.
- The parent session is the foreground process of the container rather than a
  pane, so `--current` and `$HERDR_PANE_ID` are unset for it and there is no
  calling pane to split.
- It says not to create a workspace unless asked. Here you must, since there is
  no pane to split from.
- `--machine` and `--remote` are dead: no machines are saved, there is no `ssh`
  binary in the image, and there is nothing outside the container to reach.

## Put a child to work

Create a workspace and read its pane id. The container has `python3` and no
`jq`:

    pane=$(herdr workspace create --cwd /workspace --label review --no-focus |
        python3 -c "import json,sys;print(json.load(sys.stdin)['result']['root_pane']['pane_id'])")

Start Pi in that pane. Everything after the bare `--` goes to Pi, and both
flags are needed: the sandbox authenticates opencode-go but not the provider
Pi defaults to, and naming only one of the two lets Pi resolve the other
against everything it can authenticate, which has answered from the wrong
provider before.

    herdr agent start reviewer --kind pi --pane "$pane" \
        -- --provider opencode-go --model deepseek-v4.1-flash

Then drive it by name, with a timeout, since a wait without one never returns:

    herdr agent prompt reviewer "Review the diff on this branch" --wait --timeout 120000
    herdr agent read reviewer --source recent-unwrapped --lines 120
    herdr pane close "$pane"

`herdr --help`, and each command group run without a subcommand, are the
authority on syntax.

## What a child costs

A child is a full Pi session, not a subagent: it has its own context and keeps
working while you do something else. It also shares this container, meaning the
same `/workspace` checkout, the same API key and the same end when the
container exits. It is not cheap: a Pi session costs around fifteen of the
container's 512 processes and a few hundred megabytes, and the container has no
memory limit of its own, so a dozen children will exhaust the machine long
before the process limit stops them. Two or three at a time is plenty. Close
what you finish with, and tell the user what the fleet did, since they cannot
see these panes.
