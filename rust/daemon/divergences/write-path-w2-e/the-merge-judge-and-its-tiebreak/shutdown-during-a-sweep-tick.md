| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Shutdown during a sweep tick | the sweep's daemon thread dies with the process | the tick stops after the merge, link, junk, curation and candidate judges when shutdown has begun, before `dream_status` and the automatic dream; an automatic dream already in flight is dropped when the stop signal comes, so shutdown does not wait out a model's timeout |
