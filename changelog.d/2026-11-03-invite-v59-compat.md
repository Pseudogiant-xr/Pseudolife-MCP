### Fixed (2026-11-03 — An older host CLI can still invite through a newer daemon image)

- After a daemon-only update to schema v59, `pseudolife-mcp invite <name>`
  from a host CLI one release behind no longer refuses with "the daemon's
  image predates `invite`". The image's `invite --version-check` now prints
  `pseudolife-mcp invite 1` before `pseudolife-mcp invite 2`, and a current
  CLI reads the highest such line, so `--someone-else` and `--mine` still
  need a v59 image.
- `invite --list` with the daemon container stopped no longer guesses an
  environment principal's owner from this host's `config.yaml`: the
  stopped daemon's `coordination.not_owner_principals` lives in its own
  volume, so such a row shows `-` in the owner column and `null` in
  `--json`. Stored principals keep their recorded owner.
