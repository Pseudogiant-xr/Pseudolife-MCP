### Added (2026-10-27 — the Rust daemon redeems pairing codes)
- `pseudolife-mcp pair` against the Rust daemon now redeems the code instead
  of getting 501 `not_implemented`, as against the Python daemon: the new
  token works at once, a retry with the same token inside ten minutes gets
  the same answer, and a spent, expired or revoked code, a name the
  environment already uses or a token already held is refused with the same
  `pairing_refused`. At most two redemptions run at once (a third is told
  `rate_limited`, not queued), a success gives its attempt back to the
  20-a-minute failure budget, a database failure answers
  `pairing_unavailable`, and a client that disconnects mid-redemption still
  leaves the pairing completed and usable.
