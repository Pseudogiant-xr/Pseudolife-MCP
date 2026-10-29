| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| BOARD-ASYNC | 4 | Receive <=30s, max64 waiters/16 permits/4-worker isolation, cancellation retains capacity, subscribe-before-read, generations/attachments and notifier shutdown. `web/coordination.py`, `coordination_adapter.py` | `test_coordination_wait.py`, `test_coordination_dispatch_isolation.py`, `test_coordination_adapter.py` I/mixed; add concurrent wire disconnect/timeout/restart cases | deferred |
