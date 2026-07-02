import numpy as np

from pseudolife_memory.memory import graph_consolidation as gc

ENTS = [
    {"id": 1, "canonical": "daemon", "display": "daemon", "etype": None},
    {"id": 2, "canonical": "docker", "display": "docker", "etype": None},
    {"id": 3, "canonical": "user", "display": "user", "etype": None},
    {"id": 4, "canonical": "windows 11", "display": "Windows 11", "etype": None},
]


def _edge(eid, s, rel, d, conf, origin="agent"):
    return {"id": eid, "src_id": s, "relation": rel, "dst_id": d,
            "confidence": conf, "origin": origin}


def test_rescore_only_changes_agent_edges_that_differ():
    edges = [
        _edge(10, 1, "runs-on", 2, 0.6),          # clean -> should become 0.70
        _edge(11, 3, "runs-on", 4, 0.6),          # violation -> should become 0.175
        _edge(12, 1, "related-to", 2, 0.45),      # already correct -> omitted
        _edge(13, 1, "runs-on", 2, 0.6, "user"),  # non-agent -> omitted
    ]
    out = dict(gc.rescore_edges(edges, ENTS))
    assert out == {10: 0.70, 11: 0.175}


def test_hard_violation_edges_flags_only_typed_violations():
    edges = [
        _edge(10, 1, "runs-on", 2, 0.7),   # daemon(service)->docker(runtime): OK
        _edge(11, 3, "runs-on", 4, 0.175), # user(person)->windows(runtime): violation
        _edge(12, 1, "related-to", 4, 0.45),  # unconstrained relation: never a violation
    ]
    ids = [e["id"] for e in gc.hard_violation_edges(edges, ENTS)]
    assert ids == [11]


def test_exact_duplicate_pairs_folds_lower_degree_into_higher():
    ents = [
        {"id": 1, "canonical": "gemma sidecar", "display": "Gemma sidecar", "etype": None},
        {"id": 2, "canonical": "gemma sidecar", "display": "gemma  sidecar", "etype": None},
        {"id": 3, "canonical": "unrelated", "display": "unrelated thing", "etype": None},
    ]
    # entity 1 has an edge (degree 1), entity 2 has none -> fold 2 into 1
    edges = [_edge(10, 1, "related-to", 3, 0.45)]
    assert gc.exact_duplicate_pairs(ents, edges) == [(2, 1)]


def test_exact_duplicate_pairs_ignores_non_identical_token_sets():
    ents = [
        {"id": 1, "canonical": "schema v8", "display": "schema v8", "etype": None},
        {"id": 2, "canonical": "schema 11", "display": "schema 11", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == []


def test_exact_duplicate_pairs_equal_degree_folds_higher_id_into_lower():
    # token-set-identical displays, both degree 0 (no edges) -> equal degree:
    # fold the HIGHER id into the LOWER id, i.e. (from_id=9, into_id=5).
    ents = [
        {"id": 5, "canonical": "dup", "display": "dup thing", "etype": None},
        {"id": 9, "canonical": "dup", "display": "dup  thing", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == [(9, 5)]


def _vec(*xs):
    return np.asarray(xs, dtype=np.float32)


def test_exact_duplicate_pairs_excludes_concat_artifacts():
    # Two independently-extracted "A<->B" junk concat-artifacts with the same
    # token multiset (e.g. from different extraction passes) must NOT be
    # proposed for auto-merge on this path -- there is no human review here,
    # unlike partition_candidates' merge_cands queue, and _name_contains
    # already refuses to treat a concat artifact as a merge endpoint there.
    ents = [
        {"id": 1, "canonical": "memory_recall<->recall.py",
         "display": "memory_recall<->recall.py", "etype": None},
        {"id": 2, "canonical": "recall.py<->memory_recall",
         "display": "recall.py<->memory_recall", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == []


def test_entity_context_vectors_trace_primary_then_mention_fallback():
    ents = [
        {"id": 1, "canonical": "alpha", "display": "alpha", "etype": None},
        {"id": 2, "canonical": "beta", "display": "beta", "etype": None},
        {"id": 3, "canonical": "ghost", "display": "ghost", "etype": None},
    ]
    entries = [
        {"id": 100, "text": "alpha runs nightly", "embedding": _vec(1, 0)},
        {"id": 101, "text": "beta and alpha discussed", "embedding": _vec(0, 1)},
    ]
    # min_mentions=1: this test checks SOURCE selection (trace vs scan), not the threshold.
    vecs, mentions = gc.entity_context_vectors(ents, entries, {"alpha": [100]}, min_mentions=1)
    assert set(vecs) == {1, 2}                 # ghost omitted (no trace, no mention)
    assert np.allclose(vecs[1], _vec(1, 0))    # alpha from its trace entry
    assert np.allclose(vecs[2], _vec(0, 1))    # beta from the mention scan
    assert mentions[1] == frozenset({100}) and mentions[2] == frozenset({101})


def test_candidate_pairs_filters_edges_scope_and_threshold():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
        {"id": 4, "canonical": "d", "display": "d", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0), 4: _vec(0, 1)}
    mentions = {1: frozenset({10}), 2: frozenset({20}),
                3: frozenset({30}), 4: frozenset({40})}   # all distinct
    edges = [{"id": 9, "src_id": 1, "relation": "related-to", "dst_id": 3,
              "confidence": 0.45, "origin": "agent"}]
    scope = {1: ["pseudolife"], 2: ["pseudolife"], 3: ["gw2-reshade"], 4: ["pseudolife"]}
    out = gc.candidate_pairs(vectors, edges, ents, scope, mentions,
                             min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    # 1-2 kept (sim 1.0, same scope, no edge). 1-3 dropped (edge exists).
    # 2-3 dropped (disjoint scope). 1-4 / 2-4 dropped (sim 0 < 0.55).
    assert pairs == {(1, 2)}
    assert out[0]["similarity"] == 1.0


def test_exact_duplicate_pairs_keeps_short_discriminators():
    # Distinct entities whose ONLY difference is a token graph_review._token_set
    # would drop (<=2 chars, no digit) must NOT be auto-merged.
    cases = [
        ("Extractor", "pg+extractor"),               # 'pg' dropped by the old filter
        ("heuristic bug (a)", "heuristic bug (b)"),   # 'a'/'b'
        ("Phase-2 Option B", "Phase-2 Option C"),     # 'B'/'C'
    ]
    for da, db in cases:
        ents = [
            {"id": 1, "canonical": da.lower(), "display": da, "etype": None},
            {"id": 2, "canonical": db.lower(), "display": db, "etype": None},
        ]
        assert gc.exact_duplicate_pairs(ents, []) == [], f"should not merge {da!r}/{db!r}"


def test_exact_duplicate_pairs_still_merges_quote_artifacts():
    # Pairs differing only by non-alphanumeric noise (quotes, extra spaces) ARE
    # the same entity and must still auto-merge.
    ents = [
        {"id": 1, "canonical": "fixture devserver", "display": "fixture devserver", "etype": None},
        {"id": 2, "canonical": "'fixture devserver'", "display": "'fixture devserver'", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == [(2, 1)]  # equal degree -> higher id folds into lower


def test_entity_context_vectors_min_mentions_gate():
    ents = [
        {"id": 1, "canonical": "one", "display": "one", "etype": None},   # 1 entry
        {"id": 2, "canonical": "two", "display": "two", "etype": None},   # 2 entries
    ]
    entries = [
        {"id": 10, "text": "one only", "embedding": _vec(1, 0)},
        {"id": 20, "text": "two here", "embedding": _vec(1, 0)},
        {"id": 21, "text": "two again", "embedding": _vec(0, 1)},
    ]
    traces = {"one": [10], "two": [20, 21]}
    vecs, mentions = gc.entity_context_vectors(ents, entries, traces)  # default min_mentions=2
    assert set(vecs) == {2}                          # 'one' omitted (only 1 mention)
    assert mentions[2] == frozenset({20, 21})


def test_candidate_pairs_skips_dismissed_pairs():
    # A human 'these are NOT duplicates' verdict (dismissed_pairs, stored as
    # sorted canonical names) must stop the pair resurfacing as a candidate.
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    mentions = {1: frozenset({10}), 2: frozenset({20}), 3: frozenset({30})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             dismissed={("a", "b")})
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    assert pairs == {(1, 3), (2, 3)}


def test_candidate_pairs_drops_identical_mention_sets():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    # 1 and 2 share the SAME supporting entries (pure co-occurrence) -> dropped.
    # 3 has a distinct set -> 1-3 and 2-3 survive.
    mentions = {1: frozenset({10, 11}), 2: frozenset({10, 11}), 3: frozenset({12, 13})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    assert pairs == {(1, 3), (2, 3)}


def test_partition_candidates_merge_vs_link():
    ents = [
        {"id": 1, "canonical": "atlas review", "display": "Atlas Review", "etype": None},
        {"id": 2, "canonical": "atlas review queue", "display": "Atlas Review queue", "etype": None},
        {"id": 3, "canonical": "track a (recall)", "display": "Track A (recall)", "etype": None},
        {"id": 4, "canonical": "track b (insight)", "display": "Track B (insight)", "etype": None},
    ]
    # entity 1 has an edge (degree 1) so 'Atlas Review queue' folds into 'Atlas Review'.
    edges = [_edge(99, 1, "related-to", 3, 0.45)]
    pairs = [
        {"src_id": 1, "dst_id": 2, "src": "Atlas Review", "dst": "Atlas Review queue", "similarity": 0.99},
        {"src_id": 3, "dst_id": 4, "src": "Track A (recall)", "dst": "Track B (insight)", "similarity": 0.98},
    ]
    merges, links = gc.partition_candidates(pairs, ents, edges, merge_min_similarity=0.90)
    assert [(m["from_id"], m["into_id"]) for m in merges] == [(2, 1)]   # >=2-token subset -> merge
    assert merges[0]["reason"] == "token-subset"
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(3, 4)]      # distinct names -> link


def test_partition_candidates_below_threshold_is_link():
    ents = [
        {"id": 1, "canonical": "test", "display": "test", "etype": None},
        {"id": 2, "canonical": "test harness", "display": "test harness", "etype": None},
    ]
    # name-contained, but low similarity -> NOT a merge (guard against coincidental containment).
    pairs = [{"src_id": 1, "dst_id": 2, "src": "test", "dst": "test harness", "similarity": 0.70}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []
    assert len(links) == 1


def test_junk_entities_flags_artifacts_not_real():
    ents = [
        {"id": 1, "canonical": "2", "display": "2", "etype": None},          # bare number
        {"id": 2, "canonical": "live", "display": "LIVE", "etype": None},     # status word
        {"id": 3, "canonical": "ok", "display": "ok", "etype": None},        # too short AND status
        {"id": 4, "canonical": "daemon", "display": "daemon", "etype": None}, # real entity
        {"id": 5, "canonical": "merged", "display": "merged", "etype": None}, # status word, BUT high degree
    ]
    edges = [_edge(10, 5, "related-to", 4, 0.45), _edge(11, 5, "related-to", 1, 0.45)]  # entity 5 degree 2
    out = {j["entity_id"]: j["reason"] for j in gc.junk_entities(ents, edges, max_degree=1)}
    assert out == {1: "bare-number", 2: "status-word", 3: "too-short"}  # 4 real, 5 well-connected


def test_is_concat_artifact_detects_relation_separators():
    for name in ["memory_recall<->recall.py", "schema v8 <-> schema 11",
                 "a ↔ b", "x -> y", "Phase 1 plan<->Phase 2 plan"]:
        assert gc._is_concat_artifact(name) is True, name


def test_is_concat_artifact_ignores_plain_names():
    for name in ["memory_graph", "Atlas Review queue", "claude-code", "4090/Qwen3.6-27B"]:
        assert gc._is_concat_artifact(name) is False, name


def test_is_concat_artifact_requires_nonempty_both_sides():
    assert gc._is_concat_artifact("<-> y") is False   # empty left
    assert gc._is_concat_artifact("x <->") is False   # empty right


def test_name_contains_requires_two_contained_tokens():
    assert gc._name_contains("Atlas Review", "Atlas Review queue") == "token-subset"
    assert gc._name_contains("memory_graph", "Graph") is None       # {graph} = 1 token
    assert gc._name_contains("bank", "live bank") is None           # {bank} = 1 token


def test_name_contains_excludes_concat_artifacts():
    assert gc._name_contains("Phase 2 plan", "Phase 1 plan<->Phase 2 plan") is None


def test_partition_candidates_single_token_subset_is_link_not_merge():
    ents = [
        {"id": 1, "canonical": "bank", "display": "bank", "etype": None},
        {"id": 2, "canonical": "live bank", "display": "live bank", "etype": None},
    ]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "bank", "dst": "live bank", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(1, 2)]


def test_partition_candidates_concat_artifact_target_is_not_merged():
    ents = [
        {"id": 1, "canonical": "phase 2 plan", "display": "Phase 2 plan", "etype": None},
        {"id": 2, "canonical": "phase 1 plan<->phase 2 plan",
         "display": "Phase 1 plan<->Phase 2 plan", "etype": None},
    ]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "Phase 2 plan",
              "dst": "Phase 1 plan<->Phase 2 plan", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []                         # artifact endpoint excluded from merge
    assert len(links) == 1


def test_junk_entities_flags_concat_artifacts_regardless_of_degree():
    ents = [
        {"id": 1, "canonical": "memory_recall<->recall.py",
         "display": "memory_recall<->recall.py", "etype": None},
        {"id": 2, "canonical": "recall.py", "display": "recall.py", "etype": None},
        {"id": 3, "canonical": "memory_recall", "display": "memory_recall", "etype": None},
    ]
    # entity 1 is well-connected (degree 2) yet must still be flagged as an artifact
    edges = [_edge(10, 1, "related-to", 2, 0.45), _edge(11, 1, "related-to", 3, 0.45)]
    out = {j["entity_id"]: j["reason"] for j in gc.junk_entities(ents, edges, max_degree=1)}
    assert out == {1: "concat-artifact"}   # 2 and 3 are real; flagged despite degree 2
