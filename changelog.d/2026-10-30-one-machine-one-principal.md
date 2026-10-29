### Changed (2026-10-30 — client machines are steered to pairing instead of a copied token)
- A machine set up by copying another machine's bearer token shares that
  token: rotating it cuts off every machine holding a copy, and no one
  machine can be revoked. A client-only install given a token
  (`--token-file` / `--read-token`, `-TokenFile` / `-ReadToken`) against a
  daemon on another machine still works, and now prints one line
  recommending pairing instead: `pseudolife-mcp invite <name>` on the
  daemon's host, then `pseudolife-mcp connect <url> --read-code` on the
  client. A pairing code, or a loopback daemon URL (the daemon's own host
  or an SSH tunnel), gets no such line, and nothing is asked.
- The installers' help and the "Sharing one bank across machines" guide
  lead with `--pairing-code` / `-PairingCode` and `invite` / `connect
  --read-code` for client machines, with the raw token kept as the
  documented fallback.
