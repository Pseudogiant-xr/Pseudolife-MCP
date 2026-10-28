| Item | Python | Rust | Owner |
|---|---|---|---|
| Episode start-time ties | startup loads episodes with `ORDER BY started_at`, without a tie key | uses the same query ordering; ties among equal start times follow the server's row order in both arms | W3 PG-HYDRATE; Python ordering retained |
