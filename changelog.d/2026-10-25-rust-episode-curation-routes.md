### Added (2026-10-25 — the Rust daemon serves the episode curation routes)
- `POST /api/episodes/rename`, `/merge` and `/prune` answered 501 on the
  Rust daemon, so an agent repairing fragmented or empty episodes over REST
  had no way to do it there; no MCP tool offers these. The Rust daemon now
  serves all three as the Python daemon does: rename restamps the episode's
  entries, merge folds closed episodes into an existing one or a new rollup
  (re-pointing entries, outcome signals and child episodes, and widening the
  span), and prune deletes episodes with no entries, closed ones by default.
  Refusals keep Python's answers, including the 405 for a rename without an
  `id`. One declared difference: a new rollup's `hint` must be a string or
  null; Python accepts any value and can leave the merged entries pointing
  at an episode row it never wrote.
