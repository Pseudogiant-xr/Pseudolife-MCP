| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-MIGRATE-FLAT | 2 | Opt-in offline band collapse, backup pair, capacity preservation and resumable config replacement. `migrate_flat_cli.py` | `rust/cli_harness/rows/migrate_flat.py`, physical restore harness; new migration tests, exact boundary and normalization in `rust/cli_harness/rows/migrate_flat.md` | ported |
