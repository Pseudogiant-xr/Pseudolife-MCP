### Fixed (2026-10-16 — delegate-watch no longer reports cancelled CI as failing, once per job)

- `pseudolife-mcp delegate-watch` counted a cancelled check as a failure, and
  each further job of the same cancelled run that concluded woke the delegate
  again with a new "PR #N failing at <head>" line: after the delegate
  cancelled the CI of two dependabot PRs on purpose, one head was reported
  three times in 15 minutes. A cancelled check is no longer a failure. Once
  every check on a head has finished with some cancelled, the watcher sends
  one `PR #N cancelled at <head>: <names>` line, and no more as further jobs
  of that run conclude. A re-run is reported again when it finishes: green,
  failing, or cancelled when some checks are still cancelled. A real failure
  beside cancelled checks is still reported as failing, and the cancelled
  siblings of a fail-fast matrix no longer add their names to that failure or
  report it again.
