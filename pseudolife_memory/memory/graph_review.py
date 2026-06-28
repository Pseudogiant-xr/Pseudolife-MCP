# pseudolife_memory/memory/graph_review.py
"""Pure graph-hygiene analyzer (Atlas Stage 3). DB-free + unit-testable like
graph_insight.py: the service supplies edges/entities/entity_sources_map; this
returns review findings the Atlas workbench surfaces. READ-ONLY — no mutation."""
from __future__ import annotations

import re

from pseudolife_memory.graph import degree_counts

_DUBIOUS_CONF = 0.6
_TEST_PATTERNS = re.compile(
    r"(payments?[-/]|pl-healthcheck|deploy-smoke|smoke[-_]?test|noise[ _-]?agent)",
    re.I)


def _disp(entities):
    return {e["id"]: e["display"] for e in entities}


def _token_set(name):
    return {t for t in re.split(r"[^a-z0-9]+", str(name).lower())
            if len(t) > 2 or any(c.isdigit() for c in t)}


def duplicate_candidates(entities, *, min_jaccard=0.6):
    toks = [(e["id"], e["display"], _token_set(e["display"])) for e in entities]
    out = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i][2], toks[j][2]
            if not a or not b:
                continue
            jac = len(a & b) / len(a | b)
            if jac >= min_jaccard:
                out.append({"type": "duplicate", "severity": "warn",
                            "label": f"{toks[i][1]} ↔ {toks[j][1]}",
                            "entities": [toks[i][1], toks[j][1]],
                            "score": round(jac, 3), "action": "merge"})
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
             "confidence": e.get("confidence")}
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


def unattributed(entities, entity_sources_map):
    names = sorted(e["display"] for e in entities if e["id"] not in entity_sources_map)
    if not names:
        return []
    return [{"type": "unattributed", "severity": "info",
             "label": f"{len(names)} entities with no project",
             "entities": names, "action": "assign"}]


def review(edges, entities, entity_sources_map):
    findings = (duplicate_candidates(entities) + test_artifacts(entities)
                + dubious_edges(edges, entities) + orphans(edges, entities)
                + unattributed(entities, entity_sources_map))
    return {"findings": findings, "counts": {"total": len(findings)}}
