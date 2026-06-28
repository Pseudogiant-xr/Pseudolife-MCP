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
