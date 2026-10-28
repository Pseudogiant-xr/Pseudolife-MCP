| Item | Remaining behavior | Owner / reason |
|---|---|---|
| Resident cortex refresh | `memory_fact_set`, `memory_set_add`, `memory_set_remove` and `memory_fact_resolve` update the resident cortex in place (readers take snapshots), `memory_world_set` and `memory_forget` the resident world store, and `memory_forget` the resident lesson store (readers take snapshots); auto-promotion stays disabled | W2-E auto-promotion increment |
