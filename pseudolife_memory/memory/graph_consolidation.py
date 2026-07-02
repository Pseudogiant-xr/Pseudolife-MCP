"""Pure graph-consolidation logic for the deep dream (DB-free, unit-testable like
graph_insight.py / graph_review.py). Two halves: deterministic SELF-CLEAN
classifiers (re-score / hard-type-violation / exact-duplicate) and semantic
CANDIDATE generation for cross-session link discovery. The service supplies
edges / entities / entries / embeddings / scope-map and persists the decisions."""
from __future__ import annotations

import re

import numpy as np

from pseudolife_memory.graph import degree_counts, norm_name
from pseudolife_memory.memory.graph_review import _token_set
from pseudolife_memory.memory.relation_quality import (
    edge_confidence, is_hard_type_violation,
)


def _disp(entities: list[dict]) -> dict[int, str]:
    return {e["id"]: e["display"] for e in entities}


_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _full_token_set(name: str) -> frozenset[str]:
    """Every alphanumeric token, lowercased, with NO length filter — short
    discriminators (a/b, pg, id, py, version letters) are retained. This is the
    identity test for the AUTO-MERGE class; graph_review._token_set (which drops
    short tokens for recall) is kept for the fuzzy duplicate detector and the
    mention scan."""
    return frozenset(t for t in _WORD_SPLIT.split(str(name).lower()) if t)


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
    lower id (deterministic). This path auto-merges with NO human review, so
    an "A<->B" concat-artifact (a captured-relation extraction artifact, not a
    real entity) is never eligible here — two independently-extracted concat
    artifacts with the same token multiset are junk, not duplicates of each
    other; see junk_entities / _is_concat_artifact."""
    deg = degree_counts(edges)
    toks = [(e["id"], _full_token_set(e["display"])) for e in entities]
    disp = _disp(entities)
    pairs: list[tuple[int, int]] = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a_id, a = toks[i]
            b_id, b = toks[j]
            if not a or not b or a != b:
                continue
            if _is_concat_artifact(disp.get(a_id, "")) or _is_concat_artifact(disp.get(b_id, "")):
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
                           traces_by_entity: dict[str, list[int]], *,
                           min_mentions: int = 2,
                           ) -> tuple[dict[int, np.ndarray], dict[int, frozenset[int]]]:
    """Per-entity context vector = L2-normalized mean of its mentioning entries'
    embeddings, plus the set of those entry ids. Trace entries are the primary
    source; entities without traces fall back to a token-mention scan. An entity is
    included only if it has >= min_mentions DISTINCT mentioning entries (with
    embeddings) — a centroid-of-one isn't a context. Returns (vectors, mentions)."""
    by_id = {e["id"]: e for e in entries}
    entry_tokens = [(e["id"], _token_set(e.get("text", ""))) for e in entries]
    vectors: dict[int, np.ndarray] = {}
    mentions: dict[int, frozenset[int]] = {}
    for ent in entities:
        ids = list(traces_by_entity.get(ent["canonical"], []))
        if not ids:
            want = _token_set(ent["display"])
            if want:
                ids = [eid for eid, toks in entry_tokens if want <= toks]
        valid = {i for i in ids if i in by_id}      # distinct entries with embeddings
        if len(valid) < min_mentions:
            continue
        embs = [by_id[i]["embedding"] for i in valid]
        vectors[ent["id"]] = _l2(np.mean(np.stack(embs), axis=0))
        mentions[ent["id"]] = frozenset(valid)
    return vectors, mentions


def candidate_pairs(vectors: dict[int, np.ndarray], edges: list[dict],
                    entities: list[dict], scope_map: dict[int, list[str]],
                    mentions: dict[int, frozenset[int]], *,
                    min_similarity: float = 0.55, top_k: int = 50,
                    dismissed: set[tuple[str, str]] | None = None,
                    max_support_overlap: float = 1.0) -> list[dict]:
    """Unlinked, scope-coherent, semantically-near entity pairs — the link
    candidates. Drops pairs that already have an edge (either direction), exact
    duplicates (a Step-A merge), have near-identical supporting-entry sets
    (Jaccard >= ``max_support_overlap`` — co-occurrence, not independent
    similarity; 1.0 keeps only the strict-equality drop), sit in disjoint
    non-empty project scopes, or were human-dismissed (``dismissed`` holds
    sorted canonical-name pairs from dismissed_pairs)."""
    disp = _disp(entities)
    canon = {e["id"]: e["canonical"] for e in entities}
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
            if dismissed and tuple(sorted((canon.get(u, ""), canon.get(v, "")))) in dismissed:
                continue
            mu, mv = mentions.get(u), mentions.get(v)
            if mu and mv and len(mu & mv) / len(mu | mv) >= max_support_overlap:
                continue                           # shared support -> co-occurrence
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


# --- SP-1: entity consolidation (merge + junk surfacing) ----------------------

_JUNK_STOPWORDS = frozenset({
    "live", "merged", "done", "fixed", "current", "ok", "pending", "wip",
    "todo", "n/a", "none", "null",
})
_BARE_NUMBER = re.compile(r"^\d+$")

# 2026-07-02 live-cortex cleanup: the classes below covered nearly all of the
# ~612 hand-deleted junk entities. Each is a write-time name shape, tuned to
# spare the near-miss legit shapes ("2026-07-02 review roadmap",
# "arXiv:2606.22844", "docker compose", "8-band continuum").
_COUNT_PREFIX = re.compile(r"^\d+\s")                      # "236 memories"
_BARE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DUMP_FILE = re.compile(r"\.sql(\.gz)?$", re.IGNORECASE)   # pg_dump artifacts
_IMAGE_TAG = re.compile(r":\d+\.\d+\.\d+")                 # 3-part ver; arXiv ids are 2-part
_COMMAND_STRING = re.compile(                              # cmd word + >=2 more tokens
    r"^(docker|git|python|pip|curl|pwsh|npm|pytest)\s+\S+\s+\S", re.IGNORECASE)
_HASH_STATUS = re.compile(r"=\s*[0-9a-f]{7,}\b")           # "LOCAL master = 8e2b992"
_ACTION_PREFIX = re.compile(r"^action:\s", re.IGNORECASE)
_STATUS_SHARD = re.compile(r"^P\d+[ _]")                   # "P3 SURFACE POLISH"
_SENTENCE_TOKENS = 7                                       # task/status phrases

# A relation separator captured into an entity name (extraction artifact), e.g.
# "memory_recall<->recall.py". Longest arrow first so "<->" isn't split as "->".
_ARROW = re.compile(r"<-+>|↔|->|→")


def _is_concat_artifact(name: str) -> bool:
    """True if ``name`` is two names joined by a relation arrow (<->, ->, ↔, →) —
    a captured-relation extraction artifact. Requires non-empty text on both
    sides, so a name merely starting/ending with an arrow char is not caught."""
    parts = [p.strip() for p in _ARROW.split(str(name))]
    return len(parts) >= 2 and sum(1 for p in parts if p) >= 2


def junk_name_reason(name: str) -> str | None:
    """Write-time entity-name gate: the reason ``name`` must never become a
    graph entity (``concat-artifact`` / ``bare-number`` / ``status-word`` /
    ``empty``), else None.

    Deliberately narrower than :func:`junk_entities` — short names are
    legitimate at write time ("Go", "uv") and stay review-queue material
    judged by degree. This gate exists so the dream's ungated 2B extractor
    can't plant the junk classes the review queue keeps having to clean
    (2026-07-02 review, H3: ingestion was detection-side patched only).
    """
    d = str(name).strip()
    if not d:
        return "empty"
    if _is_concat_artifact(d):
        return "concat-artifact"
    if _BARE_NUMBER.match(d):
        return "bare-number"
    if d.lower() in _JUNK_STOPWORDS:
        return "status-word"
    if _BARE_DATE.match(d):
        return "bare-date"
    if _COUNT_PREFIX.match(d):
        return "count-prefix"
    if _DUMP_FILE.search(d):
        return "dump-file"
    if _IMAGE_TAG.search(d):
        return "image-tag"
    if _COMMAND_STRING.match(d):
        return "command-string"
    if _HASH_STATUS.search(d):
        return "hash-status"
    if _ACTION_PREFIX.match(d):
        return "action-prefix"
    if _STATUS_SHARD.match(d):
        return "status-shard"
    if len(d.split()) >= _SENTENCE_TOKENS:
        return "sentence"
    return None


def _name_contains(a: str, b: str) -> str | None:
    """A reason if one display asserts identity with the other, else None.
    Guards: an A<->B concat artifact is never a merge endpoint (it's junk), and
    the smaller token set must have >=2 tokens — single-token containment (a
    generic word that is a subset of countless names) is too weak to auto-merge."""
    if _is_concat_artifact(a) or _is_concat_artifact(b):
        return None
    ta, tb = _full_token_set(a), _full_token_set(b)
    if min(len(ta), len(tb)) < 2:
        return None
    if ta and tb and (ta <= tb or tb <= ta):
        return "token-subset"
    na, nb = norm_name(a), norm_name(b)
    if na and nb and (na in nb or nb in na):
        return "substring"
    return None


def partition_candidates(pairs: list[dict], entities: list[dict], edges: list[dict], *,
                         merge_min_similarity: float = 0.90,
                         ) -> tuple[list[dict], list[dict]]:
    """Split near-pairs into MERGE candidates (high sim + name-containment) and the
    remaining LINK candidates. Merge fold direction: lower-degree into higher-degree
    (tie folds higher id into lower id), matching exact_duplicate_pairs."""
    deg = degree_counts(edges)
    disp = _disp(entities)
    merges: list[dict] = []
    links: list[dict] = []
    for p in pairs:
        reason = (_name_contains(p["src"], p["dst"])
                  if float(p.get("similarity", 0.0)) >= merge_min_similarity else None)
        if reason is None:
            links.append(p)
            continue
        u, v = p["src_id"], p["dst_id"]
        du, dv = deg.get(u, 0), deg.get(v, 0)
        if du > dv or (du == dv and u < v):
            into, frm = u, v
        else:
            into, frm = v, u
        merges.append({"from_id": frm, "into_id": into,
                       "from": disp.get(frm, str(frm)), "into": disp.get(into, str(into)),
                       "similarity": p["similarity"], "reason": reason})
    return merges, links


def junk_entities(entities: list[dict], edges: list[dict], *,
                  max_degree: int = 1) -> list[dict]:
    """Over-extraction artifacts: bare numbers, <=2-char displays, or status-words —
    only when weakly connected (degree <= max_degree). Proposal-only; never deletes."""
    deg = degree_counts(edges)
    out: list[dict] = []
    for e in entities:
        d = str(e["display"]).strip()
        if _is_concat_artifact(d):
            out.append({"entity_id": e["id"], "display": e["display"],
                        "reason": "concat-artifact"})   # degree-agnostic
            continue
        if deg.get(e["id"], 0) > max_degree:
            continue
        if _BARE_NUMBER.match(d):
            reason = "bare-number"
        elif len(d) <= 2:
            reason = "too-short"
        elif d.lower() in _JUNK_STOPWORDS:
            reason = "status-word"
        else:
            continue
        out.append({"entity_id": e["id"], "display": e["display"], "reason": reason})
    return out
