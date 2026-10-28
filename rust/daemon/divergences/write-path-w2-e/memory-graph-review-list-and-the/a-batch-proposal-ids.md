| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A batch (`proposal_ids`) | takes the service lock per call, so another request can run between items | holds the writer lock for the whole batch | strictly more serialized; a single client sees the same results |
