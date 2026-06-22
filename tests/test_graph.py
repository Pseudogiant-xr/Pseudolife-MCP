"""Phase 2 graph suite — ontology-lite logic, storage CRUD, service tools.

Three tiers:
* pure-logic tests over :mod:`pseudolife_memory.graph` (no PG, fast);
* PG-backed storage tests (skip cleanly without a test server);
* service-level tests with the real embedder (one shared service).
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory import graph as G


# ── pure logic: normalization + validation ───────────────────────────────

def test_norm_name_folds_separators():
    assert G.norm_name("Depends On") == "depends-on"
    assert G.norm_name("depends_on") == "depends-on"
    assert G.norm_name("  DEPENDS---ON  ") == "depends-on"
    assert G.norm_name("a.b/c:d") == "a-b-c-d"
    assert G.norm_name("") == ""


def test_resolve_relation_normalizes_variants():
    known = ["depends-on", "part-of", "related-to"]
    assert G.resolve_relation(known, "depends_on") == ("depends-on", [])
    assert G.resolve_relation(known, "Depends On") == ("depends-on", [])


def test_resolve_relation_suggests_on_miss():
    known = ["depends-on", "part-of", "uses", "related-to"]
    name, suggestions = G.resolve_relation(known, "dependson")
    assert name is None
    assert "depends-on" in suggestions
    assert len(suggestions) <= 3


# ── pure logic: on-read inference ────────────────────────────────────────

_RELS = {
    "depends-on": {"transitive": True, "inverse_of": None},
    "part-of": {"transitive": True, "inverse_of": None},
    "runs-on": {"transitive": False, "inverse_of": "hosts"},
    "hosts": {"transitive": False, "inverse_of": None},
}


def _e(src, rel, dst):
    return {"src": src, "relation": rel, "dst": dst}


def test_transitive_closure_derived():
    edges = [_e("a", "depends-on", "b"), _e("b", "depends-on", "c")]
    derived = G.derive_edges(edges, _RELS)
    keys = {(d["src"], d["relation"], d["dst"]) for d in derived}
    assert ("a", "depends-on", "c") in keys
    via = next(d for d in derived
               if (d["src"], d["dst"]) == ("a", "c"))["via"]
    assert via == ["transitive:depends-on"]


def test_inverse_mirrors_both_directions():
    # runs-on declares inverse_of=hosts; both directions derive.
    derived = G.derive_edges([_e("svc", "runs-on", "box")], _RELS)
    keys = {(d["src"], d["relation"], d["dst"]) for d in derived}
    assert ("box", "hosts", "svc") in keys
    derived2 = G.derive_edges([_e("box", "hosts", "svc")], _RELS)
    keys2 = {(d["src"], d["relation"], d["dst"]) for d in derived2}
    assert ("svc", "runs-on", "box") in keys2


def test_cycle_terminates_and_no_self_loops():
    edges = [_e("a", "depends-on", "b"), _e("b", "depends-on", "c"),
             _e("c", "depends-on", "a")]
    derived = G.derive_edges(edges, _RELS)
    assert all(d["src"] != d["dst"] for d in derived)
    keys = {(d["src"], d["relation"], d["dst"]) for d in derived}
    assert ("a", "depends-on", "c") in keys  # closure across the cycle


def test_subgraph_depth_clamped_to_three():
    chain = [_e(i, "uses", i + 1) for i in range(6)]
    rels = {"uses": {"transitive": False, "inverse_of": None}}
    sub = G.build_subgraph(chain, rels, 0, depth=99)
    assert sub["nodes"] == {0, 1, 2, 3}  # MAX_DEPTH = 3


def test_subgraph_path_via_to():
    chain = [_e(i, "uses", i + 1) for i in range(5)]
    rels = {"uses": {"transitive": False, "inverse_of": None}}
    sub = G.build_subgraph(chain, rels, 0, depth=1, to=4)
    assert sub["paths"] == [[0, 1, 2, 3, 4]]
    # Path nodes folded into the neighborhood even beyond depth.
    assert 4 in sub["nodes"]


def test_subgraph_sees_inbound_edges():
    sub = G.build_subgraph([_e("x", "uses", "root")],
                           {"uses": {"transitive": False, "inverse_of": None}},
                           "root", depth=1)
    assert "x" in sub["nodes"]


# ── storage CRUD (PG) ────────────────────────────────────────────────────

@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


def test_builtin_relations_seeded(storage):
    rels = {r["name"]: r for r in storage.load_relations()}
    for name in ("depends-on", "part-of", "runs-on", "hosts", "uses",
                 "configures", "stores-data-in", "related-to"):
        assert name in rels and rels[name]["builtin"]
    assert rels["depends-on"]["transitive"]
    assert rels["runs-on"]["inverse_of"] == "hosts"


def test_entity_alias_roundtrip(storage):
    eid = storage.ensure_entity("postgres", display="Postgres")
    assert storage.ensure_entity("postgres") == eid  # idempotent
    storage.add_alias("pg", eid)
    found = storage.find_entity("pg")
    assert found is not None and found["id"] == eid
    assert found["canonical"] == "postgres"
    assert "pg" in found["aliases"]
    assert storage.entity_id_map()["pg"] == eid


def test_edge_upsert_bumps_confidence_and_revives(storage):
    a = storage.ensure_entity("a")
    b = storage.ensure_entity("b")
    first = storage.upsert_edge(a, "uses", b, confidence=0.8)
    again = storage.upsert_edge(a, "uses", b, confidence=0.8)
    assert again["id"] == first["id"]
    assert again["confidence"] == pytest.approx(0.85)
    assert storage.supersede_edge(a, "uses", b)
    assert storage.load_graph()["edges"] == []
    revived = storage.upsert_edge(a, "uses", b, confidence=0.5)
    assert len(storage.load_graph()["edges"]) == 1
    assert revived["confidence"] == pytest.approx(0.9)  # 0.85 + 0.05


# ── service-level (real embedder; one shared instance) ──────────────────

@pytest.fixture(scope="module")
def svc(pg_url, tmp_path_factory):
    """Module-scoped service against a wiped DB — embedder loads once."""
    import psycopg as _psy
    from pseudolife_memory.storage.schema import ensure_schema

    with _psy.connect(pg_url) as conn:
        # Pin to public first (see pg_fixtures.pg_conn) — mirrors PostgresStorage.
        conn.execute("SET search_path TO public")
        conn.commit()
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE edges, entity_aliases, relations, facts, world_facts, "
                "entries, episodes, entities, meta RESTART IDENTITY CASCADE",
            )
        conn.commit()
        ensure_schema(conn)

    from pseudolife_memory.service import MemoryService

    return MemoryService(
        data_dir=tmp_path_factory.mktemp("graph-svc"), database_url=pg_url,
    )


def test_relate_unknown_relation_suggestions(svc):
    out = svc.graph_relate("web-app", "dependsOn", "daemon")
    assert out["error"] == "unknown_relation"
    assert "depends-on" in out["suggestions"]
    assert "related-to" in out["hint"]


def test_relate_normalizes_variant_and_warns_on_type(svc):
    out = svc.graph_relate(
        "web-app", "runs_on", "agent-box",
        src_type="service", dst_type="host",
    )
    assert out["relation"] == "runs-on"
    assert out["warnings"] == []
    # Now a relation expecting different types → warning, stored anyway.
    r = svc.relation_define(
        "deployed-to", "src service is deployed to dst environment",
        src_type="service", dst_type="environment",
    )
    assert r["defined"] == "deployed-to"
    out2 = svc.graph_relate("web-app", "deployed-to", "agent-box")
    assert any("expects 'environment'" in w for w in out2["warnings"])
    assert out2["relation"] == "deployed-to"  # stored despite the warning


def test_builtin_relation_protected(svc):
    out = svc.relation_define("depends-on", "rewrite attempt")
    assert out["error"] == "builtin_relation"


def test_alias_resolves_in_relate(svc):
    svc.graph_alias("agent-box", "the-box")
    out = svc.graph_relate("sensor-hub", "runs-on", "the-box")
    assert out["dst"] == "agent-box"


def test_graph_neighborhood_facts_and_derived(svc):
    svc.graph_relate("web-app", "depends-on", "pseudolife-mcp", origin="user")
    svc.graph_relate("pseudolife-mcp", "depends-on", "postgres")
    svc.cortex_write("postgres", "port", "5433", support="user")

    out = svc.graph_neighborhood("web-app", depth=2)
    assert out["found"]
    names = {n["canonical"] for n in out["nodes"]}
    assert {"web-app", "pseudolife-mcp", "postgres"} <= names
    derived = [e for e in out["edges"] if e["derived"]]
    assert any(
        e["src"] == "web-app" and e["dst"] == "postgres"
        and e["via"] == ["transitive:depends-on"]
        for e in derived
    )
    # Inverse derivation from the earlier runs-on assertions.
    assert any(e["relation"] == "hosts" and e["derived"]
               for e in svc.graph_neighborhood("agent-box", depth=1)["edges"])
    # Node facts ride along.
    pg_node = next(n for n in out["nodes"] if n["canonical"] == "postgres")
    assert {"attribute": "port", "value": "5433", "origin": "user",
            "confidence": pytest.approx(0.7)}.items() <= pg_node["facts"][0].items()


def test_graph_path_between_two_entities(svc):
    out = svc.graph_neighborhood("web-app", depth=1, to="postgres")
    assert out["paths"] and out["paths"][0][0] == "web-app"
    assert out["paths"][0][-1] == "postgres"


def test_unrelate_hides_edge(svc):
    svc.graph_relate("tmp-a", "uses", "tmp-b")
    out = svc.graph_unrelate("tmp-a", "uses", "tmp-b")
    assert out["removed"] is True
    edges = svc.graph_neighborhood("tmp-a", depth=1)["edges"]
    assert not any(e["dst"] == "tmp-b" for e in edges)


def test_fact_set_links_entity_ids(svc, pg_url):
    """fact_set creates the subject node; the snapshot links entity_id and
    object_entity_id (value naming a known entity)."""
    import psycopg as _psy

    svc.cortex_write("web-app", "backend", "postgres", support="user")
    with _psy.connect(pg_url) as conn:
        # Pin to public so the unqualified `facts`/`entities` below read the real bank.
        conn.execute("SET search_path TO public")
        row = conn.execute(
            """
            SELECT f.entity_id, f.object_entity_id, e.canonical, o.canonical
            FROM facts f
            JOIN entities e ON e.id = f.entity_id
            JOIN entities o ON o.id = f.object_entity_id
            WHERE f.entity_norm = 'web-app' AND f.attribute_norm = 'backend'
              AND f.status = 'current'
            """,
        ).fetchone()
    assert row is not None
    assert row[2] == "web-app" and row[3] == "postgres"
    ref = svc.entity_ref("web-app")
    assert ref is not None and ref["canonical"] == "web-app"


def test_no_age_imports_remain():
    """AGE is removed — no module should import it or call cypher()."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "pseudolife_memory"
    offenders = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "storage.age" in text or "AgeGraph" in text or ".cypher(" in text:
            offenders.append(p.name)
    assert offenders == [], f"AGE references remain in: {offenders}"
