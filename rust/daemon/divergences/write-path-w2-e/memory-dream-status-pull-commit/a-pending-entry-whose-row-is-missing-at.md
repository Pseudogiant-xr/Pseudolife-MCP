| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A pending entry whose row is missing at a write failure | re-flushed (`cms.reflush_entries`) | detected, not re-inserted | the store slice's persistence follow-up; the answer (a write hold) is the same |
