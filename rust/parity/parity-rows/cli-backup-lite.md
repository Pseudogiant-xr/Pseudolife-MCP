| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-BACKUP-LITE | 2/3 | Lite-tier backup through the embedded pg0 instance (attach or start for the duration, stop only what it started). `backup_cli.py`, `storage/embedded_pg.py` | `test_backup_cli.py::test_backup_roundtrip_embedded`; the native client refuses the retired lite tier and names the Docker migration route through 0.23.x. See `shim/src/cli/backup/mod.rs` and `shim/src/cli/retired.rs`. | ported-with-substitution |
