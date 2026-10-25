### Changed (2026-10-25 — the Rust daemon's sweep runs the candidate judge)
- With dreaming enabled, the Rust daemon's sweep never judged the deep
  dream's link candidates (unlinked near-pairs). It now runs the candidate
  judge as Python does, one slice per tick after each deep apply: shadow
  remembers each verdict; auto files a proposed link for the link judge to
  settle and marks a dismissed pair distinct, remembering the dismissal and
  reopening it when the pair's evidence changes. Its fingerprints are the
  same bytes Python writes, so verdicts and dismissals recorded by either
  daemon stay valid on the other. The curation judge is still not run by
  the Rust daemon.
