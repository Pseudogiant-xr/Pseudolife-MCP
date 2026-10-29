| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| import | 2 | deferred | partly | Explicit-DSN import into a current-schema bank ported as CLI-TRANSFER-DSN; the lite tier and banks needing ensure_schema work (CLI-TRANSFER-LITE, CLI-TRANSFER-SCHEMA) defer by name; C Phase 4 direct psycopg durable writes; `test_transfer_cli.py::test_import_refuses_a_nonempty_bank`, `test_import_refuses_while_other_connections_hold_the_bank`, `test_import_refuses_embedding_dim_mismatch`, `test_import_leaves_the_curation_spelling_flag_to_the_export` |
