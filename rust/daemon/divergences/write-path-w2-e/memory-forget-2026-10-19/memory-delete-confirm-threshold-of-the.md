| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `memory.delete_confirm_threshold` of the wrong type | `int(value or 0)` per call (a float truncates) | refused at config load, like every other typed key | the existing config-type substitution |
