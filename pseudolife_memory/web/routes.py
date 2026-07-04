"""REST endpoint table for the Cortex Console.

Each read handler wraps exactly one ``MemoryService`` method; each mutation is a
small, explicitly-enumerated action. There is **no generic tool proxy** — the
console can only do what is registered here, so it can never trigger something
the design didn't intend.

Handlers are plain sync callables ``(params, body) -> dict``. The ASGI layer
(:mod:`pseudolife_memory.web.api`) runs them off the event loop in a threadpool
and serialises the returned dict to JSON. A handler may raise ``ValueError`` for
a bad request (→ 400) or any other exception (→ 500).
"""

from __future__ import annotations

from typing import Any, Callable

from pseudolife_memory.web import config_io


# ── param coercion helpers ──────────────────────────────────────────────────

def _s(params: dict, key: str, default: str | None = None) -> str | None:
    v = params.get(key)
    return v if v not in (None, "") else default


def _i(params: dict, key: str, default: int) -> int:
    v = params.get(key)
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _i_opt(params: dict, key: str) -> int | None:
    """Optional int: None when the param is absent/blank/non-numeric."""
    v = params.get(key)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _f(params: dict, key: str, default: float | None) -> float | None:
    v = params.get(key)
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _tribool(params: dict, key: str) -> bool | None:
    """None (follow config) / True / False from a query param."""
    v = params.get(key)
    if v in (None, "", "null", "auto"):
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _list(params: dict, key: str) -> list[str] | None:
    v = params.get(key)
    if not v:
        return None
    items = [s.strip() for s in str(v).split(",") if s.strip()]
    return items or None


class ConsoleRoutes:
    """Builds the method+path → handler dispatch table around a service."""

    def __init__(self, service: Any) -> None:
        self.svc = service
        self.table: dict[tuple[str, str], Callable[[dict, dict], dict]] = {}
        self._register()

    # -- dispatch ------------------------------------------------------------

    def dispatch(self, method: str, path: str, params: dict, body: dict) -> dict:
        handler = self.table.get((method, path))
        if handler is None:
            raise KeyError(path)
        return handler(params, body)

    def has(self, path: str) -> bool:
        return any(p == path for (_m, p) in self.table)

    # -- registration --------------------------------------------------------

    def _register(self) -> None:
        g = lambda p, h: self.table.__setitem__(("GET", p), h)   # noqa: E731
        p = lambda p_, h: self.table.__setitem__(("POST", p_), h)  # noqa: E731
        svc = self.svc

        # ---- health / stats / overview ----
        g("/api/health", lambda q, b: self._health())
        g("/api/stats", lambda q, b: svc.stats())
        g("/api/overview", lambda q, b: self._overview())

        # ---- cortex (canonical facts) ----
        g("/api/facts", lambda q, b: self._facts(_i(q, "limit", 500)))
        g("/api/facts/history",
          lambda q, b: svc.history(_s(q, "entity"), _s(q, "attribute")))
        g("/api/facts/contenders",
          lambda q, b: svc.cortex_contenders(_s(q, "entity"), _s(q, "attribute")))
        p("/api/facts/resolve", lambda q, b: svc.cortex_resolve(
            b["entity"], b["attribute"], bool(b.get("accept"))))
        p("/api/facts/set", lambda q, b: svc.cortex_write(
            b["entity"], b["attribute"], b["value"],
            confidence=float(b.get("confidence", 0.8)),
            support=(b.get("origin") or "agent")))
        p("/api/facts/forget", lambda q, b: svc.cortex_forget(
            b["entity"], b.get("attribute")))

        # ---- world cortex ----
        g("/api/world", lambda q, b: self._limited(svc.world_dump, _i(q, "limit", 500)))

        # ---- lessons ----
        g("/api/lessons",
          lambda q, b: self._limited(
              lambda: svc.lessons_dump(limit=100000), _i(q, "limit", 500)))

        # ---- session-start briefing (REST fast-path for `pseudolife-mcp briefing`) ----
        g("/api/briefing", lambda q, b: svc.session_briefing(
            max_unsure=_i(q, "max_unsure", 3), max_lessons=_i(q, "max_lessons", 3),
            max_world=_i(q, "max_world", 3)))

        # ---- episodes ----
        # The write endpoints (start/end/prune/rename/merge) are deliberately
        # agent/CLI-only — the Episodes view stays a read-only timeline;
        # curation happens through an agent that can reason about the history.
        g("/api/episodes", lambda q, b: svc.episode_list(
            limit=_i(q, "limit", 100), include_open=True))
        g("/api/episodes/summary", lambda q, b: svc.episode_summary(id=_s(q, "id")))
        p("/api/episode/start", lambda q, b: svc.episode_start_session(
            b.get("session_key"), b.get("title") or "session",
            b.get("hint")))
        p("/api/episode/end", lambda q, b: svc.episode_end_session(
            b.get("session_key"), run_dream=bool(b.get("run_dream", True))))
        p("/api/episodes/prune", lambda q, b: svc.episode_prune_empty(
            include_open=bool(b.get("include_open", False))))
        p("/api/episodes/rename", lambda q, b: svc.episode_rename(
            b["id"], b.get("title") or ""))
        p("/api/episodes/merge", lambda q, b: svc.episode_merge(
            b.get("sources") or [], into=b.get("into"),
            title=b.get("title"), hint=b.get("hint")))

        # ---- associative stream ----
        g("/api/recent", lambda q, b: svc.recent(
            n=_i(q, "n", 50), sources=_list(q, "source"),
            episodes=_list(q, "episode"), tags=_list(q, "tag")))
        g("/api/search", lambda q, b: self._search(q))
        g("/api/trace", lambda q, b: svc.trace(
            query=_s(q, "q", ""), top_k=_i(q, "top_k", 8),
            sources=_list(q, "source"), bands=_list(q, "band"),
            rerank=_tribool(q, "rerank"), bm25=_tribool(q, "bm25")))
        g("/api/recall", lambda q, b: svc.recall(
            _s(q, "q", ""), hops=_i(q, "hops", 3), top_k=_i(q, "top_k", 5)))
        g("/api/chain", lambda q, b: svc.chain(
            _s(q, "entity", ""), limit=_i(q, "limit", 20)))

        # ---- engram traces / retention ----
        g("/api/entry", lambda q, b: svc.get_entry(_i(q, "id", 0)))
        p("/api/reinforce", lambda q, b: svc.reinforce(int(b["entry_id"])))

        # ---- facets ----
        g("/api/sources", lambda q, b: svc.list_sources())
        g("/api/tags", lambda q, b: svc.list_tags())

        # ---- graph ----
        g("/api/graph", lambda q, b: svc.graph_neighborhood(
            entity=_s(q, "entity"), depth=_i(q, "depth", 1),
            include_facts=_tribool(q, "include_facts") is not False,
            to=_s(q, "to"), scope=_s(q, "scope"), max_nodes=_i(q, "limit", 300)))
        g("/api/graph/projects", lambda q, b: svc.graph_projects())
        g("/api/graph/digest", lambda q, b: svc.graph_digest())
        g("/api/graph/communities",
          lambda q, b: svc.communities(community_id=_i_opt(q, "id")))
        g("/api/graph/path", lambda q, b: svc.graph_path(
            _s(q, "source"), _s(q, "target"), max_hops=_i(q, "max_hops", 8)))
        g("/api/graph/review", lambda q, b: svc.graph_review(scope=_s(q, "scope")))
        g("/api/graph/entity-provenance", lambda q, b: svc.entity_provenance(
            _s(q, "entity"), limit=_i(q, "limit", 20)))
        p("/api/graph/assign-scope", lambda q, b: svc.graph_assign_scope(b["entity"], b["source"]))
        p("/api/graph/unrelate", lambda q, b: svc.graph_unrelate(b["src"], b["relation"], b["dst"]))
        p("/api/graph/bless-edge", lambda q, b: svc.graph_bless_edge(b["src"], b["relation"], b["dst"]))
        p("/api/graph/dismiss-duplicate", lambda q, b: svc.graph_dismiss_duplicate(b["a"], b["b"]))
        p("/api/graph/delete-entity", lambda q, b: svc.graph_delete_entity(b["entity"]))
        p("/api/graph/merge", lambda q, b: svc.graph_merge(b["from"], b["into"]))
        p("/api/graph/accept-proposal", lambda q, b: svc.graph_accept_proposal(b["id"]))
        p("/api/graph/reject-proposal", lambda q, b: svc.graph_reject_proposal(b["id"]))
        p("/api/graph/accept-entity-merge", lambda q, b: svc.graph_accept_entity_merge(b["id"]))
        p("/api/graph/accept-entity-junk", lambda q, b: svc.graph_accept_entity_junk(b["id"]))
        p("/api/graph/reject-entity-proposal", lambda q, b: svc.graph_reject_entity_proposal(b["id"]))

        # ---- dream / consolidation ----
        g("/api/dream/status", lambda q, b: svc.dream_status())
        p("/api/dream/run", lambda q, b: self._dream_run(b))
        g("/api/consolidation", lambda q, b: svc.consolidation_candidates(
            query=_s(q, "q"), episode=_s(q, "episode"),
            sources=_list(q, "source"), tags=_list(q, "tag"),
            top_k=_i(q, "top_k", 20), min_cohesion=_f(q, "min_cohesion", 0.6)))
        p("/api/consolidate", lambda q, b: svc.consolidate(
            replaces=b["replaces"], new_text=b["new_text"],
            source=b.get("source"), tags=b.get("tags")))

        # ---- hygiene / corrections ----
        p("/api/delete", lambda q, b: self._delete(b))
        p("/api/supersede", lambda q, b: svc.supersede(
            old_text=b["old_text"], new_text=b["new_text"]))

        # ---- config ----
        g("/api/config", lambda q, b: config_io.read_config(svc))
        p("/api/config", lambda q, b: config_io.write_config(
            svc, b.get("patch") or b))

    # -- composed / guarded handlers ----------------------------------------

    def _health(self) -> dict:
        from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
        svc = self.svc
        return {
            "status": "ok",
            "schema": SCHEMA_META_VERSION,
            "storage": "postgres" if getattr(svc, "_db_url", None) else "files",
            "writer_id": getattr(svc, "_writer_id", "unknown"),
            "persist_errors": getattr(svc, "_persist_errors", 0),
        }

    def _facts(self, limit: int) -> dict:
        return self._limited(self.svc.cortex_dump, limit)

    def _limited(self, fn: Callable[[], dict], limit: int) -> dict:
        """Cap a dump at ``limit`` rows, telling the client how many exist so
        it can say "showing first N of M" instead of silently truncating."""
        dump = fn()
        rows = dump.get("entries", [])
        entries = rows[: max(0, int(limit))]
        return {"count": len(entries), "total": len(rows),
                "truncated": len(entries) < len(rows), "entries": entries}

    def _search(self, q: dict) -> dict:
        """memory_search, with the cortex-first block the agent sees."""
        query = _s(q, "q", "") or ""
        result = self.svc.search(
            query=query, top_k=_i(q, "top_k", 12),
            sources=_list(q, "source"), bands=_list(q, "band"),
            tags=_list(q, "tag"), min_score=_f(q, "min_score", None),
            disable_recency_boost=_tribool(q, "disable_recency_boost") is True,
            rerank=_tribool(q, "rerank"), bm25=_tribool(q, "bm25"))
        cc = self.svc.config.memory.cortex
        if cc.enabled and cc.search_first and query.strip():
            facts = self.svc.cortex_search(
                query, top_k=5, min_score=cc.guard_min_score).get("entries", [])
            if facts:
                result["cortex"] = facts
        return result

    def _dream_run(self, b: dict) -> dict:
        from pseudolife_memory.memory.dream import build_extractor
        limit = b.get("limit")
        return self.svc.dream_run(
            build_extractor(self.svc.config.memory.dream),
            limit=int(limit) if limit not in (None, "") else None)

    def _delete(self, b: dict) -> dict:
        if not any(b.get(k) for k in ("text", "substring", "source", "episode", "tag")):
            raise ValueError("delete requires at least one filter")
        return self.svc.delete(
            text=b.get("text"), substring=b.get("substring"),
            source=b.get("source"), episode=b.get("episode"), tag=b.get("tag"))

    def _overview(self) -> dict:
        """One round-trip dashboard summary: counts per layer + dream backlog."""
        svc = self.svc

        def _safe(fn, default):
            try:
                return fn()
            except Exception:  # noqa: BLE001 — a missing layer must not 500 the dash
                return default

        stats = _safe(svc.stats, {})
        facts = _safe(svc.cortex_dump, {"entries": []}).get("entries", [])
        world = _safe(svc.world_dump, {"entries": []}).get("entries", [])
        lessons = _safe(lambda: svc.lessons_dump(limit=100000), {"entries": []}).get("entries", [])
        episodes = _safe(lambda: svc.episode_list(limit=100000, include_open=True), {})
        sources = _safe(svc.list_sources, {"sources": [], "total": 0})
        tags = _safe(svc.list_tags, {"tags": [], "total": 0})
        dream = _safe(svc.dream_status, {})

        bands = stats.get("bands", []) if isinstance(stats, dict) else []
        total_entries = 0
        if isinstance(stats, dict):
            total_entries = stats.get("total_memories", 0) or 0
        if not total_entries and isinstance(bands, list):
            total_entries = sum((b or {}).get("size", 0) for b in bands)

        ep_list = episodes.get("episodes", episodes.get("entries", [])) if isinstance(episodes, dict) else []
        contested = sum(1 for f in facts if f.get("contested"))
        stale = sum(1 for w in world if w.get("stale"))
        from collections import Counter
        by_origin = dict(Counter((f.get("origin") or "agent") for f in facts))

        return {
            "health": self._health(),
            "counts": {
                "entries": total_entries,
                "facts": len(facts),
                "facts_contested": contested,
                "facts_by_origin": by_origin,
                "world": len(world),
                "world_stale": stale,
                "lessons": len(lessons),
                "episodes": len(ep_list),
                # distinct source/tag counts (not summed occurrences)
                "sources": len(sources.get("sources", [])),
                "tags": len(tags.get("tags", [])),
            },
            "stats": stats,
            "dream": dream,
        }
