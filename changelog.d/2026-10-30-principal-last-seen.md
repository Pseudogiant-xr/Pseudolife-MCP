### Added (2026-10-30 — see which machines last connected, and which tokens the daemon refused)
- `pseudolife-mcp invite --list` shows when every principal last connected,
  stored (invited) and environment ones alike (`PSEUDOLIFE_MCP_TOKENS`, and
  `default` for `PSEUDOLIFE_MCP_TOKEN`), in a new "last seen" column, and
  lists the tokens the daemon refused by the first 8 characters of their
  SHA-256 with how often and when. The JSON form carries `last_seen_at` per
  principal and a `rejected` list. A machine whose token stopped working
  after a rotation no longer looks the same as a machine that is switched
  off.
- `pseudolife-mcp doctor` prints `token_sha256_prefix`, the same 8
  characters for the token this client sends (never the token), to match
  against that list.
- Schema v58: the `principal_activity` and `rejected_bearers` tables. The
  daemon (Python and Rust) notes each request's outcome in memory and writes
  it at most once a minute from the principals refresher, so requests pay no
  database write; refused prefixes are capped at 1000 rows and 90 days. Both
  tables stay out of logical exports. Additive: install and update need no
  step of their own.
