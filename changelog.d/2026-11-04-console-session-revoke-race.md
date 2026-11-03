### Fixed (2026-11-04 — a Console browser revoked right after it signs in stays signed out)
- A sign-in publishes its session to the daemon's view just after it commits. Revoking that session in Settings, Signed-in browsers in the moment between the two left a late publish that put it back, so the browser stayed signed in for up to ten seconds. The revocation now holds.
