| Item | Python | Rust | Owner |
|---|---|---|---|
| A compaction save that fails | `PersistenceError` leaves `compact_superseded`: the remaining stores are not compacted and the rest of that sweep tick (run and retrieval pruning, the dream) is skipped; the slots stay dirty for the next autosave | logged and counted (`persist_errors`); the other stores and the rest of the tick go on, and the next sweep saves any store still holding dirty slots (this daemon's autosave does not save the canonical stores) | declared (B4) |
