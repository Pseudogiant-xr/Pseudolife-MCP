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


def test_entity_sources_upsert_and_read(storage):
    import time as _t
    eid = storage.ensure_entity("es-postgres", display="es-postgres")
    storage.upsert_entity_source(eid, "es-proj-a", "derived", _t.time())
    storage.upsert_entity_source(eid, "es-proj-b", "derived", _t.time())
    assert {r["source"] for r in storage.sources_for_entity(eid)} == {"es-proj-a", "es-proj-b"}
    assert storage.entity_sources_map()[eid] == ["es-proj-a", "es-proj-b"]


def test_entity_sources_manual_not_clobbered(storage):
    import time as _t
    eid = storage.ensure_entity("es-immerse", display="es-immerse")
    storage.upsert_entity_source(eid, "es-gw2", "manual", _t.time())
    storage.upsert_entity_source(eid, "es-gw2", "derived", _t.time())
    assert storage.sources_for_entity(eid)[0]["origin"] == "manual"


def test_entity_sources_project_counts(storage):
    import time as _t
    a = storage.ensure_entity("es-c-a", display="es-c-a")
    b = storage.ensure_entity("es-c-b", display="es-c-b")
    storage.upsert_entity_source(a, "es-px", "derived", _t.time())
    storage.upsert_entity_source(b, "es-px", "derived", _t.time())
    storage.upsert_entity_source(b, "es-py", "derived", _t.time())
    counts = {r["source"]: r["entities"] for r in storage.project_source_counts()}
    assert counts["es-px"] == 2 and counts["es-py"] == 1


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


# ── pure logic: degree + shortest path ──────────────────────────────────

def test_degree_counts_undirected():
    edges = [
        {"src_id": 1, "dst_id": 2},
        {"src_id": 1, "dst_id": 3},
        {"src_id": 2, "dst_id": 3},
    ]
    assert G.degree_counts(edges) == {1: 2, 2: 2, 3: 2}


def test_degree_counts_empty():
    assert G.degree_counts([]) == {}


def test_degrees_by_name_maps_display():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 1, "dst_id": 3}]
    entities = [
        {"id": 1, "display": "hub"},
        {"id": 2, "display": "a"},
        {"id": 3, "display": "b"},
    ]
    assert G.degrees_by_name(edges, entities) == {"hub": 2, "a": 1, "b": 1}


def test_shortest_path_direct():
    edges = [{"src_id": 1, "dst_id": 2}]
    assert G.shortest_path(edges, 1, 2) == [1, 2]


def test_shortest_path_two_hop():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3}]
    assert G.shortest_path(edges, 1, 3) == [1, 2, 3]


def test_shortest_path_same_node():
    assert G.shortest_path([{"src_id": 1, "dst_id": 2}], 1, 1) == [1]


def test_shortest_path_no_path():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 3, "dst_id": 4}]
    assert G.shortest_path(edges, 1, 4) is None


def test_shortest_path_exceeds_max_hops():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3},
             {"src_id": 3, "dst_id": 4}]
    assert G.shortest_path(edges, 1, 4, max_hops=2) is None
    assert G.shortest_path(edges, 1, 4, max_hops=3) == [1, 2, 3, 4]


# ── service-level graph_path tests ──────────────────────────────────────

def test_graph_path_returns_chain(svc):
    svc.graph_relate("gp-a", "depends-on", "gp-b")
    svc.graph_relate("gp-b", "depends-on", "gp-c")
    out = svc.graph_path("gp-a", "gp-c")
    assert out["found"] is True
    assert out["path"] == ["gp-a", "gp-b", "gp-c"]
    assert out["hops"] == 2
    assert out["edges"][0]["src"] == "gp-a" and out["edges"][0]["dst"] == "gp-b"


def test_graph_path_missing_endpoint(svc):
    svc.graph_relate("gp-d", "depends-on", "gp-e")
    out = svc.graph_path("gp-d", "gp-nope")
    assert out["found"] is False
    assert out["missing"] == "gp-nope"


def test_graph_path_no_path_within_hops(svc):
    svc.graph_relate("gp-f", "depends-on", "gp-g")
    svc.graph_relate("gp-g", "depends-on", "gp-h")
    out = svc.graph_path("gp-f", "gp-h", max_hops=1)
    assert out["found"] is True
    assert out["path"] == [] and out["hops"] is None


def test_graph_path_reverse_edge(svc):
    # Path is found undirected, but the stored edges point z->y->x. The
    # returned edges must keep their canonical (stored) direction.
    svc.graph_relate("gp-z", "depends-on", "gp-y")
    svc.graph_relate("gp-y", "depends-on", "gp-x")
    out = svc.graph_path("gp-x", "gp-z")
    assert out["found"] is True and out["hops"] == 2
    assert out["path"] == ["gp-x", "gp-y", "gp-z"]
    # Traversal is x->y->z, but each edge keeps its stored direction: the x<->y
    # hop is stored as gp-y --depends-on--> gp-x, reported canonically (not flipped).
    assert out["edges"][0] == {"src": "gp-y", "relation": "depends-on", "dst": "gp-x"}
    assert out["edges"][1] == {"src": "gp-z", "relation": "depends-on", "dst": "gp-y"}


def test_replace_and_load_communities(svc):
    # Need real entity ids — create two entities via the public graph path.
    svc.graph_relate("ci-a", "depends-on", "ci-b")
    st = svc._storage  # noqa: SLF001
    g = st.load_graph()
    ids = {e["display"]: e["id"] for e in g["entities"]}
    a, b = ids["ci-a"], ids["ci-b"]
    summaries = [{"id": 0, "label": "ci-a", "size": 2, "cohesion": 1.0}]
    st.replace_communities({a: 0, b: 0}, summaries, 100.0)
    loaded = st.load_communities()
    assert loaded["assignment"][a] == 0 and loaded["assignment"][b] == 0
    assert loaded["communities"][0]["label"] == "ci-a"
    # Replace is wholesale: a second call with fewer rows clears the old ones.
    st.replace_communities({a: 3}, [{"id": 3, "label": "ci-a", "size": 1, "cohesion": 1.0}], 101.0)
    loaded2 = st.load_communities()
    assert loaded2["assignment"] == {a: 3}


def test_get_set_meta_roundtrip(svc):
    st = svc._storage  # noqa: SLF001
    st.set_meta("graph_digest", {"computed_at": 5.0, "god_nodes": []})
    assert st.get_meta("graph_digest")["computed_at"] == 5.0
    assert st.get_meta("does-not-exist") is None


def test_memory_traces_storage_roundtrip(svc):
    # Real ids: create two entities + a fact + an entry via the public paths.
    svc.graph_relate("tr-a", "depends-on", "tr-b")
    svc.cortex_write("tr-a", "role", "frontend", support="user")
    st = svc._storage  # noqa: SLF001  (fixture lazy-inits _storage on first svc call)
    svc.store("tr-a is the frontend role", source="general")
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    entry_id = st.conn.execute(
        "SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en, an = _norm_key("tr-a"), _norm_key("role")
    assert st.add_trace(en, an, entry_id, _t.time()) is True
    assert st.add_trace(en, an, entry_id, _t.time()) is False   # idempotent on PK
    assert st.traces_for_slot(en, an) == [entry_id]
    assert any(f["entity"] == "tr-a" for f in st.facts_for_entry(entry_id))
    assert st.get_entry(entry_id)["text"] == "tr-a is the frontend role"
    assert st.get_entry(10_000_000) is None
    st.bump_reinforcements(entry_id, 2)
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (entry_id,)).fetchone()[0] == 2
    # DURABILITY (the anchor-correction regression guard): a later cortex_write
    # triggers a full facts snapshot rewrite (DELETE+reinsert, new fact ids).
    # The slot-keyed trace MUST survive it.
    svc.cortex_write("tr-a", "language", "python", support="user")
    assert st.traces_for_slot(en, an) == [entry_id]
    assert any(f["entity"] == "tr-a" for f in st.facts_for_entry(entry_id))


def test_graph_assign_scope_writes_manual_source(svc):
    svc.graph_relate("as-target", "uses", "as-other")   # creates the entity
    res = svc.graph_assign_scope("as-target", "as-proj")
    assert res["assigned"] is True
    st = svc._storage  # noqa: SLF001
    eid = st.find_entity(G.norm_name("as-target"))["id"]
    rows = {r["source"]: r["origin"] for r in st.sources_for_entity(eid)}
    assert rows["as-proj"] == "manual"


def test_backfill_entity_sources_from_traces(svc):
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    # cortex_write links facts.entity_id; two entries under two sources; trace both.
    svc.cortex_write("es-shared", "role", "thing", support="user")
    st = svc._storage  # noqa: SLF001  (lazy-inits on first svc call)
    svc.store("es-shared mention x", source="es-src-x")
    ex = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    svc.store("es-shared mention y", source="es-src-y")
    ey = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en, an = _norm_key("es-shared"), _norm_key("role")
    st.add_trace(en, an, ex, _t.time())
    st.add_trace(en, an, ey, _t.time())

    n = st.backfill_entity_sources(_t.time())
    assert n >= 2
    eid = st.conn.execute(
        "SELECT entity_id FROM facts WHERE entity_norm=%s AND status='current' "
        "AND entity_id IS NOT NULL LIMIT 1", (en,)).fetchone()[0]
    assert {"es-src-x", "es-src-y"} <= {r["source"] for r in st.sources_for_entity(eid)}
    # idempotent: second run keeps the same derived set
    st.backfill_entity_sources(_t.time())
    assert {"es-src-x", "es-src-y"} <= {r["source"] for r in st.sources_for_entity(eid)}


def test_backfill_preserves_manual(svc):
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    svc.cortex_write("es-curated", "role", "thing", support="user")
    st = svc._storage  # noqa: SLF001  (lazy-inits on first svc call)
    svc.store("es-curated mention", source="es-auto")
    e1 = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en = _norm_key("es-curated")
    st.add_trace(en, _norm_key("role"), e1, _t.time())
    eid = st.conn.execute(
        "SELECT entity_id FROM facts WHERE entity_norm=%s AND status='current' "
        "AND entity_id IS NOT NULL LIMIT 1", (en,)).fetchone()[0]
    st.upsert_entity_source(eid, "es-hand", "manual", _t.time())
    st.backfill_entity_sources(_t.time())
    by_src = {r["source"]: r["origin"] for r in st.sources_for_entity(eid)}
    assert by_src["es-hand"] == "manual"
    assert by_src.get("es-auto") == "derived"


def test_graph_backfill_sources_service(svc):
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    svc.cortex_write("es-svc-target", "role", "thing", support="user")
    st = svc._storage
    svc.store("es-svc-target note", source="es-svc-proj")
    e1 = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en = _norm_key("es-svc-target")
    st.add_trace(en, _norm_key("role"), e1, _t.time())

    res = svc.graph_backfill_sources()
    assert res["attributed"] >= 1
    eid = st.conn.execute(
        "SELECT entity_id FROM facts WHERE entity_norm=%s AND status='current' "
        "AND entity_id IS NOT NULL LIMIT 1", (en,)).fetchone()[0]
    assert "es-svc-proj" in st.entity_sources_map()[eid]


def test_graph_projects_lists_sources(svc):
    import time as _t
    svc._ensure_init()  # noqa: SLF001
    st = svc._storage
    a = st.ensure_entity("es-proj-ent", display="es-proj-ent")
    st.upsert_entity_source(a, "es-proj-z", "derived", _t.time())
    assert any(p["source"] == "es-proj-z" for p in svc.graph_projects()["projects"])


def test_graph_delete_entity_removes_node_and_edges(svc):
    from pseudolife_memory import graph as G
    svc.graph_relate("del-victim", "uses", "del-bystander")
    svc.cortex_write("del-victim", "role", "junk", support="user")  # a fact references it (no-cascade FK)
    st = svc._storage
    eid = st.find_entity(G.norm_name("del-victim"))["id"]

    res = svc.graph_delete_entity("del-victim")
    assert res["deleted"] is True
    assert st.find_entity(G.norm_name("del-victim")) is None
    # its edge is gone; the fact's entity_id was nulled (fact row may remain, unlinked)
    assert all(e["src_id"] != eid and e["dst_id"] != eid for e in st.load_graph()["edges"])


def test_graph_merge_folds_from_into(svc):
    from pseudolife_memory import graph as G
    # `mg-from` and `mg-into` are the same thing stored twice; each has a distinct edge.
    svc.graph_relate("mg-from", "uses", "mg-dep")
    svc.graph_relate("mg-into", "stores-data-in", "mg-store")
    st = svc._storage
    into_id = st.find_entity(G.norm_name("mg-into"))["id"]

    res = svc.graph_merge("mg-from", "mg-into")
    assert res["merged"] is True
    assert st.find_entity(G.norm_name("mg-from"))["id"] == into_id   # alias now resolves to into
    # into absorbed from's edge: an edge from `mg-into` to `mg-dep` now exists
    edges = st.load_graph()["edges"]
    assert any(e["src_id"] == into_id for e in edges)
    disp = {e["id"]: e["display"] for e in st.load_graph()["entities"]}
    pairs = {(disp.get(e["src_id"]), e["relation"], disp.get(e["dst_id"])) for e in edges}
    assert ("mg-into", "uses", "mg-dep") in pairs
    assert ("mg-into", "stores-data-in", "mg-store") in pairs


def test_graph_merge_dedup_collision(svc):
    from pseudolife_memory import graph as G
    svc.graph_relate("mg2-from", "uses", "mg2-shared")
    svc.graph_relate("mg2-into", "uses", "mg2-shared")   # identical relation+target
    st = svc._storage
    into_id = st.find_entity(G.norm_name("mg2-into"))["id"]
    assert svc.graph_merge("mg2-from", "mg2-into")["merged"] is True
    edges = st.load_graph()["edges"]
    disp = {e["id"]: e["display"] for e in st.load_graph()["entities"]}
    shared = [e for e in edges if disp.get(e["src_id"]) == "mg2-into"
              and e["relation"] == "uses" and disp.get(e["dst_id"]) == "mg2-shared"]
    assert len(shared) == 1   # deduped, not duplicated


def test_seedless_scoped_whole_graph(svc):
    import time as _t
    svc._ensure_init()  # noqa: SLF001
    st = svc._storage
    keep = st.ensure_entity("es-keep-node", display="es-keep-node")
    drop = st.ensure_entity("es-drop-node", display="es-drop-node")
    st.upsert_entity_source(keep, "es-scope-a", "derived", _t.time())
    st.upsert_entity_source(drop, "es-scope-b", "derived", _t.time())

    full = svc.graph_neighborhood(entity=None, scope="all")
    names = {n["entity"] for n in full["nodes"]}
    assert {"es-keep-node", "es-drop-node"} <= names
    assert all("sources" in n for n in full["nodes"])

    scoped = svc.graph_neighborhood(entity=None, scope="es-scope-a")
    scoped_names = {n["entity"] for n in scoped["nodes"]}
    assert "es-keep-node" in scoped_names and "es-drop-node" not in scoped_names


def test_dream_edge_confidence_varies_by_type(svc):
    """dream-written edges carry per-edge confidence: clean ~0.70, violation ~0.175.

    Entity names are unique to this test so the module-scoped shared DB cannot
    have bumped them before. 'ec-svc-daemon' → service type ('daemon' in name);
    'docker-desktop' → runtime type (starts 'docker-'); 'user' → person type
    (in _PERSON).  runs-on requires src in {service/process/...} — person
    violates it → 0.175.
    """
    svc._ensure_init()  # noqa: SLF001 — storage must be ready before _link_dream_relations
    rels = [
        # clean: service → runtime  (runs-on allows this combination)
        {"src": "ec-svc-daemon", "relation": "runs-on", "dst": "docker-desktop"},
        # violation: person → runtime  (runs-on src must be service/process/...)
        {"src": "user", "relation": "runs-on", "dst": "docker-desktop"},
    ]
    svc._link_dream_relations(rels)
    g = svc._storage.load_graph()
    id_to_display = {e["id"]: e["display"] for e in g["entities"]}
    docker_desktop_ids = {e["id"] for e in g["entities"]
                          if e["display"] == "docker-desktop"}
    our_srcs = {"ec-svc-daemon", "user"}
    confs = sorted(
        e["confidence"] for e in g["edges"]
        if e["relation"] == "runs-on"
        and id_to_display.get(e["src_id"]) in our_srcs
        and e["dst_id"] in docker_desktop_ids
    )
    assert len(confs) == 2, f"expected 2 runs-on edges to docker-desktop, got {confs}"
    assert abs(confs[0] - 0.175) < 0.01, f"expected violation ~0.175, got {confs[0]}"
    assert abs(confs[-1] - 0.70) < 0.01, f"expected clean ~0.70, got {confs[-1]}"
