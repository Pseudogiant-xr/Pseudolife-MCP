| Item | Remaining behavior | Owner / reason |
|---|---|---|
| Malformed hydrated slot rows | Python may raise while unpacking a malformed persisted slot; Rust ignores the entire entry's structured-slot comparison if any row is not four strings, then retains the text/negation paths | W2-E retained substitution for corrupt or noncanonical persisted data; `malformed_rows_and_superseded_entries` pins it, and canonical producers emit four-string rows |
