### Changed (2026-10-30 — client machines are steered to pairing instead of a copied token)
- When two machines use one copied bearer token, rotating it cuts off both
  and neither can be revoked alone. A client-only install given a token
  (`--token-file` / `--read-token`, `-TokenFile` / `-ReadToken`) against a
  daemon on another machine still works, and now ends with one line beside
  the update command: if another machine uses this same token, give each
  its own with `pseudolife-mcp invite <name>` on the daemon's host and
  `connect <url> --read-code` here, named with the installed shim's full
  path; a machine already paired, or given a token no other machine uses,
  has nothing to do. A pairing code, or a loopback daemon URL (the daemon's
  own host or an SSH tunnel), gets no such line, and nothing is asked.
- The installers' help, their refusals for a missing token file, the
  client-only closing notes, the README and the "Sharing one bank across
  machines" guide lead with `--pairing-code` / `-PairingCode` and `invite`
  / `connect --read-code` for client machines, with the raw token kept as
  the documented fallback. A paired machine whose token is revoked or
  replaced pairs again (`invite <name> --replace`); only an
  environment-token client writes a new token by hand.
- The agent board's "the daemon rejected this token" line (`doctor`, the
  installers, `connect`, in both the Python and the Rust shim) now names
  both fixes: on the daemon's own host, re-run the installer so the token
  file matches `ops/.env`; on another machine, `pseudolife-mcp invite
  <name> --replace` on the daemon host, then `pseudolife-mcp connect <url>
  --read-code` there.
