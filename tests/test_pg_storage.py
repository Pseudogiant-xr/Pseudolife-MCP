"""Schema v8 + PostgresStorage round-trips (skips without a PG server)."""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


def test_ensure_schema_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    flags1 = ensure_schema(pg_conn)
    flags2 = ensure_schema(pg_conn)
    assert flags1 == {}  # AGE removed; ensure_schema returns empty dict
    assert flags1 == flags2


def test_schema_version_recorded(pg_conn):
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    row = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None and int(row[0]) == SCHEMA_META_VERSION


def test_write_mode_default_is_snapshot():
    """The storage write path defaults to the live snapshot rewrite; occ is a
    dormant Phase-2 seam (v0.4 T6). No PG needed."""
    from pseudolife_memory.utils.config import AppConfig

    assert AppConfig().storage.write_mode == "snapshot"


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


def test_occ_write_path_is_phase2_stub(storage):
    """The optimistic-concurrency (per-row CAS) path is a clearly-marked stub —
    building it is a separate Phase-2 plan; v0.4 only lays the seam."""
    with pytest.raises(NotImplementedError) as ei:
        storage.replace_facts_occ([])
    assert "Phase 2" in str(ei.value)


def _entry(text="a fact", band="working", **over):
    import numpy as np

    e = {
        "band": band,
        "text": text,
        "embedding": (np.arange(384, dtype=np.float32) % 7) / 7.0,
        "surprise": 0.5,
        "ts": 1000.0,
        "access_count": 0,
        "source": "t",
        "superseded_at": None,
        "superseded_by_text": None,
        "last_logical_turn": None,
        "episode_id": None,
        "episode_title": None,
        "tags": ["x"],
        "slots": [["e", "a", "v", "+"]],
    }
    e.update(over)
    return e


def test_entry_crud_roundtrip(storage):
    import numpy as np

    eid = storage.insert_entry(_entry())
    assert isinstance(eid, int)
    rows = storage.load_entries()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == eid and r["text"] == "a fact" and r["band"] == "working"
    assert r["tags"] == ["x"] and r["slots"] == [["e", "a", "v", "+"]]
    assert np.allclose(r["embedding"], _entry()["embedding"], atol=1e-6)

    storage.update_entry(eid, band="fast", access_count=3,
                         superseded_at=2000.0, superseded_by_text="newer")
    r = storage.load_entries()[0]
    assert (r["band"], r["access_count"], r["superseded_at"],
            r["superseded_by_text"]) == ("fast", 3, 2000.0, "newer")

    storage.delete_entry_ids([eid])
    assert storage.load_entries() == []


def test_episode_roundtrip(storage):
    ep = {"id": "ep1", "title": "Session", "hint": None,
          "started_at": 1.0, "ended_at": None, "closed_by_new_start": False,
          "session_key": None, "parent_id": None}
    storage.upsert_episode(ep)
    ep["ended_at"] = 2.0
    storage.upsert_episode(ep)
    rows = storage.load_episodes()
    assert len(rows) == 1 and rows[0]["ended_at"] == 2.0


def test_delete_episode_removes_row(storage):
    storage.upsert_episode({
        "id": "ep-del", "title": "t", "hint": None, "started_at": 1.0,
        "ended_at": 2.0, "closed_by_new_start": False,
        "session_key": "k", "parent_id": None})
    assert any(e["id"] == "ep-del" for e in storage.load_episodes())
    storage.delete_episode("ep-del")
    assert not any(e["id"] == "ep-del" for e in storage.load_episodes())


def test_fact_roundtrip(storage):
    f = {
        "entity": "Zanthar", "attribute": "Default Timeout",
        "entity_norm": "zanthar", "attribute_norm": "default timeout",
        "value": "4500 seconds", "polarity": "+", "status": "current",
        "confidence": 0.8, "origin": "action", "support": ["action"],
        "provenance": ["t"], "asserted_at": 1.0, "last_confirmed": 1.0,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "embedding": None,
    }
    fid = storage.upsert_fact(f)
    f2 = dict(f, id=fid, status="superseded", superseded_by_value="9000")
    storage.upsert_fact(f2)
    rows = storage.load_facts()
    assert len(rows) == 1 and rows[0]["status"] == "superseded"
    storage.delete_fact_ids([fid])
    assert storage.load_facts() == []


def test_meta_roundtrip(storage):
    assert storage.meta_get("missing", default=7) == 7
    storage.meta_set("tier_hits", {"fast": 3})
    storage.meta_set("tier_hits", {"fast": 4})
    assert storage.meta_get("tier_hits") == {"fast": 4}


def test_vector_column_roundtrip(pg_conn):
    import numpy as np
    from pgvector.psycopg import register_vector

    register_vector(pg_conn)
    vec = np.arange(384, dtype=np.float32) / 384.0
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts) VALUES (%s, %s, %s, %s)",
        ("working", "vector probe", vec, 0.0),
    )
    out = pg_conn.execute("SELECT embedding FROM entries").fetchone()[0]
    assert np.allclose(out, vec, atol=1e-6)


def test_entries_for_entity_joins_facts_traces_entries(storage):
    """The provenance join: facts.entity_id ⋈ memory_traces.entity_norm ⋈ entries.
    Returns the MIRAS source entries (band/source/ts/text) behind an entity."""
    c = storage.conn
    eid = c.execute(
        "INSERT INTO entities (canonical, display, created_at) "
        "VALUES ('daemon', 'daemon', 1000.0) RETURNING id").fetchone()[0]
    entry_id = storage.insert_entry(
        _entry(text="the daemon runs in docker", band="slow", source="pseudolife", ts=1234.0))
    c.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, "
        "status, confidence, asserted_at, last_confirmed, entity_id) "
        "VALUES ('daemon','role','daemon','role','serves MCP','current',0.9,1.0,1.0,%s)",
        (eid,))
    storage.add_trace("daemon", "role", entry_id, 1234.0)
    c.commit()

    rows = storage.entries_for_entity(eid)
    assert [r["id"] for r in rows] == [entry_id]
    r = rows[0]
    assert r["band"] == "slow" and r["source"] == "pseudolife"
    assert "docker" in r["text"]


def test_entries_for_entity_empty_without_traces(storage):
    """A graph-only node with no cortex-fact trace has no provenance entries —
    the slot-keyed engram caveat. Must return [] cleanly, not error."""
    eid = storage.conn.execute(
        "INSERT INTO entities (canonical, display, created_at) "
        "VALUES ('lonely', 'lonely', 1.0) RETURNING id").fetchone()[0]
    storage.conn.commit()
    assert storage.entries_for_entity(eid) == []


def test_failed_mutation_does_not_poison_connection(storage):
    """A statement-level error (here: an FK violation from a nonexistent
    entity id) must roll back so the shared long-lived connection is still
    usable afterward — every other mutating method must guard the same way
    delete_entity/merge_entity already do, not just those two."""
    with pytest.raises(Exception):
        storage.upsert_edge(999999, "uses", 999999)

    # The connection must not be left in "current transaction is aborted" —
    # a completely unrelated mutation must still succeed.
    eid = storage.ensure_entity("probe-after-failed-edge")
    assert eid > 0


def test_merge_entity_stale_from_id_is_a_noop_not_a_merge(storage):
    """Three mutual duplicates a<b<c generate pairwise folds (b,a), (c,a),
    (c,b) under the "higher id into lower" tie-break exact_duplicate_pairs
    uses. Applying them in order: merge(b,a) deletes b; merge(c,a) deletes c;
    the third call merge(c,b) then has BOTH ids already gone. That call must
    return False (no-op), not True — otherwise a caller counting
    `if merge_entity(...): merged += 1` reports a merge that never happened."""
    a = storage.ensure_entity("dup-a")
    b = storage.ensure_entity("dup-b")
    c = storage.ensure_entity("dup-c")

    assert storage.merge_entity(b, a) is True   # b folds into a
    assert storage.merge_entity(c, a) is True   # c folds into a
    assert storage.merge_entity(c, b) is False  # both already gone — no-op

    # a is the sole survivor.
    assert storage.conn.execute(
        "SELECT 1 FROM entities WHERE id = %s", (a,)).fetchone() is not None


def test_merge_entity_stale_into_id_is_a_noop_not_a_crash(storage):
    """A merge whose TARGET (into_id) was already deleted — e.g. a queued merge
    proposal whose `into` entity got junk-deleted before the merge is accepted —
    must return False (graceful no-op), not raise an FK violation. Without the
    guard, re-pointing edges to a nonexistent into_id violates the edges FK,
    which _txn rolls back and re-raises → a 500 in the Atlas UI instead of a
    clean 'target no longer exists'."""
    frm = storage.ensure_entity("merge-from")
    into = storage.ensure_entity("merge-into")
    storage.ensure_entity("merge-dep")
    storage.upsert_edge(frm, "uses", storage.ensure_entity("merge-dep2"))
    assert storage.delete_entity(into) is True    # target vanishes

    assert storage.merge_entity(frm, into) is False   # no-op, no raise
    # from survived untouched (not folded into a ghost).
    assert storage.conn.execute(
        "SELECT 1 FROM entities WHERE id = %s", (frm,)).fetchone() is not None


def test_txn_rolls_back_compound_cursor_shape(storage):
    """The `with self._txn(), self.conn.cursor() as cur:` shape (replace_facts /
    replace_lessons / replace_communities / etc.) must roll back cleanly when a
    statement inside the cursor block fails — pinning the nested-context-manager
    exit ordering (cursor closes, then _txn rolls back). A bad embedding value
    (wrong pgvector dimension) makes the INSERT inside replace_facts raise."""
    good = {
        "entity": "E", "attribute": "A", "entity_norm": "e", "attribute_norm": "a",
        "value": "v", "polarity": "+", "status": "current", "confidence": 0.8,
        "origin": "action", "support": ["action"], "provenance": ["t"],
        "asserted_at": 1.0, "last_confirmed": 1.0, "supersedes_value": None,
        "superseded_by_value": None, "superseded_at": None,
        "embedding": [0.0] * 5,   # wrong dim (schema expects 384) → INSERT raises
        "entity_id": None, "object_entity_id": None,
    }
    with pytest.raises(Exception):
        storage.replace_facts([good])

    # Connection must be usable afterward (not InFailedSqlTransaction), and the
    # failed rewrite left no partial rows.
    assert storage.ensure_entity("probe-after-compound-fail") > 0
    assert storage.load_facts() == []
