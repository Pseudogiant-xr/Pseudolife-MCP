### Added (2026-10-27 — the Rust daemon serves the CLI's episode routes)
- On the Rust daemon `POST /api/episode/start` and `POST /api/episode/end`
  answered 501, so the CLI's episode hooks and the shims could not open or
  close a session's episode there. Both are now served as the Python daemon
  serves them: start opens or re-finds the session's episode and registers
  it, and end closes an explicit session, or with none only the caller's
  own (its `X-PL-Session` header, else the active session), pruning an
  empty one and firing the session-end dream after a session that captured
  memories. `GET /api/briefing` follows in a later change.
