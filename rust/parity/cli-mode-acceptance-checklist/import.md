| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| import | 2 | deferred | partly | Explicit-DSN import now initializes and repairs targets with the daemon's generated schema plan. Connection guard precedes DDL; schema commits precede archive import checks; future/malformed stamps refuse without changes. `transfer` has blank/older/missing-table/NULL-stamp and lock-failure cases. Schema source pin passed; native build, bank differential and hosted Windows stress pending. The lite lifecycle stays deferred. |
