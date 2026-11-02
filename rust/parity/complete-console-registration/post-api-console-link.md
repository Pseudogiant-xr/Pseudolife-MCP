| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/console/link | 3/4 | deferred | 401 / 404 | Not a `ConsoleRoutes` entry (`web/api.py` `_console_route` serves it behind the bearer gate; a Console session is refused by name, 403 `session_cannot_issue`); answers a one-time sign-in code and `<origin>/ui/#/signin?code=…`. Rust answers 401 without a bearer and 404 with one (divergence CONSOLE-SIGNIN). `tests/test_console_signin.py` |
