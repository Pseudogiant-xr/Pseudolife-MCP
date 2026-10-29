| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-CONNECT-PAIR | 2 | `connect --code` / `--read-code` (pairing during connect). `connect_cli.py`, `pair_cli.py` | Native defers before any effect. Blocker: connect's own pairing flow (redeem the code, then re-point registrations in the same run) is not ported; the standalone `pair` leaf is (CLI-PAIRING) | deferred |
