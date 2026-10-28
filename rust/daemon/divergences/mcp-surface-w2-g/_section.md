## MCP surface (W2-G)

Bearer-authenticated `/mcp` is served (spec.md, "MCP surface (W2-G)"); `tools/list` is byte-equal
at every tier. Every tool's arguments are bound first (spec.md, "Tool argument
binding"), so binding refusals match Python for every tool. A `tools/call` of a
tool whose body has not landed then answers the
declared stub (delegate ruling 2026-10-09): the normal refusal envelope,
`isError: true`, structured content equal to the JSON text,
`{"error": "not_implemented", "message": "not_implemented: <tool> is not yet served by the Rust daemon"}`,
with no `mutation` key because nothing ran. Nothing installs the Rust daemon
before cutover, so no client sees it. The harness (`harness/mcp_compare.py`,
`UNSERVED`) checks the stub for every row here; the PR that wires a body
deletes its row in both places, and from then on the harness compares that
tool's answers by value.

Since B7's document tools no tool answers this stub: every one of the 38
has its body registered, and `UNSERVED` is empty. The stub stays the
answer for a tool added to the catalogue before its body.

The integration batch retains W2-G's refusal stubs for every tool except
`memory_toolset` and, since their body registrations, `document_ingest`,
`document_search`, `memory_store`,
`memory_search`, `memory_recent`, `memory_fact_get`, `memory_get`,
`memory_fact_set`, `memory_set_add`, `memory_set_remove`,
`memory_fact_resolve`, `memory_stats`, `memory_episode_start`,
`memory_episode_end`, `memory_session_title`, `memory_episode_summary`,
`memory_outcome`, `memory_reinforce`, `memory_history`, `memory_world_set`,
`memory_world_search`, `memory_lesson_search`, `memory_supersede`,
`memory_reinstate`, `memory_forget`, `memory_consolidation_candidates`,
`memory_graph_relate`, `memory_graph_unrelate`, `memory_alias`,
`memory_relation_define`, `memory_graph`, `memory_recall`,
`memory_graph_review`, `memory_consolidate`,
`memory_dream` (all but `rollback`, below), `memory_agents` and
`memory_message`. The two board tools
inherit W2F-CLOCK-RETRY below (it affects `send`, on REST and MCP alike).
