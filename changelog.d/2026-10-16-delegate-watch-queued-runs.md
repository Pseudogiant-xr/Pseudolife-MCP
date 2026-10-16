### Fixed (2026-10-16 — delegate-watch no longer calls a head green while a workflow run is still queued)

- `pseudolife-mcp delegate-watch` decided a pull request was green from its
  check runs alone. A workflow run still waiting in its queue or concurrency
  group has no check runs yet, so "PR #781 green at 941539fb, merge state
  blocked" went out while that head's Rust shim run had not started. Before
  reporting a head green or cancelled, the watcher now lists the head's
  workflow runs (`gh run list --commit`) and keeps it pending while the
  newest run of any workflow has not completed. A run that then ends failed,
  timed out, failing to start, or cancelled without creating a check makes
  the head failing or cancelled, naming the workflow, instead of green. The
  lookup is made only for a head about to be reported green or cancelled,
  then once a poll while it is held.
