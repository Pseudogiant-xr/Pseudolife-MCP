| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-PAIRING-CONTAINER | 2/3 | `invite` without a DSN: the lite tier's embedded bank, the `docker exec` re-run in the daemon container and the psql fallback for `--list`/`--revoke`; https, proxied or terminal-prompt `pair` shapes. `invite_cli.py`, `pair_cli.py` | `test_invite_cli.py`, `test_pair_cli.py` remain the oracle; native defers by name before any effect. Blocker: native embedded_pg and container dispatch | deferred |
