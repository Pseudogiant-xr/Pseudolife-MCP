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
