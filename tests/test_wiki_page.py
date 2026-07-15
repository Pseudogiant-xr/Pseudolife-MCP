"""Wiki page assembly — the one-call payload behind GET /api/wiki.
PG-backed (skips without a test server)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path_factory):
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path_factory.mktemp("wiki-svc"), database_url=pg_url)


def _seed(svc, *, band="forever", source="pseudolife"):
    """An entity with a current fact, a traced source entry, a project
    attribution row, and one explicit edge to a second entity."""
    with svc._lock:
        svc._ensure_init()
        eid = svc._resolve_or_create_entity("daemon")["id"]
        other = svc._resolve_or_create_entity("docker-desktop")["id"]
    st = svc._storage
    entry_id = st.insert_entry({
        "band": band, "text": "the daemon runs in docker",
        "embedding": np.zeros(384, dtype=np.float32), "surprise": 0.5, "ts": 1234.0,
        "access_count": 0, "source": source, "superseded_at": None,
        "superseded_by_text": None, "last_logical_turn": None, "episode_id": None,
        "episode_title": None, "tags": [], "slots": [],
    })
    # Through the real write path so the in-memory cortex (which wiki_page
    # reads) and the persisted facts row stay in step; then pin the graph FK
    # the provenance bridge keys on.
    svc.cortex_write("daemon", "role", "serves MCP", confidence=0.9, support="user")
    st.conn.execute(
        "UPDATE facts SET entity_id = %s "
        "WHERE entity_norm = 'daemon' AND attribute_norm = 'role'", (eid,))
    st.add_trace("daemon", "role", entry_id, 1234.0)
    st.upsert_entity_source(eid, source, "derived", time.time())
    st.conn.execute(
        "INSERT INTO edges (src_id, relation, dst_id, confidence, origin, asserted_at) "
        "VALUES (%s, 'runs-on', %s, 0.9, 'user', 2000.0) ON CONFLICT DO NOTHING",
        (eid, other))
    st.conn.commit()
    return eid, other, entry_id


def test_find_entity_and_load_graph_expose_created_at(svc):
    _seed(svc)
    st = svc._storage
    e = st.find_entity("daemon")
    assert isinstance(e["created_at"], float) and e["created_at"] > 0
    g = st.load_graph()
    assert all(isinstance(row["created_at"], float) for row in g["entities"])


def test_wiki_page_assembles_identity_facts_relations_mentions(svc):
    _seed(svc)
    out = svc.wiki_page("daemon")
    assert out["found"] is True and out["entity"] == "daemon"
    assert out["canonical"] == "daemon" and isinstance(out["first_seen"], float)
    assert any(p["source"] == "pseudolife" for p in out["projects"])
    assert [f["attribute"] for f in out["facts"]] == ["role"]
    assert out["facts"][0]["history_available"] is False
    assert any(r["target"] == "docker-desktop" and r["relation"] == "runs-on"
               for r in out["relations"]["out"])
    # The inverse edge is derived (rule provenance) and must be marked as such.
    assert all(r["derived"] for r in out["relations"]["in"])
    assert any(r["source"] == "docker-desktop" for r in out["relations"]["in"])
    assert out["mentions"] and "docker" in out["mentions"][0]["text"]


def test_wiki_page_timeline_merges_and_orders_newest_first(svc):
    _seed(svc)
    tl = svc.wiki_page("daemon")["timeline"]
    kinds = {t["kind"] for t in tl}
    assert {"entity-created", "edge-asserted", "fact-stamped", "mention"} <= kinds
    ts = [t["ts"] for t in tl]
    assert ts == sorted(ts, reverse=True)


def test_wiki_page_world_facts_filtered_to_entity(svc):
    _seed(svc)
    svc.world_write("daemon", "latest-release", "v2.0",
                    source_url="https://example.com/rel", source_quote="v2.0 shipped")
    svc.world_write("unrelated", "x", "y",
                    source_url="https://example.com/x", source_quote="q")
    wf = svc.wiki_page("daemon")["world_facts"]
    assert [w["attribute"] for w in wf] == ["latest-release"]
    assert wf[0]["source_url"] == "https://example.com/rel"


def test_wiki_page_unknown_entity_not_found(svc):
    assert svc.wiki_page("nonexistent thing")["found"] is False


def test_wiki_page_resolves_colloquial_name_and_flags_unattributed(svc):
    with svc._lock:
        svc._ensure_init()
        svc._resolve_or_create_entity("lonely node")
    svc._storage.conn.commit()
    out = svc.wiki_page("Lonely Node")
    assert out["found"] is True and out["entity"] == "lonely node"
    assert {"kind": "unattributed"} in out["flags"]


def test_whole_graph_payload_carries_timestamps(svc):
    _seed(svc)
    out = svc.graph_neighborhood(entity=None)
    assert out["found"] is True
    assert all(isinstance(n["created_at"], float) for n in out["nodes"])
    assert all(isinstance(e["asserted_at"], float) for e in out["edges"])
