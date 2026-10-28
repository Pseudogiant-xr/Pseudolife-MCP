| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Entry insert fails after eviction | the entry stays resident (served until restart) and the error propagates | the entry is not kept; the error propagates | failure path only; keeps bank and table aligned |
