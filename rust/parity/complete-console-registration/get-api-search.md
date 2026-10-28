| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| GET | /api/search | 3 | ported | served | `read/search_route.rs`; `daemon/harness/w2d_search.py live`: recorded Linux run, 11 scenarios, 118 cases, zero response and bank-state differences ([W2-D evidence](daemon/FORWARD-w2d.md)); hosted CI runs only the offline controls. The reference pool since B7's second Rust PR: `daemon/harness/w2e.py live` `documents-search-*` scenarios (recorded Linux run cited in that PR). Declared: `explain` refused (MCP-SEARCH-EXPLAIN), resident-only access counts |
