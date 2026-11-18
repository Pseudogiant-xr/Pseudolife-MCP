| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-TRANSFER-LITE | 2/3 | Export and import through the lite tier's embedded pg0 instance. `transfer_cli.py`, `storage/embedded_pg.py` | The native client refuses the retired lite tier and names the Docker migration route through 0.23.x. See `shim/src/cli/transfer.rs` and `shim/src/cli/retired.rs`. | ported-with-substitution |
