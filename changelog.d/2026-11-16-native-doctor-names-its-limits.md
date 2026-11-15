### Fixed (2026-11-16 — the native `doctor` reports everything it can instead of giving up on the whole run)
- With the native client, `pseudolife-mcp doctor` no longer hands the entire run to the Python client because of one unrelated condition. With no Python installed, that left no diagnosis at all.
- A saved-tunnel directory no longer stops the run. An absent or empty `~/.pseudolife-mcp/tunnel` adds nothing. Saved profiles appear in a read-only `tunnels` section with state, catalog, key presence, key expiry and last refresh. A profile or store the client cannot read is one named `unavailable` entry with its reason and recovery.
- A proxy variable no longer stops the run. It is named in a `proxy` line (variable names only, never values), and a handshake failure while one routes traffic names `NO_PROXY` as the fix.
- `PYTHONINTMAXSTRDIGITS` is a Python-only setting and is now ignored.
- A token no client can send, or a token source that cannot be read, is now the named error `BearerUnsendable` or `CredentialUnavailable` (exit 1). The checks that need no credential still run, the token is never sent, and it is never printed.
- An invalid `PSEUDOLIFE_MCP_DAEMON_URL` is now the named error `DaemonUrlInvalid` (exit 1), with no request made.
- A `/health` answer that is not a daemon health report is now `DaemonUnrecognized` (exit 1).
- A dream stall, any Codex hook-copy state, a maintainer answer that never completes, and client files the native reader does not parse are each named in their own field. With no `PATH`, the platform's default search path is used.
- Only argument shapes (`--help`, non-canonical options, a timeout past the cap) and an `--agent-state` path the native resolver does not decide still defer.
