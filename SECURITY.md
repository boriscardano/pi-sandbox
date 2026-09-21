# Security policy

## What counts as a vulnerability

pi-sandbox exists to keep a sandboxed agent away from the host. A report counts
when it shows the agent, or anything it runs in the container, breaking that
boundary. For example:

- reading or writing a host file outside the project mounted at `/workspace`
- running a command on the host
- reaching a host socket, including the Docker socket or a host Herdr socket
- obtaining a credential the README says the container does not receive, such
  as a host Pi OAuth token or any key other than the ones the README lists

## What does not count

The known limitations in the README are not vulnerabilities. In particular:

- the API keys forwarded into the container are readable there, and outbound
  network is open, so anything in the container can use or send them
- the mounted project is fully readable and writable by the agent
- the agent can leave files your host may later execute, such as a `Makefile`
  or an `.envrc`
- nested git metadata the agent creates is detected at exit, not prevented
- a container escape defeats the boundary, and a container is not a virtual
  machine

## Supported versions

Only the latest `main` is supported. Fixes land there and there are no
released branches.

## Reporting a vulnerability

Report it privately with GitHub's "Report a vulnerability" button, under the
repository's Security tab, which opens private vulnerability reporting. Do not
open a public issue. Include what the agent can do, the commit you tested, and
a reproduction if you have one. Please leave out the contents of any real
project and any real key.
