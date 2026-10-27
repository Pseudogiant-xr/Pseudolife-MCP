### Changed (2026-10-25 — the Rust daemon's sweep runs the junk judge)
- With dreaming enabled, the Rust daemon's sweep ran the merge judge but not
  the review-queue judges, so pending junk-entity proposals were never
  judged on it. It now runs the junk judge as Python does: shadow records a
  verdict per proposal; auto keeps a real entity (closing the proposal and
  remembering the decision so a re-mint is not re-proposed) and deletes a
  junk one only under the evidence bar (low degree, at most one fact),
  writing a graph snapshot first. An automatic keep is reconsidered when the
  entity's evidence changes. The link, curation and candidate judges are
  still not run by the Rust daemon.
