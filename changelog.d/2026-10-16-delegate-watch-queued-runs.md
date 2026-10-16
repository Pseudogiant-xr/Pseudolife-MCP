### Fixed (2026-10-16 — delegate-watch no longer calls a head green while a workflow run is still queued)

- `pseudolife-mcp delegate-watch` decided a pull request was green from its
  check runs alone. A workflow run still waiting in its queue or concurrency
  group has no check runs yet, so "PR #781 green at 941539fb, merge state
  blocked" went out while that head's Rust shim run had not started. Before
  reporting a head green or cancelled, the watcher now lists the head's
  workflow runs (`gh run list --commit`) and keeps it pending while any run
  has not completed. The lookup is made only for a head about to be
  reported, so a quiet poll costs no extra `gh` calls.
