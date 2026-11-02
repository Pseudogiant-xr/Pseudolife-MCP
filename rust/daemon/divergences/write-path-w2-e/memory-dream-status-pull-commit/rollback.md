| Item | Python | Rust | Owner / why | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| `rollback` | served | refused | the rollback increment (`deep` is served since 2026-10-20) | behaviour with a caller | At `54063c1788f2`: `rust/daemon/src/dream/service.rs` `mcp_memory_dream` explicitly routes rollback to `unserved`; `pseudolife_memory/service_dream.py` `dream_rollback` implements journal rollback. Caller: `pseudolife_memory/mcp_server.py` `memory_dream(action="rollback")` calls `service.dream_rollback`; B14b owner: B1-P (dream rollback), B14b. |
