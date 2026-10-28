### Added (2026-10-27 — the Rust daemon serves MCP on installs without a token)
- The Rust daemon now answers `/mcp` when no bearer token is configured, as
  the Python daemon does, instead of 501 `not_implemented`. It keeps the
  Python daemon's DNS-rebinding protection exactly: the Host header must name
  `127.0.0.1`, `localhost` or `[::1]` with a port (else 421), and a browser
  Origin must be `http://` on one of those with a port (else 403), on every
  request. A bound identity (`X-PL-Bank` / `X-PL-Principal`) on such an
  install is still refused with `authentication_required`.
- Fixed in the same change: a request in the 2026-07-28 MCP era (an unknown
  `MCP-Protocol-Version` header) now passes the Content-Type, Host and Origin
  checks before its refusal, as the MCP SDK runs them first.
