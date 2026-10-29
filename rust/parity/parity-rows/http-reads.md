| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| HTTP-READS | 3 | Every GET Console route including configuration, episodes, graph/review/provenance and telemetry reads. `web/routes.py`, `web/config_io.py` | `test_web.py`, `test_coordination_console.py`, `test_coordination_web.py`, `test_web_hardening.py` I/mixed; add HTTP corpus for every GET route | deferred |
