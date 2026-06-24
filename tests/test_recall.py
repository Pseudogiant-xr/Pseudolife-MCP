import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory.memory import recall as rc  # noqa: E402


class _FakeSvc:
    """Weak search (returns only snippets sharing a query token) + a structural
    graph, so multi-hop terminals are reachable ONLY via graph traversal."""

    def __init__(self, snippets, edges):
        self.snippets = snippets
        self.edges = edges  # list[(src, rel, dst)]

    def search(self, query, top_k=5):
        import re
        toks = set(re.findall(r"[\w-]+", query.lower()))
        hits = [s for s in self.snippets
                if toks & set(re.findall(r"[\w-]+", s.lower()))][:top_k]
        return {"entries": [{"text": s} for s in hits]}

    def graph(self, entity, depth=1):
        nbrs = set()
        for (s, _r, d) in self.edges:
            if s == entity:
                nbrs.add(d)
            if d == entity:
                nbrs.add(s)
        nodes = [{"entity": entity, "facts": [{"attribute": "t", "value": entity}]}]
        nodes += [{"entity": n, "facts": []} for n in sorted(nbrs)]
        edges = [{"src": s, "relation": r, "dst": d, "derived": False}
                 for (s, r, d) in self.edges if s == entity or d == entity]
        return {"found": True, "nodes": nodes, "edges": edges, "paths": []}


def _two_hop():
    snippets = ["alpha depends-on beta", "ZZZ runtime note gamma here"]
    edges = [("alpha", "depends-on", "beta"), ("beta", "runs-on", "gamma")]
    return _FakeSvc(snippets, edges)


def test_mechanical_seeds_query_first_subject_only():
    # query names only "alpha"; the hit also mentions "beta", but query-first
    # must seed ONLY the query subject (beta is reached later via the graph).
    c = rc.MechanicalController()
    seeds = c.seed_entities("what does alpha run on", ["alpha depends-on beta"],
                            ["alpha", "beta", "gamma"])
    assert seeds == ["alpha"]


def test_mechanical_seeds_fall_back_to_hits_when_query_bare():
    # query names no known entity -> fall back to hit-derived matches.
    c = rc.MechanicalController()
    seeds = c.seed_entities("what does it run on?", ["alpha depends-on beta"],
                            ["alpha", "beta", "gamma"])
    assert seeds == ["alpha", "beta"]


def test_run_recall_reaches_two_hop_terminal():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController())
    assert "gamma" in st.entities
    assert any(e["dst"] == "gamma" for e in st.edges)
    assert st.low_confidence is False


def test_run_recall_low_confidence_when_no_seed():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "totally unrelated question", rc.MechanicalController())
    assert st.low_confidence is True
    assert st.seeds == []


def test_run_recall_respects_hops_cap():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController(), hops=1)
    assert st.iterations <= 1


def test_run_recall_respects_max_entities():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController(),
                       max_entities=1)
    assert len(st.entities) <= 1


def test_llm_controller_seeds_from_completion_filtered_to_vocab():
    calls = {}

    def fake_complete(prompt):
        calls["prompt"] = prompt
        return '["alpha", "not-in-vocab"]'

    c = rc.LLMController(fake_complete)
    seeds = c.seed_entities("which thing runs alpha",
                            ["alpha depends-on beta"], ["alpha", "beta", "gamma"])
    assert seeds == ["alpha"]            # not-in-vocab dropped
    assert "alpha" in calls["prompt"]    # vocab/query passed to the model


def test_llm_controller_next_queries_match_mechanical():
    c = rc.LLMController(lambda p: "[]")
    assert c.next_queries("q", ["beta"]) == ["q beta"]


def test_parse_name_list_tolerates_noise():
    assert rc._parse_name_list('junk ["a", "b"] trailing') == ["a", "b"]
    assert rc._parse_name_list("not json at all") == []


# ---------------------------------------------------------------------------
# PG-backed integration tests (require bench Postgres on 127.0.0.1:5433)
# ---------------------------------------------------------------------------

_ADMIN = os.environ.get(
    "PSEUDOLIFE_BENCH_ADMIN_URL",
    "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres",
)


def _pg_up() -> bool:
    try:
        import psycopg
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_bridges_two_hop_on_real_service(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service  # reuse isolated bench DB
    svc = build_service(tmp_path)
    svc.store("checkout-svc depends on the billing-lib package.", source="bench")
    svc.store("billing-lib is compiled against the jdk-21 toolchain.", source="bench")
    assert not svc.graph_relate("checkout-svc", "depends-on", "billing-lib").get("error")
    assert not svc.graph_relate("billing-lib", "runs-on", "jdk-21").get("error")

    out = svc.recall("what does checkout-svc run on?")
    assert out["low_confidence"] is False
    assert "checkout-svc" in out["seeds"]
    visited = {n["entity"] for n in out["entities"]}
    assert "jdk-21" in visited                       # bridged 2 hops via graph
    assert any(e["dst"] == "jdk-21" for e in out["edges"])


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_low_confidence_when_query_names_no_entity(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.store("checkout-svc depends on the billing-lib package.", source="bench")
    svc.graph_relate("checkout-svc", "depends-on", "billing-lib")
    out = svc.recall("what is the airspeed velocity of an unladen swallow?")
    assert out["low_confidence"] is True
    assert out["entities"] == []


# ---------------------------------------------------------------------------
# Hub-gating tests (pure orchestration, no DB)
# ---------------------------------------------------------------------------

def _hub_svc():
    # S-B, S-H, B-T(gold); H is a degree-5 hub fanning to X1..X4.
    edges = [("S", "r", "B"), ("S", "r", "H"), ("B", "r", "T"),
             ("H", "r", "X1"), ("H", "r", "X2"), ("H", "r", "X3"), ("H", "r", "X4")]
    snippets = ["S relates to B and H"]
    return _FakeSvc(snippets, edges)


_HUB_DEGREE = {"S": 2, "B": 2, "H": 5, "T": 1,
               "X1": 1, "X2": 1, "X3": 1, "X4": 1}


def test_hub_included_but_not_expanded():
    svc = _hub_svc()
    state = rc.run_recall(
        svc.search, svc.graph, vocab=["S", "B", "H", "T", "X1", "X2", "X3", "X4"],
        query="about S", controller=rc.MechanicalController(),
        hops=3, degree_fn=_HUB_DEGREE.get, hub_threshold=4, expand_budget=None)
    ents = set(state.entities)
    assert "H" in ents          # hub still surfaced as a result
    assert "T" in ents          # gold still reached via the non-hub branch
    assert "X1" not in ents     # hub NOT expanded through — no blast radius


def test_no_gating_pulls_in_hub_neighbors():
    svc = _hub_svc()
    state = rc.run_recall(
        svc.search, svc.graph, vocab=["S", "B", "H", "T", "X1", "X2", "X3", "X4"],
        query="about S", controller=rc.MechanicalController(), hops=3)  # degree_fn=None
    assert "X1" in set(state.entities)  # un-gated expansion fans out through H


def test_seed_that_is_a_hub_still_expands():
    # Seed S is itself a degree-5 hub; seed exemption must let it expand to T.
    edges = [("S", "r", "T"), ("S", "r", "A"), ("S", "r", "B"),
             ("S", "r", "C"), ("S", "r", "D")]
    svc = _FakeSvc(["S relates to things"], edges)
    deg = {"S": 5, "T": 1, "A": 1, "B": 1, "C": 1, "D": 1}
    state = rc.run_recall(
        svc.search, svc.graph, vocab=["S", "T", "A", "B", "C", "D"],
        query="about S", controller=rc.MechanicalController(),
        hops=2, degree_fn=deg.get, hub_threshold=3)
    assert "T" in set(state.entities)


def test_select_frontier_orders_and_budgets():
    frontier = ["c", "a", "b"]               # none are seeds
    deg = {"a": 5, "b": 1, "c": 3}
    out = rc._select_frontier(frontier, set(), deg.get, hub_threshold=100,
                              expand_budget=2)
    assert out == ["b", "c"]                  # ascending degree, capped at 2


def test_select_frontier_seeds_exempt_from_gate_and_budget():
    frontier = ["seed", "x", "y"]
    deg = {"seed": 99, "x": 1, "y": 1}
    out = rc._select_frontier(frontier, {"seed"}, deg.get, hub_threshold=10,
                              expand_budget=1)
    assert out[0] == "seed"                   # seed always present, never gated
    assert set(out) == {"seed", "x"} or set(out) == {"seed", "y"}
    assert len(out) == 2                       # seed + 1 budgeted non-seed


def test_select_frontier_off_is_identity():
    frontier = ["c", "a", "b"]
    assert rc._select_frontier(frontier, set(), None, None, None) == ["c", "a", "b"]


def test_hub_threshold_percentile_and_floor():
    # All low-degree -> percentile lands at 1, floor wins.
    assert rc._hub_threshold([1, 1, 1, 1, 1], percentile=95.0, floor=4) == 4
    # A clear hub -> percentile (50) exceeds the floor and wins.
    assert rc._hub_threshold([1, 2, 3, 50], percentile=95.0, floor=2) == 50
    # Empty distribution -> floor.
    assert rc._hub_threshold([], percentile=95.0, floor=7) == 7


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_recall_tool_delegates(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.store("web-portal uses the gateway-proxy for calls.", source="bench")
    svc.store("the gateway-proxy is deployed on the edge-cluster.", source="bench")
    svc.graph_relate("web-portal", "uses", "gateway-proxy")
    svc.graph_relate("gateway-proxy", "runs-on", "edge-cluster")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_recall("what does web-portal run on?")
    assert "edge-cluster" in {n["entity"] for n in out["entities"]}
