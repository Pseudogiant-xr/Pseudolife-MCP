"""Retract-direction traversal over the engram cross-index.

The cross-index (``memory_traces``, schema v13) has only ever been read
forwards: slot -> the source entries that formed it. Correcting a source
memory therefore left everything the dream derived from it standing as
current, with nothing on the served fact saying its evidence had moved
(arXiv 2608.10502, "From Faulty Memories to Corrected Actions": the fix is
a typed provenance graph traversed DOWNSTREAM to scope the repair, not a
delete and not a store reset).

Two halves, both flag-only:

* ``slots_for_entries`` / ``derived_from_entries`` read the same edge
  backwards, so ``supersede`` can report what it invalidated;
* served cortex facts carry ``re_verify`` + ``re_verify_reason`` — the
  SAME shape lessons already use for "subject facts changed since"
  (``service.MemoryService._annotate_lesson_staleness``) — computed at
  read time from the cross-index plus live entry state. Nothing is stored,
  nothing is auto-deleted, and nothing is auto-superseded: cascading a
  correction is a review judgment, which is the project's two-man-rule
  culture.

PG-backed (the cross-index is a Postgres table); skips without a test
server.
"""

from __future__ import annotations

import time as _time

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path_factory):
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path_factory.mktemp("retract-svc"),
                         database_url=pg_url)


def _entry(svc, text: str, *, source: str = "pseudolife", ts: float = 1234.0):
    """A raw source entry straight into storage (no CMS gate) — the tests
    that need it in the bank too use ``svc.store``."""
    with svc._lock:
        svc._ensure_init()
    return svc._storage.insert_entry({
        "band": "forever", "text": text,
        "embedding": np.zeros(1024, dtype=np.float32), "surprise": 0.5,
        "ts": ts, "access_count": 0, "source": source, "superseded_at": None,
        "superseded_by_text": None, "last_logical_turn": None,
        "episode_id": None, "episode_title": None, "tags": [], "slots": [],
    })


# ── the edge, read backwards ──────────────────────────────────────────────

def test_slots_for_entries_enumerates_every_slot_one_entry_formed(svc):
    """One source entry can seed many slots; the retract read must return
    all of them, not the first."""
    eid = _entry(svc, "the daemon runs in docker on port 8077")
    svc._storage.add_trace("daemon", "runtime", eid, 1234.0)
    svc._storage.add_trace("daemon", "port", eid, 1234.0)
    svc._storage.conn.commit()

    rows = svc._storage.slots_for_entries([eid])
    assert {(r["entity_norm"], r["attribute_norm"]) for r in rows} == {
        ("daemon", "runtime"), ("daemon", "port")}
    assert all(r["entry_id"] == eid for r in rows)


def test_slots_for_entries_is_scoped_and_empty_safe(svc):
    """Only the named entries come back, and an empty/unknown id list is a
    cheap empty answer — never a full-table read."""
    a = _entry(svc, "entry a")
    b = _entry(svc, "entry b")
    svc._storage.add_trace("alpha", "attr", a, 1234.0)
    svc._storage.add_trace("beta", "attr", b, 1234.0)
    svc._storage.conn.commit()

    rows = svc._storage.slots_for_entries([a])
    assert [(r["entity_norm"], r["attribute_norm"]) for r in rows] == [
        ("alpha", "attr")]
    assert svc._storage.slots_for_entries([]) == []
    assert svc._storage.slots_for_entries([a + b + 9999]) == []


def test_derived_from_entries_resolves_display_names(svc):
    """The traversal answers in the vocabulary a human reviews in — the
    cross-index stores norms, the report carries the display slot."""
    svc.cortex_write("Payments DB", "Host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db moved to db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()

    out = svc.derived_from_entries([eid])
    assert out["count"] == 1
    (fact,) = out["facts"]
    assert fact["entity"] == "Payments DB" and fact["attribute"] == "Host"
    assert fact["source_entry_ids"] == [eid]


# ── the read-time flag ────────────────────────────────────────────────────

def test_live_evidence_leaves_the_served_fact_byte_identical(svc):
    """The no-harm half: a fact whose evidence still stands must not gain a
    key. An absent flag is the common case, so pre-change payloads are
    unchanged (the ``stance`` precedent in _cortex_record_to_dict)."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()

    rec = svc.cortex_lookup("payments-db", "host")
    assert "re_verify" not in rec and "re_verify_reason" not in rec


def test_superseding_the_source_entry_flags_what_the_dream_derived(svc):
    """The gap this closes: today the derived fact stays current with no
    signal that the memory it came from was corrected."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="payments db is db-prod-9")

    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["re_verify"] is True
    assert "corrected since" in rec["re_verify_reason"]
    # FLAG, never cascade: the value and its status are untouched.
    assert rec["value"] == "db-prod-1" and rec["status"] == "current"


def test_flag_reaches_the_search_cortex_block_and_fact_get(svc):
    """Every read surface that already carries ``source_entries`` must carry
    the flag, or the correction is invisible on the surface agents use."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="payments db is db-prod-9")

    hits = svc.cortex_search("payments-db host", top_k=5)["entries"]
    assert any(f.get("re_verify") for f in hits)
    dumped = svc.cortex_dump()["entries"]
    assert any(f.get("re_verify") for f in dumped)


def test_supersede_reports_the_derivations_it_invalidated(svc):
    """``supersede`` is where a human corrects a memory; the traversal makes
    the blast radius visible AT that moment instead of leaving it to be
    noticed on a later read."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.store("payments db is db-prod-1", source="pseudolife")
    with svc._lock:
        entry = svc._cms.bands[0].entries[-1]
        eid = entry.db_id
    assert eid is not None
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()

    out = svc.supersede("payments db is db-prod-1", "payments db is db-prod-9")
    assert out["superseded_count"] == 1
    assert [f["entity"] for f in out["derived_flagged"]] == ["payments-db"]
    # Still a flag, not a cascade.
    assert svc.cortex_lookup("payments-db", "host")["status"] == "current"


def test_flag_off_when_the_cross_index_is_disabled(svc):
    """``memory.traces.enabled`` gates the whole feature — with the
    cross-index off there is no evidence edge to traverse and the read
    surface must not pay for one."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="corrected")

    svc.config.memory.traces.enabled = False
    rec = svc.cortex_lookup("payments-db", "host")
    assert "re_verify" not in rec


# ── the MCP read surface (file mode; the whitelist is the risk) ───────────

def test_the_mcp_search_projection_carries_the_flag(tmp_path, monkeypatch):
    """``memory_search``'s cortex block re-selects keys by whitelist, so a
    new read-time field reaches the surface agents actually use only if it
    is named there — the failure mode the stale-policy fields hit in the
    2026-08-09 review.

    And it stays PASSIVE. The fact below is fresh, evergreen and
    uncontested, so any ``correct_with`` on it could only have come from
    ``re_verify`` — and there must not be one. The flag fires on ~25% of a
    mature bank (measured 2026-09-02: 1264/5153 live facts), so wiring it
    into an affordance whose served note says to write a correction NOW
    would be a standing instruction to rewrite a quarter of the cortex."""
    from tests.helpers import reload_mcp_filemode

    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    now = _time.time()
    fact = {
        "entity": "payments-db", "attribute": "host", "value": "db-prod-1",
        "polarity": "+", "status": "current", "confidence": 0.9,
        "origin": "agent", "support": [], "provenance": [],
        "asserted_at": now, "last_confirmed": now, "tx_time": now,
        "valid_time": None, "supersedes_value": None,
        "superseded_by_value": None, "superseded_at": None,
        "writer_id": "w", "session_id": "s", "age": "just now",
        "score": 0.6, "contested": False,
        "re_verify": True,
        "re_verify_reason": "derived from 1 source memory superseded since",
    }
    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": False})
    monkeypatch.setattr(mod.service, "cortex_search",
                        lambda *a, **k: {"entries": [fact]})

    out = mod.memory_search(query="payments db host")
    (served,) = out["cortex"]
    assert served["re_verify"] is True
    assert "superseded" in served["re_verify_reason"]
    assert "correct_with" not in served
    assert "correction_note" not in out


# ── the flag must CLEAR, or it is a standing nag ──────────────────────────

def test_evidence_corrected_before_the_fact_was_confirmed_does_not_flag(svc):
    """The cross-index is slot-keyed and trace rows are never deleted, so
    ``source_entries`` lists every entry that ever formed the slot across
    its whole supersession history. Without the ``last_confirmed``
    comparison, any slot that ever had a corrected contributor would latch
    on forever — on a mature bank, a large fraction of the cortex."""
    old_entry = _entry(svc, "payments db is db-prod-0")
    svc._storage.add_trace("payments-db", "host", old_entry, 1.0)
    svc._storage.conn.commit()
    # Retracted in the past...
    svc._storage.update_entry(old_entry, superseded_at=_time.time() - 600,
                              superseded_by_text="superseded long ago")
    # ...and the standing value asserted AFTER that retraction.
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")

    rec = svc.cortex_lookup("payments-db", "host")
    assert "re_verify" not in rec


def test_re_asserting_the_fact_clears_the_flag(svc):
    """``correct_with`` tells the reader to write the verified value at the
    slot. That act MUST silence the flag, or the served text is instructing
    a rewrite that changes nothing and recurs every session."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time(),
                              superseded_by_text="payments db is db-prod-9")
    assert svc.cortex_lookup("payments-db", "host")["re_verify"] is True

    # The documented remedy: re-assert the slot with the verified value.
    svc.cortex_write("payments-db", "host", "db-prod-9", support="user")
    assert "re_verify" not in svc.cortex_lookup("payments-db", "host")


def test_re_confirming_the_same_value_also_clears_it(svc):
    """Confirming the standing value IS the re-verification the flag asks
    for — ``correct_with`` says to re-assert the same value if it checks
    out, so a confirm has to count."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time(),
                              superseded_by_text="corrected")
    assert svc.cortex_lookup("payments-db", "host")["re_verify"] is True

    svc.cortex_write("payments-db", "host", "db-prod-1", support="user")
    assert "re_verify" not in svc.cortex_lookup("payments-db", "host")


def test_consolidate_marks_are_seen_before_any_write_through(svc):
    """``memory_consolidate`` stamps ``superseded_at`` in memory and never
    calls ``update_entry`` (unlike ``supersede`` and ``cms.store``'s
    contradiction decay, which both write through), and ``_persist_all``
    syncs only access counts. Reading the live band entries as well as the
    column is what keeps the flag honest across that path — this is the
    test that makes that branch load-bearing rather than decoration."""
    svc.store("payments db is db-prod-1", source="pseudolife")
    with svc._lock:
        eid = svc._cms.bands[0].entries[-1].db_id
    assert eid is not None
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")

    svc.consolidate(replaces=["payments db is db-prod-1"],
                    new_text="payments db is db-prod-9")

    # The column is still NULL — the mark exists only in RAM.
    row = svc._storage.conn.execute(
        "SELECT superseded_at FROM entries WHERE id = %s", (eid,)).fetchone()
    assert row[0] is None, "consolidate started writing through; simplify me"
    assert svc.cortex_lookup("payments-db", "host")["re_verify"] is True
