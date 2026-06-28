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


# --- Step B: candidate generation ---------------------------------------------

def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def entity_context_vectors(entities: list[dict], entries: list[dict],
                           traces_by_entity: dict[str, list[int]]) -> dict[int, np.ndarray]:
    """Per-entity context vector = L2-normalized mean of its mentioning entries'
    embeddings. Trace entries are the primary source; entities without traces fall
    back to a token-mention scan; entities with neither are omitted (we don't guess)."""
    by_id = {e["id"]: e for e in entries}
    entry_tokens = [(e["id"], _token_set(e.get("text", ""))) for e in entries]
    out: dict[int, np.ndarray] = {}
    for ent in entities:
        ids = list(traces_by_entity.get(ent["canonical"], []))
        if not ids:
            want = _token_set(ent["display"])
            if want:
                ids = [eid for eid, toks in entry_tokens if want <= toks]
        embs = [by_id[i]["embedding"] for i in ids if i in by_id]
        if not embs:
            continue
        out[ent["id"]] = _l2(np.mean(np.stack(embs), axis=0))
    return out


def candidate_pairs(vectors: dict[int, np.ndarray], edges: list[dict],
                    entities: list[dict], scope_map: dict[int, list[str]], *,
                    min_similarity: float = 0.55, top_k: int = 50) -> list[dict]:
    """Unlinked, scope-coherent, semantically-near entity pairs — the link
    candidates. Drops pairs that already have an edge (either direction), exact
    duplicates (a Step-A merge), or that sit in disjoint non-empty project scopes."""
    disp = _disp(entities)
    linked = {frozenset((e["src_id"], e["dst_id"])) for e in edges}
    dup = {frozenset(p) for p in exact_duplicate_pairs(entities, edges)}
    ids = sorted(vectors)
    scored: list[dict] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            u, v = ids[i], ids[j]
            key = frozenset((u, v))
            if key in linked or key in dup:
                continue
            su, sv = set(scope_map.get(u, [])), set(scope_map.get(v, []))
            if su and sv and not (su & sv):       # disjoint, both attributed
                continue
            sim = float(np.dot(vectors[u], vectors[v]))
            if sim < min_similarity:
                continue
            scored.append({"src_id": u, "dst_id": v, "src": disp.get(u, str(u)),
                           "dst": disp.get(v, str(v)), "similarity": round(sim, 4)})
    scored.sort(key=lambda c: (-c["similarity"], c["src_id"], c["dst_id"]))
    return scored[:top_k]
