| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| HTTP-SECURITY | 3 | Health public/degraded semantics; MCP/REST bearer gates on UTF-8/Latin-1 bytes, fail-closed map, remote bind/trust policy, DNS/Origin/rebinding, JSON/body limits, error status, redirect refusal. `daemon.py`, `web/api.py`, `principals.py`, `utils/no_redirect.py` | `test_daemon_http.py` P/I; `test_web.py`, `test_principals.py`, `test_extractor_no_redirect.py`, `test_shim_transport_recovery.py`, `test_dim_mismatch_health.py` I/mixed | deferred |
