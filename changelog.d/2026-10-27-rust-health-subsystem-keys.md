### Fixed (2026-10-27 — the Rust daemon's /health says when dreams stall, the bank is nearly full or dream tracking failed)
- The Rust daemon's `/health` left out three of Python's warnings, so a
  stalled dream extractor, a bank about to start deleting old memories and
  a failed dream-tracking start were invisible there. It now reports them
  as Python does: `extractor` reads `"stalled"` with a `stall` record
  (reason and times only) once two live dreams in a row fail, a fallback
  that serves instead of the primary keeps `"configured"` with the record,
  `capacity_warning: true` appears when the last band is 80% full, and
  `dream_tracking_error` names the failure's code. None of them changes
  `status`, so the Docker healthcheck never restarts a daemon that is
  still serving.
- `/health` also logs the "daemon memory is near its limit" warning, at
  most every ten minutes, as Python does.
- `migration_partial` and `lesson_reconciliation_required` stay absent:
  the Rust daemon has neither a legacy `.pt` import nor a lesson latch yet.
