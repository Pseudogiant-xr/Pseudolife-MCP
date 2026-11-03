### Changed (2026-11-03 — Inviting keeps working from a host CLI one release behind the daemon)

- After a daemon-only update to schema v59, `pseudolife-mcp invite <name>`
  from a host CLI one release behind still works through the daemon's
  container. The invite's note for a machine that is not yours now reads
  "add --someone-else (an older pseudolife-mcp refuses it: update it
  first)", wherever the invite ran, since a CLI before v59 has no
  `--someone-else`.
- `invite --list` with the daemon container stopped shows an environment
  token's owner as unknown (`-`, and `null` in `--json`): the stopped
  daemon's `coordination.not_owner_principals` is in its own volume, not
  this host's config. Stored principals keep their recorded owner.
