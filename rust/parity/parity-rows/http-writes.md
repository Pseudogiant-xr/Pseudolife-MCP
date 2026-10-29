| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| HTTP-WRITES | 4 | Every POST Console route, coercion/error mapping, config whitelist/live/restart classification, backup preservation. `web/routes.py`, `config_io.py` | `test_web.py`, `test_console_source_guards.py`, `test_correction_interfaces.py`, `test_web_hardening.py` I/mixed; record POST routes and their storage effects | deferred |
