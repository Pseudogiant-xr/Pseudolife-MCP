| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Reply nesting between about 900 and the interpreter's depth limit | parsed | `RecursionError` | Python's limit depends on the stack; Rust's is a fixed 900 |
