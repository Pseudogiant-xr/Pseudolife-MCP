| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Locking across the walk | none of its own: each search and each graph read takes the service lock, so writes can land between them; the vocabulary and the degrees are two locked graph loads | each graph read takes the writer lock, but a search takes only the search guard, which does not exclude writes (as for `memory_search`); one graph load serves the vocabulary and the degrees | a write landing mid-walk is visible to the later steps on both; only a write racing one search, or landing between Python's two loads, can tell them apart |
