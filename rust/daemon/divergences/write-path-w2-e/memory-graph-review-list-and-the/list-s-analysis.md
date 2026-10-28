| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `list`'s analysis | between two locked blocks, so writes can land while it runs and `recent_merges` can follow a later state than the findings | under the writer lock throughout | writes wait for the analysis (quadratic in the entities); the answer is one consistent state |
