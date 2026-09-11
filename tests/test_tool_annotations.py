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
    "memory_store", "document_ingest",
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
        # Dream calls an extractor; addressed mail can reach another agent.
        assert a.open_world_hint == (tool.name in {"memory_dream", "memory_message"})
        if tool.name not in READS:
            assert a.idempotent_hint is False, tool.name


def test_store_can_replace_a_canonical_value_when_auto_promotion_is_enabled(pristine_service, monkeypatch):
    from pseudolife_memory.mcp_server import mcp

    svc = pristine_service
    monkeypatch.setattr(svc.config.memory.cortex, "auto_promote", True)
    svc.store("The fixture pipeline default timeout is 250 ms", source="fixture", origin="user")
    assert svc.cortex_lookup("fixture pipeline", "default timeout")["value"] == "250 ms"
    svc.store("The fixture pipeline default timeout is 500 ms", source="fixture", origin="user")
    assert svc.cortex_lookup("fixture pipeline", "default timeout")["value"] == "500 ms"
    manifest = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert manifest["memory_store"].annotations.destructive_hint is True


def test_document_reingest_can_replace_existing_chunk_text(pristine_service, tmp_path):
    from pseudolife_memory.mcp_server import mcp

    svc = pristine_service
    document = tmp_path / "fixture.txt"
    prefix = "Stable fixture document introduction. " * 4
    document.write_text(prefix + "Original ending.", encoding="utf-8")
    assert svc.ingest_document(str(document), source="fixture-doc")["chunks_stored"] == 1
    before = svc._reference._collection.get(include=["documents"])
    document.write_text(prefix + "Replacement ending.", encoding="utf-8")
    assert svc.ingest_document(str(document), source="fixture-doc")["chunks_stored"] == 1
    after = svc._reference._collection.get(include=["documents"])
    assert after["ids"] == before["ids"]
    assert after["documents"] == [prefix + "Replacement ending."]
    manifest = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert manifest["document_ingest"].annotations.destructive_hint is True
