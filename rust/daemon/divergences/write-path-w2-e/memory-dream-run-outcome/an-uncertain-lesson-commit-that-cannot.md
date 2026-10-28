| Item | Python | Rust | Owner / why |
|---|---|---|---|
| An uncertain lesson commit that cannot be reconciled | latches `lesson_reconciliation_required`, blocking lesson reads and the snapshot until a recheck or restart | answers `reconciliation_required` and the error; reads are not blocked | the read slices' barrier work; a commit whose outcome is unknown |
