import time
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401 (fixtures)
from pseudolife_memory.storage.postgres import PostgresStorage


@pytest.fixture()
def storage(pg_conn, pg_url):
    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _two_entities(st):
    a = st.ensure_entity("alpha", display="alpha")
    b = st.ensure_entity("beta", display="beta")
    return a, b


def test_insert_then_pending_then_accept(storage):
    a, b = _two_entities(storage)
    pid = storage.insert_proposal(a, "related-to", b, 0.45, 0.91, "why", "deep-dream", time.time())
    assert pid is not None
    pend = storage.pending_proposals()
    assert len(pend) == 1 and pend[0]["src"] == "alpha" and pend[0]["dst"] == "beta"
    assert storage.set_proposal_status(pid, "accepted") is True
    assert storage.pending_proposals() == []


def test_insert_is_idempotent_on_triple(storage):
    a, b = _two_entities(storage)
    first = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x", "deep-dream", time.time())
    dup = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x", "deep-dream", time.time())
    assert first is not None and dup is None


# ── v35: the link judge's verdict rides the proposal row ──────────────────

def test_link_judgment_round_trips_and_gates_on_pending(storage):
    """Same contract as the v30 merge-judge columns: an OPINION recorded on
    a PENDING row, frozen once a decision path settles it."""
    a, b = _two_entities(storage)
    pid = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x",
                                  "deep-dream", time.time())
    assert storage.set_proposal_judgment(
        pid, verdict="retype", confidence=0.85, note="src uses dst",
        model="stub", relation="uses", at=time.time())
    row = next(p for p in storage.pending_proposals() if p["id"] == pid)
    assert row["judge_verdict"] == "retype"
    assert row["judge_confidence"] == 0.85
    assert row["judge_relation"] == "uses"
    assert row["judge_model"] == "stub"
    assert storage.set_proposal_status(pid, "rejected")
    assert not storage.set_proposal_judgment(
        pid, verdict="accept", confidence=0.5, note=None, model="stub",
        relation=None, at=time.time())


def test_curation_judgment_memo_round_trip(storage):
    """A judged lesson/world pair is remembered so the sweep does not re-send
    it every tick; the memo is keyed like dismissed_pairs (store + sorted
    keys) and re-judging overwrites in place."""
    assert storage.record_curation_judgment(
        "lesson", "b|pitfall", "a|approach", verdict="distinct", keep=None,
        fold=None, confidence=0.9, note="aspect siblings", model="stub",
        at=100.0)
    memo = storage.curation_judgments("lesson")
    assert ("a|approach", "b|pitfall") in memo          # sorted key order
    rec = memo[("a|approach", "b|pitfall")]
    assert rec["verdict"] == "distinct" and rec["judged_at"] == 100.0
    storage.record_curation_judgment(
        "lesson", "a|approach", "b|pitfall", verdict="leave", keep=None,
        fold=None, confidence=0.4, note="unsure", model="stub", at=200.0)
    assert storage.curation_judgments("lesson")[
        ("a|approach", "b|pitfall")]["judged_at"] == 200.0
    assert storage.curation_judgments("world") == {}
