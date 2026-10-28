| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A judge memo, decision index or response identity meta value that is not an object, or a decision record missing a key | `.items()` / `.get()` / `int()` raise, and the tick answers `error` | read as empty, or the key as absent | corrupt persisted data only; the judge writes these values |
