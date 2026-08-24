"""Round-2 review-queue noise leads (2026-08-16 deep-dream scan + Console
triage). Five independent fixes, one contract each:

1. Scan-fallback hub cap — a trace-less entity whose token-mention fallback
   matches an outsized share of the corpus gets a corpus-centroid context
   vector and pairs promiscuously (live bank 2026-08-16: ``pseudolife-pg``
   token-matched 301/695 embedded entries — its short token ``pg`` is
   dropped by ``_token_set``, leaving the single generic token
   ``pseudolife`` — and filed 9 cross-hub merge pairs in one pass).
2. Lesson-synthesis tallies journaled into ``dream_runs`` — the counters
   existed only in the transient dream-run result.
3. Junk-accept durability — a reviewed junk deletion left no record (the
   proposal row CASCADEs away with the entity), so the same name re-minted
   and re-queued for a second human verdict; merge-precision stats must
   not absorb the new junk rows.
4. Junk tombstones — a name already accepted as junk, re-minted and
   junk-flagged again, auto-deletes at detector degree instead of waiting
   for a second review. The tombstone relaxes the degree bar only: a
   re-mint carrying real facts still goes to the review queue (#177).
5. The stateless duplicate listing applies the same ``merge_veto`` the
   filing paths apply (PR #137) and dismissals key on the ENTITY's stored
   canonical, not ``norm_name(display)`` — the Console pair
   ``GND (Enshrouded server)`` (canonical ``gnd``) re-listed after every
   dismissal because the two key spaces never met.

PG-backed tests skip without the bench server (tests/pg_fixtures).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from pseudolife_memory.graph import norm_name
from pseudolife_memory.memory import graph_consolidation as gc
from pseudolife_memory.memory import graph_review as gr
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


class _Stub:
    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab, known_facts=None):
        return [dict(c) for c in self._claims]


# ── 1. scan-fallback hub cap ─────────────────────────────────────────────

def _entry(i: int, text: str) -> dict:
    return {"id": i, "text": text,
            "embedding": np.ones(4, dtype=np.float32)}


def _ents():
    return [{"id": 1, "display": "alpha beta", "canonical": "alpha-beta"},
            {"id": 2, "display": "gamma delta", "canonical": "gamma-delta"}]


def _hub_entries():
    # "alpha beta" token-matches 5 entries (the hub); "gamma delta" only 2.
    return ([_entry(i, f"alpha beta note number {i}") for i in range(5)]
            + [_entry(10 + i, "gamma delta pairing note") for i in range(2)])


def test_fallback_hub_entity_is_excluded_from_vectors():
    vecs, mentions = gc.entity_context_vectors(
        _ents(), _hub_entries(), {}, min_mentions=2, max_fallback_mentions=4)
    assert 1 not in vecs and 1 not in mentions      # corpus centroid, dropped
    assert 2 in vecs                                 # ordinary fallback kept


def test_fallback_under_cap_and_uncapped_behavior_unchanged():
    vecs, _ = gc.entity_context_vectors(
        _ents(), _hub_entries(), {}, min_mentions=2, max_fallback_mentions=5)
    assert 1 in vecs and 2 in vecs                   # at the cap is allowed
    vecs, _ = gc.entity_context_vectors(
        _ents(), _hub_entries(), {}, min_mentions=2)
    assert 1 in vecs                                 # default: no cap


def test_trace_backed_mentions_are_never_capped():
    traces = {"alpha-beta": [0, 1, 2, 3, 4]}         # real evidence, 5 entries
    vecs, mentions = gc.entity_context_vectors(
        _ents(), _hub_entries(), traces, min_mentions=2,
        max_fallback_mentions=2)
    assert 1 in vecs and len(mentions[1]) == 5       # traces exempt from cap


# ── 2. lesson tallies journaled into dream_runs ──────────────────────────

def test_update_dream_run_tallies_merges_keys(svc):
    with svc._lock:
        svc._ensure_init()
    st = svc._storage
    run_id = st.start_dream_run(time.time(), 0.0, 1)
    st.finish_dream_run(run_id, status="committed", finished_at=time.time(),
                        cursor_after=1.0, claims=1, tallies={"inserted": 1})
    st.update_dream_run_tallies(run_id, {"lessons_deduped": 2})
    row = st.recent_dream_runs(limit=1)[0]
    assert row["tallies"]["inserted"] == 1           # existing keys survive
    assert row["tallies"]["lessons_deduped"] == 2    # new key merged


def test_dream_run_journals_lesson_tallies(svc):
    svc.store("the mascot is a fox", source="notes")
    out = svc.dream_run(_Stub([{"entity": "team", "attribute": "mascot",
                                "value": "fox", "confidence": 0.8,
                                "origin": "agent", "source": 0}]))
    assert out["inserted"] == 1
    tallies = svc._storage.recent_dream_runs(limit=1)[0]["tallies"]
    for key in ("lesson_signals", "lessons_written", "lessons_deduped"):
        assert key in tallies                        # history, not just result


# ── 3. junk-accept durability ────────────────────────────────────────────

def _file_junk(svc, norm: str, display: str) -> int:
    with svc._lock:
        svc._ensure_init()
        svc._storage.ensure_entity(norm, display=display)
    eid = svc._storage.find_entity(norm)["id"]
    pid = svc._storage.insert_entity_proposal(
        "junk", eid, None, None, "bare-number", time.time())
    assert pid is not None
    return pid


def test_junk_accept_records_durable_decision(svc):
    pid = _file_junk(svc, "42", "42")
    out = svc.graph_accept_entity_junk(pid, decided_by="agent")
    assert out["accepted"] is True
    assert "42" in svc._storage.junk_accepted_displays()


def test_junk_decisions_stay_out_of_merge_precision_stats(svc):
    pid = _file_junk(svc, "42", "42")
    before = svc._storage.merge_decision_stats()
    assert svc.graph_accept_entity_junk(pid, decided_by="human")["accepted"]
    assert svc._storage.merge_decision_stats() == before


# ── 4. junk tombstones suppress re-review ────────────────────────────────

def test_tombstoned_remint_autodeletes_at_detector_degree(svc):
    # Round 1: a human/agent verdict deletes "42".
    pid = _file_junk(svc, "42", "42")
    assert svc.graph_accept_entity_junk(pid, decided_by="agent")["accepted"]
    assert svc._storage.find_entity("42") is None
    # Round 2: the name re-mints WITH an edge (degree 1) and no facts —
    # previously this re-queued for a second human verdict. "43" is the
    # never-judged control at the same degree: it must stay a pending
    # proposal. (Fact-bearing re-mints are the case below.)
    with svc._lock:
        svc._ensure_init()
        svc._storage.ensure_entity("42", display="42")
        svc._storage.ensure_entity("43", display="43")
    svc.graph_relate("42", "related-to", "daemon", origin="agent")
    svc.graph_relate("43", "related-to", "daemon", origin="agent")
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert svc._storage.find_entity("42") is None            # tombstoned: gone
    assert svc._storage.find_entity("43") is not None        # control: kept
    pending = [p for p in svc._storage.pending_entity_proposals()
               if p.get("kind") == "junk"]
    ids = {p["entity_id"] for p in pending}
    assert svc._storage.find_entity("43")["id"] in ids       # awaits review


def test_tombstoned_remint_with_facts_is_kept_for_review(svc):
    # A tombstone is permanent and has no removal path, so the degree bar
    # alone let a once-junked short name stay deletable forever: months
    # later the same name can be a real entity with accumulated cortex
    # facts (#177). The fact-count half of the evidence bar still applies.
    pid = _file_junk(svc, "42", "42")
    assert svc.graph_accept_entity_junk(pid, decided_by="agent")["accepted"]
    # The node re-mints from a relation (a fact write alone never mints a
    # junk-shaped subject), then real facts accumulate against it.
    svc.graph_relate("42", "related-to", "daemon", origin="agent")
    svc.cortex_write("42", "purpose", "the deployment cutover window",
                     support="user")
    svc.cortex_write("42", "owner", "the platform crew", support="user")
    assert svc.deep_dream(apply=True)["applied"] is True
    ent = svc._storage.find_entity("42")
    assert ent is not None                                   # not auto-deleted
    pending = {p["entity_id"] for p in svc._storage.pending_entity_proposals()
               if p.get("kind") == "junk"}
    assert ent["id"] in pending                              # awaits review


# ── 5. duplicate listing: veto parity + canonical-keyed dismissal ────────

def test_duplicate_listing_applies_merge_veto():
    ents = [
        {"id": 1, "display": "pgvector 0.8.5", "canonical": "pgvector-0-8-5"},
        {"id": 2, "display": "pgvector 0.8.6", "canonical": "pgvector-0-8-6"},
        {"id": 3, "display": "update.ps1", "canonical": "update-ps1"},
        {"id": 4, "display": "ops update.ps1", "canonical": "ops-update-ps1"},
    ]
    labels = [f["label"] for f in gr.duplicate_candidates(ents)]
    assert not any("pgvector" in lb for lb in labels)   # numeric-substitution
    assert any("update.ps1" in lb for lb in labels)     # legit pair still listed


def test_dismissal_keys_on_entity_canonical(svc):
    # Entity minted from the bare name, display enriched later: canonical
    # ("gnd") diverges from norm_name(display) ("gnd-(enshrouded-server)").
    with svc._lock:
        svc._ensure_init()
        svc._storage.ensure_entity("gnd", display="GND (Enshrouded server)")
        svc._storage.ensure_entity("enshrouded-server",
                                   display="enshrouded-server")

    def _dup_labels():
        return [f["label"] for f in svc.graph_review()["findings"]
                if f["type"] == "duplicate"]

    assert any("GND" in lb for lb in _dup_labels())          # pair flags
    out = svc.graph_dismiss_duplicate("GND (Enshrouded server)",
                                      "enshrouded-server")
    assert out["dismissed"] is True
    assert not any("GND" in lb for lb in _dup_labels())      # and stays gone
