| Item | Python | Rust | Owner / why | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| A refresh failure's `error` text | `str(exc)` | the Rust error's text | failure path only | presentation | At `54063c1788f2`: `rust/daemon/src/graph_insight.rs` `safe_refresh` returns refreshed=false/error=e.to_string(); `pseudolife_memory/service_dream.py` `_safe_refresh_graph_insight` returns the same failure envelope with str(exc). This row covers diagnostic wording, not algorithm differences. B14b owner: B14b presentation disposition. |
