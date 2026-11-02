| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| GET | /api/stats | 3 | native; Linux differential verified | native route | CLI route calls the complete `memory_stats` service snapshot; obsolete 501 declaration removed. `cargo test -p pseudolife-daemon routes::` passes 31 cases. `daemon/harness/test_move_stats.py` passes all three authenticated REST/MCP/REST scenarios with complete snapshots, unchanged reads and no bank differences (exit 0, Linux dev box). Windows execution and hosted stress are not claimed. |
