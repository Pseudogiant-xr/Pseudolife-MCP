"""Pure graph-consolidation logic for the deep dream (DB-free, unit-testable like
graph_insight.py / graph_review.py). Two halves: deterministic SELF-CLEAN
classifiers (re-score / hard-type-violation / exact-duplicate) and semantic
CANDIDATE generation for cross-session link discovery. The service supplies
edges / entities / entries / embeddings / scope-map and persists the decisions."""
from __future__ import annotations

import numpy as np

from pseudolife_memory.graph import degree_counts
from pseudolife_memory.memory.graph_review import _token_set
from pseudolife_memory.memory.relation_quality import (
    edge_confidence, is_hard_type_violation,
)


def _disp(entities: list[dict]) -> dict[int, str]:
    return {e["id"]: e["display"] for e in entities}


# --- Step A: self-clean classifiers -------------------------------------------

def rescore_edges(edges: list[dict], entities: list[dict]) -> list[tuple[int, float]]:
    """(edge_id, new_conf) for every agent edge whose recomputed edge_confidence
    differs from what's stored. Mirrors ops/backfill_edge_confidence.py, but pure."""
    disp = _disp(entities)
    out: list[tuple[int, float]] = []
    for e in edges:
        if e.get("origin") != "agent":
            continue
        new = edge_confidence(disp.get(e["src_id"], ""), e["relation"],
                              disp.get(e["dst_id"], ""))
        if round(float(e.get("confidence", 0.0)), 3) != new:
            out.append((e["id"], new))
    return out


def hard_violation_edges(edges: list[dict], entities: list[dict]) -> list[dict]:
    """Agent edges that are hard type-violations (both endpoints confidently typed
    AND incompatible) — the auto-supersede bucket."""
    disp = _disp(entities)
    return [e for e in edges
            if e.get("origin") == "agent"
            and is_hard_type_violation(disp.get(e["src_id"], ""), e["relation"],
                                       disp.get(e["dst_id"], ""))]


def exact_duplicate_pairs(entities: list[dict], edges: list[dict]) -> list[tuple[int, int]]:
    """(from_id, into_id) for entity pairs with token-set-IDENTICAL displays
    (Jaccard == 1.0). Fold the lower-degree entity into the higher-degree one
    (preserve the more-connected node); tie-break folds the higher id into the
    lower id (deterministic)."""
    deg = degree_counts(edges)
    toks = [(e["id"], _token_set(e["display"])) for e in entities]
    pairs: list[tuple[int, int]] = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a_id, a = toks[i]
            b_id, b = toks[j]
            if not a or not b or a != b:
                continue
            da, db = deg.get(a_id, 0), deg.get(b_id, 0)
            if da > db or (da == db and a_id < b_id):
                into, frm = a_id, b_id
            else:
                into, frm = b_id, a_id
            pairs.append((frm, into))
    pairs.sort()
    return pairs
