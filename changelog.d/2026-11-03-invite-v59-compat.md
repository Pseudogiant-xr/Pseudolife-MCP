### Changed (2026-11-03 — Inviting keeps working from a host CLI one release behind the daemon)

- After a daemon-only update to schema v59, `pseudolife-mcp invite <name>`
  from a host CLI one release behind still works through the daemon's
  container. Its note for a machine that is not yours says to update
  `pseudolife-mcp` on that host first, since only an updated CLI takes
  `--someone-else`.
- `invite --list` with the daemon container stopped shows an environment
  token's owner as unknown (`-`, and `null` in `--json`): the stopped
  daemon's `coordination.not_owner_principals` is in its own volume, not
  this host's config. Stored principals keep their recorded owner.
