### Added (2026-10-14 — A watcher that wakes a delegate session only for decisions)

- `ops/delegate_watch.py` waits, without a model, for the events a
long-running delegate session acts on, and exits with one line per event. The
events are a pull request opened, merged or closed, turning green, failing or
newly conflicting; a master workflow failing or recovering; a new suite
result; and a full-suite lease freeing up. A quiet run exits after 55 minutes
with one status line, which also keeps the prompt cache warm. Each exit
reports the session's pending board mail count from its coordination digest.
Mail itself still wakes the session only through the daemon's ring. State
persists between runs, so each event is reported once, and each exit appends
one ledger line. Measured on 2026-10-14 over six delegate sessions: about a
third of their turns took no action, and 2,504 began with a background-task
notification.
