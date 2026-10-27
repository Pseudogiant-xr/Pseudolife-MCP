### Fixed (2026-10-27 — the Rust daemon keeps its memory counters across restarts)
- The Rust daemon's autosave and clean-exit flush did nothing, so the
  counters `memory_stats` reports (interactions, retrieval queries and
  per-band hits) and the per-band surprise history started from zero at
  every restart. It now writes them to the bank's `cms_counters` row as the
  Python daemon does: when the bank or its facts changed since the last
  save (or the last write failed), from the autosave loop and at a clean
  exit; a restart reads them back. Two counters it does not maintain yet are kept
  as the row holds them rather than zeroed.
- A bank whose counters still sit in an old `memory_state/weights.pt`
  (from a Python release before schema 57) is not imported by the Rust
  daemon, which does not read that file format. It logs this once and
  `/health` shows `counters_import_required` with the step: start the
  Python daemon once on that bank. The cutover's update step will do that
  before switching a host to the Rust daemon.
