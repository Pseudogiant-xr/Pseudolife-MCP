| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/console/sessions/revoke | 3/4 | deferred | 401 / 404 | Not a `ConsoleRoutes` entry (`web/api.py` `_console_route`): signs one browser out, an operator any, anyone else only their principal's. Rust answers 401 without a bearer and 404 with one (divergence CONSOLE-SIGNIN). tests/test_console_signin.py |
