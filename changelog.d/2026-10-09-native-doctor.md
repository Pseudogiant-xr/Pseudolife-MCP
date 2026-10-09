### Added (2026-10-09 — native read-only doctor diagnostics)

- The experimental Rust `doctor` runs the read-only diagnostics natively for
  `--host`, `--timeout` and `--agent-state`. It covers credentials borrowed
  from Claude Code and Codex registrations, the board and maintainer-passkey
  probes, `/health`, the MCP handshake through its own stdio shim, and each
  client's wake path. It also covers Codex's hook copy, Git Bash on Windows,
  `pseudolife-mcp` on PATH and the saved-instance nonce proof, with the
  Python report's bytes and request bytes.
- Its runtime-identity fields name the native executable, its directory, its
  Cargo version and `mcp: not installed`.
- Some inputs still defer, always before the handshake starts the shim: a
  nonempty `--disposable-proof` database, saved tunnels, HTTP proxies and
  non-ASCII bearers. So do a `--timeout` past 1,000,000 seconds,
  `PYTHONINTMAXSTRDIGITS`, a Codex plugin hook comparison against a daemon
  digest, and inputs only Python's parsers read.
