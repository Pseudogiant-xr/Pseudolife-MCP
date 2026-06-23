import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import memcot_bench as mb  # noqa: E402


def test_known_entities_cover_all_edge_endpoints():
    endpoints = set()
    for rec in mb.CORPUS:
        for src, _rel, dst in rec["edges"]:
            endpoints.add(src)
            endpoints.add(dst)
    assert endpoints <= mb.KNOWN_ENTITIES


def test_every_edge_uses_closed_vocab():
    allowed = {"depends-on", "runs-on", "part-of", "uses", "stores-data-in"}
    for rec in mb.CORPUS:
        for _src, rel, _dst in rec["edges"]:
            assert rel in allowed


def test_every_question_gold_is_reachable_within_its_hops():
    # BFS over the seeded edges (undirected) from any entity named in the
    # question; gold must be reachable within `hops` steps.
    adj: dict[str, set[str]] = {}
    for rec in mb.CORPUS:
        for src, _rel, dst in rec["edges"]:
            adj.setdefault(src, set()).add(dst)
            adj.setdefault(dst, set()).add(src)
    for q in mb.QUESTIONS:
        seeds = mb.spot_entities(q["question"], mb.KNOWN_ENTITIES)
        seen = set(seeds)
        frontier = set(seeds)
        for _ in range(q["hops"]):
            nxt = set()
            for e in frontier:
                nxt |= adj.get(e, set())
            seen |= nxt
            frontier = nxt
        assert q["gold"] in seen, q


def test_spot_entities_word_boundary():
    known = {"checkout-svc", "jvm-21"}
    assert mb.spot_entities("checkout-svc depends-on billing-lib", known) == ["checkout-svc"]
    assert mb.spot_entities("nothing relevant here", known) == []


def test_assembled_context_unions_texts_facts_entities():
    st = mb.LoopState(entities={"jvm-21"}, texts=["t1"], facts=["runtime=jvm-21"])
    assert mb.assembled_context(st) == ["t1", "runtime=jvm-21", "jvm-21"]


def test_mechanical_controller_seeds_with_question():
    c = mb.MechanicalController()
    assert c.seed_queries("what runs checkout-svc?") == ["what runs checkout-svc?"]


def test_mechanical_controller_expands_on_new_entities():
    c = mb.MechanicalController()
    queries, stop = c.expand("q", ["billing-lib", "jvm-21"])
    assert stop is False
    assert queries == ["q billing-lib", "q jvm-21"]


def test_mechanical_controller_stops_when_no_new_entities():
    c = mb.MechanicalController()
    assert c.expand("q", []) == ([], True)


class _FakeSvc:
    """Duck-typed MemoryService for engine unit tests.

    `search` is deliberately weak — it returns only snippets that contain a
    query token verbatim — so multi-hop terminals are NOT retrievable by
    re-query alone; the graph must do the traversal.
    """

    def __init__(self, snippets, edges):
        self.snippets = snippets
        self.edges = edges  # list[(src, rel, dst)]

    def search(self, query, top_k=5):
        toks = set(re.findall(r"[\w-]+", query.lower()))
        hits = [s for s in self.snippets
                if toks & set(re.findall(r"[\w-]+", s.lower()))]
        hits = hits[:top_k]
        return {"entries": [{"text": s, "score": 0.9} for s in hits],
                "low_confidence": len(hits) == 0, "count": len(hits)}

    def graph_neighborhood(self, entity, depth=1, **kw):
        nbrs = set()
        for (s, _r, d) in self.edges:
            if s == entity:
                nbrs.add(d)
            if d == entity:
                nbrs.add(s)
        nodes = [{"entity": entity, "facts": []}]
        nodes += [{"entity": n, "facts": []} for n in sorted(nbrs)]
        return {"found": True, "entity": entity, "depth": 1,
                "nodes": nodes, "edges": [], "paths": []}


def _two_hop_fake():
    # checkout-svc -> billing-lib -> jvm-21; terminal snippet shares NO token
    # with the question, so search alone can't reach jvm-21.
    snippets = ["checkout-svc depends-on billing-lib",
                "ZZZ runtime detail jvm-21 here"]
    edges = [("checkout-svc", "depends-on", "billing-lib"),
             ("billing-lib", "runs-on", "jvm-21")]
    return _FakeSvc(snippets, edges)


def test_graph_loop_reaches_two_hop_terminal():
    svc = _two_hop_fake()
    known = {"checkout-svc", "billing-lib", "jvm-21"}
    # seed entity comes from the question text
    st = mb.run_loop(svc, "what does checkout-svc run on",
                     mb.MechanicalController(), use_graph=True,
                     known_entities=known)
    assert "jvm-21" in st.entities
    assert any(mb.value_present(s, "jvm-21") for s in mb.assembled_context(st))


def test_search_only_loop_misses_unretrievable_terminal():
    svc = _two_hop_fake()
    known = {"checkout-svc", "billing-lib", "jvm-21"}
    st = mb.run_loop(svc, "what does checkout-svc run on",
                     mb.MechanicalController(), use_graph=False,
                     known_entities=known)
    assert "jvm-21" not in st.entities


def test_hop_cap_is_respected():
    svc = _two_hop_fake()
    known = {"checkout-svc", "billing-lib", "jvm-21"}
    st = mb.run_loop(svc, "what does checkout-svc run on",
                     mb.MechanicalController(), use_graph=True,
                     known_entities=known, hop_cap=1)
    assert st.iterations <= 1


def test_gold_recovered_checks_assembled_context():
    st = mb.LoopState(entities={"jvm-21"}, texts=["unrelated"])
    assert mb.gold_recovered(st, "jvm-21") is True
    assert mb.gold_recovered(st, "k8s-prod") is False


def test_tokens_read_sums_assembled_context():
    st = mb.LoopState(texts=["abcd" * 4], facts=["x=y"])  # 16 + 3 chars
    assert mb.tokens_read(st) == mb.approx_tokens("abcd" * 4) + mb.approx_tokens("x=y")


def test_would_gate_fires_on_thin_or_low_conf():
    assert mb.would_gate(mb.LoopState(low_confidence=True, top_score=0.9)) is True
    assert mb.would_gate(mb.LoopState(low_confidence=False, top_score=0.3)) is True
    assert mb.would_gate(mb.LoopState(low_confidence=False, top_score=0.8)) is False


def test_aggregate_buckets_by_hops_and_overall():
    recs = [
        {"hops": 1, "recovered": True, "iterations": 1, "tokens": 10, "latency_ms": 1.0},
        {"hops": 2, "recovered": False, "iterations": 2, "tokens": 20, "latency_ms": 2.0},
        {"hops": 2, "recovered": True, "iterations": 2, "tokens": 30, "latency_ms": 4.0},
    ]
    agg = mb.aggregate(recs)
    assert agg["overall"]["n"] == 3
    assert abs(agg["overall"]["recall"] - 2 / 3) < 1e-6
    assert agg["by_hops"][2]["n"] == 2
    assert abs(agg["by_hops"][2]["recall"] - 0.5) < 1e-6
    assert agg["by_hops"][2]["mean_tokens"] == 25.0
