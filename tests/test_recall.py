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
    # Deterministic: seed first (exempt), then the lowest (degree, name) non-seed.
    # x and y both have degree 1, so the (degree, name) tiebreak picks "x".
    assert out == ["seed", "x"]


def test_select_frontier_off_is_identity():
    frontier = ["c", "a", "b"]
    assert rc._select_frontier(frontier, set(), None, None, None) == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# graph-insight integration tests (require bench Postgres on 127.0.0.1:5433)
# ---------------------------------------------------------------------------

def _seed_two_communities(svc):
    # cluster 1: alpha-svc <-> alpha-db <-> alpha-cache (triangle)
    svc.graph_relate("alpha-svc", "depends-on", "alpha-db")
    svc.graph_relate("alpha-db", "depends-on", "alpha-cache")
    svc.graph_relate("alpha-svc", "depends-on", "alpha-cache")
    # cluster 2: beta-svc <-> beta-db
    svc.graph_relate("beta-svc", "depends-on", "beta-db")
    # bridge
    svc.graph_relate("alpha-cache", "relates-to", "beta-svc")


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_refresh_graph_insight_persists_and_is_stable(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    out = svc._refresh_graph_insight()  # noqa: SLF001
    assert out["refreshed"] is True and out["communities"] >= 2
    loaded = svc._storage.load_communities()  # noqa: SLF001
    assert len(loaded["assignment"]) >= 5            # entities stamped
    digest = svc.graph_digest()
    assert digest["available"] is True
    assert {"god_nodes", "surprises", "questions", "communities"} <= set(digest["digest"])
    # Stable ids: a second refresh with no graph change keeps the assignment.
    before = svc._storage.load_communities()["assignment"]  # noqa: SLF001
    svc._refresh_graph_insight()  # noqa: SLF001
    after = svc._storage.load_communities()["assignment"]  # noqa: SLF001
    assert before == after


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_graph_neighborhood_carries_community(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    out = svc.graph_neighborhood("alpha-svc", depth=1)
    node = next(n for n in out["nodes"] if n["entity"] == "alpha-svc")
    assert isinstance(node["community"], int)


def test_hub_threshold_percentile_and_floor():
    # All low-degree -> percentile lands at 1, floor wins.
    assert rc._hub_threshold([1, 1, 1, 1, 1], percentile=95.0, floor=4) == 4
    # A clear hub -> percentile (50) exceeds the floor and wins.
    assert rc._hub_threshold([1, 2, 3, 50], percentile=95.0, floor=2) == 50
    # Empty distribution -> floor.
    assert rc._hub_threshold([], percentile=95.0, floor=7) == 7


def test_recall_config_hub_defaults():
    from pseudolife_memory.utils.config import RecallConfig
    c = RecallConfig()
    assert c.hub_gate is True
    assert c.hub_percentile == 95.0
    assert c.hub_floor == 8
    assert c.expand_budget == 0


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_get_neighbors_relation_filter(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.graph_relate("gnx", "depends-on", "gny")
    svc.graph_relate("gnx", "runs-on", "gnz")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.get_neighbors("gnx", relation_filter="depends-on")
    rels = {e["relation"] for e in out["edges"]}
    assert rels == {"depends-on"}                 # runs-on filtered out


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_path_tool_delegates(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.graph_relate("mp-a", "depends-on", "mp-b")
    svc.graph_relate("mp-b", "depends-on", "mp-c")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_path("mp-a", "mp-c")
    assert out["path"] == ["mp-a", "mp-b", "mp-c"] and out["hops"] == 2


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


# ---------------------------------------------------------------------------
# Hub-gating integration tests (PG-backed)
# ---------------------------------------------------------------------------

def _seed_hub_graph(svc):
    # checkout -> billing -> jdk-21 (gold), plus a shared-config hub that many
    # heads depend on (degree 6), fanning out to unrelated services.
    svc.graph_relate("checkout-service", "depends-on", "billing-engine")
    svc.graph_relate("billing-engine", "runs-on", "jdk-21")
    svc.graph_relate("checkout-service", "depends-on", "shared-config")
    for head in ("order-service", "web-portal", "mobile-app",
                 "analytics-ui", "notify-service"):
        svc.graph_relate(head, "depends-on", "shared-config")


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_hub_gating_keeps_gold_drops_blast_radius(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_hub_graph(svc)
    svc.config.memory.recall.hub_gate = True
    svc.config.memory.recall.hub_floor = 3       # shared-config has degree 6
    out = svc.recall("What does checkout-service run on?", hops=3)
    names = {e["entity"] for e in out["entities"]}
    assert "jdk-21" in names                      # gold still reached
    assert "order-service" not in names           # hub not expanded through


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_no_gating_pulls_in_hub_siblings(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_hub_graph(svc)
    svc.config.memory.recall.hub_gate = False
    out = svc.recall("What does checkout-service run on?", hops=3)
    names = {e["entity"] for e in out["entities"]}
    assert "order-service" in names               # un-gated fan-out through hub


# ---------------------------------------------------------------------------
# MCP tool tests: memory_digest / memory_communities (Task 7)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_digest_tool(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_digest()
    assert out["available"] is True and "god_nodes" in out["digest"]


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_communities_tool(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    monkeypatch.setattr(srv, "service", svc, raising=False)
    listing = srv.memory_communities()
    assert listing["communities"]
    members = srv.memory_communities(community_id=listing["communities"][0]["id"])
    assert "members" in members


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_dream_run_refreshes_digest_with_no_backlog(tmp_path):
    # A dream with no memory backlog must still recompute the graph digest, so
    # manual graph edits (cleanup / direct graph_relate) are reflected promptly.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.memory.dream import NoOpExtractor
    svc = build_service(tmp_path)
    _seed_two_communities(svc)            # graph edges only — no stored memories
    out = svc.dream_run(NoOpExtractor())
    assert out["pulled"] == 0             # exercised the no-backlog path
    assert out["graph_insight"]["refreshed"] is True
    assert svc._storage.load_communities()["assignment"]  # communities persisted


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_dream_writes_fact_traces(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.memory.dream import RegexExtractor
    svc = build_service(tmp_path)
    # A memory whose text the regex floor extracts a fact from (lexicon-gated
    # attribute — "runtime"; verify RegexExtractor yields a claim for it).
    svc.store("trace-svc runtime: jdk-21", source="general")
    out = svc.dream_run(RegexExtractor())
    assert out["pulled"] >= 1
    assert out.get("traces", 0) >= 1
    st = svc._storage  # noqa: SLF001
    # The entry that produced the fact is now reinforced + linked.
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert st.facts_for_entry(eid)                      # entry -> fact(s)
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0] >= 1
    # Durability: the trace survives a SUBSEQUENT cortex write (snapshot rewrite).
    svc.cortex_write("unrelated-x", "kind", "probe", support="user")
    assert st.facts_for_entry(eid)


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_get_and_reinforce_roundtrip(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.memory.dream import RegexExtractor
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.store("getme-svc runtime: jdk-22", source="general")
    svc.dream_run(RegexExtractor())
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    monkeypatch.setattr(srv, "service", svc, raising=False)
    got = srv.memory_get(eid)
    assert got["found"] is True and "getme-svc" in got["text"]
    assert got["consolidated_into"]                      # entry -> facts
    # source_entries surfaces on a fact read (the fact advertises its episodes).
    facts = srv.memory_facts()["entries"]
    assert any(eid in (f.get("source_entries") or []) for f in facts)
    before = st.conn.execute("SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0]
    assert srv.memory_reinforce(eid)["reinforced"] is True
    after = st.conn.execute("SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0]
    assert after == before + 1
    assert srv.memory_get(9_000_001) == {"found": False, "faded": True}


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_reinforcements_loads_into_entry(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.storage.sync import row_to_entry
    svc = build_service(tmp_path)
    svc.store("retain-me runtime: jdk-21", source="general")
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    st.bump_reinforcements(eid, 3)
    row = next(r for r in st.load_entries() if r["id"] == eid)
    assert row["reinforcements"] == 3
    assert row_to_entry(row).reinforcements == 3


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_reinforce_syncs_in_memory(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.store("sync-me runtime: jdk-21", source="general")
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]

    def resident():
        for b in svc._cms.bands:                      # noqa: SLF001
            for e in b.entries:
                if e.db_id == eid:
                    return e
        return None

    r = resident()
    assert r is not None and r.reinforcements == 0
    out = svc.reinforce(eid)
    assert out["reinforced"] is True
    assert resident().reinforcements == 1            # in-memory synced
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0] == 1
