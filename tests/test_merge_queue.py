"""The merge queue: which pairs get proposed, and how the queue presents them.

Two halves of one 2026-08-12 change.

**Veto rules** (``graph_review.merge_veto``, pure, no DB) — measured against
the 2026-08-11 triage ground truth (tests/fixtures/
merge_triage_replay_20260811.json, 153 human/agent-judged proposals):

* ``event-slug`` — a date/run-tag-stamped name is an EVENT; a broader name
  must not fold into it (project-vs-event was the largest false-positive
  class). Same-name-modulo-date pairs stay proposable: the date-stripped
  token sets are compared for equality.
* ``numeric-substitution`` — token sets that differ only by numeric tokens
  with matching alpha stems (CT200/CT400) or by the pre/post antonym pair
  are siblings, not duplicates. One-sided numeric EXTENSIONS (v2.0.0 vs v2)
  stay proposable — only substitutions veto.

The replay test is the acceptance gate: a rule that suppresses even one
accepted merge does not ship.

**Queue presentation** — same-from grouping, junk-first routing, fold
re-orientation, and the merge accept-rate stat. Motivated by the same
triage: 22 of 153 merge proposals shared a ``from`` entity (the write-dedup
detector files up to three matches per mint), so what is really one
where-does-this-entity-belong decision presented as independent rows;
several rows paired a junk-shaped entity that the junk queue already owned;
and the accept/reject split — the direct measure of detector precision —
vanished into the audit log with no queryable summary.

Staging note: the service-level tests share the ``svc`` fixture and the
staging helpers but NOT one module-scoped bank. Their state is the pending
*proposal* set, and they mutate it in conflicting ways — one asserts
``_pending_merges(svc) == []``, and the junk/link-exclusion tests each
insert a proposal whose presence makes another test's control assertion
vacuous. A shared bank would make them order-coupled and let them pass for
the wrong reason, so staging stays function-scoped.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pseudolife_memory.memory.graph_review import merge_veto

FIXTURE = Path(__file__).parent / "fixtures" / "merge_triage_replay_20260811.json"


# ── event-slug rule ───────────────────────────────────────────────────────

def test_event_slug_vetoes_broader_name_vs_dated_event():
    # Project/programme names must not fold into a dated run/event node.
    assert merge_veto("Tri-Serpent",
                      "tri-serpent-overnight-build-20260807") == "event-slug"
    assert merge_veto("evlora-preregistration",
                      "evq-gate-run-0806") == "event-slug"
    assert merge_veto("hermes-fix-deploy",
                      "hermes-engine-deploy-20260807") == "event-slug"


def test_event_slug_allows_same_name_modulo_date():
    # The dated slug IS the other side's name plus a date — naming drift of
    # one event, exactly what the merge queue exists for.
    assert merge_veto("Tri-Serpent overnight build",
                      "tri-serpent-overnight-build-20260807") is None
    assert merge_veto("EVQ residual decomposition",
                      "evq-0806-residual-decomposition") is None


def test_event_slug_ignores_pairs_where_both_sides_are_dated():
    # Two dated events (even different dates) are out of this rule's scope —
    # deciding them needs evidence, not a name heuristic.
    assert merge_veto("retention-interval eval (ret-0809)",
                      "ret-0809-eval") is None
    assert merge_veto("dream-consolidation-contamination-20260807",
                      "deep-dream-2026-08-05") is None


def test_event_slug_ignores_plain_version_numbers():
    # v0.13.0 / PR numbers / ports are not run tags.
    assert merge_veto("GitHub release v0.13.0", "v0.13.0") is None
    assert merge_veto("claude-opus-5 shim on :8082", "opus-shim") is None


# ── numeric-substitution rule ─────────────────────────────────────────────

def test_numeric_substitution_vetoes_sibling_ids():
    assert merge_veto("CT200 hostb-lab", "CT400") == "numeric-substitution"
    assert merge_veto("0-11-0-release", "0-13-0-release") == "numeric-substitution"


def test_compact_date_siblings_veto_as_numeric_substitution():
    # Same name, two COMPACT dates: sibling events. (Separator-form dates are
    # stripped whole, so their pairs fall through to evidence — documented
    # asymmetry, see the merge_veto docstring.)
    assert merge_veto("notes-20260805", "notes-20260807") == "numeric-substitution"


def test_mmdd_window_includes_dimension_shaped_tokens():
    # Documented limit of the run-tag window: 1024 parses as MM/DD, so a
    # dimension/port-shaped token can flag a name as dated...
    assert merge_veto("gemma 1024 ctx", "gemma context window") == "event-slug"
    # ...but the one-sided extension stays safe via strip-and-compare.
    assert merge_veto("gemma 1024", "gemma") is None


def test_pre_post_antonyms_veto():
    assert merge_veto("evals/results/qwen-27b-pr104-post.json",
                      "evals/results/qwen-27b-pr104-pre.json") == "numeric-substitution"


def test_numeric_extension_is_not_a_substitution():
    # One side merely EXTENDS the other with numeric tokens — classic
    # naming drift of one referent (accepted merges in the ground truth).
    assert merge_veto("MCP SDK v2.0.0", "MCP SDK v2") is None
    assert merge_veto("CT100 hosta-hermes", "hosta-hermes") is None
    assert merge_veto("v28 chronicle events", "chronicle events") is None


def test_alpha_substitution_is_not_vetoed():
    # Differing ALPHA tokens need evidence, not a name rule.
    assert merge_veto("ev2-separate-pass", "separate-pass-events") is None
    assert merge_veto("Run B", "Run T") is None


# ── replay gate: the 2026-08-11 triage ground truth ───────────────────────

@pytest.fixture(scope="module")
def replay():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]


def _verdicts(replay, kind):
    return [p for p in replay if p["verdict"] == kind]


def test_replay_never_suppresses_an_accepted_merge(replay):
    hits = [(p["from"], p["into"], merge_veto(p["from"], p["into"]))
            for p in _verdicts(replay, "accept")
            if merge_veto(p["from"], p["into"])]
    assert hits == []


def test_replay_each_rule_is_load_bearing(replay):
    # Every shipped rule must kill at least a handful of real rejects —
    # a rule that never fires on ground truth is decoration.
    fired = {}
    for p in _verdicts(replay, "reject"):
        r = merge_veto(p["from"], p["into"])
        if r:
            fired[r] = fired.get(r, 0) + 1
    # The merge-queue ground truth holds only two numeric-substitution
    # rejects (the CT-sibling class was dismissed at the CANDIDATE stage,
    # before filing) — the unit tests above carry that class's coverage.
    assert fired.get("event-slug", 0) >= 5
    assert fired.get("numeric-substitution", 0) >= 2


def test_replay_reports_total_suppression(replay):
    # Not a gate — a pinned record of measured coverage so a future change
    # that silently weakens the rules goes red here, with the real number
    # in the assertion message.
    rejects = _verdicts(replay, "reject")
    killed = sum(1 for p in rejects if merge_veto(p["from"], p["into"]))
    assert killed == 12, (
        f"measured coverage changed: {killed}/101 rejects suppressed "
        "(was 12 — update this pin AND the CHANGELOG entry together)")


# ── wiring, routing, and presentation (PG-backed) ─────────────────────────

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401,E402  (fixtures)


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


def _pending_merges(svc):
    return [p for p in svc._storage.pending_entity_proposals()
            if p.get("kind") == "merge"]


def _stage_gadget_daemon_pair(svc):
    """A plain near-duplicate pair that files a merge candidate.

    Entities mint via graph_relate; their context vectors come from
    token-mention entries (entity_context_vectors' fallback scan), with the
    broader name mentioned in the narrower name's entries too, so the
    mention sets differ and the co-occurrence drop does not fire.
    """
    svc.graph_relate("gadget daemon", "related-to", "anchor-s", origin="agent")
    svc.graph_relate("live gadget daemon", "related-to", "anchor-t",
                     origin="agent")
    for text in (
        "gadget daemon serves the endpoint from the container",
        "gadget daemon restarted cleanly after the deploy",
        "live gadget daemon serves the endpoint from the container",
        "live gadget daemon restarted cleanly after the deploy",
    ):
        svc.store(text, source="merge-queue-test")


def _stage_link_pair(svc, a, b):
    # Two non-name-related entities with near-identical mention entries →
    # a LINK candidate (high sim, no name containment → not a merge).
    svc.graph_relate(a, "related-to", f"anchor-{a[:4]}", origin="agent")
    svc.graph_relate(b, "related-to", f"anchor-{b[:4]}", origin="agent")
    for ent in (a, b):
        svc.store(f"{ent} serves the relay endpoint from the container",
                  source="merge-queue-test")
        svc.store(f"{ent} restarted cleanly after the deploy",
                  source="merge-queue-test")


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


# ── veto wiring at the two filing sites ───────────────────────────────────

def test_write_dedup_skips_vetoed_match_but_files_plain_near_dup(svc):
    # Existing programme entity; a dream then mints the dated build event.
    # Jaccard clears the 0.6 threshold, but the event-slug veto must stop
    # the filing. The control mint (no slug) proves the plumbing still files.
    svc.graph_relate("tri-serpent-build-programme", "related-to", "anchor-x")
    svc.graph_relate("tri-serpent-build-20260807", "related-to", "anchor-y")
    with svc._lock:
        svc._propose_write_dedup(
            _entity_id(svc, "tri-serpent-build-20260807"),
            "tri-serpent-build-20260807")
    assert _pending_merges(svc) == []          # vetoed: nothing filed
    svc.graph_relate("tri-serpent-build-prog", "related-to", "anchor-z")
    with svc._lock:
        svc._propose_write_dedup(
            _entity_id(svc, "tri-serpent-build-prog"),
            "tri-serpent-build-prog")
    assert len(_pending_merges(svc)) >= 1      # control: plain near-dup files


def test_deep_dream_filters_vetoed_merge_candidates(svc):
    # The deep-dream filing site: a high-similarity name-contained pair that
    # trips the event-slug veto must be absent from would_merge_propose,
    # while a plain near-duplicate pair staged the same way survives —
    # deleting the merge_cands filter in deep_dream turns this red.
    svc.graph_relate("tri serpent", "related-to", "anchor-q", origin="agent")
    svc.graph_relate("tri serpent overnight build 20260807", "related-to",
                     "anchor-r", origin="agent")
    _stage_gadget_daemon_pair(svc)
    for text in (
        "tri serpent ran clean on the lab box overnight",
        "tri serpent output verified against the manifest",
        "tri serpent overnight build 20260807 ran clean on the lab box",
        "tri serpent overnight build 20260807 output verified against the manifest",
    ):
        svc.store(text, source="merge-queue-test")
    out = svc.deep_dream(apply=False)
    flat = {n for m in out["would_merge_propose"] for n in (m["from"], m["into"])}
    assert "gadget daemon" in flat or "live gadget daemon" in flat
    assert not any("20260807" in n for n in flat)


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
    merges = _pending_merges(svc)
    paired = {p["entity"] for p in merges} | {p["into"] for p in merges}
    assert "flurble relay service point" in paired    # control filed
    assert "flurble relay service node" not in paired  # junk-owned: skipped


def test_deep_dream_skips_merge_cands_with_pending_junk(svc):
    # Same staging as the veto wiring test's control pair — which must file
    # a merge candidate — EXCEPT one side carries a pending junk proposal.
    _stage_gadget_daemon_pair(svc)
    svc._storage.insert_entity_proposal(
        "junk", _entity_id(svc, "live gadget daemon"), None, None,
        "slot-key-artifact", time.time())
    out = svc.deep_dream(apply=False, include_snippets=False)
    flat = {n for m in out["would_merge_propose"] for n in (m["from"], m["into"])}
    assert "live gadget daemon" not in flat


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
    _stage_link_pair(svc, "gadget relay", "widget beacon")
    svc._storage.insert_entity_proposal(
        "junk", _entity_id(svc, "widget beacon"), None, None,
        "compound-artifact", time.time())
    out = svc.deep_dream(apply=False, include_snippets=False)
    flat = {n for c in out["candidates"] for n in (c["src"], c["dst"])}
    assert "widget beacon" not in flat


# ── same-from grouping ────────────────────────────────────────────────────

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


# ── fold direction is re-derived at review time ───────────────────────────

def test_enrich_reorients_stale_direction(tmp_path):
    # A proposal stored (rich -> thin) at filing time presents flipped once
    # current evidence favors the other side — rows 981/983 regression.
    # File-mode service on purpose: this contract must stay in the
    # always-green set, not skip silently with the PG-backed tests.
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path)
    entities = [{"id": 1, "display": "rich", "canonical": "rich", "etype": None},
                {"id": 2, "display": "thin", "canonical": "thin", "etype": None}]
    edges = [{"src_id": 1, "dst_id": 9, "relation": "uses"},
             {"src_id": 1, "dst_id": 8, "relation": "uses"}]
    pending = [{"id": 7, "kind": "merge", "entity_id": 1, "into_id": 2,
                "score": 0.9, "reason": "write-dedup: stale direction"}]
    out = svc._enrich_merge_proposals(
        pending, entities, edges, [], {}, {}, {}, 0, 0, False,
        fact_counts={})
    assert out[0]["from"]["display"] == "thin"
    assert out[0]["into"]["display"] == "rich"


def test_graph_review_presents_accept_direction(svc):
    # The graph_review payload drives the Atlas/console/wiki Merge buttons —
    # it must show the direction graph_accept_entity_merge will APPLY, not
    # the stale stored one (review finding, 2026-08-12).
    svc.graph_relate("review-rich-node", "uses", "dep-a")
    svc.graph_relate("review-rich-node", "uses", "dep-b")
    svc.graph_relate("review-thin-node", "related-to", "dep-c")
    rich = _entity_id(svc, "review-rich-node")
    thin = _entity_id(svc, "review-thin-node")
    svc._storage.insert_entity_proposal(
        "merge", rich, thin, 0.9, "test: stale direction", time.time())
    finding = next(f for f in svc.graph_review()["findings"]
                   if f["type"] == "merge_candidate")
    m = next(m for m in finding["merges"]
             if "review-rich-node" in (m["from"], m["into"]))
    assert m["from"] == "review-thin-node"
    assert m["into"] == "review-rich-node"


def test_accept_merge_folds_thin_into_evidence_bearing_current(svc):
    svc.graph_relate("stale-rich-node", "uses", "dep-one")
    svc.graph_relate("stale-rich-node", "uses", "dep-two")
    svc.graph_relate("thin-node", "related-to", "dep-three")
    rich, thin = _entity_id(svc, "stale-rich-node"), _entity_id(svc, "thin-node")
    # Stored backwards: rich as the fold-away side.
    pid = svc._storage.insert_entity_proposal(
        "merge", rich, thin, 0.9, "test: stale direction", time.time())
    res = svc.graph_accept_entity_merge(pid)
    assert res["accepted"] is True
    assert res["from"] == "thin-node" and res["into"] == "stale-rich-node"
    survivors = {e["display"] for e in svc._storage.load_graph()["entities"]}
    assert "stale-rich-node" in survivors and "thin-node" not in survivors


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
