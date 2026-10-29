| Boundary | Candidate coverage | Status | Notes |
|---|---|---|---|
| Query limit | `ascii-limit`: ASCII decimal digits, optional sign and ASCII whitespace, default 50 and clamp 1..200; substitution for pinned `web/routes.py:31` Python `int()` | deferred | candidate; underscores/Unicode digits/Unicode whitespace fall back to 50; no CPython digit cap emulation |
