### Added (2026-11-12 — Native Console sign-in links, list and sign-out)

- The Rust CLI answers `pseudolife-mcp console --no-open`, `console --list`
  and `console --revoke SESSION_ID` (each with `--json`), their help and
  their argument, credential and daemon refusals, byte for byte as the Python
  command does. A sign-in that would open a browser, and
  `--unless-signed-in`, are not native yet: the Rust CLI reports them as
  deferred, and `pseudolife-mcp console` keeps running the Python command.
