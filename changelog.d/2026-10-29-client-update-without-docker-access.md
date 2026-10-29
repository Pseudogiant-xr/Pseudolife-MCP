### Fixed (2026-10-29 — clients update on a machine whose Docker the user cannot use)
- `pseudolife-mcp update` and `update --clients-only` on a client machine
  stopped with "docker is installed but did not answer" whenever Docker was
  installed but refused the user (no `docker` group, say), even with
  `--daemon-url` naming a daemon on another machine. When the command runs
  from a shim runtime and the daemon URL names another machine whose daemon
  answers, the update now says so in one line and moves this machine's
  clients as it does on a machine without Docker. With a local daemon URL, or
  a remote one that does not answer, it still stops; its refusal now names
  `--daemon-url` for a client machine that did not say where its daemon is.
