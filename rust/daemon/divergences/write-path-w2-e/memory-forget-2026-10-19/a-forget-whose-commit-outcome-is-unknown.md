| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A forget whose commit outcome is unknown | (reconciled by the read/save barriers) | the transaction rolls back, or a lost commit answer is an internal error with the resident bank unchanged | the lifecycle increment, with the corrections' commit-unknown row above; failure path only |
