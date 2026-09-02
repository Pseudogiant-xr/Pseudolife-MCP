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
                           max_fallback_mentions: int | None = None,
                           ) -> tuple[dict[int, np.ndarray], dict[int, frozenset[int]]]:
    """Per-entity context vector = L2-normalized mean of its mentioning entries'
    embeddings, plus the set of those entry ids. Trace entries are the primary
    source; entities without traces fall back to a token-mention scan. An entity is
    included only if it has >= min_mentions DISTINCT mentioning entries (with
    embeddings) — a centroid-of-one isn't a context. Returns (vectors, mentions).

    ``max_fallback_mentions`` caps the SCAN branch only: a trace-less entity
    whose token set subset-matches more entries than the cap is excluded
    outright — such a match set is a corpus centroid, not a context, and its
    vector pairs promiscuously (2026-08-16 live bank: ``pseudolife-pg``
    matched 301/695 embedded entries because ``_token_set`` drops its short
    ``pg`` token, and filed 9 cross-hub merge pairs in one pass). Trace-backed
    mentions are real evidence and are never capped."""
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
                if (max_fallback_mentions is not None
                        and len(ids) > max_fallback_mentions):
                    continue                    # corpus centroid, not a context
        valid = {i for i in ids if i in by_id}      # distinct entries with embeddings
        if len(valid) < min_mentions:
            continue
        embs = [by_id[i]["embedding"] for i in valid]
        vectors[ent["id"]] = _l2(np.mean(np.stack(embs), axis=0))
        mentions[ent["id"]] = frozenset(valid)
    return vectors, mentions


def shared_mention_entries(entries: list[dict], a_display: str, b_display: str,
                           limit: int = 4) -> list[str]:
    """Texts of the entries naming BOTH entities, in order, capped at ``limit``.

    The evidence a retype judgement needs: an untyped edge exists because two
    names co-occurred, so "what relation actually holds?" is only answerable
    from the notes where they co-occur — not from everything mentioning either
    one. Token-subset matching mirrors :func:`entity_context_vectors`'
    fallback scan."""
    wa, wb = _token_set(a_display), _token_set(b_display)
    if not wa or not wb:
        return []
    cap = max(0, int(limit))
    out: list[str] = []
    for e in entries:
        toks = _token_set(e.get("text", ""))
        if wa <= toks and wb <= toks:
            out.append(e.get("text", ""))
            if len(out) >= cap:
                break
    return out


def candidate_pairs(vectors: dict[int, np.ndarray], edges: list[dict],
                    entities: list[dict], scope_map: dict[int, list[str]],
                    mentions: dict[int, frozenset[int]], *,
                    min_similarity: float = 0.55, top_k: int = 50,
                    dismissed: set[tuple[str, str]] | None = None,
                    max_support_overlap: float = 1.0,
                    pending_pairs: set[frozenset[int]] | None = None,
                    excluded_ids: set[int] | None = None) -> list[dict]:
    """Unlinked, scope-coherent, semantically-near entity pairs — the link
    candidates. Drops pairs that already have an edge (either direction), exact
    duplicates (a Step-A merge), have co-occurring supporting-entry sets
    (CONTAINMENT ``|shared| / min(|a|, |b|)`` >= ``max_support_overlap`` —
    when the smaller side's support sits inside the other's, the similarity
    is co-mention, not independent evidence; 1.0 still admits any pair with
    one non-shared entry per side), sit in disjoint non-empty project
    scopes, or were human-dismissed (``dismissed`` holds sorted
    canonical-name pairs from dismissed_pairs).

    ``pending_pairs`` (id frozensets) and ``excluded_ids`` drop pairs that
    already have a PENDING link proposal, and pairs touching junk-flagged
    entities. Both must be excluded HERE, before top-k — the 2026-08-12
    round-2 pass lost ~20 of 49 slots to already-proposed pairs and 6 more
    to one junk-flagged compound entity."""
    disp = _disp(entities)
    canon = {e["id"]: e["canonical"] for e in entities}
    linked = {frozenset((e["src_id"], e["dst_id"])) for e in edges}
    linked |= pending_pairs or set()
    excl = excluded_ids or set()
    dup = {frozenset(p) for p in exact_duplicate_pairs(entities, edges)}
    ids = sorted(i for i in vectors if i not in excl)
    scored: list[dict] = []
    n = len(ids)
    if n < 2:
        return scored
    # Descending-similarity scan with early exit (2026-09-01). The
    # per-pair Python loop this replaces was O(n^2) in interpreter time —
    # measured 4.2s per deep tick at the live bank's 2,070 eligible
    # entities and quadratic in entity growth — and the similarity
    # threshold alone cannot prune it: on that bank 64% of ALL pairs sit
    # at >= 0.55 (crowded embedding space), so a threshold prefilter
    # keeps 1.37M pairs. What bounds the work is the OUTPUT: only the
    # top_k survivors ship, so ranking pairs by similarity first (blocked
    # matmul + one argsort, GIL-releasing numpy) lets the Python filter
    # chain run on just the top-of-ranking prefix until top_k survive —
    # ~0.5s at live scale, and the worst case (every pair filtered)
    # degenerates to the old full scan, never worse. The REPORTED value
    # stays the same per-pair np.dot, and the scan continues through the
    # k-th survivor's rounded-similarity tie band before stopping (any
    # pair rounding strictly below it sorts after every survivor), so
    # output is identical to the loop it replaced — equivalence-pinned
    # against a verbatim copy in tests/test_graph_consolidation.py. The
    # margins absorb matmul-vs-dot accumulation differences (f32-safe).
    # Memory note: the candidate index/sim arrays are O(pairs-past-thresh)
    # at ~20 bytes each — ~27MB at the live bank's 1.37M such pairs. Runs
    # OUTSIDE the service lock (an allocation cost, never a daemon pause).
    # `block` only shapes the transient matmul slab (block x n floats),
    # not a tuned trade-off.
    mat = np.stack([vectors[i] for i in ids])
    thresh = min_similarity - 1e-3
    block = 1024
    cand: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for lo in range(0, n, block):
        sims = mat[lo:lo + block] @ mat.T          # (block, n)
        bi, bj = np.nonzero(sims >= thresh)
        keep = bj > bi + lo                        # upper triangle only
        cand.append((bi[keep] + lo, bj[keep], sims[bi[keep], bj[keep]]))
    ci = np.concatenate([c[0] for c in cand])
    cj = np.concatenate([c[1] for c in cand])
    cs = np.concatenate([c[2] for c in cand])
    stop_below: float | None = None    # raw floor once top_k survivors held
    for idx in np.argsort(-cs, kind="stable").tolist():
        if stop_below is not None and float(cs[idx]) < stop_below:
            break
        u, v = ids[int(ci[idx])], ids[int(cj[idx])]
        key = frozenset((u, v))
        if key in linked or key in dup:
            continue
        if dismissed and tuple(sorted((canon.get(u, ""), canon.get(v, "")))) in dismissed:
            continue
        mu, mv = mentions.get(u), mentions.get(v)
        # Containment, not Jaccard: when the SMALLER side's mentions sit
        # almost wholly inside the other's, its context IS the
        # co-mention — one shared note pair generated ten cross-product
        # candidates at Jaccard 0.67 on 2026-08-12, all noise. EXEMPT:
        # name-contained pairs (the MERGE-shaped class) co-occur by
        # construction — the scan fallback makes the shorter name's
        # mentions a superset of the longer's — so the drop would
        # silently disable merge-candidate discovery for them.
        if (mu and mv
                and (len(mu & mv) / min(len(mu), len(mv))
                     >= max_support_overlap)
                and not _name_contains(disp.get(u, ""), disp.get(v, ""))):
            continue                           # shared support -> co-occurrence
        su, sv = set(scope_map.get(u, [])), set(scope_map.get(v, []))
        if su and sv and not (su & sv):       # disjoint, both attributed
            continue
        sim = float(np.dot(vectors[u], vectors[v]))
        if sim < min_similarity:
            continue
        scored.append({"src_id": u, "dst_id": v, "src": disp.get(u, str(u)),
                       "dst": disp.get(v, str(v)), "similarity": round(sim, 4)})
        if stop_below is None and len(scored) >= top_k:
            # The k-th survivor (smallest raw sim so far, hence smallest
            # rounded) sets the boundary; finish its rounded tie band.
            stop_below = scored[-1]["similarity"] - 5e-5 - 1e-3
    scored.sort(key=lambda c: (-c["similarity"], c["src_id"], c["dst_id"]))
    return scored[:top_k]


# --- Store curation: cross-key near-duplicate slot pairs ----------------------

# Identifier-keyed entity names ("arxiv:2602-05665") — two records whose
# entities share the prefix but differ in the identifier denote DIFFERENT
# referents by construction, however similar their values embed (a paper
# corpus keyed by id fills the listing with same-attribute siblings
# otherwise; 15 of 20 world listings on 2026-08-05 were exactly this).
# The suffix must be one whitespace-free token so prose after a colon
# ("action: rebuild the daemon") never matches.
_ID_KEYED_ENTITY = re.compile(r"^([A-Za-z][\w.-]*):(\S+)$")

# Same-entity pairs are usually deliberate aspect siblings (approach vs
# pitfall vs correction on one task) — 13 of 13 such lesson listings on
# 2026-08-05 were siblings, not duplicates. A near-verbatim value under a
# second attribute IS the known key-mint-drift failure, so the pair is
# still listed above this stricter floor rather than exempted outright.
_SAME_ENTITY_MIN_SIMILARITY = 0.95


def _slot_entity(rec: dict, key: str) -> str:
    ent = rec.get("entity")
    return str(ent) if ent else key.split("|", 1)[0]


def _id_keyed_siblings(ea: str, eb: str) -> bool:
    ma, mb = _ID_KEYED_ENTITY.match(ea), _ID_KEYED_ENTITY.match(eb)
    return bool(ma and mb
                and ma.group(1).casefold() == mb.group(1).casefold()
                and ma.group(2).casefold() != mb.group(2).casefold())


def slot_duplicate_candidates(records: list[dict], *,
                              min_similarity: float = 0.80, top_k: int = 20,
                              dismissed: set[tuple[str, str]] | None = None,
                              same_entity_min_similarity: float = _SAME_ENTITY_MIN_SIMILARITY,
                              ) -> list[dict]:
    """Cross-key near-duplicate pairs in a slot-keyed store (lessons / world
    facts) — the store-curation REVIEW candidates. Slot supersession dedups
    only WITHIN one ``(entity, attribute)`` slot, so near-duplicates parked
    under different keys accumulate silently; this surfaces them the same way
    :func:`candidate_pairs` surfaces unlinked graph entities: cosine over the
    records' own embeddings, floor + top-k, human-dismissed pairs skipped.
    Listing-only — settling (forget / re-key / dismiss) stays with the
    reviewer; nothing is deleted here.

    Same-entity pairs (aspect siblings) are held to
    ``same_entity_min_similarity``; identifier-keyed sibling entities
    (``arxiv:X`` vs ``arxiv:Y``) are never listed.

    Each record: ``{"key": <norm slot key>, "embedding": vector | None,
    ...label fields}``. Records without embeddings (legacy rows) are skipped.
    Output pairs are ``{a_key, b_key, a, b, similarity}`` with
    ``a_key < b_key`` and the label fields (everything but key/embedding)
    carried as evidence."""
    recs = [r for r in records if r.get("embedding") is not None]
    vecs = [_l2(np.asarray(r["embedding"], dtype=np.float32).reshape(-1))
            for r in recs]

    def _label(r: dict) -> dict:
        return {k: v for k, v in r.items() if k not in ("key", "embedding")}

    scored: list[dict] = []
    for i in range(len(recs)):
        for j in range(i + 1, len(recs)):
            ka, kb = sorted((recs[i]["key"], recs[j]["key"]))
            if ka == kb:
                continue
            if dismissed and (ka, kb) in dismissed:
                continue
            ea = _slot_entity(recs[i], recs[i]["key"]).strip().casefold()
            eb = _slot_entity(recs[j], recs[j]["key"]).strip().casefold()
            if _id_keyed_siblings(ea, eb):
                continue                       # distinct identifiers, never dups
            sim = float(np.dot(vecs[i], vecs[j]))
            floor = same_entity_min_similarity if ea == eb else min_similarity
            if sim < floor:
                continue
            a, b = ((recs[i], recs[j]) if recs[i]["key"] == ka
                    else (recs[j], recs[i]))
            scored.append({"a_key": ka, "b_key": kb,
                           "a": _label(a), "b": _label(b),
                           "similarity": round(sim, 4)})
    scored.sort(key=lambda c: (-c["similarity"], c["a_key"], c["b_key"]))
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
# Trailing segment of a real filename/host, never a flattened slot attribute.
_CODE_OR_DATA_EXT = re.compile(
    r"^(py|js|mjs|ts|tsx|jsx|ps1|sh|bat|md|json|jsonl|ya?ml|toml|cfg|ini|txt|"
    r"csv|html?|sql|gz|tgz|zip|exe|dll|gguf|pt|bin|vhdx|fx|cpp|cc|h|hpp|rs|go|"
    r"rb|java|log|com|io|org|net|dev|internal|local|localdomain)$",
    re.IGNORECASE)
_IMAGE_TAG = re.compile(r":\d+\.\d+\.\d+")                 # 3-part ver; arXiv ids are 2-part
_COMMAND_STRING = re.compile(                              # cmd word + >=2 more tokens
    r"^(docker|git|python|pip|curl|pwsh|npm|pytest)\s+\S+\s+\S", re.IGNORECASE)
_HASH_STATUS = re.compile(r"=\s*[0-9a-f]{7,}\b")           # "LOCAL master = 8e2b992"
_ACTION_PREFIX = re.compile(r"^action:\s", re.IGNORECASE)
_STATUS_SHARD = re.compile(r"^P\d+[ _]")                   # "P3 SURFACE POLISH"
_SENTENCE_TOKENS = 7                                       # task/status phrases

_DECIMAL_OR_RANGE = re.compile(r"^\d+\.\d+(?:-\d+(?:\.\d+)?)?$")  # 0.8 / 0.7-0.8
_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9_-]*$")

# Variant tokens: size / quant / dotted-version markers whose DIFFERENCE means
# two names denote different artifacts (E4B vs E2B, Q4_K_M vs Q4_K_XL) even when
# every other token matches (2026-07-11 curation: 9 such merge proposals
# hand-rejected). "_"/"-" are interchangeable inside tokens (norm_name folds
# both), so custom boundaries treat any non-alphanumeric as a separator.
_VB = r"(?<![A-Za-z0-9])"
_VE = r"(?![A-Za-z0-9])"
_VARIANT_PATTERNS = (
    re.compile(_VB + r"E\d+B" + _VE, re.IGNORECASE),                  # E2B / E4B
    re.compile(_VB + r"\d+(?:\.\d+)?[MK]?B" + _VE, re.IGNORECASE),    # 26B / 4B
    re.compile(_VB + r"Q\d[_-]K(?:[_-](?:XS|S|M|L|XL))?" + _VE,       # Q4_K_XL (K required)
               re.IGNORECASE),
    re.compile(_VB + r"q\d[_-]\d" + _VE, re.IGNORECASE),              # q4_0 / Q4_0
    re.compile(_VB + r"UD[_-]Q[A-Za-z0-9_-]*" + _VE, re.IGNORECASE),  # UD-Q4_K_XL
    re.compile(r"\d+\.\d+(?:\.\d+)*"),                                # 0.2.0 / 3.6
)


def variant_tokens(name: str) -> frozenset[str]:
    """Size / quant / dotted-version markers in ``name``, casefolded with
    ``_`` folded to ``-`` so display and canonical forms compare equal."""
    out: set[str] = set()
    for pat in _VARIANT_PATTERNS:
        for m in pat.finditer(str(name)):
            out.add(m.group(0).casefold().replace("_", "-"))
    return frozenset(out)


def variant_conflict(a: str, b: str) -> bool:
    """True when BOTH names carry variant tokens and the sets differ — such a
    pair is never a merge candidate (it may still be link-related, e.g. a
    quant of a model). Absent-on-either-side never conflicts."""
    ta, tb = variant_tokens(a), variant_tokens(b)
    return bool(ta) and bool(tb) and ta != tb


# A relation separator captured into an entity name (extraction artifact), e.g.
# "memory_recall<->recall.py". Longest arrow first so "<->" isn't split as "->".
_ARROW = re.compile(r"<-+>|↔|->|→")


def _is_concat_artifact(name: str) -> bool:
    """True if ``name`` is two names joined by a relation arrow (<->, ->, ↔, →) —
    a captured-relation extraction artifact. Requires non-empty text on both
    sides, so a name merely starting/ending with an arrow char is not caught."""
    parts = [p.strip() for p in _ARROW.split(str(name))]
    return len(parts) >= 2 and sum(1 for p in parts if p) >= 2


def _is_metric_reading(name: str) -> bool:
    """2-3 tokens, decimal/decimal-range tail, all other tokens lowercase — a
    metric READING ("stale 0.8"), not an entity. Any uppercase exempts
    ("CUDA Toolkit 13.1"); accepted trade-off: lowercase "python 3.12"-style
    names are blocked (version belongs in a fact, not the entity name)."""
    toks = str(name).split()
    if not 2 <= len(toks) <= 3 or not _DECIMAL_OR_RANGE.match(toks[-1]):
        return False
    return all(_LOWER_TOKEN.match(t) for t in toks[:-1])


def _split_outside_parens(s: str) -> list[str]:
    parts: list[str] = []
    depth, cur = 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


def _is_list_artifact(name: str) -> bool:
    """>=2 non-empty comma-separated segments OUTSIDE parentheses — a captured
    enumeration ("data/, ops/.env, *.pt"), not an entity. A parenthesized
    comma ("User (jdoe, a@b)") does not count."""
    return sum(1 for p in _split_outside_parens(str(name)) if p) >= 2


# A slash joins a compound only when SPACED ("codex-cli / installer"): every
# unspaced slash in the live bank is a ref, branch, path, route or repo slug
# (origin/master, fix/…, /api/graph/merge, owner/repo — 106 of 106 sampled
# on 2026-09-02, and every slash-joined junk tombstone was spaced), so the
# unspaced form is a name, not a join. "+" joins either way (pg+extractor).
_COMPOUND_SEP = re.compile(r"\s/\s|\+")
_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,4}$")


def _compound_halves(name: str) -> tuple[str, str] | None:
    """Split at the FIRST spaced ``/`` or any ``+``; both halves must be
    non-empty and carry alphanumeric content, and neither may end in a
    dot-extension (file paths are exempt: ``ops/backup.ps1``). Detection-only
    feeder — a compound is junk-PROPOSED, never write-dropped (2026-07-11:
    ``pg+extractor``)."""
    s = str(name)
    m = _COMPOUND_SEP.search(s)
    if not m:
        return None
    a, b = s[:m.start()].strip(), s[m.end():].strip()
    if not a or not b:
        return None
    if not re.search(r"[A-Za-z0-9]", a) or not re.search(r"[A-Za-z0-9]", b):
        return None
    if _FILE_EXTENSION.search(a) or _FILE_EXTENSION.search(b):
        return None
    return a, b


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
    if _is_metric_reading(d):
        return "metric-reading"
    if _is_list_artifact(d):
        return "list-artifact"
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
                         fact_counts: dict[int, int] | None = None,
                         ) -> tuple[list[dict], list[dict]]:
    """Split near-pairs into MERGE candidates (high sim + name-containment) and the
    remaining LINK candidates. Merge fold direction: the side with more evidence
    absorbs the other, ranked by ``(degree, fact_count)`` (tie folds higher id
    into lower id), matching exact_duplicate_pairs.

    ``fact_counts`` matters because degree alone let a CONTENTLESS node (no
    facts, no edges) win a 0-0 tie by id and swallow a richly-specified work
    item — "Atlas graph cleanup" repeatedly pulled in real PRs that way
    (2026-07-26). Evidence is counted as ``degree + facts`` so a fact-rich
    node is not out-ranked by one stray edge; when BOTH sides are equally
    thin the id tie-break stands, which keeps ordinary bare-vs-path proposals
    (``update.ps1`` -> ``ops/update.ps1``) intact."""
    deg = degree_counts(edges)
    facts = fact_counts or {}
    disp = _disp(entities)
    merges: list[dict] = []
    links: list[dict] = []
    for p in pairs:
        reason = (_name_contains(p["src"], p["dst"])
                  if float(p.get("similarity", 0.0)) >= merge_min_similarity
                  and not variant_conflict(p["src"], p["dst"])
                  else None)
        if reason is None:
            links.append(p)
            continue
        u, v = p["src_id"], p["dst_id"]
        ru = (deg.get(u, 0) + facts.get(u, 0), deg.get(u, 0))
        rv = (deg.get(v, 0) + facts.get(v, 0), deg.get(v, 0))
        if ru > rv or (ru == rv and u < v):
            into, frm = u, v
        else:
            into, frm = v, u
        merges.append({"from_id": frm, "into_id": into,
                       "from": disp.get(frm, str(frm)), "into": disp.get(into, str(into)),
                       "similarity": p["similarity"], "reason": reason})
    return merges, links


# A flattened slot key came through the cortex normalizer, so its tail is
# lowercase-hyphenated prose ("deferred-work", "pending slot"). A dotted
# CODE/CONFIG path keeps what the normalizer would have folded: underscores,
# capitals, a leading underscore (cortex._norm_key, lme.RAG_TOP_K,
# nomem_arm.nomem_system, memory.dream.extractor_reasoning_effort), and a
# version dot sits between digits (gpt-5.6-luna). The 2026-09-02 junk panel
# scored the class at 3/10 precision before this exclusion: every false
# positive was one of those two shapes.
_CODE_TAIL = re.compile(r"^_|[A-Z_]")


def _is_code_dotted(head: str, tail: str) -> bool:
    if _CODE_TAIL.search(tail):
        return True
    return bool(head and head[-1].isdigit() and tail[:1].isdigit())


def junk_entities(entities: list[dict], edges: list[dict], *,
                  max_degree: int = 1,
                  known_norms: frozenset[str] | None = None) -> list[dict]:
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
        if _is_list_artifact(d):
            out.append({"entity_id": e["id"], "display": e["display"],
                        "reason": "list-artifact"})  # degree-agnostic
            continue
        if known_norms:
            halves = _compound_halves(d)
            if halves:
                na, nb = norm_name(halves[0]), norm_name(halves[1])
                nd = norm_name(d)
                if (na and nb and na != nb and na != nd and nb != nd
                        and na in known_norms and nb in known_norms):
                    out.append({"entity_id": e["id"], "display": e["display"],
                                "reason": "compound-artifact"})  # degree-agnostic
                    continue
            # Slot-key artifact: `X.attribute` minted when an extractor
            # flattened a vocab key (see dream.unflatten_slot_key_claims,
            # which stops new ones). Requires the PREFIX to be a known entity,
            # so real dotted names survive — `llama.cpp` is flagged only if an
            # entity `llama` exists, `host.docker.internal` never is.
            head, dot, tail = d.rpartition(".")
            if (dot and head and tail and not _CODE_OR_DATA_EXT.match(tail)
                    and not _is_code_dotted(head, tail)):
                nh = norm_name(head)
                if nh and nh in known_norms and nh != norm_name(d):
                    out.append({"entity_id": e["id"], "display": e["display"],
                                "reason": "slot-key-artifact"})
                    continue
        if deg.get(e["id"], 0) > max_degree:
            continue
        if _BARE_NUMBER.match(d):
            reason = "bare-number"
        elif len(d) <= 2:
            reason = "too-short"
        elif d.lower() in _JUNK_STOPWORDS:
            reason = "status-word"
        elif _is_metric_reading(d):
            reason = "metric-reading"
        else:
            continue
        out.append({"entity_id": e["id"], "display": e["display"], "reason": reason})
    return out
