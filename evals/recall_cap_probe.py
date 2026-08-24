#!/usr/bin/env python
"""In-tree measurement backing the memory_recall output-cap size claim
(issue #186; docs/guide/retrieval.md; CHANGELOG 2026-08-25).

Issue #186's headline number (93.7 KB / 53 entities / 75 edges / 45 texts)
is a LIVE audit measurement (2026-08-21, real daemon + bank) and is NOT
reproduced here -- it can't be, without that daemon and bank. This probe
instead builds a synthetic hub graph (no DB, no embedder -- the same
fake search_fn/graph_fn harness tests/test_recall.py uses for
``run_recall``), drives it through the real
``pseudolife_memory.memory.recall.run_recall`` plus the real
``mcp_server.memory_recall`` capping path, and records THIS repo's own
fixture's pre-cap vs post-cap serialized JSON size -- so the size-reduction
claim in the docs has a committed, regenerable artifact
(tests/test_eval_evidence.py pins the numbers below).

The fixture deliberately gives the seed a wide 1-hop fan-out (20 direct
children) so a NAIVE flat prefix cap on edges would be entirely consumed by
that one hop, exactly the failure mode the 2026-08-25 review of #186 found
in the first cut of this fix.

Run: python evals/recall_cap_probe.py --out evals/results/recall-cap-186-payload-probe.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO / "evals" / "results"
sys.path.insert(0, str(REPO))


def _fixture():
    """1 root + 20 L1 children (the wide hub ring) + 20 L2 leaves (one per
    L1) = 41 entities, 40 edges. Two hub entities (the root and the first
    L1 child) carry 8 facts each so the per-entity facts cap has something
    real to cut; every other entity carries 1."""
    root = "root-svc"
    l1 = [f"svc-l1-{i}" for i in range(20)]
    l2 = {p: f"{p}-leaf" for p in l1}
    all_entities = [root, *l1, *l2.values()]

    tree_edges = [(root, "depends-on", p) for p in l1]
    tree_edges += [(p, "depends-on", l2[p]) for p in l1]

    neighbors: dict[str, list[tuple[str, str, str]]] = {e: [] for e in all_entities}
    for (src, rel, dst) in tree_edges:
        neighbors[src].append((src, rel, dst))
        neighbors[dst].append((src, rel, dst))

    hubs = {root, l1[0]}

    def facts_for(name: str) -> list[dict]:
        n = 8 if name in hubs else 1
        return [{"attribute": f"attr{i}", "value": f"val-{name}-{i}",
                 "origin": "bench", "confidence": 0.9} for i in range(n)]

    base_query = "what does root-svc connect to"
    filler = ("This sentence exists purely to pad the stored memory past "
              "the recall preview-length cap so the truncation path is "
              "exercised. ")
    texts = {name: f"{base_query} -- entity {name} is a fixture node. {filler * 2}"
             for name in all_entities}

    def search_fn(query: str, top_k: int) -> dict:
        # Rank by token-overlap COUNT (not just presence): every stored
        # text shares the base_query tokens, but only the text for the
        # entity named at the tail of a hop-driven re-query also matches
        # its own name token, so that entity's text ranks first for its
        # own query -- the same "this query is about X" signal a real
        # dense search gives, without needing an embedder.
        toks = set(re.findall(r"[\w-]+", query.lower()))
        scored = []
        for name, text in texts.items():
            overlap = len(toks & set(re.findall(r"[\w-]+", text.lower())))
            if overlap:
                scored.append((overlap, name, text))
        scored.sort(key=lambda x: -x[0])  # stable: ties keep insertion order
        return {"entries": [{"text": t} for (_, _, t) in scored[:top_k]]}

    def graph_fn(entity: str, depth: int) -> dict:
        edges_here = neighbors.get(entity)
        if edges_here is None:
            return {"found": False}
        nbr_names = sorted({(dst if src == entity else src)
                            for (src, _rel, dst) in edges_here})
        nodes = [{"entity": entity, "facts": facts_for(entity)}]
        nodes += [{"entity": n, "facts": facts_for(n)} for n in nbr_names]
        edges_out = [{"src": s, "relation": r, "dst": d, "derived": False}
                     for (s, r, d) in edges_here]
        return {"found": True, "nodes": nodes, "edges": edges_out, "paths": []}

    return base_query, all_entities, search_fn, graph_fn


def _size(d: dict) -> int:
    return len(json.dumps(d))


def _avg_facts(entities: list[dict]) -> float:
    if not entities:
        return 0.0
    return sum(len(e.get("facts", [])) for e in entities) / len(entities)


def _max_facts(entities: list[dict]) -> int:
    return max((len(e.get("facts", [])) for e in entities), default=0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=RESULTS_DIR / "recall-cap-186-payload-probe.json")
    ap.add_argument("--hops", type=int, default=3)
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    from pseudolife_memory.memory.recall import (
        MechanicalController, recall_state_to_dict, run_recall,
    )
    import pseudolife_memory.mcp_server as mcp_server

    query, vocab, search_fn, graph_fn = _fixture()
    state = run_recall(search_fn, graph_fn, vocab, query,
                       MechanicalController(), hops=args.hops, top_k=args.top_k)
    uncapped = recall_state_to_dict(state, query, args.hops)
    assert not state.low_confidence, "fixture query failed to seed -- probe is broken"

    class _FakeService:
        def recall(self, query, hops=None, top_k=None):
            return dict(uncapped)  # a fresh top-level copy each call

    orig_service = mcp_server.service
    mcp_server.service = _FakeService()
    try:
        capped_compact = mcp_server.memory_recall(
            query, hops=args.hops, top_k=args.top_k, verbose=False)
        capped_verbose = mcp_server.memory_recall(
            query, hops=args.hops, top_k=args.top_k, verbose=True)
    finally:
        mcp_server.service = orig_service

    leaf_names = {n["entity"] for n in uncapped["entities"]
                  if n["entity"].endswith("-leaf")}
    capped_names = {n["entity"] for n in capped_compact["entities"]}

    out = {
        "issue": 186,
        "date": "2026-08-25",
        "description": (
            "In-tree reproduction of the memory_recall output-cap size "
            "reduction on a synthetic fixture graph (no DB/daemon) -- NOT "
            "a reproduction of issue #186's live 93.7 KB audit number, "
            "which needs the live daemon and bank it was measured "
            "against and cannot be regenerated here."
        ),
        "params": {
            "hops": args.hops, "top_k": args.top_k,
            "entities_cap": mcp_server._RECALL_MAX_ENTITIES,
            "edges_cap": mcp_server._RECALL_MAX_EDGES,
            "texts_cap": mcp_server._RECALL_MAX_TEXTS,
            "facts_per_entity_cap": mcp_server._RECALL_MAX_FACTS_PER_ENTITY,
            "text_chars": mcp_server._RECALL_TEXT_CHARS,
        },
        "fixture": {
            "entities": len(uncapped["entities"]),
            "edges": len(uncapped["edges"]),
            "texts": len(uncapped["texts"]),
            "hops_reached": uncapped["iterations"],
            "note": ("root has 20 direct (hop-1) children by construction "
                     "-- wider than the edges cap alone, so a naive flat "
                     "edges[:N] would be entirely consumed by hop 1."),
        },
        "uncapped_bytes": _size(uncapped),
        "capped_bytes_compact": _size(capped_compact),
        "capped_bytes_verbose": _size(capped_verbose),
        "reduction_pct_compact": round(
            100 * (1 - _size(capped_compact) / _size(uncapped)), 1),
        "hop2_entities_survive_compact": bool(leaf_names & capped_names),
        "facts_per_entity": {
            "uncapped_avg": round(_avg_facts(uncapped["entities"]), 2),
            "uncapped_max": _max_facts(uncapped["entities"]),
            "capped_max": _max_facts(capped_verbose["entities"]),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nartifact: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
