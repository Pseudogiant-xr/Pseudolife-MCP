| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| export | 2 | deferred | partly | Explicit-DSN export ported as CLI-TRANSFER-DSN (harness row `transfer`); the lite tier (CLI-TRANSFER-LITE) defers by name; C Phase 3; `transfer_cli.py` direct psycopg/schema/vector text, torch-free at CLI module; `test_transfer_cli.py::test_export_import_roundtrip_preserves_every_table`, `test_export_skips_transient_meta_and_telemetry` |
