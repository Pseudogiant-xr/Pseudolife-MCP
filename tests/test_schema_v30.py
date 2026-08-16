"""Schema v30 — the autonomous Step-C judge's shadow verdict on entity
proposals.

Adds five nullable columns to ``entity_proposals`` (``judge_verdict``,
``judge_confidence``, ``judge_note``, ``judge_model``, ``judged_at``): a
model pre-judgment recorded on the pending row and surfaced beside the
evidence in review payloads. The verdict is an OPINION — the durable
decision record stays ``merge_decisions``, written only when a decision
path (human, agent, or the auto-reject mode) ratifies it. NULL means
"not yet judged", which is how every pre-v30 row behaved, so the
migration is a no-op on existing banks.

Skips without a PG server (mirrors test_schema_v29).
"""
from __future__ import annotations

import time

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_30():
    # The newest schema test carries the exact current-version pin — the
    # deliberate tripwire that forces a bump author through the shipping
    # checklist. On the v31 bump: relax this to >= 30 and pin == 31 in the
    # new test_schema_v31.py (two-file touch, not ten).
    assert SCHEMA_META_VERSION == 30


def test_judge_columns_exist_and_are_nullable(pg_conn):
    cols = {r[0]: r[1] for r in pg_conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'entity_proposals'").fetchall()}
    for col in ("judge_verdict", "judge_confidence", "judge_note",
                "judge_model", "judged_at"):
        assert cols.get(col) == "YES", col


def test_judgment_round_trips_and_gates_on_pending(pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    st = PostgresStorage(pg_url)
    st.ensure_entity("alpha", display="alpha")
    st.ensure_entity("alpha service", display="alpha service")
    a = st.find_entity("alpha")["id"]
    b = st.find_entity("alpha service")["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.9, "test", time.time())
    assert st.set_entity_proposal_judgment(
        pid, verdict="reject", confidence=0.9, note="siblings",
        model="stub", at=time.time())
    row = next(p for p in st.pending_entity_proposals() if p["id"] == pid)
    assert row["judge_verdict"] == "reject"
    assert row["judge_confidence"] == 0.9
    assert row["judge_note"] == "siblings"
    # A decided row can no longer be re-judged (the verdict froze with it).
    st.set_entity_proposal_status(pid, "rejected")
    assert not st.set_entity_proposal_judgment(
        pid, verdict="accept", confidence=0.5, note=None, model="stub",
        at=time.time())


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'entity_proposals'").fetchall()}
    assert "judge_verdict" in cols
