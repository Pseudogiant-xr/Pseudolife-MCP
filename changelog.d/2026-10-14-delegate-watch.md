### Added (2026-10-14 — `pseudolife-mcp delegate-watch` wakes a delegate session with board mail, only for decisions)

- `pseudolife-mcp delegate-watch` waits, without a model, for the events a
long-running delegate session acts on, and sends that session one board
message per batch. The events are a pull request opened, merged or closed,
turning green, failing or newly conflicting; a master workflow failing or
recovering; a new suite result; and a full-suite lease freeing up. The
watcher registers its own board agent; each message carries
`clears="decision events"`, so an owner parked `waiting_peer` with
`park_needs="decision events"` and `park_clear_by=<watcher id>` is rung by
the daemon. After 55 quiet minutes it sends one quiet line, which keeps the
prompt cache warm, unless the owner is parked `done`. `--detach` starts it
as a detached process (one per session) and prints its board id; it exits
when the owner leaves the board, when `--until-lease` names a lease the
owner no longer holds, after 14 days, or on `--stop`. When `gh` is missing
or fails five polls in a row it tells the owner, then exits 2. Each event is
reported once, and each message and exit appends one ledger line. The
board client gains `update`, and `clears` and `urgent` on `send`. This
replaces the contributor script `ops/delegate_watch.py`. Measured on
2026-10-14 over six delegate sessions: about a third of their turns took no
action, and 2,504 began with a background-task notification.
