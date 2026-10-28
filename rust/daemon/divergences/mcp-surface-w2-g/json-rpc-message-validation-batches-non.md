| Item | Python | Rust | Owner |
|---|---|---|---|
| JSON-RPC message validation: batches, non-object messages, ids that are not integers or strings | pydantic union validation | 400 -32602 with a fixed message (the text after `Validation error: ` is free) | non-canonical |
