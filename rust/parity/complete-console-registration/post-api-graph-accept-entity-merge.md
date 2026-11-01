| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/graph/accept-entity-merge | 4 | ported | served | `routes/graph.rs`; `w2e_graph_actions.py`: Console HTTP status/body and durable-state differential scenarios ; `console-graph-malformed` compares boolean, array, object and Unicode-digit IDs, including rejection retirement before binding errors |
