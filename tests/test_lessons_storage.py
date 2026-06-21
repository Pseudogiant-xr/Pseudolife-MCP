"""Schema v10 — lessons + outcome_signals PostgresStorage round-trips + relations.

Skips cleanly without a PG server (mirrors test_pg_storage.py / test_world_*).
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _lesson_row(entity="deploy engine to host", attribute="approach",
                value="use tar --no-same-owner", **over):
    r = {
        "entity": entity, "attribute": attribute,
        "entity_norm": entity.replace(" ", "-"), "attribute_norm": attribute,
        "value": value, "about": "tar", "polarity": "+", "outcome": "success",
        "status": "current", "confidence": 0.7, "origin": "action",
        "support": ["action"], "provenance": ["ep-1", "sig-3"],
        "asserted_at": 1000.0, "last_confirmed": 1000.0,
        "supersedes_value": None, "superseded_by_value": None, "superseded_at": None,
        "embedding": (np.arange(384, dtype=np.float32) % 5) / 5.0,
        "entity_id": None, "object_entity_id": None,
    }
    r.update(over)
    return r


def test_schema_v10(pg_conn):
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    assert SCHEMA_META_VERSION >= 10              # lessons landed at v10
    row = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None and int(row[0]) == SCHEMA_META_VERSION
    for t in ("lessons", "outcome_signals"):
        reg = pg_conn.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()
        assert reg[0] is not None, f"{t} table not created"


def test_prefers_avoids_relations_seeded(storage):
    names = {r["name"] for r in storage.load_relations()}
    assert {"prefers", "avoids"} <= names


def test_lessons_roundtrip(storage):
    storage.replace_lessons([
        _lesson_row(),
        _lesson_row(attribute="pitfall", value="never run extract as root",
                    polarity="-", outcome="failure"),
    ])
    rows = storage.load_lessons()
    assert len(rows) == 2
    approach = next(r for r in rows if r["attribute"] == "approach")
    assert approach["outcome"] == "success" and approach["polarity"] == "+"
    assert approach["value"] == "use tar --no-same-owner"
    assert approach["about"] == "tar"
    assert approach["provenance"] == ["ep-1", "sig-3"]
    assert approach["support"] == ["action"]
    assert np.allclose(approach["embedding"], _lesson_row()["embedding"], atol=1e-6)
    pitfall = next(r for r in rows if r["attribute"] == "pitfall")
    assert pitfall["polarity"] == "-" and pitfall["outcome"] == "failure"


def test_lessons_replace_is_full_rewrite(storage):
    storage.replace_lessons([_lesson_row()])
    storage.replace_lessons([_lesson_row(attribute="tool-choice", value="x")])
    rows = storage.load_lessons()
    assert len(rows) == 1 and rows[0]["attribute"] == "tool-choice"


def test_signal_crud(storage):
    sid = storage.add_signal(
        "deploy engine to host", "failure", about="tar --same-owner",
        detail="chown errors aborted the extract", polarity="-",
        origin="action", episode_id="ep-9", now=1000.0,
    )
    assert isinstance(sid, int)
    pend = storage.pending_signals()
    assert len(pend) == 1
    s = pend[0]
    assert s["task"] == "deploy engine to host" and s["outcome"] == "failure"
    assert s["about"] == "tar --same-owner" and s["polarity"] == "-"
    assert s["episode_id"] == "ep-9"

    assert storage.consume_signals([sid], now=1100.0) == 1
    assert storage.pending_signals() == []
    # idempotent: an already-consumed signal is not re-consumed
    assert storage.consume_signals([sid], now=1200.0) == 0


def test_snapshot_hydrate_roundtrip_with_entity_linking(storage):
    from pseudolife_memory.memory.lessons import LessonStore
    from pseudolife_memory.storage import sync

    # Pre-create the task-type + object entities so linking resolves.
    tid = storage.ensure_entity(
        "deploy-engine-to-host", display="deploy engine to host", etype="task-type")
    oid = storage.ensure_entity("tar", display="tar")

    s = LessonStore()
    s.write_fact("deploy engine to host", "approach", "use tar --no-same-owner",
                 about="tar", outcome="success", origin="action",
                 provenance={"ep-1", "sig-2"})
    assert sync.snapshot_lessons(s, storage) == 1

    rows = storage.load_lessons()
    assert rows[0]["entity_id"] == tid and rows[0]["object_entity_id"] == oid

    # Hydrate a fresh store from the table.
    s2 = LessonStore()
    assert sync.hydrate_lessons(s2, storage) == 1
    got = s2.lookup("deploy engine to host", "approach")
    assert got is not None and got.value == "use tar --no-same-owner"
    assert got.about == "tar" and got.outcome == "success"
    assert got.origin == "action" and got.provenance == {"ep-1", "sig-2"}


def test_signal_prune_by_age(storage):
    storage.add_signal("old-task", "success", now=100.0)
    storage.add_signal("recent-task", "success", now=10_000.0)
    removed = storage.prune_signals(older_than_ts=5_000.0)
    assert removed == 1
    pend = storage.pending_signals()
    assert len(pend) == 1 and pend[0]["task"] == "recent-task"
