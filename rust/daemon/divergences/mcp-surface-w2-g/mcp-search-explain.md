**MCP-SEARCH-EXPLAIN:** `memory_search(explain=true)` answers the refusal
envelope `{"error": "not_implemented", "message": "not_implemented: explain
(the ranking trace) is not yet served by the Rust daemon"}` before any
search runs, so no retrieval event or access count is written. Python runs
the search, then `service.trace` (a second, instrumented retrieval) and
serves verbose entries with `trace`. The trace port belongs with
`/api/trace` (`w2d-console-projections`). W2-D supplies expanded REST search, W2-E supplies the
harness-gated store body, and W2-F supplies the REST board. Their presence
does not register MCP tool bodies: their projections remain the
prerequisites listed above. Bound identity is enforced on the mount (M18).
