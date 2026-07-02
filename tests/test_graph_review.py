# tests/test_graph_review.py
from pseudolife_memory.memory import graph_review as gr


def _ents(*names):
    return [{"id": i + 1, "display": n, "canonical": n.lower(), "etype": None}
            for i, n in enumerate(names)]


def test_duplicate_candidates_flags_near_identical_names():
    ents = _ents("Cortex Console web frontend", "web frontend (Cortex Console)", "postgres")
    dups = gr.duplicate_candidates(ents)
    assert dups and dups[0]["type"] == "duplicate" and dups[0]["action"] == "merge"
    assert "postgres" not in dups[0]["label"]


def test_test_artifacts_matches_known_patterns():
    ents = _ents("payments/payments-db", "pl-healthcheck-target", "pseudolife-mcp")
    arts = gr.test_artifacts(ents)
    assert arts and arts[0]["action"] == "delete"
    assert set(arts[0]["entities"]) == {"payments/payments-db", "pl-healthcheck-target"}


def test_orphans_flags_degree_le_1():
    ents = _ents("a", "b", "lonely")
    edges = [{"src_id": 1, "relation": "uses", "dst_id": 2, "origin": "action", "confidence": 0.9}]
    orph = gr.orphans(edges, ents)
    assert orph and "lonely" in orph[0]["entities"]


def test_dubious_edges_flags_low_conf_agent():
    ents = _ents("memory_recall", "docker-desktop")
    edges = [{"src_id": 1, "relation": "runs-on", "dst_id": 2, "origin": "agent", "confidence": 0.6}]
    dub = gr.dubious_edges(ents and edges, ents)
    assert dub and dub[0]["action"] == "prune" and dub[0]["edges"][0]["src"] == "memory_recall"


def test_unattributed_flags_entities_without_sources():
    ents = _ents("attributed", "orphan-of-project")
    un = gr.unattributed(ents, {1: ["pseudolife"]})
    assert un and un[0]["entities"] == ["orphan-of-project"] and un[0]["action"] == "assign"


def test_review_aggregates_all_groups():
    ents = _ents("payments-db", "lonely")
    out = gr.review([], ents, {})
    types = {f["type"] for f in out["findings"]}
    assert {"test_artifact", "orphan", "unattributed"} <= types
    assert out["counts"]["total"] == len(out["findings"])


def _pairs(findings):
    return {frozenset(f["entities"]) for f in findings}


def test_version_and_phase_numbers_not_collapsed():
    ents = _ents("schema v8", "schema 11", "schema 15->16",
                 "Phase 1 plan", "Phase 2 plan",
                 "Atlas Stage 1", "Atlas Stage 2")
    assert gr.duplicate_candidates(ents) == []


def test_genuine_phrasing_duplicate_still_flagged():
    ents = _ents("memcot_bench.py", "memcot bench")
    assert frozenset({"memcot_bench.py", "memcot bench"}) in _pairs(gr.duplicate_candidates(ents))


def test_duplicate_candidates_skips_dismissed_pairs():
    # 2026-07-02 review fix 3: a human-dismissed false positive (postgres vs
    # postgres.py class) must stay dismissed across analyzer runs.
    ents = _ents("memcot_bench.py", "memcot bench")
    key = tuple(sorted((ents[0]["canonical"], ents[1]["canonical"])))
    assert gr.duplicate_candidates(ents, dismissed={key}) == []
    # and review() threads the set through
    out = gr.review([], ents, {1: ["p"], 2: ["p"]}, dismissed_pairs={key})
    assert not [f for f in out["findings"] if f["type"] == "duplicate"]


def test_legit_fixtures_and_lessons_not_flagged():
    ents = _ents("fixture devserver",
                 "TDD pattern: PG service test + fixture stubs + web routes")
    assert gr.test_artifacts(ents) == []


def test_real_test_artifacts_still_flagged():
    ents = _ents("deploy-smoke-foo", "pl-healthcheck-probe", "payments/payments-db",
                 "Cortex Console")  # a normal entity, must NOT be flagged
    out = gr.test_artifacts(ents)
    assert out and set(out[0]["entities"]) == {
        "deploy-smoke-foo", "pl-healthcheck-probe", "payments/payments-db"}


from pseudolife_memory.memory.graph_review import dubious_edges


def test_dubious_edges_discriminate_by_confidence():
    entities = _ents("a", "b", "c")
    ids = {e["display"]: e["id"] for e in entities}
    edges = [
        {"src_id": ids["a"], "relation": "runs-on", "dst_id": ids["b"],
         "origin": "agent", "confidence": 0.175},   # violation -> flagged
        {"src_id": ids["a"], "relation": "uses", "dst_id": ids["c"],
         "origin": "agent", "confidence": 0.70},      # good -> NOT flagged
    ]
    out = dubious_edges(edges, entities)
    assert out, "low-confidence edge should produce a finding"
    flagged = out[0]["edges"]
    assert len(flagged) == 1 and flagged[0]["confidence"] == 0.175


def test_proposed_links_finding_shape():
    props = [{"id": 7, "src": "alpha", "relation": "related-to", "dst": "beta",
              "confidence": 0.45, "similarity": 0.91, "rationale": "co-discussed"}]
    out = gr.proposed_links(props)
    assert len(out) == 1
    f = out[0]
    assert f["type"] == "proposed_link" and f["action"] == "review"
    assert f["links"][0]["src"] == "alpha" and f["links"][0]["dst"] == "beta"
    # the edge_proposals id must travel so the link is accept/reject-able
    assert f["links"][0]["id"] == 7


def test_proposed_links_empty_when_none():
    assert gr.proposed_links([]) == []


def test_review_includes_proposals_when_passed():
    out = gr.review([], [], {}, proposals=[
        {"src": "a", "relation": "related-to", "dst": "b", "confidence": 0.45}])
    assert any(f["type"] == "proposed_link" for f in out["findings"])


def test_merge_and_junk_candidate_findings():
    eprops = [
        {"id": 1, "kind": "merge", "entity": "live daemon", "into": "daemon",
         "score": 0.99, "reason": "token-subset"},
        {"id": 2, "kind": "junk", "entity": "2", "into": None, "reason": "bare-number"},
    ]
    out = gr.review([], [], {}, entity_proposals=eprops)
    types = {f["type"] for f in out["findings"]}
    assert "merge_candidate" in types and "junk_candidate" in types
    mc = next(f for f in out["findings"] if f["type"] == "merge_candidate")
    assert mc["merges"][0]["from"] == "live daemon" and mc["merges"][0]["into"] == "daemon"
    jc = next(f for f in out["findings"] if f["type"] == "junk_candidate")
    assert jc["entities"][0]["entity"] == "2" and jc["entities"][0]["reason"] == "bare-number"


def test_entity_proposals_default_none_no_findings():
    out = gr.review([], [], {})
    assert all(f["type"] not in ("merge_candidate", "junk_candidate") for f in out["findings"])
