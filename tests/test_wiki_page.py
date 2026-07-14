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
    st.conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, "
        "status, confidence, asserted_at, last_confirmed, entity_id) "
        "VALUES ('daemon','role','daemon','role','serves MCP','current',0.9,1.0,1.0,%s)",
        (eid,))
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
