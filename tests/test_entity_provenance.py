"""Entity provenance for the Atlas review queue — the MIRAS source entries +
project attribution behind a graph entity, so a human can judge a
merge/junk/link finding. PG-backed (skips without a test server)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path_factory):
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path_factory.mktemp("prov-svc"), database_url=pg_url)


def _seed_entity_with_provenance(svc, *, band="forever", source="pseudolife"):
    """An entity that carries a current fact, a source entry behind that fact's
    slot (via memory_traces), and a project attribution row."""
    with svc._lock:
        svc._ensure_init()
        eid = svc._resolve_or_create_entity("daemon")["id"]
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
    st.conn.commit()
    return eid, entry_id


def test_entity_provenance_returns_sources_and_entries(svc):
    _seed_entity_with_provenance(svc)
    out = svc.entity_provenance("daemon")
    assert out["found"] is True and out["entity"] == "daemon"
    assert any(s["source"] == "pseudolife" for s in out["sources"])
    assert out["entries"] and out["entries"][0]["band"] == "forever"
    assert "docker" in out["entries"][0]["text"]


def test_entity_provenance_resolves_colloquial_name_via_norm(svc):
    _seed_entity_with_provenance(svc)
    # mixed-case / spacing resolves through the same norm as the graph entity
    out = svc.entity_provenance("Daemon")
    assert out["found"] is True and out["entity"] == "daemon"


def test_entity_provenance_unknown_entity_is_not_found(svc):
    out = svc.entity_provenance("nonexistent thing")
    assert out["found"] is False
