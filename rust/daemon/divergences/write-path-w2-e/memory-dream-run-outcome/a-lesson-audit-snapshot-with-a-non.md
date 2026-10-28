| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A lesson audit snapshot with a non-string `task`/`aspect`/`lesson`/`about`/`outcome`/`polarity`/`origin` or provenance item | passed to `lesson_write` as is | refused (`internal_error`) before that slot is written | corrupt audit rows only; as for world snapshots |
