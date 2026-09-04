import json
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


# ---------------------------------------------------------------------------
# Per-hop provenance (issue #186): the MCP layer's output caps need to know
# WHICH hop discovered each entity/edge, and how many texts came from the
# flat seed search vs. hop-driven re-queries, to avoid a flat prefix slice
# silently dropping deep hops or hop-discovered text (2026-08-25 review).
# ---------------------------------------------------------------------------

def test_run_recall_tags_entity_and_edge_hops():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController())
    assert st.entity_hop["alpha"] == 0     # seed
    assert st.entity_hop["beta"] == 1      # first hop
    assert st.entity_hop["gamma"] == 2     # bridged via the second hop
    assert len(st.edge_hop) == len(st.edges)   # parallel arrays, same length
    assert st.edge_hop == [1, 2]           # alpha-beta then beta-gamma


def test_run_recall_seed_text_count_bounds_texts():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController())
    assert 0 <= st.seed_text_count <= len(st.texts)


def test_recall_state_to_dict_carries_hop_metadata():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController())
    d = rc.recall_state_to_dict(st, "what does alpha run on", 3)
    assert d["entity_hop"] == st.entity_hop
    assert d["edge_hop"] == st.edge_hop
    assert d["seed_text_count"] == st.seed_text_count
    # And the pre-existing shape is unchanged.
    assert d["entities"] == [{"entity": n, "facts": st.entity_facts.get(n, [])}
                             for n in st.entities]


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
def test_memory_graph_relation_filter(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.graph_relate("gnx", "depends-on", "gny")
    svc.graph_relate("gnx", "runs-on", "gnz")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_graph("gnx", relation_filter="depends-on")
    rels = {e["relation"] for e in out["edges"]}
    assert rels == {"depends-on"}                 # runs-on filtered out


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_graph_path_service_shortest_path(tmp_path):
    # The memory_path MCP tool was folded into memory_graph(to=...); the
    # Console still reaches this via /api/graph/path -> service.graph_path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.graph_relate("mp-a", "depends-on", "mp-b")
    svc.graph_relate("mp-b", "depends-on", "mp-c")
    out = svc.graph_path("mp-a", "mp-c")
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
# Recall output-cap tests (issue #186 — 2026-08-21 audit: a plain 3-hop
# query returned an uncapped 93.7 KB; see mcp_server._RECALL_MAX_* comment).
# These exercise the REAL service.recall() -> mcp_server.memory_recall()
# pipeline end to end (real hop tagging, real embedding search) against a
# modest fixture; tests/test_mcp_server.py's
# test_memory_recall_caps_preserve_deep_hops_and_bound_size is the fast,
# PG-free unit test that specifically reproduces the wide-hub crowd-out
# bug the 2026-08-25 review of this fix caught (a flat prefix slice
# entirely dropping hop-2 behind a hub's own 1-hop ring).
# ---------------------------------------------------------------------------

def _seed_recall_cap_fixture(svc, base_query: str) -> None:
    """A wide 2-level tree under one seed entity: 1 + 5 + 15 = 21 entities,
    5 + 15 = 20 edges — past the caps under test (10/15/6) with margin, and
    deliberately no bigger than that: this is a real (if CPU-slow) dense
    embedding search, not a stub, so every extra store/graph_relate call
    costs real wall-clock time. Every stored entry repeats ``base_query``
    verbatim plus long filler text (longer than the truncation cap), so
    real dense search reliably surfaces well over the text cap too across
    the many hop-driven re-queries — each newly discovered entity
    re-triggers a search for "<base_query> <name>", and every entry shares
    enough of that phrasing to be a plausible hit.

    Uses the ``uses`` relation, NOT ``depends-on``: ``depends-on`` is a
    TRANSITIVE builtin (storage/postgres.py's _BUILTIN_RELATIONS), so
    chaining it root->L1->L2 derives a root->L2 edge and collapses the
    intended 2-level tree into a single hop from graph_neighborhood's
    point of view (found via a real hop2_raw assertion going unexpectedly
    empty against this fixture, 2026-08-25) — the fixture would then never
    exercise the deep-hop preservation this test exists to check.
    """
    filler = (
        "This sentence exists purely to pad the stored memory past the "
        "recall preview-length cap so the truncation path is exercised. "
    )
    root = "root-svc"
    l1 = [f"svc-l1-{i}" for i in range(5)]
    svc.store(f"{base_query} -- entity {root} is the fixture root. {filler * 2}",
              source="bench")
    for name in l1:
        svc.graph_relate(root, "uses", name)
        svc.store(f"{base_query} -- entity {name} is a level-1 fixture node. {filler * 2}",
                  source="bench")
    for parent in l1:
        for j in range(3):
            name = f"{parent}-{j}"
            svc.graph_relate(parent, "uses", name)
            svc.store(f"{base_query} -- entity {name} is a level-2 fixture leaf. {filler * 2}",
                      source="bench")




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
def test_graph_digest_service(tmp_path):
    # digest left the MCP surface (Console-only via /api/graph/digest).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    out = svc.graph_digest()
    assert out["available"] is True and "god_nodes" in out["digest"]


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_communities_service(tmp_path):
    # communities left the MCP surface (Console-only via /api/graph/communities).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    listing = svc.communities()
    assert listing["communities"]
    members = svc.communities(community_id=listing["communities"][0]["id"])
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
    # consolidated_into exposes the stable slot handle, NOT the ephemeral facts.id.
    for f in got["consolidated_into"]:
        assert set(f) == {"entity", "attribute", "value"}
        assert "id" not in f
    # source_entries surfaces on a fact read (the fact advertises its episodes).
    facts = svc.cortex_dump()["entries"]
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
def test_memory_get_surfaces_reinforcements(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.store("retainshow runtime: jdk-21", source="general")
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    st.bump_reinforcements(eid, 4)
    got = svc.get_entry(eid)
    assert got["reinforcements"] == 4
    assert "access_count" in got


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_get_syncs_access_count_no_clobber(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.store("accesssync runtime: jdk-21", source="general")
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]

    def resident():
        for b in svc._cms.bands:                       # noqa: SLF001
            for e in b.entries:
                if e.db_id == eid:
                    return e
        return None

    mem_before = resident().access_count
    db_before = st.conn.execute("SELECT access_count FROM entries WHERE id=%s", (eid,)).fetchone()[0]
    svc.get_entry(eid)
    assert resident().access_count == mem_before + 1     # in-memory bumped (the clobber fix)
    assert st.conn.execute("SELECT access_count FROM entries WHERE id=%s", (eid,)).fetchone()[0] == db_before + 1
    # Regression: a save-cadence sync (in-memory -> DB) must NOT clobber the bump back to db_before.
    st.update_access_counts([(eid, resident().access_count)])
    assert st.conn.execute("SELECT access_count FROM entries WHERE id=%s", (eid,)).fetchone()[0] == db_before + 1


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

@pytest.fixture(scope="module")
def recall_cap_service(tmp_path_factory):
    """The cap fixture built ONCE for the two payload-cap tests below.

    Each build was a `build_service` (reset_bench + a real embedder) plus 21
    stores and 20 `graph_relate` calls through real dense search — the most
    expensive setup in this file, and both tests need the identical corpus.
    Neither consumer writes to the service: the first only calls
    `svc.recall`, the second only monkeypatches `srv.service` (undone per
    test), so one shared build is equivalent to two.

    This block sits LAST in the module, structurally: anything that calls
    `build_service` runs evals' `reset_bench`, which reaps every backend
    on the bench database and would terminate THIS service's connection
    mid-module. With every other `build_service` user strictly before the
    fixture is first built, no collection order can interleave one. Keep it
    last; do not insert tests between or after the two consumers.
    """
    if not _pg_up():
        pytest.skip("bench Postgres not reachable")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path_factory.mktemp("recall-caps"))
    base_query = "what does root-svc connect to"
    _seed_recall_cap_fixture(svc, base_query)
    return svc, base_query


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_recall_caps_payload_non_verbose(monkeypatch,
                                                recall_cap_service):
    import pseudolife_memory.mcp_server as srv
    svc, base_query = recall_cap_service

    # Sanity: the fixture really is big enough that the UNCAPPED service
    # layer blows past every cap under test — otherwise the caps below
    # would pass vacuously.
    raw = svc.recall(base_query, hops=3, top_k=5)
    assert len(raw["entities"]) > srv._RECALL_MAX_ENTITIES
    assert len(raw["edges"]) > srv._RECALL_MAX_EDGES
    assert len(raw["texts"]) > srv._RECALL_MAX_TEXTS
    # The fixture's leaves are all hop 2 (root=hop0, svc-l1-*=hop1,
    # svc-l1-*-*=hop2) — real service.recall() must tag them that way for
    # the per-hop quota to have anything to preserve.
    hop2_raw = {n for n, h in raw["entity_hop"].items() if h == 2}
    assert hop2_raw, "fixture produced no hop-2 entities to preserve"

    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_recall(base_query, hops=3, top_k=5)
    assert len(out["entities"]) <= srv._RECALL_MAX_ENTITIES
    assert len(out["edges"]) <= srv._RECALL_MAX_EDGES
    assert len(out["texts"]) <= srv._RECALL_MAX_TEXTS
    # Per-hop quota: the deep (hop-2) bridge must survive the cap, not just
    # the seed and its immediate ring (issue #186 review finding 1).
    kept = {e["entity"] for e in out["entities"]}
    assert kept & hop2_raw, "hop-2 entities dropped by the entity cap"
    # Compact text projection: each supporting text is truncated to the
    # preview cap (+ 1 for the trailing ellipsis character).
    assert all(len(t) <= srv._RECALL_TEXT_CHARS + 1 for t in out["texts"])
    assert any(t.endswith("…") for t in out["texts"])  # cap actually bound
    # Serialized-size regression guard (issue #186 finding 3): this
    # fixture's uncapped response runs several KB (21 entities, 20 edges,
    # 20+ full-length texts); assert the capped compact payload stays a
    # small fraction of that rather than drifting back toward it.
    assert len(json.dumps(out)) < len(json.dumps(raw)) // 2


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_recall_verbose_keeps_full_texts(monkeypatch,
                                                recall_cap_service):
    # verbose=True is the escape hatch: entity/edge counts still cap the
    # payload, but supporting text content must NOT be truncated.
    # Shares the module-scoped corpus with the test directly above — keep the
    # two adjacent (see the fixture's docstring).
    import pseudolife_memory.mcp_server as srv
    svc, base_query = recall_cap_service
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_recall(base_query, hops=3, top_k=5, verbose=True)
    assert len(out["entities"]) <= srv._RECALL_MAX_ENTITIES
    assert len(out["edges"]) <= srv._RECALL_MAX_EDGES
    assert len(out["texts"]) <= srv._RECALL_MAX_TEXTS
    assert any(len(t) > srv._RECALL_TEXT_CHARS for t in out["texts"])
    # Facts per entity are capped even in verbose mode (issue #186 finding 3).
    assert all(len(e.get("facts", [])) <= srv._RECALL_MAX_FACTS_PER_ENTITY
              for e in out["entities"])
    assert not any(t.endswith("…") for t in out["texts"])



# ---------------------------------------------------------------------------
# Search fan-out caps (2026-09-04). A 3-hop memory_recall on a restored
# copy of the live bank issued a mean of 89.15 service.search calls (max
# 205) and took 25.25 s per call — one seed search plus one re-query per
# newly discovered entity per hop, on a star-shaped graph (5,504 entities,
# degree p50 1 / p95 5 / max 132), so one hub's ring priced the call
# (evals/results/recall-fanout-cap-20260904.json). These pin the three
# caps and the fail-soft budget; the byte-identity test below pins that a
# walk which stays under them is unchanged.
# ---------------------------------------------------------------------------

class _CountingSvc:
    """``_FakeSvc`` plus a record of every search query the walk issued."""

    def __init__(self, inner, delay: float = 0.0):
        self.inner = inner
        self.delay = delay
        self.queries: list[str] = []

    def search(self, query, top_k=5):
        self.queries.append(query)
        if self.delay:
            import time
            time.sleep(self.delay)
        return self.inner.search(query, top_k)

    def graph(self, entity, depth=1):
        return self.inner.graph(entity, depth)


def _star(n: int = 10, relation: str = "depends-on"):
    """One seed fanning out to ``n`` spokes — the live graph's shape."""
    edges = [("hub", relation, f"spoke{i:02d}") for i in range(n)]
    return _FakeSvc(["hub relates to many things"], edges)


def _star_vocab(n: int = 10) -> list[str]:
    return ["hub"] + [f"spoke{i:02d}" for i in range(n)]


def _layers(width: int = 8, depth: int = 5, relation: str = "depends-on"):
    """A graph wide at EVERY hop: ``depth`` layers of ``width`` nodes, with
    node j of layer k linked to node j of layer k+1, so each hop discovers
    exactly ``width`` new entities and each hop spends its full per-hop
    re-query allowance. ``_star`` is only wide at hop 1."""
    edges = [("hub", relation, f"L1_{j:02d}") for j in range(width)]
    for k in range(1, depth):
        edges += [(f"L{k}_{j:02d}", relation, f"L{k + 1}_{j:02d}")
                  for j in range(width)]
    return _FakeSvc(["hub relates to many things"], edges)


def _layers_vocab(width: int = 8, depth: int = 5) -> list[str]:
    return ["hub"] + [f"L{k}_{j:02d}"
                      for k in range(1, depth + 1) for j in range(width)]


def test_max_searches_per_hop_caps_requeries_but_keeps_entities():
    svc = _CountingSvc(_star(10))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(10), "about hub",
                       rc.MechanicalController(), hops=1,
                       max_searches_per_hop=3)
    # 1 seed search + at most 3 re-queries.
    assert len(svc.queries) == 4
    assert st.searches_issued == 4
    # Every spoke is still a RESULT with its facts — only the re-query is cut.
    assert len([e for e in st.entities if e.startswith("spoke")]) == 10
    assert st.entity_facts["spoke00"] == []


def test_uncapped_walk_issues_one_search_per_new_entity():
    """The behavior being bounded: without the cap, a 10-spoke hub costs
    11 searches for one hop."""
    svc = _CountingSvc(_star(10))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(10), "about hub",
                       rc.MechanicalController(), hops=1)
    assert len(svc.queries) == 11
    assert st.searches_issued == 11
    assert st.truncated is False


def test_requery_ranking_prefers_seed_mentions_then_low_degree():
    # Seed hit names spoke07 only; degrees make spoke01 the cheapest of the
    # unmentioned ones. Cap 2 must pick the mentioned one, then the lowest
    # degree — never the hub-ward spoke09.
    edges = [("hub", "depends-on", f"spoke{i:02d}") for i in (1, 7, 9)]
    inner = _FakeSvc(["hub connects to spoke07 and others"], edges)
    svc = _CountingSvc(inner)
    degrees = {"hub": 3, "spoke01": 1, "spoke07": 4, "spoke09": 20}
    st = rc.run_recall(svc.search, svc.graph,
                       ["hub", "spoke01", "spoke07", "spoke09"],
                       "about hub", rc.MechanicalController(), hops=1,
                       degree_fn=degrees.get, hub_threshold=None,
                       max_searches_per_hop=2)
    assert svc.queries[1:] == ["about hub spoke07", "about hub spoke01"]
    assert st.searches_issued == 3


def test_max_total_searches_stops_the_walk_and_flags_truncated():
    svc = _CountingSvc(_star(10))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(10), "about hub",
                       rc.MechanicalController(), hops=3,
                       max_total_searches=5)
    assert len(svc.queries) == 5          # seed + 4 re-queries, then stop
    assert st.searches_issued == 5
    assert st.truncated is True


def test_total_ceiling_counts_the_seed_search():
    svc = _CountingSvc(_star(10))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(10), "about hub",
                       rc.MechanicalController(), hops=3,
                       max_total_searches=1)
    assert len(svc.queries) == 1          # the seed search alone exhausts it
    assert st.truncated is True
    assert "hub" in st.entities           # and it still returns what it has


def test_time_budget_returns_partial_instead_of_running_on():
    svc = _CountingSvc(_star(20), delay=0.02)
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(20), "about hub",
                       rc.MechanicalController(), hops=3,
                       time_budget_seconds=0.05)
    assert st.truncated is True
    assert len(svc.queries) < 21          # did not run the full fan-out
    assert st.seeds == ["hub"]            # fail-soft: returns what it has


def test_skip_part_of_expansion_keeps_facts_but_does_not_requery():
    edges = [("hub", "part-of", "container"), ("hub", "depends-on", "dep")]
    svc = _CountingSvc(_FakeSvc(["hub relates to things"], edges))
    st = rc.run_recall(svc.search, svc.graph, ["hub", "container", "dep"],
                       "about hub", rc.MechanicalController(), hops=1,
                       skip_part_of_expansion=True)
    assert svc.queries == ["about hub", "about hub dep"]
    assert "container" in st.entities     # still a result, with its facts
    assert "container" in st.entity_facts


def test_skip_part_of_expansion_off_requeries_everything():
    edges = [("hub", "part-of", "container"), ("hub", "depends-on", "dep")]
    svc = _CountingSvc(_FakeSvc(["hub relates to things"], edges))
    rc.run_recall(svc.search, svc.graph, ["hub", "container", "dep"],
                  "about hub", rc.MechanicalController(), hops=1)
    assert svc.queries == ["about hub", "about hub container", "about hub dep"]


# The pre-change response, captured verbatim from this repo at 7595ce6f
# (the commit this branch forks from) for the ``_two_hop`` fixture, whose
# walk issues 3 searches — under every default cap. Any drift in the
# capped walk's output on an uncapped-sized neighborhood shows up here as
# an inequality, not as a subtle ranking change nobody notices.
_TWO_HOP_PRE_CHANGE_RESPONSE = {
    "query": "what does alpha run on",
    "seeds": ["alpha"],
    "entities": [
        {"entity": "alpha", "facts": [{"attribute": "t", "value": "alpha"}]},
        {"entity": "beta", "facts": []},
        {"entity": "gamma", "facts": []},
    ],
    "edges": [
        {"src": "alpha", "relation": "depends-on", "dst": "beta",
         "derived": False},
        {"src": "beta", "relation": "runs-on", "dst": "gamma",
         "derived": False},
    ],
    "paths": [],
    "texts": ["alpha depends-on beta", "ZZZ runtime note gamma here"],
    "iterations": 3,
    "hops": 3,
    "low_confidence": False,
    "entity_hop": {"alpha": 0, "beta": 1, "gamma": 2},
    "edge_hop": [1, 2],
    "seed_text_count": 1,
}


def test_response_is_byte_identical_when_the_walk_stays_under_the_caps():
    from pseudolife_memory.utils.config import RecallConfig
    cfg = RecallConfig()
    svc = _CountingSvc(_two_hop())
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController(),
                       max_searches_per_hop=cfg.max_searches_per_hop,
                       max_total_searches=cfg.max_total_searches,
                       time_budget_seconds=cfg.time_budget_seconds,
                       skip_part_of_expansion=cfg.skip_part_of_expansion)
    assert st.searches_issued < cfg.max_total_searches   # caps did not bind
    assert st.truncated is False
    out = rc.recall_state_to_dict(st, "what does alpha run on", 3)
    assert out == _TWO_HOP_PRE_CHANGE_RESPONSE
    # ... and the two new fields are served only when they mean something.
    assert "truncated" not in out and "searches_issued" not in out


def test_truncated_response_carries_the_two_new_fields():
    svc = _CountingSvc(_star(10))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(10), "about hub",
                       rc.MechanicalController(), hops=3,
                       max_total_searches=4)
    out = rc.recall_state_to_dict(st, "about hub", 3)
    assert out["truncated"] is True
    assert out["searches_issued"] == 4


def test_recall_config_fanout_cap_defaults():
    from pseudolife_memory.utils.config import RecallConfig
    c = RecallConfig()
    assert c.max_searches_per_hop == 6
    assert c.max_total_searches == 31
    assert c.time_budget_seconds == 20.0
    assert c.skip_part_of_expansion is False


def test_default_ceiling_is_a_backstop_at_the_max_advertised_hops():
    """``memory_recall`` clamps ``hops`` to 1..5, so a ceiling that calls
    itself a backstop has to sit above what the per-hop cap can spend at
    5 hops: 1 + 6 x 5 = 31. At the first-cut default of 20 a 5-hop request
    silently ran four hops and came back flagged ``truncated`` — a working
    limit dressed as a backstop (2026-09-04 review finding 1). This walks
    the widest shape the defaults allow (8 new entities per hop, every
    hop) and pins that nothing is cut."""
    from pseudolife_memory.utils.config import RecallConfig
    cfg = RecallConfig()
    svc = _CountingSvc(_layers())
    st = rc.run_recall(svc.search, svc.graph, _layers_vocab(), "about hub",
                       rc.MechanicalController(), hops=5,
                       max_searches_per_hop=cfg.max_searches_per_hop,
                       max_total_searches=cfg.max_total_searches,
                       time_budget_seconds=cfg.time_budget_seconds)
    # Every hop ran, and each spent its full per-hop allowance.
    assert st.iterations == 5
    assert st.searches_issued == 1 + cfg.max_searches_per_hop * 5
    assert st.truncated is False
    assert "L5_00" in st.entities          # the deepest layer was reached


def test_fanout_caps_are_exposed_in_the_console_config_registry():
    from pseudolife_memory.web.config_io import KNOBS
    paths = {row["path"]: row for row in KNOBS}
    for name, default in (("max_searches_per_hop", 6),
                          ("max_total_searches", 31),
                          ("time_budget_seconds", 20.0),
                          ("skip_part_of_expansion", False)):
        row = paths[f"memory.recall.{name}"]
        assert row["default"] == default
        assert row["group"] == "Recall"


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_service_recall_passes_the_caps_through(tmp_path, monkeypatch):
    """The knobs are useless if ``service.recall`` doesn't hand them to the
    walk — the seam every other recall config item is wired through."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.config.memory.recall.max_searches_per_hop = 2
    svc.config.memory.recall.max_total_searches = 7
    svc.config.memory.recall.time_budget_seconds = 3.5
    svc.config.memory.recall.skip_part_of_expansion = True
    seen = {}

    def spy(*a, **kw):
        seen.update(kw)
        return rc.RecallState()

    monkeypatch.setattr(rc, "run_recall", spy)
    svc.recall("anything at all")
    assert seen["max_searches_per_hop"] == 2
    assert seen["max_total_searches"] == 7
    assert seen["time_budget_seconds"] == 3.5
    assert seen["skip_part_of_expansion"] is True


# ---------------------------------------------------------------------------
# Review findings, 2026-09-04 (independent pass over this branch's diff).
# ---------------------------------------------------------------------------

class _InducedSvc(_CountingSvc):
    """Like ``_CountingSvc`` but its ``graph`` returns the INDUCED subgraph,
    the way the real ``graph_neighborhood`` does (``graph.py``'s
    ``build_subgraph`` keeps every edge whose endpoints are both inside the
    neighborhood, not just the ones incident to the root). ``_FakeSvc``
    returns only incident edges, which hid a bug in the first cut of
    ``skip_part_of_expansion``: a neighbour-to-neighbour edge made a
    containment-only arrival look like a domain one."""

    def graph(self, entity, depth=1):
        nbrs = {entity}
        for (s, _r, d) in self.inner.edges:
            if s == entity:
                nbrs.add(d)
            if d == entity:
                nbrs.add(s)
        nodes = [{"entity": entity,
                  "facts": [{"attribute": "t", "value": entity}]}]
        nodes += [{"entity": n, "facts": []}
                  for n in sorted(nbrs - {entity})]
        edges = [{"src": s, "relation": r, "dst": d, "derived": False}
                 for (s, r, d) in self.inner.edges
                 if s in nbrs and d in nbrs]
        return {"found": True, "nodes": nodes, "edges": edges, "paths": []}


def test_skip_part_of_ignores_neighbour_to_neighbour_edges():
    # container is reached from the seed ONLY by part-of, but it also sits
    # on a container->dep edge that the induced subgraph returns. Only the
    # edges incident to the node being EXPANDED say how a neighbour
    # arrived, so container must still be skipped.
    edges = [("hub", "part-of", "container"),
             ("hub", "depends-on", "dep"),
             ("container", "relates-to", "dep")]
    svc = _InducedSvc(_FakeSvc(["hub relates to things"], edges))
    st = rc.run_recall(svc.search, svc.graph, ["hub", "container", "dep"],
                       "about hub", rc.MechanicalController(), hops=1,
                       skip_part_of_expansion=True)
    assert svc.queries == ["about hub", "about hub dep"]
    assert "container" in st.entities        # still a result


def test_per_hop_cap_does_not_narrow_the_next_hop_frontier():
    """The caps bound SEARCH, not expansion: an entity that lost its
    re-query to the per-hop cap must still be expanded through, or the
    knob quietly becomes a depth limit (``frontier = newly``, not
    ``targets``)."""
    edges = [("hub", "depends-on", f"spoke{i:02d}") for i in range(6)]
    edges += [("spoke05", "runs-on", "deep")]      # only reachable via hop 2
    svc = _CountingSvc(_FakeSvc(["hub relates to many things"], edges))
    vocab = ["hub", "deep"] + [f"spoke{i:02d}" for i in range(6)]
    st = rc.run_recall(svc.search, svc.graph, vocab, "about hub",
                       rc.MechanicalController(), hops=2,
                       max_searches_per_hop=1)
    # Only spoke00 was re-queried on hop 1 (alphabetical, no seed mentions,
    # no degrees) — spoke05 still expanded, so the hop-2 terminal arrives.
    assert svc.queries[1] == "about hub spoke00"
    assert "deep" in st.entities
    assert st.entity_hop["deep"] == 2


def test_per_hop_cap_alone_does_not_flag_truncated():
    """Deliberate: a per-hop cut still returns every entity, edge and fact
    the walk found — only supporting texts thin out, and the MCP layer caps
    those at 6 anyway. ``truncated`` is reserved for the hard ceilings, so
    it stays a signal rather than firing on nearly every live call."""
    svc = _CountingSvc(_star(10))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(10), "about hub",
                       rc.MechanicalController(), hops=1,
                       max_searches_per_hop=3)
    assert st.truncated is False
    out = rc.recall_state_to_dict(st, "about hub", 1)
    assert "truncated" not in out and "searches_issued" not in out


def test_requery_order_is_untouched_when_the_cap_exactly_fits():
    """The reorder is gated on a strict ``>``: at exactly the cap the walk
    must keep discovery order, which is what makes an under-cap response
    byte-identical."""
    edges = [("hub", "depends-on", f"spoke{i:02d}") for i in (9, 1, 5)]
    svc = _CountingSvc(_FakeSvc(["hub connects to spoke09"], edges))
    degrees = {"hub": 3, "spoke01": 1, "spoke05": 2, "spoke09": 20}
    rc.run_recall(svc.search, svc.graph,
                  ["hub", "spoke01", "spoke05", "spoke09"], "about hub",
                  rc.MechanicalController(), hops=1, degree_fn=degrees.get,
                  max_searches_per_hop=3)
    # Discovery order (the graph's own sorted neighbor order), NOT the
    # seed-mention/degree ranking that would have led with spoke09.
    assert svc.queries[1:] == ["about hub spoke01", "about hub spoke05",
                               "about hub spoke09"]


def test_negative_caps_read_as_off_not_as_a_one_short_slice():
    """A hand-edited config can carry a negative (the Console's minimums
    are 0). ``targets[:-1]`` would silently drop one re-query per hop and
    a negative ceiling would stop after the seed search — both look like
    working caps."""
    svc = _CountingSvc(_star(4))
    st = rc.run_recall(svc.search, svc.graph, _star_vocab(4), "about hub",
                       rc.MechanicalController(), hops=1,
                       max_searches_per_hop=-1, max_total_searches=-5,
                       time_budget_seconds=-1.0)
    assert len(svc.queries) == 5          # seed + all four, uncapped
    assert st.truncated is False


def test_time_budget_stops_inside_the_expansion_loop():
    """The budget is checked per expanded node, not only at the search
    boundaries — a hop over a wide frontier is many ``graph_fn`` calls with
    no search in between, and a walk that only checked at the search
    boundaries would run the whole hop out first."""
    slow = {"n": 0}
    edges = [(f"hub{i}", "depends-on", f"spoke{i}") for i in range(3)]
    inner = _FakeSvc(["three hubs"], edges)

    def slow_graph(entity, depth=1):
        import time as _t
        slow["n"] += 1
        _t.sleep(0.06)
        return inner.graph(entity, depth)

    svc = _CountingSvc(inner)
    st = rc.run_recall(svc.search, slow_graph,
                       ["hub0", "hub1", "hub2", "spoke0", "spoke1", "spoke2"],
                       "about hub0 hub1 hub2", rc.MechanicalController(),
                       hops=3, time_budget_seconds=0.1)
    assert st.seeds == ["hub0", "hub1", "hub2"]
    assert st.truncated is True
    assert slow["n"] == 2                 # cut before the third seed expanded
    assert len(svc.queries) == 1          # and never reached the re-queries
