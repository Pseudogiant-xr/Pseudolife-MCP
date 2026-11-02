| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| GET | /api/console/sessions | 3/4 | deferred | 401 / 404 | Not a `ConsoleRoutes` entry (`web/api.py` `_console_route`): the signed-in browsers, every one for an operator, else the caller's principal's, with `operator` and `current`. Rust answers 401 without a bearer and 404 with one (divergence CONSOLE-SIGNIN); not in `SHADOW_SAFE_GETS`. tests/test_console_signin.py |
