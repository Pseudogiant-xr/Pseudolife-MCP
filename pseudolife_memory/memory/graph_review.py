# pseudolife_memory/memory/graph_review.py
"""Pure graph-hygiene analyzer (Atlas Stage 3). DB-free + unit-testable like
graph_insight.py: the service supplies edges/entities/entity_sources_map; this
returns review findings the Atlas workbench surfaces. READ-ONLY — no mutation."""
from __future__ import annotations

import re

from pseudolife_memory.graph import degree_counts

_DUBIOUS_CONF = 0.6
_TAG_AMBIGUOUS_CONF = 0.5
_TEST_PATTERNS = re.compile(
    r"(payments?[-/]|pl-healthcheck|deploy-smoke|smoke[-_]?test|noise[ _-]?agent)",
    re.I)


def classify_edge(edge: dict, *, proposed: bool = False) -> str:
    """Three-way provenance tag for an edge (graphify-style).

    EXTRACTED — asserted by a human or a confirming action (origin
    user/action); never demoted by low confidence. AMBIGUOUS — a proposal
    awaiting review, or confidence below 0.5. INFERRED — everything else
    (agent/dream extraction at working confidence)."""
    origin = (edge.get("origin") or "").lower()
    if origin in ("user", "action"):
        return "EXTRACTED"
    conf = edge.get("confidence")
    if proposed or (conf is not None and float(conf) < _TAG_AMBIGUOUS_CONF):
        return "AMBIGUOUS"
    return "INFERRED"


def near_duplicate_names(name, existing, *, min_jaccard=0.6,
                         dismissed=frozenset()):
    """Score a candidate name against existing entities (write-time dedup).

    ``existing`` rows are ``{"id", "canonical", "display", "aliases"}``;
    the best token-Jaccard across canonical/display/aliases wins per entity.
    ``dismissed`` holds sorted (canonical_a, canonical_b) pairs — human-
    settled distinct pairs never match. ``min_jaccard <= 0`` disables.
    Returns ``[{"entity_id", "display", "score"}]`` sorted by score desc."""
    if min_jaccard is None or min_jaccard <= 0:
        return []
    from pseudolife_memory.graph import norm_name
    from pseudolife_memory.memory.graph_consolidation import variant_conflict
    cand_tokens = _token_set(name)
    if not cand_tokens:
        return []
    cand_canon = norm_name(name)
    out = []
    for e in existing:
        if tuple(sorted((cand_canon, e.get("canonical") or ""))) in dismissed:
            continue
        if (variant_conflict(name, e.get("display") or "")
                or variant_conflict(name, e.get("canonical") or "")):
            continue          # size/quant/version mismatch: never a merge
        best = 0.0
        for variant in [e.get("canonical"), e.get("display"),
                        *(e.get("aliases") or [])]:
            toks = _token_set(variant or "")
            if not toks:
                continue
            jac = len(cand_tokens & toks) / len(cand_tokens | toks)
            if jac > best:
                best = jac
        if best >= min_jaccard:
            out.append({"entity_id": e["id"], "display": e.get("display"),
                        "score": round(best, 3)})
    out.sort(key=lambda m: -m["score"])
    return out


def _disp(entities):
    return {e["id"]: e["display"] for e in entities}


def _token_set(name):
    return {t for t in re.split(r"[^a-z0-9]+", str(name).lower())
            if len(t) > 2 or any(c.isdigit() for c in t)}


_CODE_FILE_RE = re.compile(
    r"\.(py|js|mjs|ts|tsx|jsx|ps1|sh|bat|rb|go|rs|java|c|cc|cpp|h|hpp)$", re.I)


def _stem_key(name):
    """Fold a bare name for stem comparison: case, separators and the
    directory prefix all ignored."""
    return re.sub(r"[^a-z0-9]+", "", str(name).rsplit("/", 1)[-1].lower())


def file_concept_split(a, b):
    """``(file, concept)`` when one name is a SOURCE FILE and the other is its
    own bare stem — ``("band.py", "band")``, ``("evals/dg_shim.py", "dg_shim")``.

    Such a pair is neither a duplicate nor unrelated. The concept usually has
    identity the file does not: an independent runtime ("dream" runs-on the
    host shim, "band" stores-data-in postgres — both FALSE of the module), or
    several implementing files (`backup.sh` AND `ops/backup.ps1` realize
    "Backup", so no single merge is even well-defined). Merging asserts false
    things about the file; dismissing throws away a real relationship. Callers
    offer ``relate`` instead and let the reviewer pick the edge — the
    suggestion is only a default (2026-07-26 curation review).

    Returns ``None`` unless exactly one side is code and the stems match, so
    two source files (``test_shim.py`` / ``tests/test_shim.py``) and non-code
    pairs (``README.md`` / ``README``) keep the ordinary merge action.
    """
    for f, c in ((a, b), (b, a)):
        if not _CODE_FILE_RE.search(str(f)) or _CODE_FILE_RE.search(str(c)):
            continue
        if _stem_key(_CODE_FILE_RE.sub("", str(f))) == _stem_key(c):
            return f, c
    return None


def duplicate_candidates(entities, *, min_jaccard=0.6, dismissed=frozenset()):
    """``dismissed`` holds human-settled false positives as ordered
    (canonical_a, canonical_b) tuples — those pairs never re-flag.

    A file/concept pair (see ``file_concept_split``) carries ``action:
    "relate"`` and a ``suggested_relation`` instead of ``"merge"``, with the
    file listed first so the edge reads ``<file> implements <concept>``."""
    toks = [(e["id"], e["display"], _token_set(e["display"]), e.get("canonical"))
            for e in entities]
    out = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i][2], toks[j][2]
            if not a or not b:
                continue
            if tuple(sorted((toks[i][3] or "", toks[j][3] or ""))) in dismissed:
                continue
            jac = len(a & b) / len(a | b)
            if jac < min_jaccard:
                continue
            pair = file_concept_split(toks[i][1], toks[j][1])
            names = list(pair) if pair else [toks[i][1], toks[j][1]]
            found = {"type": "duplicate", "severity": "warn",
                     "label": f"{names[0]} ↔ {names[1]}", "entities": names,
                     "score": round(jac, 3),
                     "action": "relate" if pair else "merge"}
            if pair:
                found["suggested_relation"] = "implements"
            out.append(found)
    out.sort(key=lambda f: -f["score"])
    return out


def orphans(edges, entities, *, max_degree=1):
    deg = degree_counts(edges)
    names = sorted(e["display"] for e in entities if deg.get(e["id"], 0) <= max_degree)
    if not names:
        return []
    return [{"type": "orphan", "severity": "info",
             "label": f"{len(names)} weakly-connected entities",
             "entities": names, "action": "review"}]


def dubious_edges(edges, entities, *, conf=_DUBIOUS_CONF):
    disp = _disp(entities)
    rows = [{"src": disp.get(e["src_id"], str(e["src_id"])),
             "relation": e.get("relation", ""),
             "dst": disp.get(e["dst_id"], str(e["dst_id"])),
             "confidence": e.get("confidence"),
             "tag": "AMBIGUOUS"}
            for e in edges
            if e.get("origin") == "agent" and (e.get("confidence") or 1.0) <= conf]
    if not rows:
        return []
    return [{"type": "dubious_edge", "severity": "warn",
             "label": f"{len(rows)} low-confidence / type-suspect edges",
             "edges": rows, "action": "prune"}]


def test_artifacts(entities):
    names = sorted(e["display"] for e in entities if _TEST_PATTERNS.search(e["display"]))
    if not names:
        return []
    return [{"type": "test_artifact", "severity": "warn",
             "label": f"{len(names)} test/smoke artifacts",
             "entities": names, "action": "delete"}]


_LESSON_RELATIONS = frozenset({"prefers", "avoids"})


def unattributed(entities, entity_sources_map, edges=(),
                 lesson_entity_ids=frozenset()):
    """Entities whose every edge is a lesson relation (prefers/avoids) are
    lesson-minted task/approach nodes — the mention-scan can never attribute
    them, so they are excluded rather than flagged forever.
    ``lesson_entity_ids`` covers the residual tail: entities still referenced
    by lessons.entity_id/object_entity_id whose lesson edges were pruned
    carry ZERO edges, so the edge signal alone can't identify them."""
    rels: dict = {}
    for ed in edges:
        for eid in (ed["src_id"], ed["dst_id"]):
            rels.setdefault(eid, set()).add(ed.get("relation", ""))
    lesson_only = {eid for eid, rs in rels.items() if rs <= _LESSON_RELATIONS}
    names = sorted(e["display"] for e in entities
                   if e["id"] not in entity_sources_map
                   and e["id"] not in lesson_only
                   and e["id"] not in lesson_entity_ids)
    if not names:
        return []
    return [{"type": "unattributed", "severity": "info",
             "label": f"{len(names)} entities with no project",
             "entities": names, "action": "assign"}]


def proposed_links(proposals):
    if not proposals:
        return []
    links = [{"id": p.get("id"), "src": p["src"], "relation": p["relation"],
              "dst": p["dst"], "confidence": p.get("confidence"),
              "similarity": p.get("similarity"), "rationale": p.get("rationale"),
              "tag": classify_edge(p, proposed=True)}
             for p in proposals]
    return [{"type": "proposed_link", "severity": "info", "action": "review",
             "label": f"{len(links)} proposed cross-session links",
             "links": links}]


def merge_candidates(entity_proposals):
    rows = [p for p in (entity_proposals or []) if p.get("kind") == "merge"]
    if not rows:
        return []
    merges = [{"from": p["entity"], "into": p["into"], "similarity": p.get("score"),
               "reason": p.get("reason"), "id": p["id"]} for p in rows]
    return [{"type": "merge_candidate", "severity": "warn", "action": "merge",
             "label": f"{len(merges)} near-duplicate entity merges", "merges": merges}]


def junk_candidates(entity_proposals):
    rows = [p for p in (entity_proposals or []) if p.get("kind") == "junk"]
    if not rows:
        return []
    items = [{"entity": p["entity"], "reason": p.get("reason"), "id": p["id"]} for p in rows]
    return [{"type": "junk_candidate", "severity": "warn", "action": "delete",
             "label": f"{len(items)} junk entities to prune", "entities": items}]


def review(edges, entities, entity_sources_map, proposals=None, entity_proposals=None,
           dismissed_pairs=None, lesson_entity_ids=None):
    findings = (duplicate_candidates(entities, dismissed=dismissed_pairs or frozenset())
                + test_artifacts(entities)
                + dubious_edges(edges, entities) + orphans(edges, entities)
                + unattributed(entities, entity_sources_map, edges,
                               lesson_entity_ids or frozenset())
                + proposed_links(proposals or [])
                + merge_candidates(entity_proposals or [])
                + junk_candidates(entity_proposals or []))
    return {"findings": findings, "counts": {"total": len(findings)}}
