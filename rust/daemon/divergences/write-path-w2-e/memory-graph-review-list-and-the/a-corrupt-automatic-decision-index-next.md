| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A corrupt automatic-decision index (`next_seq` not an integer, `cursors`/`active` not mappings) | `int()` / `dict()` raise | read leniently in `list` | corrupt persisted data only; the judges write the index |
