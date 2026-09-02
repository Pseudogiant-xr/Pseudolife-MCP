"""What ``ensure_schema`` must declare: columns, indexes, key shapes.

``ensure_schema`` has no version branching — it is one flat, idempotent
DDL pass, and the ``vNN`` comments in ``storage/schema.py`` are
documentation, not code paths. So there is nothing to test per version;
there is one shape, and this file pins the parts of it that no other test
would notice going missing.

Deliberately NOT here:

* **Table existence.** ``tests/pg_fixtures.py`` TRUNCATEs every table in
  ``BENCH_RESET_TABLES`` at the top of every PG-backed test, and
  ``tests/test_bench_reset_tables.py`` proves that tuple covers every
  table the DDL declares. A dropped table therefore fails every PG test
  in the suite at fixture setup — a far louder signal than one
  ``to_regclass`` assertion.
* **``ensure_schema`` idempotence.** The fixture calls it on a database a
  previous test already provisioned, i.e. every PG test in the suite is a
  re-run. The re-run assertions that used to sit in each per-version file
  could not fail independently of it. The one idempotence guard with real
  teeth is the kind-aware healing test in
  ``tests/test_schema_healing.py``, which seeds state the pass must NOT
  destroy.
* **The ``meta`` schema_version stamp.** Canonical copy lives in
  ``tests/test_pg_storage.py::test_schema_version_recorded``.
* **The version number itself.** ``tests/test_schema_version.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage import schema

# (table, columns that must exist). Add a row here when a bump adds pure
# DDL shape that nothing reads yet; when the bump adds behaviour, test the
# behaviour beside its consumer instead.
_REQUIRED_COLUMNS = [
    # v13 — provenance-as-link: the trace index is SLOT-keyed.
    ("memory_traces",
     {"entity_norm", "attribute_norm", "entry_id", "created_at"}),
    # v13 reinforcements / v33 explicit_reinforcements (the split counter).
    ("entries", {"reinforcements", "explicit_reinforcements"}),
    # v16 — per-entity source attribution.
    ("entity_sources",
     {"entity_id", "source", "count", "origin", "updated_at"}),
    # v24 — per-entity kind, the input to the freshness policy.
    ("entity_kinds",
     {"entity_norm", "kind", "origin", "confidence", "decided_at"}),
    # v23 freshness_class / v26 kind+value_norm / v29 stance / v35 labels.
    ("facts", {"freshness_class", "kind", "value_norm", "stance",
               "authority", "distortion_tolerance"}),
    # v35 — the write-time label pair on entries (authority collapse /
    # compaction cliff); NULL = observation / unlabelled.
    ("entries", {"authority", "distortion_tolerance"}),
    # v27 — dream-run audit.
    ("dream_runs",
     {"id", "started_at", "finished_at", "cursor_before", "cursor_after",
      "pulled", "claims", "tallies", "status", "extractor", "writer_id",
      "rolled_back_at"}),
    # v27 pre-image journal + v28's chronicle_event_id.
    ("dream_run_slots",
     {"id", "run_id", "seq", "entity", "attribute", "entity_norm",
      "attribute_norm", "kind", "op", "prev_kind", "prev_value",
      "prev_status", "prev_confidence", "prev_support", "new_value",
      "action", "src_entry_id", "at", "chronicle_event_id"}),
    # v28 — chronicle events, bitemporal.
    ("chronicle_events",
     {"id", "occurred_at", "occurred_phrase", "recorded_at", "actor",
      "actor_norm", "description", "description_norm", "episode",
      "src_entry_id", "hlc_phys", "hlc_logical", "writer_id",
      "invalidated_at"}),
    # v36 — the link judge's verdict on edge proposals (+ retype relation)
    # and the store-curation judgment memo.
    ("edge_proposals",
     {"judge_verdict", "judge_confidence", "judge_note", "judge_model",
      "judged_at", "judge_relation", "decided_by", "decided_at"}),
    ("entity_proposals",
     {"judge2_verdict", "judge2_confidence", "judge2_model", "judged2_at"}),
    ("curation_judgments",
     {"store", "a_key", "b_key", "verdict", "keep", "fold", "confidence",
      "note", "model", "judged_at"}),
    # v30 — the Step-C judge's shadow verdict on merge proposals.
    ("entity_proposals",
     {"judge_verdict", "judge_confidence", "judge_note", "judge_model",
      "judged_at"}),
    # v31 log + v32 params + v34 served_facts.
    ("retrieval_events", {"params", "served_facts"}),
    # v33 — per-slot read counters.
    ("slot_reads",
     {"entity_norm", "attribute_norm", "read_count", "last_read_at"}),
]


def _columns(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s", (table,)).fetchall()}


@pytest.mark.parametrize("table,required", _REQUIRED_COLUMNS,
                         ids=[t for t, _ in _REQUIRED_COLUMNS])
def test_table_declares_its_required_columns(pg_conn, table, required):
    assert required <= _columns(pg_conn, table), (
        f"{table} is missing {sorted(required - _columns(pg_conn, table))}")


# ── column types and defaults ─────────────────────────────────────────────
# Where the TYPE (not just the presence) is load-bearing: a JSONB blob read
# back as a dict, a counter that must start at zero, a column whose NULL
# means "not yet" for every pre-bump row.


_COLUMN_TYPES = [
    ("retrieval_events", "params", "jsonb"),        # v32
    ("retrieval_events", "served_facts", "jsonb"),  # v34
    ("slot_reads", "entity_norm", "text"),          # v33
    ("slot_reads", "attribute_norm", "text"),
    ("slot_reads", "read_count", "bigint"),
    ("slot_reads", "last_read_at", "double precision"),
    ("entries", "explicit_reinforcements", "integer"),
]

_NULLABLE_COLUMNS = [
    ("facts", "stance"),                          # v29: NULL = asserted plainly
    ("entity_proposals", "judge_verdict"),        # v30: NULL = not yet judged
    ("entity_proposals", "judge_confidence"),
    ("entity_proposals", "judge_note"),
    ("entity_proposals", "judge_model"),
    ("entity_proposals", "judged_at"),
    ("edge_proposals", "judge_verdict"),          # v36: NULL = not yet judged
    ("edge_proposals", "judge_confidence"),
    ("edge_proposals", "judge_note"),
    ("edge_proposals", "judge_model"),
    ("edge_proposals", "judged_at"),
    ("edge_proposals", "judge_relation"),         # v36: a retype verdict's relation
    ("edge_proposals", "decided_by"),
    ("edge_proposals", "decided_at"),
    ("entity_proposals", "judge2_verdict"),       # v36: the second opinion
    ("entity_proposals", "judge2_confidence"),
    ("entity_proposals", "judge2_model"),
    ("entity_proposals", "judged2_at"),
    ("dream_run_slots", "chronicle_event_id"),    # v28: NULL on non-event rows
    ("entries", "authority"),                     # v35: NULL = observation
    ("entries", "distortion_tolerance"),          # v35: NULL = unlabelled
    ("facts", "authority"),
    ("facts", "distortion_tolerance"),
]


def _column_attr(conn, table: str, column: str, attr: str):
    row = conn.execute(
        f"SELECT {attr} FROM information_schema.columns "  # noqa: S608 — literal
        "WHERE table_name = %s AND column_name = %s",
        (table, column)).fetchone()
    assert row is not None, f"{table}.{column} not created"
    return row[0]


def test_columns_declare_their_types(pg_conn):
    """Looped rather than parametrized: each parametrized case would pay its
    own ``pg_conn`` setup (ensure_schema + TRUNCATE of every table), and one
    query per column against one connection is the same coverage."""
    wrong = [(t, c, want, got) for t, c, want in _COLUMN_TYPES
             if (got := _column_attr(pg_conn, t, c, "data_type")) != want]
    assert not wrong, f"wrong column types: {wrong}"


def test_explicit_reinforcements_defaults_to_zero(pg_conn):
    default = _column_attr(pg_conn, "entries", "explicit_reinforcements",
                           "column_default")
    assert "0" in (default or ""), "explicit_reinforcements must default to 0"


def test_additive_columns_are_nullable(pg_conn):
    """Each of these is additive over an existing bank: NULL is what every
    pre-bump row already reads as, which is what makes the migration a
    no-op rather than a backfill."""
    not_null = [(t, c) for t, c in _NULLABLE_COLUMNS
                if _column_attr(pg_conn, t, c, "is_nullable") != "YES"]
    assert not not_null, f"must be nullable: {not_null}"


# ── structural one-offs ───────────────────────────────────────────────────


def test_memory_traces_is_slot_keyed_not_fact_keyed(pg_conn):
    """v13. ``facts.id`` is regenerated on every cortex snapshot save, so a
    fact-keyed trace index goes stale silently. Lock the slot anchor so a
    regression to the old one fails."""
    assert "fact_id" not in _columns(pg_conn, "memory_traces")


def test_entity_kinds_primary_key_is_entity_norm(pg_conn):
    """v24. One kind per entity — a second write to the same entity updates
    it. Keyed on entity_norm, NOT entity_id: that is what cortex slots key
    on, a third of cortex entities have no graph node at all, and a graph
    merge would otherwise silently retarget the kind."""
    rows = pg_conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid='entity_kinds'::regclass AND i.indisprimary").fetchall()
    assert [r[0] for r in rows] == ["entity_norm"]


def test_edges_dst_id_index_present(pg_conn):
    """v22 (2026-07-12 perf review). UNIQUE(src_id, relation, dst_id)
    supports src_id-leading lookups only; dst_id-only lookups
    (``merge_entity``'s dst-side dedup/repoint, any "what points to X"
    traversal) fell back to a sequential scan without this."""
    row = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'edges' AND indexname = 'edges_dst_idx'"
    ).fetchone()
    assert row is not None, "edges_dst_idx is missing"
    assert "dst_id" in row[0]


def test_dream_run_slots_src_entry_id_carries_no_foreign_key(pg_conn):
    """v27. Entries are evictable, and the ``memory_traces`` FK is the
    origin of the reflush-stall class the dream self-heals around — so the
    only FK on this table is ``run_id`` (which does CASCADE, exercised in
    tests/test_dream_runs.py)."""
    fks = pg_conn.execute(
        "SELECT conname FROM pg_constraint c "
        "JOIN pg_class t ON c.conrelid = t.oid "
        "WHERE t.relname = 'dream_run_slots' AND c.contype = 'f'"
    ).fetchall()
    assert len(fks) == 1, f"expected only the run_id FK, got {fks}"


def test_chronicle_events_carries_no_foreign_keys(pg_conn):
    """v28. Same rationale as ``dream_run_slots.src_entry_id``: entries are
    evictable, so nothing here may reference them — and nothing else may
    grow a reference either."""
    fks = pg_conn.execute(
        "SELECT conname FROM pg_constraint c "
        "JOIN pg_class t ON c.conrelid = t.oid "
        "WHERE t.relname = 'chronicle_events' AND c.contype = 'f'"
    ).fetchall()
    assert fks == []


def test_facts_table_declares_freshness_class_defaulting_to_evergreen():
    """v23. Personal facts are mostly durable ("this project is Python").
    Defaulting to ``volatile`` — the way the world cortex does, where facts
    are external and rot — would silently re-rank an existing bank of
    hundreds of facts on an unmeasured assumption. Read from the DDL text
    so the DEFAULT clause itself is pinned, not just the column."""
    ddl = getattr(schema, "SCHEMA_SQL", None) or Path(
        schema.__file__).read_text(encoding="utf-8")
    facts_block = ddl.split("CREATE TABLE IF NOT EXISTS facts (", 1)[1].split(");", 1)[0]
    assert "freshness_class" in facts_block, (
        "facts table has no freshness_class column")
    assert "'evergreen'" in facts_block, (
        "personal facts must default to evergreen — defaulting to volatile "
        "would silently re-rank every existing fact")
