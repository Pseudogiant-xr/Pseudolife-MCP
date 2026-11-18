| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-CONNECT-PAIR | 2 | `connect --code` / `--read-code` (pairing during connect). `connect_cli.py`, `pair_cli.py` | The native command redeems the code and re-points client registrations in the same run. See the pairing-code and prompt cases in the `connect` harness row. | ported |
