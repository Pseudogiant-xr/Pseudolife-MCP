### Changed (2026-10-25 — the Rust daemon dreams on its own again)
- The Rust daemon's background sweep ran only its reapers and the merge
  judge, so with `memory.dream.enabled` on a backlog was never dreamed
  automatically, idle sessions closed without the dream they trigger, a
  stalled extractor was never announced on the board, and the deep dream's
  mechanical half and the analyzer's duplicate filing never ran on their
  own. The sweep now runs the Python sweep's steps in its order: the
  analyzer filing tick (with its pair reconciliation), the deep tick, the
  merge judge, `dream_status`, the stall tick (the overdue rule and the
  begin, repeat, escalate and clear notices) and the automatic dream; the
  session reaper's idle close fires the session-end dream in the
  background. The link, junk, curation and candidate judges are still not
  run by the Rust daemon.
- `memory.dream.stall_repeat_hours` is now read (it was only validated).
