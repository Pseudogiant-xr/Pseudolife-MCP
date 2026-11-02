**MCP-SEARCH-EXPLAIN:** `memory_search(explain=true)` answers the refusal
envelope `{"error": "not_implemented", "message": "not_implemented: explain
(the ranking trace) is not yet served by the Rust daemon"}` before any
search runs, so no retrieval event or access count is written. Python runs
the search, then `service.trace` (a second, instrumented retrieval) and
serves verbose entries with `trace`. The instrumented retrieval itself is
served since B2a's trace PR (`GET /api/trace`, `search::rank_traced`); the
MCP adapter over it (search, then the traced retrieval, verbose entries
with `trace`) is a follow-up. The search, store and board MCP adapters are now registered in
`mcp/dispatch.rs`; that registration does not serve the explain option. Bound identity is enforced on the mount (M18). D7 bucket: behaviour with a caller. Code verification: `rust/daemon/src/read/mcp_search.rs::mcp_memory_search` still refuses explain before search; `pseudolife_memory/mcp_server.py::memory_search` runs search then trace; `rust/daemon/src/read/trace.rs::route` serves REST trace only. Other body adapters are registered in `rust/daemon/src/mcp/dispatch.rs::registry` and do not close this option. Caller: agents calling the exposed `memory_search(explain=true)` parameter in `pseudolife_memory/mcp_server.py::memory_search`; B14b owner: B2a read projections / MCP search adapter.
