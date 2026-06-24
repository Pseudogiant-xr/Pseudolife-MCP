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


def test_suggest_questions_contested_fact():
    contested = [{"entity": "postgres", "attribute": "port", "value": "5433",
                  "contender_value": "5432", "contender_origin": "agent"}]
    qs = gi.suggest_questions([], [], {}, {}, contested, [], top_n=5)
    assert any(q["type"] == "contested_fact" and "5433" in q["question"]
               and "5432" in q["question"] for q in qs)


def test_suggest_questions_isolated_entity():
    edges = [{"src_id": 1, "dst_id": 2, "relation": "r", "confidence": 0.9, "origin": "user"}]
    entities = [{"id": 1, "display": "a", "etype": None},
                {"id": 2, "display": "b", "etype": None},
                {"id": 3, "display": "lonely", "etype": None}]  # degree 0
    comms = {0: [1, 2], 1: [3]}
    qs = gi.suggest_questions(edges, entities, comms, gi._node_community(comms), [], [], top_n=5)
    assert any(q["type"] == "isolated_entity" and "lonely" in q["question"] for q in qs)


def test_suggest_questions_verify_inferred():
    # Hub e1 has 2 agent-origin (dream-inferred) edges -> a verify question.
    edges = [
        {"src_id": 1, "dst_id": 2, "relation": "r", "confidence": 0.9, "origin": "user"},
        {"src_id": 1, "dst_id": 3, "relation": "r", "confidence": 0.5, "origin": "agent"},
        {"src_id": 1, "dst_id": 4, "relation": "r", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 5)]
    comms = {0: [1, 2, 3, 4]}
    qs = gi.suggest_questions(edges, entities, comms, gi._node_community(comms), [],
                              gi.summarize_communities(comms, edges, entities), top_n=10)
    assert any(q["type"] == "verify_inferred" and "e1" in q["question"] for q in qs)


def test_suggest_questions_bridge_entity():
    # Inject a 2-community split so the test doesn't depend on Louvain: node 3
    # bridges {1,2,3} to {4,5} and carries the highest betweenness.
    edges = [
        {"src_id": 1, "dst_id": 2, "relation": "r", "confidence": 0.9, "origin": "user"},
        {"src_id": 2, "dst_id": 3, "relation": "r", "confidence": 0.9, "origin": "user"},
        {"src_id": 1, "dst_id": 3, "relation": "r", "confidence": 0.9, "origin": "user"},
        {"src_id": 4, "dst_id": 5, "relation": "r", "confidence": 0.9, "origin": "user"},
        {"src_id": 3, "dst_id": 4, "relation": "r", "confidence": 0.9, "origin": "user"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 6)]
    comms = {0: [1, 2, 3], 1: [4, 5]}
    qs = gi.suggest_questions(edges, entities, comms, gi._node_community(comms), [],
                              gi.summarize_communities(comms, edges, entities), top_n=10)
    assert any(q["type"] == "bridge_entity" for q in qs)


def test_suggest_questions_low_cohesion():
    # Injected 6-node community with only 2 edges -> cohesion 2/15 < 0.15, size >= 5.
    edges = [{"src_id": 1, "dst_id": 2, "relation": "r", "confidence": 0.9, "origin": "user"},
             {"src_id": 3, "dst_id": 4, "relation": "r", "confidence": 0.9, "origin": "user"}]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 7)]
    comms = {0: [1, 2, 3, 4, 5, 6]}
    qs = gi.suggest_questions(edges, entities, comms, gi._node_community(comms), [],
                              gi.summarize_communities(comms, edges, entities), top_n=10)
    assert any(q["type"] == "low_cohesion" for q in qs)


def test_build_digest_assembles_all_sections():
    edges = [
        {"src_id": 1, "dst_id": 2, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 2, "dst_id": 3, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 1, "dst_id": 3, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 4, "dst_id": 5, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 3, "dst_id": 4, "relation": "x", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 6)]
    comms = gi.detect_communities(edges)
    summ = gi.summarize_communities(comms, edges, entities)
    digest = gi.build_digest(comms, summ, edges, entities, [], 123.0)
    assert digest["computed_at"] == 123.0
    assert digest["totals"] == {"entities": 5, "edges": 5, "communities": len(comms)}
    assert {"communities", "god_nodes", "surprises", "questions"} <= set(digest)
    assert digest["god_nodes"][0]["degree"] >= 1


def test_graph_insight_config_defaults():
    from pseudolife_memory.utils.config import GraphInsightConfig
    c = GraphInsightConfig()
    assert c.enabled is True and c.algorithm == "louvain"
    assert c.resolution == 1.0 and c.max_community_fraction == 0.25
    assert c.god_nodes_top_n == 10 and c.surprises_top_n == 10
    assert c.questions_top_n == 7 and c.betweenness_sample == 200
