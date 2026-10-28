| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A stage cursor whose `retry` is a list of pairs with non-string keys, or whose lists hold non-strings where they are sorted | coerced by `dict()` / compared by `sorted()` (or raises) | refused (`internal_error`) | corrupt persisted data only; the stages write these cursors |
