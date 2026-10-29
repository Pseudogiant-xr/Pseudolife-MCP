| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| DELIVERY | 4 | Codex app-server messages/doorbell, opt-in host transports, wake/park leases and Claude channel behavior. `codex_delivery.py`, `codex_doorbell.py`, `codex_connection.py`, `channel.py`, `wake_liveness.py` | `test_codex_delivery.py`, `test_codex_doorbell.py`, `test_codex_doorbell_probe.py`, `test_codex_coordination.py`, `test_channel.py`, `test_stop_wake_hook.py`, `test_coordination_turn_digest.py` I/mixed/A; fake local host E2E only | deferred |
