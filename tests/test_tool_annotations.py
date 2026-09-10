"""Approval hints describe each tool's entire argument space, not one probe."""
import asyncio


READS = {
    "memory_search", "memory_recent", "memory_stats", "memory_fact_get",
    "memory_history", "memory_world_search", "memory_lesson_search",
    "memory_episode_summary", "memory_consolidation_candidates",
    "memory_graph", "memory_recall", "document_search",
}
DESTRUCTIVE = {
    "memory_supersede", "memory_forget", "memory_fact_set", "memory_set_remove",
    "memory_fact_resolve", "memory_consolidate", "memory_graph_unrelate",
    "memory_graph_review", "memory_dream", "memory_world_set",
    "memory_graph_relate", "memory_alias", "memory_relation_define",
}


def test_all_tools_have_conservative_approval_hints():
    from pseudolife_memory.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert READS | DESTRUCTIVE <= names
    for tool in tools:
        a = tool.annotations
        assert a is not None, tool.name
        assert a.read_only_hint == (tool.name in READS), tool.name
        assert a.destructive_hint == (tool.name in DESTRUCTIVE), tool.name
        # Ingest reads a local path; dream can call the configured extractor.
        assert a.open_world_hint == (tool.name == "memory_dream")
        if tool.name not in READS:
            assert a.idempotent_hint is False, tool.name
