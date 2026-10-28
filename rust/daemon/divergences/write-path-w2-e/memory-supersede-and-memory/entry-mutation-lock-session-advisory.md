| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `entry_mutation_lock` (session advisory locks per target id, held from before the transaction to after publication) | taken | not taken; the per-operation `pg_advisory_xact_lock` of `reinstate_entry` is | the leased single writer already serializes every entry mutation in one process; another writer cannot hold the bank |
