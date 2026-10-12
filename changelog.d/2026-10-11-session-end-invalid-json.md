### Fixed (2026-10-11 — reject invalid native SessionEnd requests)
- The native daemon returns HTTP 400 with `invalid_json` for malformed or non-object SessionEnd bodies, instead of acknowledging a close that did nothing. This deliberately differs from Python's HTTP 200 no-op response; empty bodies and valid objects keep their existing behavior.
