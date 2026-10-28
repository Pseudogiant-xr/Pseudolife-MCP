| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `_entity_kind_cache` reset when a changed writer lease rehydrates the service (`service.py:1416`) | the next `"auto"` fact write rereads `entity_kinds` | the same: the cache lives in the ready service's write state, which the rehydrate drops | closed (B4) |
