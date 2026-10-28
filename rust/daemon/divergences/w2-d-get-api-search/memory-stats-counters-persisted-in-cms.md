| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_stats` counters persisted in `cms_counters` (`interaction_count`, per-band `hit_count`, `retrieval_queries`; schema v57, D2) | restored at startup from the bank's row, so they count across restarts | the same | closed (B4): the `cms_counters` rows below |
