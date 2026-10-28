| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A merge-group tie between keys of different types (merge rows with a NULL target) | `TypeError: '<' not supported between instances of …` | `invalid_argument` with Rust's own wording | merge rows always carry a target |
