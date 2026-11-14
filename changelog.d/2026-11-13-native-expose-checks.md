### Added (2026-11-13 — Native `expose status` and expose's checks)

- The Rust CLI answers `pseudolife-mcp expose status` (with `--json`), the
  help and argument errors of `expose`, and every check `expose tailscale`
  and `expose off` make before changing anything (the local daemon, the
  `tailscale` CLI, the current serve on the port, the tailnet address), with
  each refusal, "already exposed" and "not exposed", byte for byte as the
  Python command does. Changing the serve config, and the confirmation
  question, are not native yet: the Rust CLI reports them as deferred and
  `pseudolife-mcp expose` keeps running the Python command for them.
