"""Backend-agnostic contract for the GraphStore port. The default
PostgresNetworkxGraphStore must pass; any future backend must pass the same.
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.fixture
def graph_store(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
    return PostgresNetworkxGraphStore(PostgresStorage(pg_url))


def test_upsert_and_subgraph_returns_edge(graph_store):
    st = graph_store._st
    a = st.ensure_entity("svc-a")
    b = st.ensure_entity("host-b")
    graph_store.upsert_edge(a, "runs-on", b, confidence=0.8)
    sub = graph_store.subgraph(a, depth=1)
    pairs = {(e["src"], e["relation"], e["dst"]) for e in sub["edges"]}
    assert (a, "runs-on", b) in pairs
    assert a in sub["nodes"] and b in sub["nodes"]
    assert a in sub["entities"]
    assert {"id", "canonical", "display", "etype"} <= set(sub["entities"][a])
    assert isinstance(sub["aliases"], dict)


def test_subgraph_derives_transitive(graph_store):
    st = graph_store._st
    a = st.ensure_entity("a-pkg")
    b = st.ensure_entity("b-pkg")
    c = st.ensure_entity("c-pkg")
    graph_store.upsert_edge(a, "depends-on", b)
    graph_store.upsert_edge(b, "depends-on", c)
    sub = graph_store.subgraph(a, depth=3)
    derived = {(e["src"], e["dst"]) for e in sub["edges"] if e["derived"]}
    assert (a, c) in derived  # transitive depends-on closure
    assert all(e.get("via") for e in sub["edges"] if e["derived"])


def test_supersede_hides_edge(graph_store):
    st = graph_store._st
    a = st.ensure_entity("x-svc")
    b = st.ensure_entity("y-svc")
    graph_store.upsert_edge(a, "uses", b)
    assert graph_store.supersede_edge(a, "uses", b) is True
    sub = graph_store.subgraph(a, depth=1)
    base = {(e["src"], e["relation"], e["dst"])
            for e in sub["edges"] if not e["derived"]}
    assert (a, "uses", b) not in base
