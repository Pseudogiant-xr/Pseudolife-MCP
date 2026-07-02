"""GraphStore — the swappable graph backend port (design 2026-06-22).

The Postgres `entities` table is the source of truth and is shared with
facts/lessons/world (they FK into it), so entities are NOT part of this port.
The port owns edges, the relation registry, and traversal/derivation — the
parts a future AGE / graph-DB backend could own. The default impl wraps the
existing PostgresStorage edge methods + the NetworkX derivation in
``pseudolife_memory.graph``.
"""
from __future__ import annotations

from typing import Any, Protocol

from pseudolife_memory.graph import build_subgraph


class GraphStore(Protocol):
    def upsert_edge(self, src_id: int, relation: str, dst_id: int, *,
                    confidence: float = 0.8, origin: str | None = None,
                    revive: bool = True) -> dict: ...

    def supersede_edge(self, src_id: int, relation: str, dst_id: int) -> bool: ...

    def bless_edge(self, src_id: int, relation: str, dst_id: int, *,
                   confidence: float = 0.8) -> bool: ...

    def load_relations(self) -> list[dict]: ...

    def upsert_relation(self, name: str, description: str, *,
                        src_type: str | None = None, dst_type: str | None = None,
                        transitive: bool = False,
                        inverse_of: str | None = None) -> None: ...

    def subgraph(self, root_id: int, *, depth: int = 1,
                 to_id: int | None = None) -> dict: ...


class PostgresNetworkxGraphStore:
    """Default GraphStore: Postgres edge tables + NetworkX derive-on-read."""

    def __init__(self, storage) -> None:
        self._st = storage

    # ── writes (delegate to the hub's edge/relation tables) ─────────────
    def upsert_edge(self, src_id: int, relation: str, dst_id: int, *,
                    confidence: float = 0.8, origin: str | None = None,
                    revive: bool = True) -> dict:
        return self._st.upsert_edge(src_id, relation, dst_id,
                                    confidence=confidence, origin=origin,
                                    revive=revive)

    def supersede_edge(self, src_id: int, relation: str, dst_id: int) -> bool:
        return self._st.supersede_edge(src_id, relation, dst_id)

    def bless_edge(self, src_id: int, relation: str, dst_id: int, *,
                   confidence: float = 0.8) -> bool:
        return self._st.bless_edge(src_id, relation, dst_id,
                                   confidence=confidence)

    def load_relations(self) -> list[dict]:
        return self._st.load_relations()

    def upsert_relation(self, name: str, description: str, *,
                        src_type: str | None = None, dst_type: str | None = None,
                        transitive: bool = False,
                        inverse_of: str | None = None) -> None:
        self._st.upsert_relation(name, description, src_type=src_type,
                                 dst_type=dst_type, transitive=transitive,
                                 inverse_of=inverse_of)

    # ── reads / traversal (NetworkX derive-on-read over the hub) ────────
    def subgraph(self, root_id: int, *, depth: int = 1,
                 to_id: int | None = None) -> dict[str, Any]:
        g = self._st.load_graph()
        registry = {r["name"]: {"transitive": r["transitive"],
                                "inverse_of": r["inverse_of"]}
                    for r in self._st.load_relations()}
        edges = [{"src": e["src_id"], "relation": e["relation"],
                  "dst": e["dst_id"], "confidence": e["confidence"],
                  "origin": e["origin"]} for e in g["edges"]]
        sub = build_subgraph(edges, registry, root_id, depth=depth, to=to_id)
        return {
            "nodes": sub["nodes"], "edges": sub["edges"], "paths": sub["paths"],
            "entities": {e["id"]: e for e in g["entities"]},
            "aliases": g["aliases"],
        }
