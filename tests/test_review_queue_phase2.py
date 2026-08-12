"""Review-queue Phase 2: same-from grouping, junk-first routing, and the
merge accept-rate stat.

Motivated by the 2026-08-11 full-queue triage: 22 of 153 merge proposals
shared a ``from`` entity (the write-dedup detector files up to three
matches per mint), so what is really one where-does-this-entity-belong
decision presented as independent rows; several rows paired a junk-shaped
entity that the junk queue already owned; and the triage's accept/reject
split — the direct measure of detector precision — vanished into the audit
log with no queryable summary.
"""
from __future__ import annotations

import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


def _entity_id(svc, display):
    for e in svc._storage.load_graph()["entities"]:
        if e["display"] == display:
            return e["id"]
    raise AssertionError(f"entity {display!r} not found")


# ── same-from grouping ────────────────────────────────────────────────────

def _stage_shared_from(svc):
    # One entity near-duplicating three targets (the events-v2 shape),
    # plus an unrelated singleton pair.
    for name in ("widget pass", "widget pass v2", "widget bank",
                 "widget dataset", "gizmo relay", "gizmo relay live"):
        svc.graph_relate(name, "related-to", f"anchor-{name[:5]}",
                         origin="agent")
    ids = {n: _entity_id(svc, n) for n in (
        "widget pass", "widget pass v2", "widget bank", "widget dataset",
        "gizmo relay", "gizmo relay live")}
    now = time.time()
    for target in ("widget pass v2", "widget bank", "widget dataset"):
        svc._storage.insert_entity_proposal(
            "merge", ids["widget pass"], ids[target], 0.7,
            f"write-dedup: 'widget pass' ~ {target!r}", now)
    svc._storage.insert_entity_proposal(
        "merge", ids["gizmo relay"], ids["gizmo relay live"], 0.7,
        "write-dedup: 'gizmo relay' ~ 'gizmo relay live'", now)


def test_enrich_groups_shared_from_proposals(svc):
    _stage_shared_from(svc)
    out = svc.deep_dream(apply=False, include_snippets=False)
    rows = out["merge_proposals"]
    grouped = [r for r in rows if r.get("group") == "widget pass"]
    singles = [r for r in rows
               if "gizmo relay" in (r["from"]["display"], r["into"]["display"])]
    assert len(grouped) == 3
    assert singles and all(r.get("group") is None for r in singles)


def test_graph_review_merges_carry_group(svc):
    _stage_shared_from(svc)
    finding = next(f for f in svc.graph_review()["findings"]
                   if f["type"] == "merge_candidate")
    groups = [m.get("group") for m in finding["merges"]]
    assert groups.count("widget pass") == 3
    assert None in groups                          # the singleton pair


# ── junk-first routing ────────────────────────────────────────────────────

def test_write_dedup_skips_pair_with_pending_junk(svc):
    # Two near-dup targets clear the 0.6 Jaccard threshold; the junk-flagged
    # one must be skipped while the clean one still files — the control
    # proves the pair WOULD file, so the skip is the junk routing, not the
    # matcher.
    svc.graph_relate("flurble relay service", "related-to", "anchor-a",
                     origin="agent")
    svc.graph_relate("flurble relay service node", "related-to", "anchor-b",
                     origin="agent")
    svc.graph_relate("flurble relay service point", "related-to", "anchor-c",
                     origin="agent")
    svc._storage.insert_entity_proposal(
        "junk", _entity_id(svc, "flurble relay service node"), None, None,
        "slot-key-artifact", time.time())
    with svc._lock:
        svc._propose_write_dedup(_entity_id(svc, "flurble relay service"),
                                 "flurble relay service")
    merges = [p for p in svc._storage.pending_entity_proposals()
              if p.get("kind") == "merge"]
    paired = {p["entity"] for p in merges} | {p["into"] for p in merges}
    assert "flurble relay service point" in paired    # control filed
    assert "flurble relay service node" not in paired  # junk-owned: skipped


def test_deep_dream_skips_merge_cands_with_pending_junk(svc):
    # Same staging as the veto wiring test's control pair — which must file
    # a merge candidate — EXCEPT one side carries a pending junk proposal.
    svc.graph_relate("gadget daemon", "related-to", "anchor-s", origin="agent")
    svc.graph_relate("live gadget daemon", "related-to", "anchor-t",
                     origin="agent")
    for text in (
        "gadget daemon serves the endpoint from the container",
        "gadget daemon restarted cleanly after the deploy",
        "live gadget daemon serves the endpoint from the container",
        "live gadget daemon restarted cleanly after the deploy",
    ):
        svc.store(text, source="phase2-junk-test")
    svc._storage.insert_entity_proposal(
        "junk", _entity_id(svc, "live gadget daemon"), None, None,
        "slot-key-artifact", time.time())
    out = svc.deep_dream(apply=False, include_snippets=False)
    flat = {n for m in out["would_merge_propose"] for n in (m["from"], m["into"])}
    assert "live gadget daemon" not in flat


# ── candidate-slot filters (Phase 2.5) ────────────────────────────────────
#
# Both exclusions must run INSIDE candidate_pairs, before top-k — filtering
# afterwards would still let the excluded pairs consume top-k slots (the
# 2026-08-12 round-2 pass lost ~20 of 49 slots to pairs with pending link
# proposals and 6 more to one junk-flagged compound entity).

def _vec2(x, y):
    import numpy as np
    v = np.array([x, y], dtype=np.float32)
    return v / np.linalg.norm(v)


def _cand_fixture():
    ents = [{"id": 1, "canonical": "a", "display": "a", "etype": None},
            {"id": 2, "canonical": "b", "display": "b", "etype": None},
            {"id": 3, "canonical": "c", "display": "c", "etype": None}]
    vectors = {1: _vec2(1, 0), 2: _vec2(1, 0), 3: _vec2(1, 0)}
    mentions = {1: frozenset({10}), 2: frozenset({20}), 3: frozenset({30})}
    return ents, vectors, mentions


def test_candidate_pairs_skips_pending_proposal_pairs():
    from pseudolife_memory.memory import graph_consolidation as gc

    ents, vectors, mentions = _cand_fixture()
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             pending_pairs={frozenset((1, 2))})
    assert {(c["src_id"], c["dst_id"]) for c in out} == {(1, 3), (2, 3)}


def test_candidate_pairs_skips_excluded_ids():
    from pseudolife_memory.memory import graph_consolidation as gc

    ents, vectors, mentions = _cand_fixture()
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             excluded_ids={3})
    assert {(c["src_id"], c["dst_id"]) for c in out} == {(1, 2)}


def _stage_link_pair(svc, a, b):
    # Two non-name-related entities with near-identical mention entries →
    # a LINK candidate (high sim, no name containment → not a merge).
    svc.graph_relate(a, "related-to", f"anchor-{a[:4]}", origin="agent")
    svc.graph_relate(b, "related-to", f"anchor-{b[:4]}", origin="agent")
    for ent in (a, b):
        svc.store(f"{ent} serves the relay endpoint from the container",
                  source="phase25-test")
        svc.store(f"{ent} restarted cleanly after the deploy",
                  source="phase25-test")


def test_deep_dream_candidates_exclude_pending_link_proposals(svc):
    _stage_link_pair(svc, "gadget relay", "widget beacon")
    out1 = svc.deep_dream(apply=False, include_snippets=False)
    pairs1 = {frozenset((c["src"], c["dst"])) for c in out1["candidates"]}
    assert frozenset(("gadget relay", "widget beacon")) in pairs1  # control
    r = svc.graph_propose_links([
        {"src": "gadget relay", "relation": "related-to",
         "dst": "widget beacon", "similarity": 0.9, "rationale": "test"}])
    assert r["proposed"] == 1
    out2 = svc.deep_dream(apply=False, include_snippets=False)
    pairs2 = {frozenset((c["src"], c["dst"])) for c in out2["candidates"]}
    assert frozenset(("gadget relay", "widget beacon")) not in pairs2


def test_deep_dream_candidates_exclude_junk_owned_entities(svc):
    import time
    _stage_link_pair(svc, "gadget relay", "widget beacon")
    svc._storage.insert_entity_proposal(
        "junk", _entity_id(svc, "widget beacon"), None, None,
        "compound-artifact", time.time())
    out = svc.deep_dream(apply=False, include_snippets=False)
    flat = {n for c in out["candidates"] for n in (c["src"], c["dst"])}
    assert "widget beacon" not in flat


# ── merge accept-rate stat ────────────────────────────────────────────────

def test_graph_review_reports_merge_decision_stats(svc):
    svc.graph_relate("stat-rich-node", "uses", "dep-a")
    svc.graph_relate("stat-rich-node", "uses", "dep-b")
    svc.graph_relate("stat-thin-node", "related-to", "dep-c")
    svc.graph_relate("stat-other-node", "related-to", "dep-d")
    rich = _entity_id(svc, "stat-rich-node")
    thin = _entity_id(svc, "stat-thin-node")
    other = _entity_id(svc, "stat-other-node")
    now = time.time()
    p1 = svc._storage.insert_entity_proposal(
        "merge", thin, rich, 0.9, "test: accept me", now)
    p2 = svc._storage.insert_entity_proposal(
        "merge", other, rich, 0.9, "test: reject me", now)
    assert svc.graph_accept_entity_merge(p1)["accepted"] is True
    assert svc.graph_reject_entity_proposal(p2)["rejected"] is True
    # A dream-auto junk deletion also logs to merge_decisions — it must NOT
    # count toward the MERGE accept rate.
    svc._storage.record_merge_decision(
        None, "junk-node", None, "accepted", None,
        "junk auto-delete: bare-number", "dream-auto", time.time())
    stats = svc.graph_review()["merge_decision_stats"]
    assert stats["accepted"] == 1 and stats["rejected"] == 1
    assert stats["total"] == 2
    assert stats["accept_rate"] == 0.5
