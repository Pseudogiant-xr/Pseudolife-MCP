### Changed (2026-10-25 — the Rust daemon's sweep runs the curation judge)
- With dreaming enabled, the Rust daemon's sweep never judged the lesson and
  world duplicate listings. It now runs the curation judge as Python does:
  shadow remembers each verdict bound to the two records it saw;
  auto-distinct also marks a pair it judges distinct as dismissed, and
  reopens that dismissal when either record or the judging policy changes.
  In auto, a duplicate safe to fold is recorded but not yet folded by the
  Rust daemon; it waits for the fold to be ported (a Python daemon on the
  same bank folds it).
