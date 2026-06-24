import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory.memory import graph_insight as gi  # noqa: E402


def _two_clusters():
    # {1,2,3} clique and {4,5,6} clique joined by a single bridge 3-4.
    return [
        {"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3}, {"src_id": 1, "dst_id": 3},
        {"src_id": 4, "dst_id": 5}, {"src_id": 5, "dst_id": 6}, {"src_id": 4, "dst_id": 6},
        {"src_id": 3, "dst_id": 4},
    ]


def test_detect_two_communities():
    comms = gi.detect_communities(_two_clusters())
    # Two clusters recovered; ids are size-desc, 0-indexed.
    assert set(comms.keys()) == {0, 1}
    members = sorted(sorted(v) for v in comms.values())
    assert members == [[1, 2, 3], [4, 5, 6]]


def test_detect_empty():
    assert gi.detect_communities([]) == {}


def test_cohesion_score_full_triangle():
    # 3 nodes, 3 edges = complete -> cohesion 1.0
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3}, {"src_id": 1, "dst_id": 3}]
    assert gi.cohesion_score(edges, [1, 2, 3]) == 1.0
    assert gi.cohesion_score(edges, [1]) == 1.0          # singleton


def test_remap_to_previous_keeps_ids_stable():
    # New partition identical to prior but with permuted temp ids -> prior ids restored.
    communities = {0: [4, 5, 6], 1: [1, 2, 3]}
    prior = {1: 7, 2: 7, 3: 7, 4: 9, 5: 9, 6: 9}
    remapped = gi.remap_to_previous(communities, prior)
    # {1,2,3} carried prior id 7; {4,5,6} carried prior id 9.
    assert {min(v): k for k, v in remapped.items()} == {1: 7, 4: 9}


def test_remap_first_run_no_prior():
    communities = {0: [1, 2], 1: [3]}
    assert gi.remap_to_previous(communities, {}) == {0: [1, 2], 1: [3]}


def test_summarize_communities_labels_by_top_degree():
    edges = _two_clusters()
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 7)]
    comms = gi.detect_communities(edges)
    summ = gi.summarize_communities(comms, edges, entities)
    assert {s["id"] for s in summ} == set(comms.keys())
    for s in summ:
        assert s["size"] >= 1 and 0.0 <= s["cohesion"] <= 1.0 and s["label"].startswith("e")


def test_god_nodes_ranks_by_degree():
    # hub=1 connects to 2,3,4; leaf edge 5-6.
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 1, "dst_id": 3},
             {"src_id": 1, "dst_id": 4}, {"src_id": 5, "dst_id": 6}]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 7)]
    gods = gi.god_nodes(edges, entities, top_n=2)
    assert gods[0]["entity_id"] == 1 and gods[0]["degree"] == 3
    assert gods[0]["display"] == "e1" and len(gods) == 2


def test_god_nodes_excludes_etype():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 1, "dst_id": 3}]
    entities = [{"id": 1, "display": "hub", "etype": "structural"},
                {"id": 2, "display": "a", "etype": None},
                {"id": 3, "display": "b", "etype": None}]
    gods = gi.god_nodes(edges, entities, top_n=5, exclude_etypes=("structural",))
    assert all(g["entity_id"] != 1 for g in gods)


def test_surprising_connections_flags_cross_community_bridge():
    edges = [
        {"src_id": 1, "dst_id": 2, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 4, "dst_id": 5, "relation": "uses", "confidence": 0.9, "origin": "user"},
        # bridge between the two communities, agent-inferred + low confidence
        {"src_id": 2, "dst_id": 4, "relation": "relates-to", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 6)]
    node_comm = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}
    out = gi.surprising_connections(edges, entities, node_comm, top_n=5)
    assert out and out[0]["src"] == "e2" and out[0]["dst"] == "e4"
    assert "community" in out[0]["why"].lower()
    assert out[0]["origin"] == "agent"


def test_surprising_connections_dedup_by_community_pair():
    # Two edges between the same community pair -> only one representative kept.
    edges = [
        {"src_id": 1, "dst_id": 3, "relation": "r", "confidence": 0.5, "origin": "agent"},
        {"src_id": 2, "dst_id": 4, "relation": "r", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 5)]
    node_comm = {1: 0, 2: 0, 3: 1, 4: 1}
    out = gi.surprising_connections(edges, entities, node_comm, top_n=5)
    assert len(out) == 1
