| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-TRANSFER-SCHEMA | 4 | Import into a bank that needs `ensure_schema` work (another schema version, missing tables, NULL v11 stamps, another owner). `transfer_cli.py`, `storage/schema.py` | Native defers by name before any effect. Blocker: needs native ensure_schema (W1-A) | deferred |
