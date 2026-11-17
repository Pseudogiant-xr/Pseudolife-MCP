### Fixed (2026-11-18 — actionable native expose argument errors)
- Native `expose` now refuses malformed arguments with a usage error and a repair instead of reporting the mode as deferred. Unicode arguments work independently of locale; valid status, consent, apply and off flows retain their behavior.
