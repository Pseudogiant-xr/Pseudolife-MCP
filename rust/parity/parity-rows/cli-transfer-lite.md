| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-TRANSFER-LITE | 2/3 | Export and import through the lite tier's embedded pg0 instance. `transfer_cli.py`, `storage/embedded_pg.py` | Native defers by name before any effect. Blocker: needs native embedded_pg (W1-A/W3) | deferred |
