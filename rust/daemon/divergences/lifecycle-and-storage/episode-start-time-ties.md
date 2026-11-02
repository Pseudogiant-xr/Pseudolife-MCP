| Item | Python | Rust | Owner | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| Episode start-time ties | startup loads episodes with `ORDER BY started_at`, without a tie key | uses the same query ordering; ties among equal start times follow the server's row order in both arms | W3 PG-HYDRATE; Python ordering retained | closed | `rust/daemon/src/startup.rs::hydrate` and `pseudolife_memory/storage/postgres.py::PostgresStorage.load_episodes` both use `ORDER BY started_at` with no secondary key; equal-time order is equally unspecified. B14b owner: none (closed). |
