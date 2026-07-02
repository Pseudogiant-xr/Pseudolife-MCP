"""Canned fixtures + a fake ``MemoryService`` for offline UI development.

The real daemon can't run a second ``MemoryService`` against the live Postgres
(schema DDL takes a lock), so all visual QA of the console runs against this
fixture service via :mod:`pseudolife_memory.web.devserver`. The data is
deliberately self-referential — PseudoLife's memory of building itself — so the
console looks realistic in screenshots. Shapes mirror the real service methods
(verified against ``cms.stats`` / the cortex/world/lessons dumps).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pseudolife_memory.utils.config import AppConfig

_NOW = time.time()
_H = 3600.0
_D = 86400.0


def _age(seconds: float) -> str:
    d = seconds / _D
    if d >= 1:
        return f"{int(d)} day{'s' if int(d) != 1 else ''} ago"
    h = seconds / _H
    if h >= 1:
        return f"{int(h)} hour{'s' if int(h) != 1 else ''} ago"
    return f"{int(seconds / 60)} min ago"


# ── canonical facts (cortex) ────────────────────────────────────────────────
_FACTS = [
    ("pseudolife-mcp", "status", "active", "agent", 1.0, 2 * _H, False),
    ("pseudolife-mcp", "transport", "streamable HTTP at /mcp", "agent", 0.95, 6 * _D, False),
    ("pseudolife-mcp", "schema version", "11", "action", 1.0, 3 * _H, False),
    ("pseudolife-mcp", "default port", "8765", "user", 1.0, 9 * _D, False),
    ("postgres", "role", "pseudolife", "action", 0.9, 12 * _D, False),
    ("postgres", "host port", "5433", "user", 1.0, 12 * _D, False),
    ("docker-desktop", "wsl memory cap", "via %USERPROFILE%/.wslconfig", "user", 0.8, 20 * _D, False),
    ("project", "language", "python", "user", 1.0, 30 * _D, False),
    ("project", "test count", "384 passing", "action", 0.9, 1 * _D, False),
    ("dream extractor", "model", "Gemma 4 E2B (Q4_K_M)", "agent", 0.85, 4 * _D, True),
    ("memory_recall", "driver", "mechanical (query-first)", "agent", 0.9, 8 * _H, False),
    ("cortex", "writer policy", "single-writer (dream + fact_set)", "agent", 0.95, 5 * _D, False),
]

# ── world cortex (cited external facts) ─────────────────────────────────────
_WORLD = [
    ("anthropic", "latest-model", "Claude Opus 4.8", "https://anthropic.com/news",
     "Opus 4.8 is the most capable model in the Claude 4 family.", "volatile", 0.82, False, 5 * _D),
    ("zep", "headline-feature", "temporal knowledge graph (Graphiti)",
     "https://getzep.com", "Zep stores memory as a temporal knowledge graph tracking fact validity over time.",
     "slow", 0.78, False, 18 * _D),
    ("mempalace", "longmemeval-r5", "96.6%", "https://rohitraj.tech",
     "MemPalace scored 96.6% R@5 on LongMemEval with zero API calls (fully local).",
     "volatile", 0.4, True, 70 * _D),
]

# ── procedural lessons ──────────────────────────────────────────────────────
_LESSONS = [
    ("deploy daemon to host", "approach", "Rebuild only the daemon: build pseudolife-daemon + up -d --no-deps; postgres/extractor untouched.",
     "docker compose up -d --no-deps", "+", "success", 0.9),
    ("deploy daemon to host", "pitfall", "Never docker compose down -v — it can wipe the bank volume.",
     "down -v", "-", "failure", 0.95),
    ("smoke-test live tools", "tool-choice", "Use the HTTP MCP client, not a 2nd MemoryService (LockNotAvailable on schema DDL).",
     "second MemoryService", "-", "failure", 0.9),
    ("tune recall seeding", "approach", "Seed entities word-matched in the query only; fall back to hits only when the query names none.",
     "query-first seeding", "+", "success", 0.88),
]

# ── associative stream ──────────────────────────────────────────────────────
_BANDS = ["working", "micro", "instant", "fast", "medium", "slow", "archival", "forever"]
_STREAM = [
    ("Building a web frontend (Cortex Console) for PseudoLife-MCP — overnight /loop build.", "pseudolife", "instant", ["frontend", "web-ui"], 6 * 60),
    ("memory_recall mechanical seeder is now query-first: seed precision 1.0 vs 0.262.", "pseudolife", "instant", ["memory_recall", "milestone"], 8 * _H),
    ("GAM #2 graph-population is live; dream writes origin=agent relation edges on sweeps.", "pseudolife", "fast", ["gam", "graph"], 1 * _D),
    ("Procedural/outcome memory (lessons, schema v10) merged to master and pushed.", "pseudolife", "medium", ["lessons", "merged"], 2 * _D),
    ("World-knowledge cortex Phase 3 complete: sourced facts + age-decayed freshness.", "pseudolife", "slow", ["world-knowledge", "complete"], 11 * _D),
    ("Postgres bank wipe incident — never down -v; external volumes + backup.ps1 added.", "pseudolife", "forever", ["incident", "guard"], 13 * _D),
    ("Evaluated Perplexity Brain vs PseudoLife; the one real gap was procedural memory.", "pseudolife", "slow", ["research", "competitive"], 4 * _D),
    ("Cosine spine (v0.5): bands are plain cosine stores; novelty surprise gate.", "pseudolife", "archival", ["v0.5", "cosine"], 30 * _D),
]


def _fact_dict(t):
    e, a, v, origin, conf, age_s, contested = t
    d = {"entity": e, "attribute": a, "value": v, "origin": origin,
         "confidence": conf, "age": _age(age_s), "tx_time": _NOW - age_s,
         "writer_id": "claude-code", "contested": contested}
    if contested:
        d["contender_value"] = "Gemma 4 E4B"
        d["contender_origin"] = "agent"
    return d


def _world_dict(t):
    e, a, v, url, quote, fresh, eff, stale, age_s = t
    return {"entity": e, "attribute": a, "value": v, "source_url": url,
            "source_quote": quote, "freshness_class": fresh,
            "effective_confidence": eff, "stale": stale, "age": _age(age_s)}


def _lesson_dict(t):
    task, aspect, lesson, about, pol, outcome, conf = t
    return {"task": task, "aspect": aspect, "lesson": lesson, "about": about,
            "polarity": pol, "outcome": outcome, "confidence": conf}


def _stream_dict(t, idx):
    text, source, band, tags, age_s = t
    return {"id": 1000 + idx, "text": text, "source": source, "bank": band, "tags": tags,
            "timestamp": _NOW - age_s, "age": _age(age_s),
            "score": round(0.9 - idx * 0.06, 3), "superseded": idx == 5,
            "episode_id": "ep-build" if idx == 0 else None,
            "access_count": 14 - idx}


class FixtureService:
    """Implements the subset of MemoryService the console routes call."""

    def __init__(self) -> None:
        self.config = AppConfig()
        self.config.memory.recency_base_half_life_s = 86400.0
        self.data_dir = Path(__file__).parent / ".devdata"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_url = "postgresql://pseudolife@localhost/pseudolife_memory"
        self._writer_id = "cortex-console-dev"
        self._persist_errors = 0

    # health / stats
    def stats(self) -> dict[str, Any]:
        caps = [200, 400, 2000, 5000, 8000, 12000, 20000, 100000]
        sizes = [3, 11, 240, 612, 410, 380, 120, 64]
        hits = [0.21, 0.34, 0.61, 0.52, 0.40, 0.28, 0.12, 0.41]
        bands = [{"name": n, "size": s, "capacity": c, "update_interval": 1,
                  "retention_policy": "balanced", "hit_rate": h,
                  "hit_count": int(h * 100)}
                 for n, s, c, h in zip(_BANDS, sizes, caps, hits)]
        return {"bands": bands, "preset": "continuum",
                "total_memories": sum(sizes), "interaction_count": 1840,
                "retrieval_queries": 412, "weights_reset": False,
                "reference": {"count": 3}}

    # cortex
    def cortex_dump(self) -> dict[str, Any]:
        return {"count": len(_FACTS), "entries": [_fact_dict(t) for t in _FACTS]}

    def cortex_lookup(self, entity, attribute):
        for t in _FACTS:
            if t[0] == entity and t[1] == attribute:
                return _fact_dict(t)
        return None

    def cortex_contenders(self, entity, attribute):
        rec = self.cortex_lookup(entity, attribute)
        if rec and rec.get("contested"):
            return {"contenders": [{"value": rec["contender_value"],
                                    "origin": rec["contender_origin"],
                                    "confidence": 0.7}]}
        return {"contenders": []}

    def cortex_search(self, query, top_k=5, min_score=0.0):
        q = (query or "").lower()
        hits = [_fact_dict(t) for t in _FACTS
                if q in (t[0] + " " + t[1] + " " + t[2]).lower()]
        for h in hits:
            h["score"] = 0.6
        return {"entries": hits[:top_k]}

    def cortex_write(self, entity, attribute, value, confidence=0.8, support="agent"):
        return {"action": "inserted", "entity": entity, "attribute": attribute,
                "value": value, "origin": support, "confidence": confidence}

    def cortex_resolve(self, entity, attribute, accept):
        return {"resolved": True, "accepted": accept,
                "action": "adopted" if accept else "discarded"}

    def cortex_forget(self, entity, attribute=None):
        return {"removed": 1, "entity": entity, "attribute": attribute}

    def history(self, entity, attribute):
        cur = self.cortex_lookup(entity, attribute)
        val = cur["value"] if cur else "(unknown)"
        return {"entity": entity, "attribute": attribute, "count": 2,
                "versions": [
                    {"value": "(earlier value)", "status": "superseded",
                     "writer_id": "claude-code", "session_id": "s-001",
                     "tx_time": _NOW - 9 * _D, "valid_time": _NOW - 9 * _D,
                     "age": _age(9 * _D)},
                    {"value": val, "status": "current", "writer_id": "claude-code",
                     "session_id": "s-014", "tx_time": _NOW - 2 * _H,
                     "valid_time": _NOW - 2 * _H, "age": _age(2 * _H)},
                ]}

    # world / lessons
    def world_dump(self):
        return {"count": len(_WORLD), "entries": [_world_dict(t) for t in _WORLD]}

    def lessons_dump(self, limit=120):
        return {"count": len(_LESSONS), "entries": [_lesson_dict(t) for t in _LESSONS][:limit]}

    # episodes
    def episode_list(self, limit=20, include_open=True):
        eps = [
            {"id": "ep-build", "title": "Cortex Console build", "started_at": _NOW - 3 * _H,
             "ended_at": None, "hint": "Overnight web frontend", "entry_count": 12},
            {"id": "ep-recall", "title": "Recall seed tuning", "started_at": _NOW - 9 * _H,
             "ended_at": _NOW - 8 * _H, "hint": "query-first seeding", "entry_count": 8},
            {"id": "ep-lessons", "title": "Procedural memory", "started_at": _NOW - 3 * _D,
             "ended_at": _NOW - 2 * _D, "hint": "schema v10", "entry_count": 21},
        ]
        return {"count": len(eps), "episodes": eps[:limit]}

    def episode_summary(self, id):
        return {"found": True, "id": id, "title": "Cortex Console build",
                "started_at": _NOW - 3 * _H, "ended_at": None, "entry_count": 12,
                "tag_distribution": [{"tag": "frontend", "count": 7}, {"tag": "web-ui", "count": 5}],
                "source_distribution": [{"source": "pseudolife", "count": 12}],
                "recent_entries": [_stream_dict(_STREAM[0], 0)]}

    # stream / search
    def recent(self, n=10, sources=None, episodes=None, tags=None):
        items = [_stream_dict(t, i) for i, t in enumerate(_STREAM)]
        if sources:
            items = [x for x in items if x["source"] in sources]
        if tags:
            items = [x for x in items if set(x["tags"]) & set(tags)]
        return {"count": len(items[:n]), "entries": items[:n]}

    def search(self, query, top_k=12, sources=None, bands=None, tags=None,
               min_score=None, disable_recency_boost=False, rerank=None, bm25=None):
        q = (query or "").lower()
        items = [_stream_dict(t, i) for i, t in enumerate(_STREAM)
                 if not q or q in t[0].lower() or any(q in tg for tg in t[3])]
        return {"query": query, "count": len(items[:top_k]),
                "entries": items[:top_k], "cortex": [], "low_confidence": not items}

    def trace(self, query, top_k=8, sources=None, bands=None, rerank=None, bm25=None,
              episodes=None, tags=None):
        res = self.search(query, top_k=top_k)
        # Shapes mirror cms.retrieve_with_trace — pinned by
        # tests/test_fixture_contract.py (the old invented keys shipped a
        # broken drawer that QA'd green against these fixtures).
        res["trace"] = {
            "config": {"min_score": 0.25, "rerank": bool(rerank), "bm25": bool(bm25)},
            "tiers": [{"name": b["name"], "depth": i, "filtered_out": False,
                       "candidates": [
                           {"text_preview": e["text"][:80], "source": e["source"],
                            "raw_score": e["score"], "superseded": False,
                            "kept": j == 0, "drop_reason": None}
                           for j, e in enumerate(res["entries"][:2])]}
                      for i, b in enumerate(self.stats()["bands"])],
            "final_topk": [{"text_preview": e["text"][:120], "score": e["score"],
                            "source": e["source"], "bank": e["bank"]}
                           for e in res["entries"]],
        }
        return res

    def recall(self, query, hops=3, top_k=5):
        return {"seeds": ["pseudolife-mcp"], "low_confidence": False, "iterations": 2,
                "entities": [{"entity": "pseudolife-mcp", "facts": [_fact_dict(_FACTS[0])]},
                             {"entity": "postgres", "facts": [_fact_dict(_FACTS[4])]},
                             {"entity": "docker-desktop", "facts": []}],
                "edges": [{"src": "pseudolife-mcp", "relation": "stores-data-in", "dst": "postgres", "derived": False},
                          {"src": "postgres", "relation": "runs-on", "dst": "docker-desktop", "derived": False},
                          {"src": "docker-desktop", "relation": "hosts", "dst": "postgres", "derived": True, "via": ["inverse:runs-on"]}],
                "paths": [["pseudolife-mcp", "postgres", "docker-desktop"]],
                "texts": [_STREAM[2][0]]}

    # facets
    def list_sources(self):
        srcs = [{"source": "pseudolife", "count": 1804}, {"source": "claude", "count": 240},
                {"source": "correction", "count": 31}, {"source": "consolidation", "count": 12}]
        return {"sources": srcs, "total": sum(s["count"] for s in srcs)}

    def list_tags(self):
        tags = [{"tag": "pseudolife-mcp", "count": 412}, {"tag": "milestone", "count": 88},
                {"tag": "deploy", "count": 54}, {"tag": "frontend", "count": 7},
                {"tag": "graph", "count": 33}, {"tag": "research", "count": 29}]
        return {"tags": tags, "total": sum(t["count"] for t in tags)}

    # graph
    def graph_neighborhood(self, entity, depth=1, include_facts=True, to=None,
                           scope=None, max_nodes=None):
        entity = entity or "pseudolife-mcp"
        # A deliberately dense neighbourhood (~20 nodes) so the visualizer's
        # spread / zoom / fit behaviour can be exercised like a real bank.
        def n(e, et, facts=None):
            return {"entity": e, "canonical": e, "etype": et, "aliases": [], "facts": facts or [],
                    "community": sum(ord(c) for c in e) % 4}  # stable 4-community split for QA colouring
        nodes = [
            n("pseudolife-mcp", "service",
              [{"attribute": "status", "value": "active", "origin": "agent", "confidence": 1.0},
               {"attribute": "schema version", "value": "11", "origin": "action", "confidence": 1.0}]),
            n("Cortex Console web frontend", "concept",
              [{"attribute": "status", "value": "first pass complete", "origin": "agent", "confidence": 0.9}]),
            n("postgres", "database", [{"attribute": "host port", "value": "5433", "origin": "user", "confidence": 1.0}]),
            n("docker-desktop", "host"), n("extractor", "service",
              [{"attribute": "model", "value": "Gemma 4 E2B", "origin": "agent", "confidence": 0.85}]),
            n("chromadb", "database"), n("claude-code", "person"),
            n("pseudolife-daemon", "service"), n("memory_recall", "concept"),
            n("memory_trace", "concept"), n("memory_history/HLC", "concept"),
            n("8-band MIRAS continuum viz", "concept"), n("Observatory", "concept"),
            n("Cortex", "concept"), n("World", "concept"), n("Lessons", "concept"),
            n("Stream", "concept"), n("Graph", "concept"), n("Episodes", "concept"),
            n("Console", "concept"), n("Auth flow", "concept"),
            n("config.py dataclasses", "concept"), n("<data_dir>/config.yaml", "concept"),
            n("tool run_in_background", "concept"), n("docker compose", "concept"),
        ]
        hub = "Cortex Console web frontend"
        tabs = ["Observatory", "Cortex", "World", "Lessons", "Stream", "Graph", "Episodes", "Console"]
        edges = [
            {"src": "pseudolife-mcp", "relation": "has-frontend", "dst": hub, "derived": False, "confidence": 0.95},
            {"src": "pseudolife-mcp", "relation": "stores-data-in", "dst": "postgres", "derived": False, "confidence": 0.95},
            {"src": "pseudolife-mcp", "relation": "uses", "dst": "extractor", "derived": False, "confidence": 0.8},
            {"src": "pseudolife-mcp", "relation": "stores-data-in", "dst": "chromadb", "derived": False, "confidence": 0.7},
            {"src": "postgres", "relation": "runs-on", "dst": "docker-desktop", "derived": False, "confidence": 0.9},
            {"src": "docker-desktop", "relation": "hosts", "dst": "postgres", "derived": True, "via": ["inverse:runs-on"]},
            {"src": "pseudolife-daemon", "relation": "serves", "dst": hub, "derived": False, "confidence": 0.9},
            {"src": "claude-code", "relation": "writes-to", "dst": "pseudolife-mcp", "derived": False, "confidence": 0.85},
            {"src": "pseudolife-mcp", "relation": "exposes", "dst": "memory_recall", "derived": False, "confidence": 0.8},
            {"src": "pseudolife-mcp", "relation": "exposes", "dst": "memory_trace", "derived": False, "confidence": 0.8},
            {"src": "pseudolife-mcp", "relation": "tracks", "dst": "memory_history/HLC", "derived": False, "confidence": 0.7},
            {"src": "Observatory", "relation": "shows", "dst": "8-band MIRAS continuum viz", "derived": False, "confidence": 0.7},
            {"src": "Console", "relation": "edits", "dst": "config.py dataclasses", "derived": False, "confidence": 0.8},
            {"src": "Console", "relation": "writes", "dst": "<data_dir>/config.yaml", "derived": False, "confidence": 0.8},
            {"src": "pseudolife-daemon", "relation": "started-via", "dst": "tool run_in_background", "derived": False, "confidence": 0.6},
            {"src": "docker-desktop", "relation": "runs", "dst": "docker compose", "derived": False, "confidence": 0.7},
            {"src": "Console", "relation": "guards", "dst": "Auth flow", "derived": True, "via": ["inferred"]},
        ] + [{"src": hub, "relation": "tab", "dst": t, "derived": False, "confidence": 0.9} for t in tabs]
        # Deterministically spread demo nodes across the advertised projects so
        # every scope in graph_projects() has matching members (coherent demo).
        _FX_PROJECTS = ("pseudolife-mcp", "gw2-reshade", "hermes-infra")
        for nd in nodes:
            nd["sources"] = [_FX_PROJECTS[sum(ord(ch) for ch in nd["entity"]) % 3]]
        if scope and scope != "all":
            keep = {nd["entity"] for nd in nodes if scope in nd["sources"]}
            nodes = [nd for nd in nodes if nd["entity"] in keep]
            edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
        total_nodes, total_edges, truncated = len(nodes), len(edges), False
        if max_nodes and total_nodes > max_nodes:
            deg = {}
            for e in edges:
                deg[e["src"]] = deg.get(e["src"], 0) + 1
                deg[e["dst"]] = deg.get(e["dst"], 0) + 1
            ranked = sorted(nodes, key=lambda nd: (deg.get(nd["entity"], 0), nd["entity"]),
                            reverse=True)
            kept = {nd["entity"] for nd in ranked[:max_nodes]}
            nodes = [nd for nd in nodes if nd["entity"] in kept]
            edges = [e for e in edges if e["src"] in kept and e["dst"] in kept]
            truncated = True
        return {"found": True, "entity": entity, "depth": depth,
                "nodes": nodes, "edges": edges, "truncated": truncated,
                "total_nodes": total_nodes, "total_edges": total_edges,
                "paths": [["pseudolife-mcp", "postgres", "docker-desktop"]] if to else []}

    # graph insight
    def graph_digest(self):
        return {"available": True, "digest": {
            "computed_at": _NOW - 40 * 60,
            "communities": [
                {"id": 0, "label": "pseudolife-mcp", "size": 12, "cohesion": 0.42},
                {"id": 1, "label": "Cortex Console web frontend", "size": 9, "cohesion": 0.55},
                {"id": 2, "label": "postgres", "size": 4, "cohesion": 0.31}],
            "god_nodes": [
                {"entity_id": 1, "display": "pseudolife-mcp", "degree": 12,
                 "betweenness": 0.42},
                {"entity_id": 2, "display": "Cortex Console web frontend",
                 "degree": 10, "betweenness": 0.31},
                {"entity_id": 3, "display": "postgres", "degree": 6,
                 "betweenness": 0.12},
                {"entity_id": 4, "display": "docker-desktop", "degree": 4,
                 "betweenness": 0.05}],
            "surprises": [
                {"src": "claude-code", "dst": "pseudolife-mcp", "relation": "writes-to",
                 "confidence": 0.85, "origin": "agent", "score": 5,
                 "why": "agent-inferred; bridge between community 1 and 2"},
                {"src": "Console", "dst": "Auth flow", "relation": "guards",
                 "confidence": 0.5, "origin": "agent", "score": 3,
                 "why": "agent-inferred or low-confidence; peripheral node reaches a hub"}],
            "questions": [
                {"type": "contested_fact",
                 "question": "Which value of `model` for `dream extractor` is correct — `Gemma 4 E2B` or `Gemma 4 E4B`?",
                 "why": "Contested fact; rival from origin=agent."},
                {"type": "isolated_entity",
                 "question": "What connects `Auth flow` to the rest of the graph?",
                 "why": "1 weakly-connected entity — possible gap."}],
            "totals": {"entities": 25, "edges": 26, "communities": 3}}}

    def communities(self, community_id=None):
        comms = [{"id": 0, "label": "pseudolife-mcp", "size": 12, "cohesion": 0.42},
                 {"id": 1, "label": "Cortex Console web frontend", "size": 9, "cohesion": 0.55},
                 {"id": 2, "label": "postgres", "size": 4, "cohesion": 0.31}]
        if community_id is None:
            return {"communities": comms}
        return {"community_id": community_id,
                "members": ["pseudolife-mcp", "postgres", "docker-desktop"]}

    def graph_path(self, source, target, max_hops=8):
        return {"found": True, "source": source, "target": target, "hops": 2,
                "path": [source or "pseudolife-mcp", "postgres", target or "docker-desktop"],
                "edges": [{"src": source or "pseudolife-mcp", "relation": "stores-data-in", "dst": "postgres"},
                          {"src": "postgres", "relation": "runs-on", "dst": target or "docker-desktop"}]}

    def graph_projects(self):
        # Counts derived from the same node assignment graph_neighborhood uses,
        # so the switcher labels match the scoped subgraph sizes.
        from collections import Counter
        nodes = self.graph_neighborhood(None, scope="all")["nodes"]
        c = Counter(s for nd in nodes for s in nd.get("sources", []))
        return {"projects": [{"source": s, "entities": n} for s, n in c.most_common()]}

    def graph_review(self, scope=None):
        findings = [
            {"type": "duplicate", "severity": "warn", "action": "merge",
             "label": "Cortex Console web frontend ↔ web frontend (Cortex Console)",
             "entities": ["Cortex Console web frontend", "web frontend (Cortex Console)"]},
            # long, path-like names exercise the merge modal's button truncation
            {"type": "duplicate", "severity": "warn", "action": "merge",
             "label": "deep-dream-graph-consolidation-design.md ↔ deep-dream-graph-consolidation.md",
             "entities": ["docs/superpowers/specs/2026-06-28-deep-dream-graph-consolidation-design.md",
                          "docs/superpowers/plans/2026-06-28-deep-dream-graph-consolidation.md"]},
            {"type": "test_artifact", "severity": "warn", "action": "delete",
             "label": "2 test/smoke artifacts", "entities": ["payments-db", "pl-healthcheck-target"]},
            {"type": "dubious_edge", "severity": "warn", "action": "prune",
             "label": "12 low-confidence inferred edges",
             "edges": [{"src": f"node{i}", "relation": "related-to", "dst": f"node{i+1}",
                        "confidence": 0.55} for i in range(12)]},
            {"type": "unattributed", "severity": "info", "action": "assign",
             "label": "5 entities with no project",
             "entities": ["alpha", "beta", "gamma", "delta", "epsilon"]},
            {"type": "orphan", "severity": "info", "action": "review",
             "label": "4 weakly-connected entities",
             "entities": ["lonely-1", "lonely-2", "lonely-3", "lonely-4"]},
            {"type": "proposed_link", "severity": "info", "action": "review",
             "label": "1 proposed cross-session link",
             "links": [{"id": 1, "src": "Track A", "relation": "related-to", "dst": "Track B",
                        "confidence": 0.45, "similarity": 0.9, "rationale": "co-discussed"}]},
            {"type": "merge_candidate", "severity": "warn", "action": "merge",
             "label": "1 near-duplicate entity merges",
             "merges": [{"from": "live daemon", "into": "daemon", "similarity": 0.99,
                         "reason": "token-subset", "id": 1}]},
            {"type": "junk_candidate", "severity": "warn", "action": "delete",
             "label": "1 junk entities to prune",
             "entities": [{"entity": "2", "reason": "bare-number", "id": 2}]},
        ]
        return {"findings": findings, "counts": {"total": len(findings)}}

    def entity_provenance(self, entity, limit=20):
        return {"found": True, "entity": entity,
                "sources": [{"source": "pseudolife", "count": 12, "origin": "derived"},
                            {"source": "homelab-local-models", "count": 3, "origin": "derived"}],
                "entries": [
                    {"id": 5, "band": "forever", "source": "pseudolife", "ts": 1782200000.0,
                     "text": "the live daemon serves MCP from docker on port 8765"},
                    {"id": 9, "band": "slow", "source": "homelab-local-models", "ts": 1782100000.0,
                     "text": "daemon image pseudolife-daemon:0.2.0 rebuilt and deployed"}]}

    def chain(self, entity, limit=20):
        return {"found": True, "entity": entity, "count": 4, "events": [
            {"t": 1782100000.0, "kind": "fact_set",
             "summary": "image = pseudolife-daemon:0.1.9",
             "refs": {"attribute": "image"}},
            {"t": 1782150000.0, "kind": "entry",
             "summary": "rebuilt the daemon image after the graph rename",
             "refs": {"entry_id": 9, "episode_title": "containerization"}},
            {"t": 1782190000.0, "kind": "superseded",
             "summary": "image: pseudolife-daemon:0.1.9 superseded by "
                        "pseudolife-daemon:0.2.0",
             "refs": {"attribute": "image"}},
            {"t": 1782200000.0, "kind": "edge",
             "summary": "pseudolife-daemon —runs-on→ docker-desktop",
             "refs": {"relation": "runs-on"}},
        ]}

    def graph_assign_scope(self, entity, source):
        return {"assigned": True, "entity": entity, "source": source}

    def graph_unrelate(self, src, relation, dst):
        return {"removed": True, "src": src, "relation": relation, "dst": dst}

    def graph_bless_edge(self, src, relation, dst):
        return {"blessed": True, "src": src, "relation": relation, "dst": dst}

    def graph_dismiss_duplicate(self, a, b):
        return {"dismissed": True, "new": True, "a": a, "b": b}

    def graph_delete_entity(self, entity):
        return {"deleted": True, "entity": entity}

    def graph_merge(self, from_entity, into_entity):
        return {"merged": True, "from": from_entity, "into": into_entity}

    def graph_propose_links(self, proposals):
        return {"proposed": len(proposals), "skipped": 0}

    def graph_accept_proposal(self, proposal_id):
        return {"accepted": True, "src": "Track A", "relation": "related-to", "dst": "Track B"}

    def graph_reject_proposal(self, proposal_id):
        return {"rejected": True, "id": int(proposal_id)}

    def graph_accept_entity_merge(self, proposal_id):
        return {"accepted": True, "from": "live daemon", "into": "daemon"}

    def graph_accept_entity_junk(self, proposal_id):
        return {"accepted": True, "entity": "2"}

    def graph_reject_entity_proposal(self, proposal_id):
        return {"rejected": True, "id": int(proposal_id)}

    # engram traces / retention
    def get_entry(self, entry_id):
        return {"found": True, "entry_id": int(entry_id),
                "text": _STREAM[2][0], "source": "pseudolife",
                "reinforcements": 2, "access_count": 13,
                "consolidated_into": [
                    {"entity": "pseudolife-mcp", "attribute": "graph population",
                     "value": "GAM #2 graph-population is live"}]}

    def reinforce(self, entry_id):
        return {"reinforced": True, "entry_id": int(entry_id), "reinforcements": 3}

    # dream / consolidation
    def dream_status(self):
        return {"backlog": 14, "idle_seconds": 2100.0, "dream_cursor": _NOW - 6 * _H,
                "would_fire": True}

    def dream_run(self, extractor, limit=None):
        return {"pulled": 14, "claims": 9, "inserted": 5, "confirmed": 2,
                "contested": 1, "superseded": 1, "relations": 3, "lessons": 1,
                "cursor": _NOW}

    def consolidation_candidates(self, query=None, episode=None, sources=None,
                                 tags=None, top_k=20, min_cohesion=0.6,
                                 min_cluster_size=2, max_clusters=10):
        return {"query": query, "episode": episode, "count": 1, "clusters": [
            {"cohesion": 0.84, "seed_score": 0.9, "size": 3, "members": [
                _stream_dict(_STREAM[1], 1), _stream_dict(_STREAM[2], 2), _stream_dict(_STREAM[3], 3)]}]}

    def consolidate(self, replaces, new_text, source=None, tags=None):
        return {"superseded_count": len(replaces), "superseded_texts": replaces[:20],
                "new_memory_stored": True, "new_memory_surprise": 0.42}

    def supersede(self, old_text, new_text):
        return {"superseded_count": 1, "superseded_texts": [old_text], "new_memory_stored": True}

    def delete(self, text=None, substring=None, source=None, episode=None, tag=None):
        return {"deleted_count": 3, "deleted_texts": ["(fixture) deleted entry"]}
