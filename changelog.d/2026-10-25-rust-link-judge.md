### Changed (2026-10-25 — the Rust daemon's sweep runs the link judge)
- With dreaming enabled, the Rust daemon's sweep never judged pending link
  proposals. It now runs the link judge as Python does: shadow records a
  verdict per proposal, and asks a second model about an accept or reject
  that fell short of its confidence gate; auto promotes a confident accept
  to a live edge and rejects a confident reject (remembering the decision,
  which is reconsidered when the endpoints' evidence changes), applies two
  agreeing votes only when they came from different models, and records a
  retype for a reviewer without applying it. The curation and candidate
  judges are still not run by the Rust daemon.
