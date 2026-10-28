| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A malformed `meta.session_digest_cursor` (a `ts` that is not a number or numeric string, a `retry` that is not an object, a non-iterable or non-string `regenerate`) | `float()` / `dict()` / `sorted()` raise their own `TypeError` / `ValueError` text | the same refusal kind (`invalid_argument`) with Rust's own wording | corrupt persisted data only; the dream writes the cursor |
