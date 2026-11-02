| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/console/session | 3/4 | deferred | 401 | Pre-auth, browser-called, served before the bearer gate beside `/api/pair` (`web/api.py` `_signin`): Origin equal to the code's, JSON only, 1 KiB, uniform 400 `signin_refused`, its own failure budget and two slots. Rust has no pre-auth dispatch for it, so the bearer gate answers 401 (divergence CONSOLE-SIGNIN). `tests/test_console_signin.py`, `tests/test_console_sessions_pg.py` |
