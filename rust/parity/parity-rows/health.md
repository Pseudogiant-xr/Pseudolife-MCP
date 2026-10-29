| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| HEALTH | 3 | Open /health payload mandatory and conditional fields and 200/503/500 mapping listed below; bank fingerprint only after successful DB ping; warnings that leave status ok remain non-fatal. `daemon.py:_build_health_payload`, `web/api.py` | `test_daemon_http.py`, `test_dim_mismatch_health.py`, `test_daemon_moved_fence.py` I/P; network health corpus pending | deferred |
