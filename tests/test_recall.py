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


def test_mechanical_seeds_from_query_and_hits():
    c = rc.MechanicalController()
    seeds = c.seed_entities("what does alpha run on", ["alpha depends-on beta"],
                            ["alpha", "beta", "gamma"])
    assert seeds == ["alpha", "beta"]  # both present in query+hits, vocab order


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
