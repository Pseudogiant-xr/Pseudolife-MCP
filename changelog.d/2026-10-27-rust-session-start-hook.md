### Added (2026-10-27 — the Rust daemon serves the SessionStart hook)
- On the Rust daemon the plugin's SessionStart hook got a 501 for every
  caller, so a session on it started with no memory briefing, no episode
  handle and no registration, and the active-session pointer never moved.
  The hook is now served as the Python daemon serves it: the version,
  hooks and release notices, the dream-stall and review-queue lines, the
  session's registration with its episode handle, the startup memory
  policy, the daemon-side custom instructions, onboarding on an empty bank
  and the bounded briefing (lessons, world facts and where the last
  session left off), or a short pointer on a resume or compaction.
