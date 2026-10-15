### Fixed (2026-10-16 — CI failures report their cause and wait for observable state)
- Rust CI allows a cold Windows build to finish, bounds the override-build step, and timestamps each override configuration.
- Parity checks wait for a mismatched bank's initialization refusal, name failed coverage selections, and retry transient local socket-resource errors before sending a request.
- Cancellation, wait-mail, and suite-lock regression tests synchronize on observable state instead of short wall-clock assumptions.
