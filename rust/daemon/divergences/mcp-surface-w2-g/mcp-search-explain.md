**MCP-SEARCH-EXPLAIN:** closed. `memory_search(explain=true)` performs the
ordinary search and cortex projection with verbose entries, then independently
retrieves the ranking trace. It preserves Python's original episode filter,
normalized tags, trace score-floor and recency defaults, access-count timing,
and single retrieval event. A blank query returns `trace: null`; non-finite
trace floats use the MCP serializer's null representation.

D7 bucket: closed. Implementation: `read/mcp_search.rs::mcp_memory_search`
and `read/trace.rs::trace`. The master `0f4f71ea` baseline refused all twelve
live explain cases. The offline `run.py` scenario `mcp-search-explain` now
matches Python in all nine cases and final bank state, and replays the
committed `harness/goldens/mcp-search-explain.json` without Python. The
`trace-no-trace` negative control changes five responses. The fixture also
asserts that explain preserves the ordinary search's IDs, scores and order;
`harness/mcp_search_baseline.py` compares four explain-false responses byte
for byte against the unmodified daemon and compares final bank state.

The live `w2d_search.py` explain scenario matches Python in all twelve
cases and final bank state with the pinned oracle runtime. Real-model
coverage remains in `w2d_search.py`'s `mcp-search-explain` and
`mcp-search` scenarios, including reranking, cortex projection, original
episode filters and subsequent access counts. This adapter does not change
the embedding, retrieval, ranking or extraction algorithms.
