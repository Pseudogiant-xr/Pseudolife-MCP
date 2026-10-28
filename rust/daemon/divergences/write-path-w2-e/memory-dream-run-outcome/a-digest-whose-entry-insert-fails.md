| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A digest whose entry insert fails | the resident entry is kept without a row id, so later dreams see the root digested | nothing resident: the root is retried (one held retry, then advance) | failure path only |
