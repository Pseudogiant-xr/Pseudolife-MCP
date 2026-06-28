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
    # alpha has a trace to entry 100; beta has none -> mention-scan finds entry 101
    vecs = gc.entity_context_vectors(ents, entries, {"alpha": [100]})
    assert set(vecs) == {1, 2}                 # ghost omitted (no trace, no mention)
    assert np.allclose(vecs[1], _vec(1, 0))    # alpha from its trace entry
    assert np.allclose(vecs[2], _vec(0, 1))    # beta from the mention scan


def test_candidate_pairs_filters_edges_scope_and_threshold():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
        {"id": 4, "canonical": "d", "display": "d", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0), 4: _vec(0, 1)}
    edges = [{"id": 9, "src_id": 1, "relation": "related-to", "dst_id": 3,
              "confidence": 0.45, "origin": "agent"}]
    scope = {1: ["pseudolife"], 2: ["pseudolife"], 3: ["gw2-reshade"], 4: ["pseudolife"]}
    out = gc.candidate_pairs(vectors, edges, ents, scope, min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    # 1-2 kept (sim 1.0, same scope, no edge). 1-3 dropped (edge exists).
    # 2-3 dropped (disjoint scope). 1-4 / 2-4 dropped (sim 0 < 0.55).
    assert pairs == {(1, 2)}
    assert out[0]["similarity"] == 1.0
