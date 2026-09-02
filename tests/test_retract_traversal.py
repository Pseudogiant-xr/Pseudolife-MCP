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


def test_the_mcp_recall_projection_carries_the_flag(tmp_path, monkeypatch):
    """``memory_recall``'s DEFAULT (``verbose=False``) projection rebuilds
    each fact as ``{attribute, value}``, which strips a read-time key the
    service just attached — the same whitelist hazard the search block above
    is pinned for. Annotating recall at the service layer and losing it here
    would leave the inconsistency that motivated the work exactly as it was
    for a default caller: cautioned via ``memory_search``, clean via
    ``memory_recall``.

    Both modes are asserted, and so is the absence on an unaffected fact —
    the flag must not become a key every recalled fact carries."""
    from tests.helpers import reload_mcp_filemode

    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    flagged = {"attribute": "host", "value": "db-prod-1", "origin": "agent",
               "confidence": 0.9, "re_verify": True,
               "re_verify_reason": "derived from 1 source memory corrected "
                                   "since this fact was last confirmed"}
    clean = {"attribute": "port", "value": "5433", "origin": "user",
             "confidence": 1.0}
    monkeypatch.setattr(mod.service, "recall", lambda *a, **k: {
        "query": "q", "seeds": ["payments-db"], "paths": [], "texts": [],
        "edges": [], "iterations": 1, "low_confidence": False,
        "entities": [{"entity": "payments-db", "facts": [flagged, clean]}]})

    for verbose in (False, True):
        out = mod.memory_recall(query="payments db host", verbose=verbose)
        (ent,) = out["entities"]
        served, other = ent["facts"]
        assert served["re_verify"] is True, verbose
        assert "corrected since" in served["re_verify_reason"], verbose
        assert "re_verify" not in other, verbose


def test_recall_flags_an_alias_keyed_fact_through_the_default_projection(
        svc, tmp_path, monkeypatch):
    """``recall`` reaches a node's facts through ``graph_neighborhood``,
    which attaches a cortex record to a node by resolving the record's
    entity through the alias table (``find_entity`` semantics). The
    annotation then matches each served fact back to its slot on
    ``(entity, attribute, value)`` — and the entity side of that key has to
    be built from the SAME resolved name the attachment used: the node's
    entity is the canonical (``pr-#235``), the record's entity is the alias
    (``pr-235``), and ``norm_name`` of the two differ. Keyed on the raw
    record entity the lookup misses and the caution is silently dropped on
    exactly the facts this change makes visible.

    Asserted end to end through the MCP tool's DEFAULT projection, which
    rebuilds each fact and must re-select the flag (the whitelist hazard
    the sibling test above pins with a stubbed service)."""
    from tests.helpers import reload_mcp_filemode

    # The live shape (production bank, 2026-09-02): the dream wrote the fact
    # and its trace under "pr-235"; a later merge folded that node into
    # "PR #235", leaving the record's entity an alias of the node.
    svc.cortex_write("pr-235", "branch", "feat/refind-nomem-eval-arms",
                     support="agent")
    eid = _entry(svc, "PR 235 is on branch feat/refind-nomem-eval-arms")
    svc._storage.add_trace("pr-235", "branch", eid, 1234.0)
    svc._storage.conn.commit()
    svc.graph_relate("PR #235", "part-of", "pseudolife-mcp", origin="user")
    assert svc.graph_merge("pr-235", "PR #235")["merged"] is True
    svc._storage.update_entry(
        eid, superseded_at=_time.time(),
        superseded_by_text="PR 235 moved to branch feat/refind-v2")

    out = svc.recall("which branch is pr-235 on", hops=1)
    assert out["low_confidence"] is False
    node = next(e for e in out["entities"] if e["entity"] == "PR #235")
    (fact,) = [f for f in node["facts"] if f["attribute"] == "branch"]
    assert fact["value"] == "feat/refind-nomem-eval-arms"
    assert fact["re_verify"] is True
    assert "corrected since" in fact["re_verify_reason"]

    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "service", svc)
    served = mod.memory_recall(query="which branch is pr-235 on", hops=1)
    node = next(e for e in served["entities"] if e["entity"] == "PR #235")
    (fact,) = [f for f in node["facts"] if f["attribute"] == "branch"]
    assert fact == {"attribute": "branch",
                    "value": "feat/refind-nomem-eval-arms",
                    "re_verify": True,
                    "re_verify_reason": fact["re_verify_reason"]}
    assert "corrected since" in fact["re_verify_reason"]


def test_recall_flags_a_fact_on_a_display_enriched_node(svc):
    """The recall side of the annotation key is a node's DISPLAY name
    (``run_recall`` keys ``entity_facts`` by ``node["entity"]``), and a
    display enriched after minting no longer normalizes to the canonical the
    attachment used — ``GND (Enshrouded server)`` over canonical ``gnd``,
    the live 2026-08-16 shape ``graph_dismiss_duplicate`` records. Resolving
    that side canonical → alias → display keeps the caution reachable on
    such a node; keyed on the raw display norm, no fact on it could ever
    carry ``re_verify``, including facts that attach by canonical exactly.
    The query reaches the node through an alias because its display is not
    a name a question would use verbatim."""
    with svc._lock:
        svc._ensure_init()
        svc._storage.ensure_entity("gnd", display="GND (Enshrouded server)")
    svc.cortex_write("gnd", "host", "10.0.0.102", support="agent")
    eid = _entry(svc, "the GND server runs on 10.0.0.102")
    svc._storage.add_trace("gnd", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc.graph_alias("gnd", "enshrouded")
    svc._storage.update_entry(eid, superseded_at=_time.time(),
                              superseded_by_text="GND moved to 10.0.0.103")

    out = svc.recall("which host runs enshrouded", hops=1)
    assert out["low_confidence"] is False
    node = next(e for e in out["entities"]
                if e["entity"] == "GND (Enshrouded server)")
    (fact,) = [f for f in node["facts"] if f["attribute"] == "host"]
    assert fact["value"] == "10.0.0.102"
    assert fact["re_verify"] is True


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


def test_consolidate_marks_reach_the_flag_through_the_durable_column(svc):
    """``memory_consolidate`` is the third entry-level supersession site, and
    since PR #239 it writes its marks through to ``entries.superseded_at``
    like ``supersede`` and ``cms.store``'s contradiction decay always have.
    Both halves are asserted together deliberately: the flag reaches a
    consolidate-corrected slot BECAUSE the column is written, so if a future
    change drops that write-through this test says which half broke."""
    svc.store("payments db is db-prod-1", source="pseudolife")
    with svc._lock:
        eid = svc._cms.bands[0].entries[-1].db_id
    assert eid is not None
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")

    svc.consolidate(replaces=["payments db is db-prod-1"],
                    new_text="payments db is db-prod-9")

    row = svc._storage.conn.execute(
        "SELECT superseded_at FROM entries WHERE id = %s", (eid,)).fetchone()
    assert row[0] is not None, "consolidate stopped writing through (PR #239)"
    assert svc.cortex_lookup("payments-db", "host")["re_verify"] is True


def test_a_mark_that_never_reached_postgres_does_not_flag(svc):
    """The durable column is the authority, and this is the recorded contract
    for that choice — not an accident.

    Every entry-level site that stamps ``superseded_at`` writes it through in
    the same locked call (``supersede``, ``cms.store``'s contradiction decay,
    and ``consolidate`` since PR #239), so a RAM-only mark is not a state any
    live path can produce; the in-memory scan that used to back-stop it cost
    an O(bank) pass on every annotated read for a window that cannot open.
    Constructed directly here because nothing else can construct it. A future
    site that marks in RAM without writing through is a KNOWN miss for the
    flag — this test is where that trade-off is written down, and re-adding
    the scan is what turns it red."""
    svc.store("payments db is db-prod-1", source="pseudolife")
    with svc._lock:
        entry = svc._cms.bands[0].entries[-1]
        eid = entry.db_id
    assert eid is not None
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")

    # Direct construction: stamp the LIVE band entry and nothing else.
    with svc._lock:
        entry.superseded_at = _time.time() + 60
        entry.superseded_by_text = "payments db is db-prod-9"
    row = svc._storage.conn.execute(
        "SELECT superseded_at FROM entries WHERE id = %s", (eid,)).fetchone()
    assert row[0] is None                      # the harness, not the claim

    assert "re_verify" not in svc.cortex_lookup("payments-db", "host")


# ── the flag is BEST-EFFORT: losing the evidence loses the flag ───────────

def test_evicting_the_superseded_source_clears_the_flag(svc):
    """Pins the known limitation rather than claiming it away.

    ``memory_traces.entry_id`` is ``ON DELETE CASCADE`` and a true-drop
    capacity eviction hard-deletes the row (every eviction under the default
    flat preset, since there is no deeper band to demote into). A superseded
    entry is also the TOP eviction candidate — contradiction decay multiplies
    its surprise by 0.3, and eviction ranks on retention. So the flag can
    appear and then vanish with no re-verification having happened. Fixing
    that needs durable per-slot state, i.e. a schema change; until then the
    contract is "best-effort", and this is the test that says so."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.store("payments db is db-prod-1", source="pseudolife")
    with svc._lock:
        entry = svc._cms.bands[0].entries[-1]
        eid = entry.db_id
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="payments db is db-prod-9")
    assert svc.cortex_lookup("payments-db", "host")["re_verify"] is True

    with svc._lock:
        band = svc._cms.bands[0]
        svc._cms._on_band_evict(entry, band_idx=0)      # true drop: row gone
        band.entries = [e for e in band.entries if e is not entry]

    rec = svc.cortex_lookup("payments-db", "host")
    assert "re_verify" not in rec               # traces cascaded away with it
    assert rec["value"] == "db-prod-1"          # the fact itself is untouched


def test_deleting_the_source_entry_produces_no_flag(svc):
    """``memory_delete`` is the strongest retraction there is and it raises no
    flag at all — the row and its trace rows are gone, so there is nothing
    left to traverse. Recorded, not fixed: same schema-change gate as the
    eviction case above."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.store("payments db is db-prod-1", source="pseudolife")
    with svc._lock:
        eid = svc._cms.bands[0].entries[-1].db_id
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()

    assert svc.delete(text="payments db is db-prod-1")["deleted_count"] == 1

    assert "re_verify" not in svc.cortex_lookup("payments-db", "host")


# ── cost: the annotation is for SERVING, not for verification ─────────────

def test_verification_lookups_do_not_pay_for_the_annotation(svc,
                                                            monkeypatch):
    """``track=False`` already means "this lookup is verification, not
    serving". The dream rollback (``service_dream._rewrite_prev``) calls it
    once per journal row and reads only ``value``, so annotating there buys a
    flag nobody reads at the price of a PG query per row."""
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="corrected")

    seen = []
    real = svc._annotate_evidence_supersession
    monkeypatch.setattr(svc, "_annotate_evidence_supersession",
                        lambda rows: (seen.append(len(rows)), real(rows))[1])

    quiet = svc.cortex_lookup("payments-db", "host", track=False)
    assert seen == [] and "re_verify" not in quiet

    served = svc.cortex_lookup("payments-db", "host")
    assert seen and served["re_verify"] is True


# ── set-valued slots ──────────────────────────────────────────────────────

def _superseded_set_slot(svc):
    """A set slot whose one cited source memory has since been corrected."""
    svc.set_add("stack", "languages", "python")
    svc.set_add("stack", "languages", "rust")
    eid = _entry(svc, "the stack is python and rust")
    svc._storage.add_trace("stack", "languages", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="the stack is python and go")
    return eid


def test_set_slot_lookup_carries_the_flag(svc):
    """``slots_for_entries`` is kind-agnostic, so ``derived_flagged`` names set
    slots — and ``memory_fact_get`` promises the flag without qualification.
    A set slot that could never carry it made both statements false."""
    _superseded_set_slot(svc)
    rec = svc.cortex_lookup("stack", "languages")
    assert rec["kind"] == "set"
    assert rec["re_verify"] is True
    assert "corrected since" in rec["re_verify_reason"]


def test_set_slot_search_entry_carries_the_flag(svc):
    """The grouped set entry ``cortex_search`` composes is a different dict
    from the scalar row, built on a different branch; it needs its own
    evidence lookup or the flag stops at the lookup surface."""
    _superseded_set_slot(svc)
    hits = svc.cortex_search("stack languages", top_k=5)["entries"]
    sets = [h for h in hits if h.get("kind") == "set"]
    assert sets and sets[0]["re_verify"] is True


def test_set_slot_with_live_evidence_stays_byte_identical(svc):
    """The no-harm half, for sets too."""
    svc.set_add("stack", "languages", "python")
    eid = _entry(svc, "the stack is python")
    svc._storage.add_trace("stack", "languages", eid, 1234.0)
    svc._storage.conn.commit()

    rec = svc.cortex_lookup("stack", "languages")
    assert "re_verify" not in rec and "re_verify_reason" not in rec


# ── consistency across read paths ─────────────────────────────────────────

def test_recall_facts_carry_the_flag(svc):
    """``memory_recall`` serves the same canonical facts ``memory_search``
    does, through the graph projection rather than the cortex block. Without
    this the SAME fact reads as cautioned on one tool and clean on the
    other, which is worse than either alone."""
    svc.graph_relate("payments-db", "runs-on", "prod-host")
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.store("payments-db host notes", source="pseudolife")
    eid = _entry(svc, "payments db is db-prod-1")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="payments db is db-prod-9")

    out = svc.recall("payments-db host")
    facts = [f for e in out["entities"] for f in e["facts"]]
    flagged = [f for f in facts if f.get("re_verify")]
    assert flagged, f"no flag on any recalled fact: {facts}"
    assert "source_entries" not in flagged[0]   # annotation only, no bulk


def test_recall_does_not_confuse_slots_that_share_a_graph_node(svc):
    """The graph and the cortex normalize names differently — ``graph``
    folds ``:`` and ``\\`` to a separator, ``cortex._norm_key`` folds ``-``
    and does not touch ``:`` — so ``host:port`` and ``host-port`` are TWO
    cortex slots under ONE graph node, and ``graph_neighborhood`` hangs both
    slots' facts on that single node. Resolving a recalled fact by entity
    and attribute alone therefore annotates both against whichever slot won
    the tie, which either misses a real correction or reports one from the
    wrong slot's evidence."""
    svc.graph_relate("host-port", "runs-on", "prod")
    svc.cortex_write("host:port", "role", "colon-slot", support="agent")
    svc.cortex_write("host-port", "role", "hyphen-slot", support="agent")
    svc.store("host-port role notes", source="pseudolife")
    eid = _entry(svc, "host-port role evidence")
    # Cited by the HYPHEN slot only.
    svc._storage.add_trace("host-port", "role", eid, 1234.0)
    svc._storage.conn.commit()
    svc._storage.update_entry(eid, superseded_at=_time.time() + 60,
                              superseded_by_text="corrected")

    facts = {f["value"]: f
             for e in svc.recall("host-port role")["entities"]
             for f in e["facts"]}
    assert set(facts) >= {"colon-slot", "hyphen-slot"}, facts
    assert facts["hyphen-slot"].get("re_verify") is True
    assert "re_verify" not in facts["colon-slot"]


# ── derived_flagged is a bounded, current-vocabulary report ───────────────

def test_derived_flagged_is_capped_with_a_truncation_marker(svc):
    """One verbose memory can seed hundreds of slots, and the whole list is
    inlined into the MCP response. Cap it, and SAY that it is capped."""
    from pseudolife_memory.service import DERIVED_FLAGGED_CAP as CAP

    svc.store("a very widely cited source memory", source="pseudolife")
    with svc._lock:
        eid = svc._cms.bands[0].entries[-1].db_id
    for i in range(CAP + 3):
        svc._storage.add_trace(f"ent-{i:04d}", "attr", eid, 1234.0)
    svc._storage.conn.commit()

    out = svc.supersede("a very widely cited source memory", "corrected")
    assert len(out["derived_flagged"]) == CAP
    assert out["derived_flagged_total"] == CAP + 3
    assert out["derived_flagged_truncated"] is True


def test_the_cap_keeps_the_slots_a_reader_can_act_on(svc):
    """The cap slices an ORDER, so the order has to put live slots first.
    Trace rows outlive the facts they formed, so a correction can easily
    touch more dead slots than the cap holds — and alphabetically they win,
    because a dead slot is just as likely to sort early. Spending all 50
    rows on slots with no current value while truncating away the live ones
    reports the exact half nobody can re-check."""
    from pseudolife_memory.service import DERIVED_FLAGGED_CAP as CAP

    svc.store("another widely cited source memory", source="pseudolife")
    with svc._lock:
        eid = svc._cms.bands[0].entries[-1].db_id
    # Dead slots sort FIRST alphabetically; the live ones sort last.
    for i in range(CAP):
        svc._storage.add_trace(f"aaa-dead-{i:04d}", "attr", eid, 1234.0)
    for i in range(3):
        svc.cortex_write(f"zzz-live-{i}", "attr", f"v{i}", support="agent")
        svc._storage.add_trace(f"zzz-live-{i}", "attr", eid, 1234.0)
    svc._storage.conn.commit()

    rows = svc.supersede("another widely cited source memory",
                         "corrected")["derived_flagged"]
    assert len(rows) == CAP
    live = [r["entity"] for r in rows if r["has_current_value"]]
    assert sorted(live) == ["zzz-live-0", "zzz-live-1", "zzz-live-2"]
    assert all(r["has_current_value"] for r in rows[:3])   # and they LEAD


def test_derived_flagged_names_the_current_value_and_marks_dead_slots(svc):
    """Two bugs in one report: the display name came from whichever record
    happened to be OLDEST in the store (so a renamed slot was reported under
    a name the reader can no longer look up), and slots with no current fact
    were listed as if they were live facts to go re-check."""
    svc.cortex_write("Payments DB", "Host", "db-prod-0", support="agent")
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    eid = _entry(svc, "payments db notes")
    svc._storage.add_trace("payments-db", "host", eid, 1234.0)
    svc._storage.add_trace("ghost-slot", "attr", eid, 1234.0)
    svc._storage.conn.commit()

    facts = {f["entity"]: f for f in svc.derived_from_entries([eid])["facts"]}
    assert facts["payments-db"]["attribute"] == "host"
    assert facts["payments-db"]["has_current_value"] is True
    assert facts["ghost-slot"]["has_current_value"] is False


# ── downstream surfaces ───────────────────────────────────────────────────

@pytest.mark.parametrize("which", ["lme", "beam"])
def test_bank_dumps_drop_the_read_time_annotation(tmp_path, which):
    """Both bank dumps strip ``source_entries`` because a read-time key that
    is not part of the offline replay makes committed bank artifacts churn.
    The two new keys ride the same surface and get the same treatment — in
    BOTH dumps, which carry the pops independently."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    class _Svc:
        def cortex_dump(self):
            return {"entries": [{
                "entity": "payments-db", "attribute": "host",
                "value": "db-prod-1", "source_entries": [1, 2],
                "re_verify": True, "re_verify_reason": "corrected since"}]}

        def history(self, *a, **k):
            return {"versions": [{"value": "db-prod-1"}]}

    svc = _Svc()
    if which == "lme":
        from evals.longmemeval_bench import dump_bank
        facts = dump_bank(svc, {"question_id": "q1", "question": "?",
                                "answer": "a", "question_date": "2026-01-01"},
                          tmp_path / "bank.json.gz")
    else:
        from evals.beam_adapter import dump_chat_bank
        # ``dump_chat_bank`` returns None; it mutates the dicts in place.
        facts = svc.cortex_dump()["entries"]
        dump_chat_bank(type("S", (), {
            "cortex_dump": lambda self_: {"entries": facts},
            "history": svc.history})(), "chat-1", {},
            tmp_path / "bank.json.gz")
    assert "source_entries" not in facts[0]
    assert "re_verify" not in facts[0] and "re_verify_reason" not in facts[0]


def test_set_slot_lookup_pays_nothing_when_the_cross_index_is_off(svc):
    """``test_flag_off_when_the_cross_index_is_disabled`` states the rule:
    with the cross-index off "the read surface must not pay for one". The
    set-slot lookup fetches traces purely to feed the annotation and
    discards them, so with the knob off that query is pure waste — unlike
    the scalar path, which SERVES its traces as ``source_entries``."""
    _superseded_set_slot(svc)
    assert svc.cortex_lookup("stack", "languages")["re_verify"] is True

    calls = []
    real = svc._storage.traces_for_slot
    svc._storage.traces_for_slot = lambda *a: (calls.append(a), real(*a))[1]
    try:
        svc.config.memory.traces.enabled = False
        rec = svc.cortex_lookup("stack", "languages")
    finally:
        svc._storage.traces_for_slot = real
    assert "re_verify" not in rec
    assert calls == []


def test_the_console_renders_the_flag_on_every_fact_view():
    """Three Console views render canonical facts and all three receive the
    flag: the search block (raw cortex entries), the Cortex view
    (``cortex_dump``, which annotates), and Recall (annotated by this
    change). A caution that shows on one fact list and not the next is worse
    than none, so one shared badge serves all three.

    Asserted on the badge helper and its CALL SITES, not on the string
    ``re_verify`` — that appears in prose comments too, so a grep for it
    would stay green with the rendering deleted."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "pseudolife_memory" / "web"
          / "static" / "js")
    shared = (js / "components.js").read_text(encoding="utf-8")
    assert "export function reVerifyBadge" in shared
    assert "f.re_verify" in shared and "f.re_verify_reason" in shared
    for view in ("stream.js", "cortex.js", "recall.js"):
        src = (js / "views" / view).read_text(encoding="utf-8")
        assert "reVerifyBadge" in src.split("import", 1)[-1], view
        assert "reVerifyBadge(f)" in src, view
