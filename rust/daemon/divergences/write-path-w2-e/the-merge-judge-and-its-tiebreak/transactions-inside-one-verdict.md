| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Transactions inside one verdict | nested `transaction()` savepoints around each storage call | one transaction per verdict, with one savepoint around the relate link | no data differs: a nested block only rolls back on an exception, which ends the tick in both |
