"""Dream-side operations of :class:`~pseudolife_memory.service.MemoryService`.

This module holds the dream consolidation cycle (pull/extract/commit and its
stages: outcome inference, lesson synthesis, session digests), the deep-dream
full-corpus pass with its judge, and the private helpers only those paths use.

``DreamOps`` is a mixin with exactly one consumer: ``MemoryService`` inherits
it, and every method here runs against a fully initialised service instance
(``self._storage``, ``self._cortex``, ``self._lock``, ... are created in
``MemoryService.__init__``). It is code motion from ``service.py``, not an
independent component; nothing else may instantiate or import ``DreamOps``
for standalone use.
"""

from __future__ import annotations

import logging
from typing import Any

from pseudolife_memory.memory.titans_memory import MemoryEntry

from pseudolife_memory.memory.labels import (INHERIT, contains_verbatim,
                                             content_tokens)

logger = logging.getLogger(__name__)


class DreamOps:
    """Dream section of ``MemoryService`` (see module docstring)."""

    # Post-pass caps: screen at most this many freshly-minted entities per
    # cycle, one best-match proposal each — the queue stays reviewable.
    _ALIAS_SCAN_MAX = 20
    _INFER_CURSOR_KEY = "outcome_inference_cursor"
    _DIGEST_CURSOR_KEY = "session_digest_cursor"

    def _link_dream_relations(self, relations: list[dict], *,
                              batch_sources: set[str] | None = None) -> int:
        """Upsert dream-extracted (src,relation,dst) edges. Closed-vocab
        (resolve_relation; unknown -> related-to), entities resolved alias-aware
        and pinned to the Postgres hub, self-loops dropped, origin='agent'.
        ``batch_sources`` (the entry sources of the dream batch) stamps
        provenance onto entities MINTED here — relation endpoints have no fact
        traces, so the backfill can never scope them after the fact.
        Caller holds the lock; no-op in file mode. Returns edges written."""
        if self._storage is None or not relations:
            return 0
        import time as _t
        from pseudolife_memory import graph as G
        from pseudolife_memory.memory.relation_quality import (
            GENERIC_HUB_NORMS, edge_confidence)
        known = [r["name"] for r in self._graph.load_relations()
                 if r["name"] not in ("prefers", "avoids")]
        floor = float(self.config.memory.dream.min_relation_confidence)
        stamp = self.config.memory.scopes.scope_keys(batch_sources)
        n = 0
        from pseudolife_memory.memory.graph_consolidation import junk_name_reason
        scope_map = self._storage.entity_sources_map()
        for r in relations:
            raw_src, raw_dst = str(r.get("src", "")), str(r.get("dst", ""))
            src_n, dst_n = G.norm_name(raw_src), G.norm_name(raw_dst)
            if not src_n or not dst_n or src_n == dst_n:
                continue
            # Generic-hub gate: everything "relates to" MCP / master /
            # CLAUDE.md, so a dream edge touching one carries no information.
            # Explicit graph_relate is unaffected.
            if src_n in GENERIC_HUB_NORMS or dst_n in GENERIC_HUB_NORMS:
                logger.debug("dream relation dropped (generic-hub): %r -> %r",
                             raw_src, raw_dst)
                continue
            # Write-time junk gate: the 2B extractor's known artifact classes
            # (concat names, bare numbers, status words) never become entities.
            junk = junk_name_reason(raw_src) or junk_name_reason(raw_dst)
            if junk:
                logger.debug("dream relation dropped (%s): %r -> %r",
                             junk, raw_src, raw_dst)
                continue
            resolved, _ = G.resolve_relation(known, str(r.get("relation", "")))
            relation = resolved or "related-to"
            conf = edge_confidence(raw_src, relation, raw_dst)
            if conf < floor:
                continue
            src_e = self._resolve_or_create_entity(raw_src, propose_dupes=True)
            dst_e = self._resolve_or_create_entity(raw_dst, propose_dupes=True)
            for ent in (src_e, dst_e):
                if ent.get("created") and stamp:
                    for s in sorted(stamp):
                        self._storage.upsert_entity_source(
                            ent["id"], s, "derived", _t.time())
                    # keep the preloaded map coherent so the cross-project
                    # gate below sees the scope this entity was just given
                    scope_map[ent["id"]] = sorted(stamp)
            # Cross-project gate: entities attributed to disjoint projects
            # merely coexist in the shared bank — route the claim to
            # edge_proposals for human review instead of the live graph.
            ss = set(scope_map.get(src_e["id"], []))
            ds = set(scope_map.get(dst_e["id"], []))
            if ss and ds and not (ss & ds):
                if relation == "related-to":
                    # Untyped fallback across disjoint projects carries no
                    # information (2026-07-11: 4/4 such proposals rejected).
                    logger.debug("dream relation dropped (cross-project-"
                                 "untyped): %r -> %r", raw_src, raw_dst)
                    continue
                self._storage.insert_proposal(
                    src_e["id"], relation, dst_e["id"], conf, None,
                    f"cross-project: {sorted(ss)} x {sorted(ds)}",
                    "dream-cross-project", _t.time())
                logger.debug("dream relation proposed (cross-project): %r -> %r",
                             raw_src, raw_dst)
                continue
            # Low-confidence quarantine: untyped co-mention edges (related-to,
            # 0.45) pollute the live graph ~19/day — below the quarantine
            # threshold the claim goes to edge_proposals for review instead.
            if conf < float(self.config.memory.dream.relation_quarantine_below):
                self._storage.insert_proposal(
                    src_e["id"], relation, dst_e["id"], conf, None,
                    f"low-confidence dream edge ({conf:g})",
                    "dream-low-confidence", _t.time())
                logger.debug("dream relation quarantined (low-confidence): "
                             "%r -> %r", raw_src, raw_dst)
                continue
            # revive=False: a dream re-assertion must not resurrect an edge
            # a human (or deep-dream) superseded — removals stay sticky.
            self._graph.upsert_edge(src_e["id"], relation, dst_e["id"],
                                    confidence=conf, origin="agent",
                                    revive=False)
            n += 1
        return n

    def _dream_extract_relations(self, extractor, texts: list[str],
                                 batch_sources: set[str] | None = None) -> int:
        """Gated, best-effort graph-from-text for one dream batch: run the LLM
        relations call UNLOCKED (slow network), then write edges LOCKED. A
        failure logs and returns 0 — it must never break fact consolidation or
        drop claims (relations are best-effort, like lessons)."""
        cfg = self.config.memory.dream
        rel_fn = getattr(extractor, "extract_relations", None)
        if not (cfg.extract_relations and rel_fn is not None and texts):
            return 0
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return 0
                registry = [(r["name"], r["description"])
                            for r in self._graph.load_relations()
                            if r["name"] not in ("prefers", "avoids")]
            rels = rel_fn(texts, registry)
            with self._lock:
                return self._link_dream_relations(
                    rels, batch_sources=batch_sources)
        except Exception as exc:  # noqa: BLE001 — best-effort; never break the dream
            logger.warning("dream relation extraction failed (%s); claims kept",
                           exc)
            return 0

    def infer_outcomes_stage(self, extractor) -> dict[str, Any]:
        """Dream stage (spec 2026-07-18): infer outcome signals for closed
        zero-signal episodes. Locked pull -> unlocked extract -> locked
        commit; transport failure halts with cursor held; malformed output
        gets 2 attempts then the cursor advances past the episode."""
        cfg = self.config.memory.lessons
        if self._storage is None:
            return {"scanned": 0, "written": 0, "skipped": "no-storage"}
        if not (cfg.enabled and cfg.infer_outcomes
                and cfg.infer_outcomes_max_signals > 0):
            return {"scanned": 0, "written": 0, "skipped": "disabled"}
        fn = getattr(extractor, "infer_outcomes", None)
        if fn is None:
            return {"scanned": 0, "written": 0, "skipped": "no-extractor"}
        with self._lock:
            self._ensure_init()
            candidates = self._pending_inference_candidates()
        scanned = written = 0
        for cand in candidates:
            try:                                   # unlocked: extractor call
                claims = fn(cand["context"],
                            cap=cfg.infer_outcomes_max_signals)
            except Exception as exc:  # noqa: BLE001 — transport: hold cursor
                logger.warning(
                    "outcome inference halted (%s); cursor held", exc)
                break
            scanned += 1
            with self._lock:
                cur = self._load_infer_cursor()
                rid = cand["root_id"]
                # Another concurrent dream (fire-and-forget vs sweep — no
                # dream-level mutex) may have processed this episode while
                # our extractor call ran unlocked: re-check before writing.
                # STRICTLY greater — same-tick siblings share ended_at, and a
                # >= here would strand the second one forever (its claims
                # skipped, yet the candidate filter needs ended_at > cursor).
                # "Already written" is caught by the signal count instead.
                if (cur["ts"] > cand["ended_at"]
                        or self._storage.count_signals_for_episodes(
                            [rid]) > 0):
                    continue
                if claims is None:                 # malformed: bounded retry
                    attempts = int(cur["retry"].get(rid, 0)) + 1
                    if attempts >= 2:
                        cur["retry"].pop(rid, None)
                        cur["ts"] = cand["ended_at"]
                        self._save_infer_cursor(cur)
                        logger.warning(
                            "outcome inference: advancing past episode %s "
                            "after %d malformed attempts", rid, attempts)
                        continue
                    cur["retry"][rid] = attempts
                    self._save_infer_cursor(cur)
                    logger.warning(
                        "outcome inference: malformed output for episode "
                        "%s (attempt %d); will retry next dream",
                        rid, attempts)
                    break                          # keep episode order
                for c in claims:
                    try:
                        self._storage.add_signal(
                            task=c["task"], outcome=c["outcome"],
                            about=c.get("about"), detail=c.get("detail"),
                            origin="inferred", episode_id=rid)
                        written += 1
                    except Exception as exc:  # noqa: BLE001 — never break a dream
                        logger.warning(
                            "inferred signal skipped (%s): %s", exc, c)
                cur["retry"].pop(rid, None)
                cur["ts"] = cand["ended_at"]
                self._save_infer_cursor(cur)
        return {"scanned": scanned, "written": written}

    def generate_digests_stage(self, extractor) -> dict[str, Any]:
        """Dream stage (spec 2026-08-24): one narrative digest per closed
        session root, stored as a ``source="digest"`` band entry stamped to
        the summarized episode. Same shape as :meth:`infer_outcomes_stage`:
        locked pull -> unlocked summarize (map-reduce over
        ``digest_context_chars``) -> locked re-check + write; transport
        failure halts with cursor held; malformed output gets 2 attempts
        then the cursor advances past the episode."""
        import time as _time
        cfg = self.config.memory.dream
        if self._storage is None:
            return {"scanned": 0, "written": 0, "skipped": "no-storage"}
        if not cfg.digest_enabled:
            return {"scanned": 0, "written": 0, "skipped": "disabled"}
        fn = getattr(extractor, "summarize_session", None)
        if fn is None:
            return {"scanned": 0, "written": 0, "skipped": "no-extractor"}
        from pseudolife_memory.memory.dream import split_session_context
        target = int(cfg.digest_target_chars)
        with self._lock:
            self._ensure_init()
            candidates = self._pending_digest_candidates()
        scanned = written = 0
        for cand in candidates:
            try:                               # unlocked: extractor calls
                parts = split_session_context(
                    cand["context"], int(cfg.digest_context_chars))
                if len(parts) == 1:
                    digest = fn(parts[0], target_chars=target)
                else:
                    seg_digests: list[str] = []
                    digest = None
                    for part in parts:
                        seg = fn(part, target_chars=target)
                        if seg is None:
                            seg_digests = []
                            break
                        seg_digests.append(seg)
                    if seg_digests:            # reduce over the part digests
                        digest = fn("\n\n".join(seg_digests),
                                    target_chars=target)
            except Exception as exc:  # noqa: BLE001 — transport: hold cursor
                logger.warning("session digest halted (%s); cursor held", exc)
                break
            scanned += 1
            with self._lock:
                cur = self._load_digest_cursor()
                rid = cand["root_id"]
                # A concurrent dream (fire-and-forget vs sweep) may have
                # digested this episode while our extractor call ran
                # unlocked: re-check before writing. STRICTLY greater on
                # the cursor — same-tick siblings share ended_at (see
                # infer_outcomes_stage).
                existing = any(
                    e.source == "digest" and e.episode_id == rid
                    for band in self._cms.bands for e in band.entries)
                if cur["ts"] > cand["ended_at"] or existing:
                    continue
                if digest is None:             # malformed: bounded retry
                    attempts = int(cur["retry"].get(rid, 0)) + 1
                    if attempts >= 2:
                        cur["retry"].pop(rid, None)
                        cur["ts"] = cand["ended_at"]
                        self._save_digest_cursor(cur)
                        logger.warning(
                            "session digest: advancing past episode %s "
                            "after %d malformed attempts", rid, attempts)
                        continue
                    cur["retry"][rid] = attempts
                    self._save_digest_cursor(cur)
                    logger.warning(
                        "session digest: malformed output for episode %s "
                        "(attempt %d); will retry next dream", rid, attempts)
                    break                      # keep episode order
                d0 = _time.strftime("%Y-%m-%d", _time.gmtime(
                    cand["started_at"] or cand["ended_at"]))
                d1 = _time.strftime("%Y-%m-%d",
                                    _time.gmtime(cand["ended_at"]))
                span = d0 if d0 == d1 else f"{d0}–{d1}"
                header = (f"Session digest: {cand['title']} "
                          f"({span}, {cand['n_entries']} entries)")
                try:
                    self._store_digest(f"{header}\n{digest}", rid,
                                       cand["title"])
                except Exception as exc:  # noqa: BLE001 — never break a dream
                    # Storage/embedder failure: bounded like the malformed
                    # path — one held-cursor retry, then advance past. An
                    # unguarded raise here aborts the stages after this one
                    # and re-pays the full map-reduce every dream while the
                    # failure persists (the 2026-07-06 dream-stall shape: a
                    # broken connection fails deterministically).
                    attempts = int(cur["retry"].get(rid, 0)) + 1
                    if attempts >= 2:
                        cur["retry"].pop(rid, None)
                        cur["ts"] = cand["ended_at"]
                        self._save_digest_cursor(cur)
                        logger.warning(
                            "session digest: advancing past episode %s "
                            "after %d failed writes (%s)", rid, attempts, exc)
                        continue
                    cur["retry"][rid] = attempts
                    self._save_digest_cursor(cur)
                    logger.warning(
                        "session digest: write failed for episode %s "
                        "(attempt %d): %s; will retry next dream",
                        rid, attempts, exc)
                    break                      # keep episode order
                written += 1
                cur["retry"].pop(rid, None)
                cur["ts"] = cand["ended_at"]
                self._save_digest_cursor(cur)
        return {"scanned": scanned, "written": written}

    def synthesize_lessons(self, extractor, *, limit: int | None = None) -> dict[str, Any]:
        """Drain pending outcome signals and synthesise lessons via ``extractor``.

        Single-writer: an extractor with no ``extract_lessons`` (the no-op / a
        plain regex floor) writes nothing and leaves the signals pending. Old
        signals are pruned by retention so the log can't grow unbounded.
        """
        import time as _t
        cfg = self.config.memory.lessons
        if self._storage is None:
            return {"signals": 0, "lessons": 0, "skipped": "no-storage"}
        if not (cfg.enabled and cfg.synthesize_in_dream):
            return {"signals": 0, "lessons": 0, "skipped": "disabled"}
        cutoff = _t.time() - cfg.signal_retention_days * 86400
        with self._lock:
            self._ensure_init()
            self._storage.prune_signals(cutoff)
            signals = self._storage.pending_signals(limit=limit)
        if not signals:
            return {"signals": 0, "lessons": 0}
        all_inferred = bool(signals) and all(
            s.get("origin") == "inferred" for s in signals)
        fn = getattr(extractor, "extract_lessons", None)
        if fn is None:
            return {"signals": len(signals), "lessons": 0, "skipped": "no-extractor"}
        try:
            claims = fn(signals)
        except Exception as exc:  # noqa: BLE001 — never let synthesis break the dream
            logger.warning("lesson synthesis failed (%s); leaving signals pending", exc)
            return {"signals": len(signals), "lessons": 0, "error": str(exc)}
        # Bitemporal event time: the synthesised lesson became *true* when its
        # underlying outcomes were observed, not when the dream wrote it. Claims
        # don't map 1:1 to signals, so use the earliest contributing signal's
        # created_at as the batch valid_time (None → store defaults to tx_time).
        created = [s["created_at"] for s in signals if s.get("created_at")]
        batch_valid_time = min(created) if created else None
        written = 0
        deduped = 0
        dedup_thr = float(getattr(cfg, "synthesis_dedup_min_similarity", 0.0)
                          or 0.0)
        for c in claims:
            try:
                if dedup_thr and self._synthesized_lesson_duplicate(
                        c["task"], c.get("aspect", "lesson"), c["lesson"],
                        c.get("polarity", "+"), dedup_thr):
                    deduped += 1
                    logger.info("lesson synthesis dedup: %r near-duplicates "
                                "an existing lesson; skipped", c.get("task"))
                    continue
                self.lesson_write(
                    c["task"], c.get("aspect", "lesson"), c["lesson"],
                    about=c.get("about"), outcome=c.get("outcome", "success"),
                    polarity=c.get("polarity", "+"),
                    confidence=(0.4 if all_inferred
                                else float(c.get("confidence", 0.6))),
                    origin=c.get("origin", "agent"),
                    provenance=(set(c.get("provenance") or [])
                                | ({"inferred"} if all_inferred else set())),
                    valid_time=batch_valid_time)
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("lesson write skipped (%s): %s", exc, c)
        if written or deduped:
            # A fully-deduped batch is HANDLED, not failed — leaving its
            # signals pending would re-synthesize the same near-duplicates
            # every sweep, forever bouncing off the gate.
            with self._lock:
                self._storage.consume_signals([s["id"] for s in signals])
        else:
            # Nothing landed (empty extraction or every write failed): leave
            # the signals pending so the next sweep retries — they are the
            # only feeder for procedural memory. Retention pruning bounds
            # the retry window.
            logger.info("lesson synthesis wrote nothing; leaving %d signals "
                        "pending", len(signals))
        return {"signals": len(signals), "lessons": written, "deduped": deduped}

    def prune_dream_runs(self) -> int:
        """Retention for the v27 dream-run journal: keep the newest
        ``memory.dream.runs_keep`` runs (CASCADE removes their journals) and
        flip stale ``running`` rows to ``failed``. Sweep-tick maintenance
        beside :meth:`compact_superseded`; safe to call any time. Returns
        the number of pruned runs (0 in file mode)."""
        if self._storage is None:
            return 0
        with self._lock:
            return self._storage.prune_dream_runs(
                int(self.config.memory.dream.runs_keep))

    def dream_runs(self, limit: int = 10) -> dict[str, Any]:
        """Recent v27 dream-run rows, newest first (compact: None-valued
        bookkeeping fields dropped per row)."""
        if self._storage is None:
            return {"error": "requires_postgres"}
        with self._lock:
            self._ensure_init()
            runs = self._storage.recent_dream_runs(limit=limit)
        return {"count": len(runs),
                "runs": [{k: v for k, v in r.items() if v is not None}
                         for r in runs]}

    def dream_pull(self, limit: int = 20) -> dict[str, Any]:
        """Recent episodic conversation turns not yet consolidated (timestamp >
        cortex.dream_cursor), oldest-first, capped at ``limit``. The gateway
        runs LLM/regex extraction over these, then calls ``dream_commit``."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._cortex is not None
            cfg = self.config.memory.dream
            excluded = set(cfg.exclude_sources or [])
            allowed = set(cfg.eligible_sources) if cfg.eligible_sources else None
            cursor = self._cortex.dream_cursor
            rows: list[MemoryEntry] = []
            for band in self._cms.bands:
                for e in band.entries:
                    if allowed is not None:
                        if e.source not in allowed:
                            continue
                    elif e.source in excluded:
                        continue
                    if e.timestamp <= cursor:
                        continue
                    rows.append(e)
            rows.sort(key=lambda e: e.timestamp)
            rows = rows[: max(0, int(limit))]
            return {
                "cursor": cursor,
                "count": len(rows),
                "entries": [
                    {
                        "text": e.text,
                        "timestamp": e.timestamp,
                        "episode_id": e.episode_id,
                        "db_id": e.db_id,
                        # dream_run stamps relation-minted entities with the
                        # batch's sources — dropping this field silently
                        # disables that (2026-07-19 regression).
                        "source": e.source,
                        # v35: the source's labels travel with the pull —
                        # the dream stamps derived facts from them and the
                        # carrier/guard key on distortion_tolerance.
                        "authority": e.authority,
                        "distortion_tolerance": e.distortion_tolerance,
                    }
                    for e in rows
                ],
            }

    def dream_commit(self, cursor: float) -> dict[str, Any]:
        """Advance the dream cursor (monotonic) and persist it with the cortex."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            c = float(cursor or 0.0)
            if c > self._cortex.dream_cursor:
                self._cortex.dream_cursor = c
                self._cortex.meta_dirty = True   # cursor rides the meta sync
                self._save_cortex()
            return {"dream_cursor": self._cortex.dream_cursor}

    def _resolve_dream_slot(self, entity: str, attribute: str) -> tuple[str, str]:
        """Map a dreamed claim's (entity, attribute) onto an existing current slot
        when a confident value-free slot-embedding match exists, so a paraphrased
        update supersedes instead of forking a sibling. Dream-path only; returns
        the original pair when disabled, on an exact-key hit, or below threshold.
        Never raises — a resolver failure falls back to the original slot."""
        threshold = float(self.config.memory.cortex.dream_slot_match_threshold)
        if threshold <= 0.0:
            return entity, attribute
        try:
            with self._lock:
                self._ensure_init()
                assert self._embedder is not None and self._cortex is not None
                # Exact slot already exists -> let the normal write path supersede.
                if self._cortex.lookup(entity, attribute) is not None:
                    return entity, attribute
                slot_emb = self._embedder.encode_single(
                    f"{entity} {attribute}".strip())
                match = self._cortex.resolve_slot(slot_emb, threshold)
            return match or (entity, attribute)
        except Exception as exc:  # noqa: BLE001 — resolution must never break a dream
            logger.warning("dream slot resolve failed (%s); using literal slot", exc)
            return entity, attribute

    def _dream_hints(self, texts: list[str], vocab_limit: int = 120,
                     facts_limit: int = 0,
                     ) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Relevance-ranked slot keys plus (when ``facts_limit > 0``) the
        known-facts window — current values of the top slots — from ONE
        batch-text embedding (docs/specs/2026-07-10-known-facts-window-design.md).
        Never raises — falls back to the alphabetical vocab and no window."""
        try:
            with self._lock:
                self._ensure_init()
                assert self._embedder is not None and self._cortex is not None
                emb = self._embedder.encode_single(" ".join(texts)[:4000])
                vocab = self._cortex.vocab_ranked(emb, vocab_limit)
                facts = (self._cortex.facts_ranked(emb, facts_limit)
                         if facts_limit > 0 else [])
                return vocab, facts
        except Exception as exc:  # noqa: BLE001 — hint quality must never break a dream
            logger.warning("dream hint build failed (%s); using alphabetical "
                           "vocab, no facts window", exc)
            return self.cortex_vocab(vocab_limit).get("slots", []), []

    def _propose_dream_alias_candidates(self, new_entities: dict[str, str],
                                        known_entities: set[str]) -> int:
        """Embedding-based coreference screen over entities a dream cycle just
        minted: each new entity name is cosine-compared against existing
        entity names; the best match at/above ``alias_candidate_min_cosine``
        files a merge proposal into the SAME review queue as the token-Jaccard
        write-dedup (dismissed pairs suppressed, unique index dedupes,
        Atlas/graph_review surfaces it, a human settles it — never
        auto-folded). Complements ``_propose_write_dedup``: paraphrase
        coreference ("production extractor sidecar" ~ "Pseudolife-MCP default
        extractor sidecar") shares almost no tokens but embeds close.
        Returns the number of proposals filed; never raises."""
        import time as _t
        try:
            thr = float(self.config.memory.dream.alias_candidate_min_cosine)
            if thr <= 0 or not new_entities:
                return 0
            from pseudolife_memory.graph import norm_name
            from pseudolife_memory.memory.graph_consolidation import variant_conflict
            filed = 0
            with self._lock:
                self._ensure_init()
                if (self._storage is None or self._embedder is None
                        or self._cortex is None):
                    return 0
                # Existing display names, one per norm key (cortex entities
                # predating this cycle).
                existing: dict[str, str] = {}
                for r in self._cortex.records:
                    if r.status == "current":
                        k = r.key[0]
                        if k in known_entities and k not in existing:
                            existing[k] = r.entity
                if not existing:
                    return 0
                dismissed = frozenset(self._storage.dismissed_pairs())
                new_items = list(new_entities.items())[:self._ALIAS_SCAN_MAX]
                ex_items = list(existing.items())
                new_emb = self._embedder.encode([d for _, d in new_items])
                ex_emb = self._embedder.encode([d for _, d in ex_items])
                sims = new_emb @ ex_emb.T          # encode() normalizes
                # Fold direction is evidence-ranked like _propose_write_dedup:
                # the thin side folds into the evidence-bearing side. Filing
                # (new, existing) verbatim made the reviewer's only accept
                # fold rich standing entities into just-minted shells
                # (29 wrong-direction proposals, 2026-08-05 triage).
                from pseudolife_memory.graph import degree_counts
                deg = degree_counts(self._storage.load_graph()["edges"])
                fct = self._storage.entity_fact_counts()

                def _evidence(eid: int) -> int:
                    return deg.get(eid, 0) + fct.get(eid, 0)

                now = _t.time()
                for i, (_, disp) in enumerate(new_items):
                    j = int(sims[i].argmax())
                    score = float(sims[i][j])
                    if score < thr:
                        continue
                    target = ex_items[j][1]
                    pair = tuple(sorted((norm_name(disp), norm_name(target))))
                    if pair[0] == pair[1] or pair in dismissed:
                        continue
                    if variant_conflict(disp, target):
                        continue    # size/quant/version mismatch: never a merge
                    a = self._resolve_or_create_entity(disp)
                    b = self._resolve_or_create_entity(target)
                    if a["id"] == b["id"]:
                        continue                    # already aliased/merged
                    # The name-keyed check above misses a display-enriched
                    # entity (canonical 'gnd', display 'GND (Enshrouded
                    # server)'); dismissed_pairs is keyed on STORED
                    # canonicals, so re-check on the resolved rows.
                    if tuple(sorted((a.get("canonical") or "",
                                     b.get("canonical") or ""))) in dismissed:
                        continue
                    frm, into = a["id"], b["id"]
                    if _evidence(frm) > _evidence(into):
                        frm, into = into, frm
                    if self._storage.insert_entity_proposal(
                            "merge", frm, into, round(score, 3),
                            f"dream-alias: {disp!r} ~ {target!r} "
                            f"(cosine {score:.2f})", now) is not None:
                        filed += 1
            return filed
        except Exception as exc:  # noqa: BLE001 — screening must never break a dream
            logger.debug("dream alias-candidate scan skipped (%s)", exc)
            return 0

    def dream_run(self, extractor, *, limit: int | None = None) -> dict[str, Any]:
        """One dream cycle: pull eligible unconsolidated memories, extract claims
        via ``extractor`` (an extractor that yields nothing writes nothing —
        single-writer cortex, no regex fallback), write each to the cortex, advance
        the dream cursor. Returns a summary. The single consolidation path shared by
        the MCP tool and (later) the daemon sweep.

        Single-flight: a second caller while a cycle is in flight returns
        ``{"skipped": "dream_in_progress", ...}`` immediately instead of
        pulling the same cursor window (see ``_dream_run_guard``). The next
        sweep tick retries anything the skipped trigger would have consumed.
        """
        if not self._dream_run_guard.acquire(blocking=False):
            return {"skipped": "dream_in_progress", "pulled": 0, "claims": 0}
        try:
            return self._dream_run_locked(extractor, limit=limit)
        finally:
            self._dream_run_guard.release()

    def _dream_run_locked(self, extractor, *,
                          limit: int | None = None) -> dict[str, Any]:
        cap = int(limit if limit is not None else self.config.memory.dream.max_batch)
        pulled = self.dream_pull(limit=cap)
        entries = pulled["entries"]
        if not entries:
            # No new memories to consolidate, but outcome signals may still be
            # pending — synthesise lessons regardless. Still refresh the graph
            # digest so manual graph edits (cleanup, direct graph_relate) are
            # reflected even when there is no memory backlog.
            outcome_inference = self.infer_outcomes_stage(extractor)
            digests = self.generate_digests_stage(extractor)
            lessons = self.synthesize_lessons(extractor)
            graph_insight = self._safe_refresh_graph_insight()
            # Quarantined pairs may still be pending for the same reason
            # lessons are: no new memories doesn't mean no pending work. The
            # quarantine in fact accumulates when dreams are INFREQUENT, so
            # skipping the retype here drained it exactly when it was least
            # needed (live verification, 2026-07-26).
            retyped = self.retype_quarantined_links(
                extractor, limit=self.config.memory.dream.retype_quarantined_max)
            return {"pulled": 0, "claims": 0, "inserted": 0, "confirmed": 0,
                    "contested": 0, "superseded": 0, "relations": 0,
                    "literal_flagged": 0, "literal_dropped": 0,
                    "quarantine_parked": 0, "quarantine_held": 0,
                    "quarantine_promoted": 0,
                    "events_inserted": 0, "events_duplicate": 0,
                    "events_pass_failed": False,
                    "constraint_verbatim": 0, "constraint_misses": [],
                    "cursor": pulled["cursor"], "lessons": lessons,
                    "outcome_inference": outcome_inference,
                    "digests": digests,
                    "graph_insight": graph_insight, "retyped": retyped}
        from pseudolife_memory.memory.cortex import (_TIER_RANK, _norm_key,
                                                     _norm_value)
        from pseudolife_memory.memory.dream import (_DATE_LIKE_RE,
                                                    literal_violations,
                                                    span_unbacked)
        import time as _time
        traces_cfg = self.config.memory.traces
        dream_cfg = self.config.memory.dream
        kf_n = int(dream_cfg.known_facts_window or 0)
        vocab, known_facts = self._dream_hints(
            [e["text"] for e in entries], facts_limit=kf_n)
        tally = {"inserted": 0, "confirmed": 0, "contested": 0, "superseded": 0}
        traces_n = 0
        max_batch_failures = 3
        quarantined = 0
        # Literal-faithfulness gate (2026-08-02 design doc): counters live
        # OUTSIDE tally — tally is summed into the reported claim count.
        gate_mode = dream_cfg.literal_gate
        gate_scope = dream_cfg.literal_gate_scope
        literal_flagged = 0
        literal_dropped = 0
        # Provenance-span gate (Feature B, 2026-08-12 design). Counters
        # live outside tally like the literal gate's. Source scope by
        # construction: the quote verifies against the CITED note only.
        span_mode = dream_cfg.span_gate
        span_flagged = 0
        span_parked = 0
        # Consolidation quarantine (two-man rule, spec 2026-08-09; ships
        # OFF). Counters live outside tally like the literal gate's; the
        # name avoids "quarantined", which the link-retype path owns.
        quarantine_on = bool(dream_cfg.quarantine_low_trust)
        qt_trusted = set(dream_cfg.trusted_sources or [])
        qt_parked = 0
        qt_held = 0
        qt_promoted = 0
        # Chronicle events (schema v28): default-on since the 2026-08-12
        # soak review; requires PG (the table has no file-mode counterpart).
        chronicle_on = bool(dream_cfg.chronicle) and self._storage is not None
        events_inserted = 0
        events_duplicate = 0
        events_pass_failed = False
        # TypeCompact (v35, arXiv 2608.22752): constraint entries by batch
        # index, and the indices whose text reached a derived item
        # verbatim. Filled after the pull is extracted; read by the guard.
        constraint_idx: dict[int, dict] = {}
        carried: set[int] = set()
        constraint_misses: list[dict] = []

        def _held(reason: str, exc: Exception) -> dict[str, Any]:
            logger.warning("dream %s (%s); cursor NOT advanced, will retry "
                           "next sweep", reason, exc)
            return {"pulled": len(entries), "claims": 0, "inserted": 0,
                    "confirmed": 0, "contested": 0, "superseded": 0, "relations": 0,
                    "cursor": self._cortex.dream_cursor, "extractor_failed": True,
                    "literal_flagged": literal_flagged,
                    "literal_dropped": literal_dropped,
                    "span_flagged": span_flagged,
                    "span_parked": span_parked,
                    "quarantine_parked": qt_parked,
                    "quarantine_held": qt_held,
                    "quarantine_promoted": qt_promoted,
                    "events_inserted": events_inserted,
                    "events_duplicate": events_duplicate,
                    "events_pass_failed": False,
                    # Writes that landed before the failure keep their
                    # carrier count (the run row records the same figure);
                    # misses are not judged on a held pass.
                    "constraint_verbatim": len(carried),
                    "constraint_misses": [],
                    "lessons": {"signals": 0, "lessons": 0}}

        # ONE batched call for the whole pull: the model must see a fact's
        # initial and update turns together to name them consistently, or
        # updates land on sibling slots instead of superseding (the 2026-06-25
        # per-entry restructure cost stale_leak 0.0 -> 0.8 on the ladder).
        # Per-claim attribution travels back via the claim's "source" index.
        texts = [e["text"] for e in entries]
        # Gate corpora: the batch union is the default scope — derived sums
        # and cross-note values are measured false-drop classes under
        # per-note gating (design doc; c2op-count-verdict qid 01493427).
        src_text = {e["db_id"]: e["text"] for e in entries
                    if e.get("db_id") is not None}
        batch_text = "\n".join(texts)
        # Event-date fabrication guard: an extractor can only have resolved
        # a real calendar date if the batch actually contains date
        # information — otherwise the date is dropped and the event stores
        # undated with its verbatim phrase (design amendment 2026-08-04).
        batch_has_date = bool(_DATE_LIKE_RE.search(batch_text))
        batch_key = tuple(e.get("db_id") if e.get("db_id") is not None
                          else e["text"][:200] for e in entries)
        # (claim, source entry db_id, source entry dict-or-None). The entry
        # dict travels beside the id because file mode has no db_id and the
        # quarantine's eligibility/witness derivation reads entry metadata
        # (source, episode_id), not the id.
        pairs: list[tuple[dict, Any, dict | None]] = []
        try:
            from pseudolife_memory.memory.dream import unflatten_slot_key_claims
            extracted = (extractor.extract(texts, vocab,
                                           known_facts=known_facts)
                         if known_facts else extractor.extract(texts, vocab))
            # Repair vocab slot keys the extractor flattened into the entity
            # name before anything mints an entity from them (2026-07-26).
            extracted = unflatten_slot_key_claims(extracted, vocab)
            for c in extracted:
                idx = c.get("source")
                if isinstance(idx, int) and 0 <= idx < len(entries):
                    src_entry = entries[idx]
                elif len(entries) == 1:
                    # Unambiguous: extractors that don't cite sources (stubs,
                    # older models) still attribute a single-entry batch.
                    src_entry = entries[0]
                else:
                    src_entry = None
                src_id = src_entry.get("db_id") if src_entry else None
                pairs.append((c, src_id, src_entry))
        except Exception as exc:  # noqa: BLE001 — an extractor must never break a dream
            fails = self._dream_batch_failures.get(batch_key, 0) + 1
            self._dream_batch_failures[batch_key] = fails
            if fails < max_batch_failures:
                return _held(
                    f"extractor failed ({fails}/{max_batch_failures} "
                    "for this batch)", exc)
            # The batch fails deterministically — isolate the poison entry with
            # per-entry calls so it can't stall consolidation forever. Entries
            # that fail alone are quarantined (skipped; the commit advances the
            # cursor past them) — UNLESS everything fails, which is an endpoint
            # outage, not a poison pill: hold the cursor and retry next sweep.
            succeeded = 0
            failed_keys: list[Any] = []
            for e, key in zip(entries, batch_key):
                try:
                    e_vocab, e_kf = self._dream_hints([e["text"]],
                                                      facts_limit=kf_n)
                    e_claims = list(
                        extractor.extract([e["text"]], e_vocab,
                                          known_facts=e_kf)
                        if e_kf else extractor.extract([e["text"]], e_vocab))
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("dream: entry %s failed isolated "
                                   "extraction (%s)", key, exc2)
                    failed_keys.append(key)
                    continue
                succeeded += 1
                pairs.extend((c, e.get("db_id"), e) for c in e_claims)
            if succeeded == 0 and len(entries) > 1:
                return _held("all entries failed the isolation pass "
                             "(outage, not poison)", exc)
            quarantined = len(failed_keys)
            for key in failed_keys:
                logger.warning("dream: quarantining entry %s (fails alone "
                               "while siblings extract)", key)
            self._dream_batch_failures.pop(batch_key, None)
        else:
            self._dream_batch_failures.pop(batch_key, None)

        # TypeCompact: a CONSTRAINT source is zero-distortion — its text
        # is copied verbatim onto a derived claim, never paraphrased by
        # the extractor. Runs on the final claim set (batch or isolation
        # path) and before any write. Keyed by batch index, not db_id:
        # file mode has no ids.
        idx_of = {id(e): i for i, e in enumerate(entries)}
        constraint_idx = {i: e for i, e in enumerate(entries)
                          if e.get("distortion_tolerance") == "constraint"}
        if constraint_idx:
            self._apply_constraint_carrier(pairs, entries)

        def _source_class(c: dict, src_entry: dict | None):
            # The source's distortion class rides onto the derived fact,
            # EXCEPT that ``constraint`` (zero tolerance) is earned only by
            # the claim whose value contains the source text verbatim.
            label = (src_entry or {}).get("distortion_tolerance")
            if not label:
                return INHERIT
            if label == "constraint" and not contains_verbatim(
                    c.get("value"), src_entry.get("text")):
                return INHERIT
            return label

        def _mark_carried(c: dict, src_entry: dict | None) -> None:
            # A derived item now exists (or already existed) for a
            # constraint entry with its text verbatim — the guard's
            # pass condition. Scalars only; a member can't be a carrier.
            # ``op`` is judged the way the writer normalises it: anything
            # but add/remove is written as a scalar.
            if src_entry is None or c.get("op") in ("add", "remove"):
                return
            i = idx_of.get(id(src_entry))
            if i in constraint_idx and contains_verbatim(
                    c.get("value"), src_entry.get("text")):
                carried.add(i)

        # Entities that exist BEFORE this cycle's writes — claims landing on a
        # norm-key outside this set minted a new entity, which the alias-
        # candidate post-pass below screens against existing names.
        with self._lock:
            known_entities = {r.key[0] for r in self._cortex.records
                              if r.status == "current"}
        new_entities: dict[str, str] = {}    # norm -> display, first seen
        # Dream-run audit (schema v27): a run row exists only when the pass
        # has claims to write — outages, zero-claim batches, and retries
        # leave no row (a row per quiet tick would burn the newest-N
        # retention window on passes that provably wrote nothing).
        run_id = None
        run_seq = 0
        # Events with chronicle off are inert — they alone must not mint a
        # run row (same zero-write-no-row rule as an empty extraction).
        actionable = any(c.get("kind") != "event" or chronicle_on
                         for c, _, _ in pairs)
        if pairs and actionable and self._storage is not None:
            with self._lock:
                run_id = self._storage.start_dream_run(
                    _time.time(), self._cortex.dream_cursor, len(entries),
                    extractor=(getattr(extractor, "model", None)
                               or type(extractor).__name__))

        def _finish_run(status: str, cursor_after: float | None) -> None:
            # Bookkeeping must never break a dream (or mask the exception
            # that got us here — the failed path IS the broken-connection
            # path _dream_reflush_stale exists for).
            if run_id is None:
                return
            try:
                with self._lock:
                    self._storage.finish_dream_run(
                        run_id, status=status, finished_at=_time.time(),
                        cursor_after=cursor_after,
                        claims=sum(tally.values()),
                        tallies={**tally,
                                 "literal_flagged": literal_flagged,
                                 "literal_dropped": literal_dropped,
                                 "span_flagged": span_flagged,
                                 "span_parked": span_parked,
                                 "events_inserted": events_inserted,
                                 "events_duplicate": events_duplicate,
                                 "events_pass_failed": events_pass_failed,
                                 "quarantine_parked": qt_parked,
                                 "quarantine_held": qt_held,
                                 "quarantine_promoted": qt_promoted,
                                 "constraint_verbatim": len(carried),
                                 "constraint_missed": (len(constraint_idx)
                                                       - len(carried)),
                                 "quarantined": quarantined})
            except Exception as exc2:  # noqa: BLE001
                logger.warning("dream: run-row finish failed (%s)", exc2)

        # Chronicle event writer, shared by the (dormant with the shipped
        # v5 prompt) inline kind:"event" routing below and the SEPARATE
        # events pass after the claims loop (design doc 2026-08-04):
        # literal gate on the description exactly as for claim values,
        # the batch-corpus date-fabrication guard, exact dedup (batch
        # retries and restatements write and journal nothing), journal
        # kind "event" with the exact chronicle row id for rollback.

        def _write_event(c: dict, src_id) -> None:
            nonlocal literal_flagged, literal_dropped, events_inserted, \
                events_duplicate, run_seq
            desc = str(c.get("description", "")).strip()
            if not desc:
                return
            if gate_mode != "off" and src_id is not None:
                corpus = (batch_text if gate_scope == "batch"
                          else src_text.get(src_id, ""))
                bad = literal_violations(desc, corpus)
                if bad:
                    literal_flagged += 1
                    logger.info(
                        "dream: unsupported literal(s) %s in event %r%s",
                        bad, desc,
                        " (dropped)" if gate_mode == "enforce" else "")
                    if gate_mode == "enforce":
                        literal_dropped += 1
                        return
            actor = str(c.get("actor") or "user")
            idx = c.get("source")
            episode = (entries[idx].get("episode_id")
                       if isinstance(idx, int)
                       and 0 <= idx < len(entries) else None)
            ev_row = {
                "occurred_at": (c.get("date") if batch_has_date
                                else None),
                "occurred_phrase": c.get("date_phrase"),
                "recorded_at": _time.time(),
                "actor": actor, "actor_norm": _norm_key(actor),
                "description": desc,
                "description_norm": _norm_value(desc),
                "episode": episode, "src_entry_id": src_id}
            with self._lock:
                ev_id, ev_action = \
                    self._storage.add_chronicle_event(ev_row)
            if ev_action != "inserted":
                events_duplicate += 1
                return
            events_inserted += 1
            if run_id is not None:
                journal_row = {
                    "seq": run_seq, "entity": actor,
                    "attribute": "event",
                    "entity_norm": _norm_key(actor),
                    "attribute_norm": "event",
                    "kind": "event", "op": None,
                    "prev_kind": None, "prev_value": None,
                    "prev_status": None, "prev_confidence": None,
                    "prev_support": None, "new_value": desc,
                    "action": "event_inserted",
                    "src_entry_id": src_id, "at": _time.time(),
                    "chronicle_event_id": ev_id}
                with self._lock:
                    self._storage.add_dream_run_slot(run_id, journal_row)
                run_seq += 1

        try:
            for c, src_id, src_entry in pairs:
                # Events route before slot resolution — no entity/attribute.
                if c.get("kind") == "event":
                    if chronicle_on:
                        _write_event(c, src_id)
                    continue
                ent, attr = self._resolve_dream_slot(c["entity"], c["attribute"])
                # Claim-level op (Task 7): "add"/"remove" route to the member
                # model; anything else (absent, or a value the extractor got
                # wrong) degrades to the scalar path — never crash mid-dream.
                op = c.get("op")
                if op not in (None, "add", "remove"):
                    logger.warning(
                        "dream: malformed op %r on claim for %s.%s; "
                        "treating as scalar", op, ent, attr)
                    op = None
                ent_norm = _norm_key(ent)
                if ent_norm not in known_entities and ent_norm not in new_entities:
                    new_entities[ent_norm] = ent
                # Literal-faithfulness gate: digit-bearing tokens in the
                # value (outside date-like spans) must appear in the source
                # corpus. Uncited claims skip the gate — abstain, don't
                # guess. Runs before the has_trace guard so a dropped claim
                # never leaves a trace.
                if gate_mode != "off" and src_id is not None:
                    corpus = (batch_text if gate_scope == "batch"
                              else src_text.get(src_id, ""))
                    bad = literal_violations(c["value"], corpus)
                    if bad:
                        literal_flagged += 1
                        logger.info(
                            "dream: unsupported literal(s) %s in claim "
                            "%s.%s = %r%s", bad, ent, attr, c["value"],
                            " (dropped)" if gate_mode == "enforce" else "")
                        if gate_mode == "enforce":
                            literal_dropped += 1
                            continue
                # Provenance-span gate (Feature B): verify the claim's quote
                # against the CITED note only (span_unbacked docstring has
                # the scope rationale). Events never reach here — they
                # routed above. Member ops are counted but never parked
                # (members have no contender path in v1); scalar routing
                # happens at the write branch below.
                span_reason = None
                if span_mode != "off":
                    span_reason = span_unbacked(
                        c.get("quote"),
                        src_entry["text"] if src_entry else "")
                    if span_reason:
                        span_flagged += 1
                        # Reason only — the OUTCOME is not known yet (the
                        # quarantine route below may own the claim), and a
                        # predicted "(parking)" here would poison the
                        # log-audit trail the contend-default decision
                        # reads. Outcomes live in span_parked/the journal.
                        logger.info(
                            "dream: span gate %s for claim %s.%s = %r "
                            "(quote=%r)", span_reason, ent, attr,
                            c["value"], c.get("quote"))
                # The has_trace guard is keyed by (slot, source entry) only —
                # it has no member value to key on, so it must never gate a
                # member op: a second op:"add" for the SAME slot from the
                # SAME source entry (e.g. two collection items in one note)
                # would otherwise read as "already formed this slot" and be
                # silently dropped after the first member. Member ops are
                # already idempotent on their own retry (re-add ->
                # member_confirmed, re-remove -> member_not_found) — that is
                # the property this guard exists to protect for scalars.
                if (op is None and traces_cfg.enabled and src_id is not None
                        and self._storage is not None):
                    with self._lock:
                        already = self._storage.has_trace(
                            _norm_key(ent), _norm_key(attr), src_id)
                    if already:
                        # This source entry already formed this slot once
                        # (batch retry after a mid-batch failure). A
                        # re-dream must be a no-op, not a confirmation —
                        # the confirm path ratchets confidence. The
                        # earlier write is still this entry's carrier.
                        _mark_carried(c, src_entry)
                        continue
                # Pre-image capture (v27 journal): O(1) reads under a short
                # lock, released before the write — the lock is
                # non-reentrant and cortex_write/set_add lock internally
                # (the same hazard the trace-write comment below documents).
                # Snapshot FIELDS, not the record: supersede/remove mutate
                # the old CortexRecord in place, so a held reference would
                # read the post-write status when the journal row is built.
                prev_kind = None
                prev = None      # (value, status, confidence, support) tuple
                if run_id is not None:
                    with self._lock:
                        cur_rec = self._cortex.lookup(ent, attr)
                        mem_recs = self._cortex.members(ent, attr)
                        if cur_rec is not None:
                            prev_kind = "scalar"
                        elif mem_recs:
                            prev_kind = "set"
                        if op is None:
                            prev_rec = cur_rec
                        else:
                            want = _norm_value(c["value"])
                            prev_rec = next(
                                (m for m in mem_recs
                                 if _norm_value(m.value) == want), None)
                            if prev_rec is None and op == "add":
                                # A member-add over a current scalar CONVERTS
                                # the slot (one-way); journal the scalar's
                                # fields — the conversion unwind on rollback
                                # needs its value, and no member matches.
                                prev_rec = cur_rec
                        if prev_rec is not None:
                            prev = (prev_rec.value, prev_rec.status,
                                    prev_rec.confidence,
                                    max(prev_rec.support,
                                        key=lambda t: _TIER_RANK.get(t, 0))
                                    if prev_rec.support else None)
                if op == "add":
                    res = self.set_add(
                        ent, attr, c["value"],
                        confidence=float(c.get("confidence", 0.55)),
                        origin=c.get("origin", "agent"))
                    if res["action"] == "member_invalid":
                        logger.info(
                            "dream: member add rejected (invalid value) "
                            "for %s.%s", ent, attr)
                    elif res["action"] == "member_capped":
                        logger.warning(
                            "dream: member add rejected (cap reached) "
                            "for %s.%s", ent, attr)
                    elif res["action"] == "contested":
                        logger.info(
                            "dream: member add parked by aggregate guard "
                            "for %s.%s", ent, attr)
                elif op == "remove":
                    res = self.set_remove(ent, attr, c["value"])
                else:
                    # Consolidation quarantine (two-man rule): route the
                    # scalar claim BEFORE the write. Scope v1 is scalars
                    # only — member ops keep their existing guards (members
                    # are never contested by design; the aggregate guard
                    # already parks the member-over-scalar case).
                    q_route, q_low = None, False
                    if quarantine_on:
                        q_route, q_witness, q_low = self._quarantine_route(
                            ent, attr, c, src_entry, qt_trusted)
                    # Claim-derived kwargs shared by every scalar write
                    # below — one definition, so the next claim field
                    # cannot silently miss one of the routing paths (this
                    # change originally threaded stance into four
                    # hand-edited sites).
                    claim_kwargs = {
                        "confidence": float(c.get("confidence", 0.55)),
                        "support": c.get("origin", "agent"),
                        "stance": c.get("stance"),
                        # v35: labels are a property of the SOURCE entry,
                        # never of model output — an unlabelled source
                        # inherits whatever the slot already carries.
                        # authority applies to everything derived (who
                        # said it); a CONSTRAINT class applies only to
                        # the claim that carries the text verbatim — a
                        # paraphrased sibling is an observation and must
                        # not be pinned (review finding, 2026-09-02).
                        "authority": ((src_entry or {}).get("authority")
                                      or INHERIT),
                        "distortion_tolerance": _source_class(c, src_entry),
                    }
                    try:
                        if q_route == "promote":
                            # Independent second witness: promote the parked
                            # value via the contender machinery. Support is
                            # the LITERAL "agent" — never the claim's own
                            # origin field, which is model output and
                            # steerable by note text (2026-08-09 review).
                            rr = self.cortex_resolve(
                                ent, attr, accept=True, support="agent")
                            if rr.get("resolved"):
                                qt_promoted += 1
                                res = {"action": "quarantine_promoted"}
                            else:
                                # Raced away (e.g. the user rejected the
                                # contender from another connection between
                                # the route read and this resolve): fail
                                # CLOSED — a low-trust claim still parks.
                                res = self.cortex_write(
                                    ent, attr, c["value"],
                                    provenance=(
                                        ["quarantine:low_trust", q_witness]
                                        if q_low else None),
                                    force_contend=q_low,
                                    **claim_kwargs)
                                if q_low and res["action"] == "contested":
                                    qt_parked += 1
                        elif q_route in ("park", "hold", "hold_ordinary"):
                            # "hold_ordinary" parks WITHOUT the marker: an
                            # ordinary tier-conflict contender must never
                            # become witness-promotable by restatement.
                            prov = (["quarantine:low_trust", q_witness]
                                    if q_route != "hold_ordinary"
                                    else [q_witness])
                            res = self.cortex_write(
                                ent, attr, c["value"],
                                provenance=prov,
                                force_contend=True,
                                **claim_kwargs)
                            # Count OUTCOMES, not routes — a write that
                            # confirmed instead of parking counts nothing
                            # (the friction numbers gate 3 reports must be
                            # honest).
                            if res["action"] == "contested":
                                if q_route == "park":
                                    qt_parked += 1
                                else:
                                    qt_held += 1
                        elif span_mode == "contend" and span_reason:
                            # Span parking: an unbacked scalar claim parks
                            # as a visible contender — a fidelity failure
                            # is never a silent drop (unlike the literal
                            # gate: span failures include benign
                            # paraphrase). When quarantine routing is
                            # active it owns the claim above — trust and
                            # fidelity are orthogonal axes, and the parked
                            # outcome is the same.
                            res = self.cortex_write(
                                ent, attr, c["value"],
                                provenance=["span:unbacked"],
                                force_contend=True,
                                **claim_kwargs)
                            if res["action"] == "contested":
                                span_parked += 1
                        else:
                            res = self.cortex_write(
                                ent, attr, c["value"], **claim_kwargs)
                    except ValueError as exc:
                        if "holds a set" in str(exc):
                            # Spec rule 2: a scalar claim (no op) landing on a
                            # slot that already holds current members is
                            # dropped, not routed or crashed — explicit set
                            # ops (op field or memory_set_add/_remove) are the
                            # only way to touch a set slot.
                            tally["dropped_set_slot"] = tally.get(
                                "dropped_set_slot", 0) + 1
                            logger.info(
                                "dropped scalar claim for set slot %s.%s",
                                ent, attr)
                            continue
                        raise
                tally[res["action"]] = tally.get(res["action"], 0) + 1
                if res["action"] not in ("member_invalid", "member_capped",
                                         "member_not_found"):
                    _mark_carried(c, src_entry)   # contested still exists
                # Journal the write with its ACTUAL returned action —
                # immediately, per claim (crash-durable; a buffered journal
                # would lose exactly the rows whose writes already landed).
                # Actions that stored nothing at all are not journaled;
                # "contested" IS (a contender row persisted, and rollback
                # has a reversal for it).
                if (run_id is not None and res["action"] not in
                        ("member_invalid", "member_capped",
                         "member_not_found")):
                    journal_row = {
                        "seq": run_seq, "entity": ent, "attribute": attr,
                        "entity_norm": _norm_key(ent),
                        "attribute_norm": _norm_key(attr),
                        "kind": "member" if op else "scalar",
                        "op": op,
                        "prev_kind": prev_kind,
                        "prev_value": prev[0] if prev else None,
                        "prev_status": prev[1] if prev else None,
                        "prev_confidence": prev[2] if prev else None,
                        "prev_support": prev[3] if prev else None,
                        "new_value": c["value"],
                        "action": res["action"],
                        "src_entry_id": src_id,
                        "at": _time.time()}
                    with self._lock:
                        self._storage.add_dream_run_slot(run_id, journal_row)
                    run_seq += 1
                if res["action"] in ("member_invalid", "member_capped", "contested"):
                    # Nothing was actually stored — a trace/reinforcement
                    # bump here would link a source entry to a slot it never
                    # populated, and (combined with the has_trace guard
                    # above) could mask a later legitimate write. "contested"
                    # covers both the aggregate-conversion guard's blocked
                    # add (review finding: it left a stray trace that then
                    # silently suppressed a later same-entry scalar claim via
                    # the has_trace guard) and a plain weaker-tier scalar
                    # conflict — neither changes the slot's current value.
                    continue
                if (traces_cfg.enabled and src_id is not None
                        and self._storage is not None):
                    # Serialize trace writes on the shared psycopg connection:
                    # dream_run holds no outer lock (cortex_write locks internally
                    # and has already released), so the trace writes must take
                    # self._lock themselves. Scope it to JUST these calls — the
                    # lock is non-reentrant, so including cortex_write would deadlock.
                    with self._lock:
                        if self._storage.add_trace(
                                _norm_key(ent), _norm_key(attr), src_id, _time.time()):
                            self._storage.bump_reinforcements(src_id, 1)
                            if self._cms is not None:
                                self._cms.bump_entry_reinforcements(src_id, 1)
                            traces_n += 1
            # Separate events pass (design doc 2026-08-04): after the
            # claims loop so a claims failure never wastes the call, and
            # before dream_commit so event writes journal under this run.
            # The extractor call failing is NON-FATAL by design — events
            # are additive enrichment and must never stall consolidation
            # (the lost batch is not retried; the cursor moves). A
            # STORAGE failure inside _write_event still propagates to the
            # handler below and honestly marks the run failed.
            if chronicle_on and hasattr(extractor, "extract_events"):
                try:
                    ev_items = extractor.extract_events(texts)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dream: events pass failed (%s); events skipped "
                        "this cycle, claims unaffected", exc)
                    events_pass_failed = True
                    ev_items = []
                for c in ev_items:
                    idx = c.get("source")
                    if isinstance(idx, int) and 0 <= idx < len(entries):
                        ev_src = entries[idx].get("db_id")
                    elif len(entries) == 1:
                        ev_src = entries[0].get("db_id")
                    else:
                        ev_src = None
                    _write_event(c, ev_src)
            # Post-dream GUARD VERIFIER (TypeCompact, arXiv 2608.22752):
            # every constraint entry in the window must have a derived
            # item carrying its text verbatim. FLAG, not hard fail: the
            # paper fails a compaction whose input is still there to
            # retry; here the raw entry is never discarded, and a hard
            # fail would hold every other claim in the batch hostage to
            # one rule the extractor could not slot.
            constraint_misses = self._constraint_misses(
                constraint_idx, carried)
        except Exception as exc:  # noqa: BLE001 — a write failure must hold the cursor too
            # Partial writes may have landed and are journaled — record
            # `failed` (NOT a silent absence) so rollback can refuse to
            # reach past this run's unjournaled uncertainty.
            _finish_run("failed", None)
            healed = self._dream_reflush_stale(entries)
            if healed:
                return _held(f"claim write failed ({healed} stale entry id(s) "
                             "re-flushed; mapping repaired)", exc)
            return _held("claim write failed", exc)
        newest = max(e["timestamp"] for e in entries)
        self.dream_commit(newest)
        # Stamp `committed` HERE — before relations/lessons/graph — so a
        # failure in that bookkeeping block cannot mislabel a run whose
        # cortex writes and cursor advance really did happen.
        _finish_run("committed", newest)
        relations_n = self._dream_extract_relations(
            extractor, texts,
            batch_sources={e["source"] for e in entries if e.get("source")})
        # After relations linking, so paraphrase entities the relations pass
        # just created resolve to their graph nodes instead of re-creating.
        alias_candidates = self._propose_dream_alias_candidates(
            new_entities, known_entities)
        outcome_inference = self.infer_outcomes_stage(extractor)
        lessons = self.synthesize_lessons(extractor)
        # Lesson synthesis runs after the commit stamp by design, so its
        # counters never made it into the run row — merge them in now (a
        # bookkeeping failure must not break the dream).
        if run_id is not None:
            try:
                with self._lock:
                    self._storage.update_dream_run_tallies(run_id, {
                        "lesson_signals": int(lessons.get("signals", 0)),
                        "lessons_written": int(lessons.get("lessons", 0)),
                        "lessons_deduped": int(lessons.get("deduped", 0))})
            except Exception as exc2:  # noqa: BLE001
                logger.warning("dream: lesson-tally update failed (%s)", exc2)
        graph_insight = self._safe_refresh_graph_insight()
        sources_attributed = self.graph_backfill_sources().get("attributed", 0)
        # Drain a few quarantined untyped pairs per dream (no-op once empty).
        retyped = self.retype_quarantined_links(
            extractor, limit=self.config.memory.dream.retype_quarantined_max)
        return {"pulled": len(entries), "claims": sum(tally.values()),
                "cursor": newest, "relations": relations_n, **tally,
                "alias_candidates": alias_candidates,
                # Digests run only on the empty-pull branch (idle cycle);
                # the key is present on both exits so the result shape is
                # stable for observers.
                "digests": {"scanned": 0, "written": 0,
                            "skipped": "claims-branch"},
                "lessons": lessons, "outcome_inference": outcome_inference,
                "graph_insight": graph_insight,
                "literal_flagged": literal_flagged,
                "literal_dropped": literal_dropped,
                "span_flagged": span_flagged,
                "span_parked": span_parked,
                "quarantine_parked": qt_parked,
                "quarantine_held": qt_held,
                "quarantine_promoted": qt_promoted,
                "events_inserted": events_inserted,
                "events_duplicate": events_duplicate,
                "events_pass_failed": events_pass_failed,
                "constraint_verbatim": len(carried),
                "constraint_misses": constraint_misses,
                "traces": traces_n, "sources_attributed": sources_attributed,
                "quarantined": quarantined, "retyped": retyped}

    def _carrier_slot_eligible(self, entity: str, attribute: str) -> bool:
        """A carrier may land only on a slot that is EMPTY or already holds a
        CONSTRAINT: copying the rule over a standing non-constraint fact
        would destroy a correct value (the 2026-09-02 peer review reproduced
        exactly that — ``bank-volumes.kind = external`` superseded by a
        deploy rule because it happened to be the extractor's first claim).
        Resolves the slot the way the write loop will."""
        ent, attr = self._resolve_dream_slot(entity, attribute)
        with self._lock:
            self._ensure_init()
            cur = self._cortex.lookup(ent, attr)
            if cur is None:
                return not self._cortex.members(ent, attr)   # a set slot is never a carrier
            return cur.distortion_tolerance == "constraint"

    def _apply_constraint_carrier(self, pairs: list, entries: list[dict]) -> int:
        """TypeCompact (arXiv 2608.22752): for each CONSTRAINT-labelled
        source entry, make sure at least one derived scalar claim carries
        the entry's text verbatim. If the extractor paraphrased every
        claim it cited the entry for, ONE claim's value is replaced with
        the entry text — the extractor's entity/attribute are kept
        (slotting is what it is good at; wording is not).

        Which claim: among the scalar claims citing the entry whose target
        slot is empty or already a constraint (``_carrier_slot_eligible``),
        the one whose content tokens overlap the rule text most, and only
        if at least one token overlaps — a claim about something else
        entirely is never hijacked, whatever position it has. Position
        in extractor output decides nothing. If no claim is eligible the
        carrier REFUSES and the guard reports the miss (flag-not-fail):
        an entry with no scalar claim, or whose only claims target
        occupied non-constraint slots, is left for the reader. Returns
        the number of claims rewritten; mutates the claim dicts in
        ``pairs`` in place."""
        idx_of = {id(e): i for i, e in enumerate(entries)}
        by_src: dict[int, list[dict]] = {}
        for c, _sid, se in pairs:
            if se is None or c.get("kind") == "event":
                continue
            by_src.setdefault(idx_of[id(se)], []).append(c)
        rewritten = 0
        for i, e in enumerate(entries):
            if e.get("distortion_tolerance") != "constraint":
                continue
            text = e.get("text") or ""
            claims = by_src.get(i, [])
            # ``op`` judged as the writer normalises it (anything but
            # add/remove degrades to a scalar write).
            scalars = [c for c in claims if c.get("op") not in ("add", "remove")]
            if any(contains_verbatim(c.get("value"), text) for c in scalars):
                continue
            rule_tokens = content_tokens(text)
            best, best_overlap = None, 0
            for c in scalars:
                overlap = len(rule_tokens & content_tokens(
                    f"{c.get('attribute', '')} {c.get('value', '')}"))
                if overlap <= best_overlap:
                    continue           # ties keep the earlier candidate
                if not self._carrier_slot_eligible(c.get("entity", ""),
                                                   c.get("attribute", "")):
                    continue
                best, best_overlap = c, overlap
            if best is None:
                logger.info("dream: constraint entry %s has no eligible carrier "
                            "claim (none overlaps the rule on an empty or "
                            "constraint slot); left for the guard",
                            e.get("db_id") if e.get("db_id") is not None else i)
                continue
            logger.info("dream: constraint entry %s carried verbatim onto "
                        "%s.%s (extractor value %r replaced; %d overlapping "
                        "tokens)",
                        e.get("db_id") if e.get("db_id") is not None else i,
                        best.get("entity"), best.get("attribute"),
                        best.get("value"), best_overlap)
            best["value"] = text
            rewritten += 1
        return rewritten

    @staticmethod
    def _constraint_misses(constraint_idx: dict[int, dict],
                           carried: set[int]) -> list[dict]:
        """The guard verifier's report: constraint entries in the processed
        window with NO derived item carrying their text verbatim. Logged
        at WARNING per miss and returned on the dream result
        (``constraint_misses``) and the run row (``constraint_missed``)."""
        misses = []
        for i, e in constraint_idx.items():
            if i in carried:
                continue
            text = e.get("text") or ""
            logger.warning("dream: constraint entry %s has NO verbatim derived "
                           "item after this pass (extractor emitted no scalar "
                           "claim for it): %r",
                           e.get("db_id") if e.get("db_id") is not None else i,
                           text[:120])
            misses.append({"entry_id": e.get("db_id"), "text": text})
        return misses

    def _quarantine_route(self, ent: str, attr: str, c: dict,
                          src_entry: dict | None,
                          trusted: set[str]) -> tuple[str | None, str, bool]:
        """Route one scalar dream claim under the two-man rule.

        Returns ``(route, witness, low)``:

        * ``"promote"`` — a MARKED parked value gains an independent
          second witness AND the witness's tier (entry-metadata-derived,
          never claim text) is not below the current record's origin —
          the two-man rule must never be weaker than the provenance
          guard it reinforces (2026-08-09 review: two agent witnesses
          must not supersede a user fact);
        * ``"park"`` — first low-trust sighting (fresh marked contender);
        * ``"hold"`` — restating a marked parked value without meeting
          the promote conditions (confirms, accumulates the witness);
        * ``"hold_ordinary"`` — a low-trust claim matching an ORDINARY
          tier-conflict contender: parked without the marker so an
          explicit-resolve-only contender never becomes
          witness-promotable by restatement;
        * ``None`` — not quarantine's business (normal write): trusted
          claims with no marked contender in play, and any claim
          corroborating the CANONICAL value (confirm must not stamp the
          marker onto the current record).
        """
        from pseudolife_memory.memory.cortex import (_TIER_RANK,
                                                     _is_compression_echo,
                                                     _norm_value)
        # Late import: service.py imports this module at load time, so the
        # shared quarantine helpers cannot be imported at module level.
        from pseudolife_memory.service import (_origin_from_source,
                                               _quarantine_low_trust,
                                               _quarantine_witness)

        low = _quarantine_low_trust(c, src_entry, trusted)
        witness = _quarantine_witness(c, src_entry)
        # The witness's tier comes from the backing entry's source (the
        # only trust base the rule stands on); an unbacked claim ranks as
        # agent regardless of what its text asserts.
        claim_tier = ((_origin_from_source(src_entry.get("source"))
                       if src_entry is not None else None) or "agent")
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            cur = self._cortex.lookup(ent, attr)
            cur_value = cur.value if cur is not None else None
            cur_origin = cur.origin if cur is not None else None
            match = next(
                (r for r in self._cortex.contenders_for(ent, attr)
                 if _norm_value(r.value) == _norm_value(c["value"])), None)
            marked = (match is not None
                      and "quarantine:low_trust" in match.provenance)
            witnesses = set(match.provenance) if match is not None else set()
        if cur_value is not None and (
                _norm_value(cur_value) == _norm_value(c["value"])
                or _is_compression_echo(c["value"], cur_value)):
            return None, witness, False
        if marked:
            independent = (not low) or witness not in witnesses
            tier_ok = (cur_origin is None
                       or _TIER_RANK.get(claim_tier, 0)
                       >= _TIER_RANK.get(cur_origin, 0))
            if independent and tier_ok:
                return "promote", witness, low
            return "hold", witness, low
        if match is not None:
            return ("hold_ordinary" if low else None), witness, low
        if low:
            return "park", witness, low
        return None, witness, low

    def dream_rollback(self, run_id: int | None = None) -> dict[str, Any]:
        """Revert the latest committed dream pass by replaying its v27
        pre-image journal in reverse through the normal write paths
        (supersede-back — nothing is deleted). Refused when a newer run is
        ``failed``/``running`` (unjournaled or partial uncertainty), when
        ``run_id`` names anything but the latest committed run, and in file
        mode. Keeps source traces and does NOT rewind the dream cursor
        (design doc 2026-08-01: has_trace gates scalars but never member
        ops, so a rewind would re-add reverted members while suppressing
        reverted scalars)."""
        from pseudolife_memory.memory.cortex import _norm_value
        import time as _time

        if self._storage is None:
            return {"error": "requires_postgres"}
        with self._lock:
            self._ensure_init()
            target = self._storage.latest_committed_dream_run()
        if target is None:
            return {"error": "no_committed_run"}
        if run_id is not None and run_id != target["id"]:
            return {"error": "stale_run_id", "latest": target["id"]}
        with self._lock:
            newer = self._storage.dream_runs_newer_than(target["id"])
        blocking = [r for r in newer if r["status"] in ("failed", "running")]
        if blocking:
            return {"error": "newer_unjournaled_runs",
                    "runs": [{"id": r["id"], "status": r["status"]}
                             for r in blocking]}
        with self._lock:
            journal = self._storage.dream_run_journal(target["id"])

        counts = {"reverted": 0, "skipped": 0, "partial": 0}
        details: list[dict[str, Any]] = []

        def _prev_stance(row: dict) -> str | None:
            # v29: the journal's fixed columns carry no stance (spec
            # amendment 3), so recover it from the superseded record
            # itself — superseded rows keep every field as audit history.
            # Newest matching supersession wins; None (plainly asserted)
            # when no superseded record for that value carried a hedge.
            want = _norm_value(row["prev_value"] or "")
            with self._lock:
                recs = [r for r in self._cortex.records_for(
                            row["entity"], row["attribute"])
                        if r.status == "superseded" and r.stance
                        and _norm_value(r.value) == want]
            if not recs:
                return None
            return max(recs, key=lambda r: r.superseded_at or 0).stance

        def _prev_label(row: dict, attr: str):
            # v35: the journal carries no labels either; recover them from
            # the NEWEST superseded record for that value. None when it
            # carried none — passed explicitly so the rewrite CLEARS
            # rather than inheriting the label the rolled-back write set.
            want = _norm_value(row["prev_value"] or "")
            with self._lock:
                recs = [r for r in self._cortex.records_for(
                            row["entity"], row["attribute"])
                        if r.status == "superseded"
                        and _norm_value(r.value) == want]
            if not recs:
                return None
            return getattr(max(recs, key=lambda r: r.superseded_at or 0), attr)

        def _rewrite_prev(row: dict) -> str:
            res = self.cortex_write(
                row["entity"], row["attribute"], row["prev_value"],
                confidence=float(row["prev_confidence"] or 0.55),
                support=row["prev_support"] or "agent",
                stance=_prev_stance(row),
                authority=_prev_label(row, "authority"),
                distortion_tolerance=_prev_label(row, "distortion_tolerance"))
            if res["action"] == "contested":
                # Rollback is explicit authority: a low-confidence prev
                # must still win the slot back (the same path resolve
                # exists for).
                self.cortex_resolve(row["entity"], row["attribute"],
                                    accept=True)
            # track=False: this lookup verifies the rollback took — it is
            # not a serve, and counting it would pollute the read signal.
            cur = self.cortex_lookup(row["entity"], row["attribute"],
                                     track=False)
            if cur and _norm_value(cur.get("value", "")) == _norm_value(
                    row["prev_value"]):
                return "reverted"
            return "partial:value_not_restored"

        for row in reversed(journal):
            action = row["action"]
            outcome = "skipped:no_reversal"
            try:
                if action == "contested":
                    cands = self._cortex.contenders_for(
                        row["entity"], row["attribute"])
                    if any(_norm_value(c.value) == _norm_value(
                            row["new_value"] or "") for c in cands):
                        self.cortex_resolve(row["entity"], row["attribute"],
                                            accept=False)
                        outcome = "reverted"
                    else:
                        outcome = "skipped:superseded_by_later"
                elif row["kind"] == "event":
                    # Chronicle rows are additive-only, so the reversal of
                    # an insert is a plain delete of the exact row the
                    # journal names (schema v28).
                    ev_id = row.get("chronicle_event_id")
                    deleted = False
                    if ev_id is not None:
                        with self._lock:
                            deleted = self._storage.delete_chronicle_event(
                                ev_id)
                    outcome = "reverted" if deleted else "skipped:already_gone"
                elif row["kind"] == "scalar":
                    if action == "inserted" and row["prev_status"] is None:
                        with self._lock:
                            res = self._cortex.retire_current(
                                row["entity"], row["attribute"])
                        outcome = ("reverted" if res is not None
                                   else "skipped:already_gone")
                    elif action == "superseded":
                        outcome = _rewrite_prev(row)
                    elif action == "quarantine_promoted":
                        # Reversal of a two-man promotion: restore the
                        # previous current (the promoted value stays in
                        # history as superseded, not re-parked — rollback
                        # is a revert, not a re-quarantine). A promotion
                        # onto an EMPTY slot reverses like an insert.
                        if row["prev_status"] is None:
                            with self._lock:
                                res = self._cortex.retire_current(
                                    row["entity"], row["attribute"])
                            outcome = ("reverted" if res is not None
                                       else "skipped:already_gone")
                        else:
                            outcome = _rewrite_prev(row)
                    elif action == "confirmed":
                        # Only confidence/last_confirmed moved; unwinding
                        # that is not expressible through the write path.
                        outcome = "skipped:confirmed"
                else:  # member
                    if action == "member_added":
                        self.set_remove(row["entity"], row["attribute"],
                                        row["new_value"])
                        outcome = "reverted"
                        if (row["prev_kind"] == "scalar"
                                and row["prev_value"]):
                            with self._lock:
                                mems = self._cortex.members(
                                    row["entity"], row["attribute"])
                            survivors = [_norm_value(m.value) for m in mems]
                            if survivors == [_norm_value(row["prev_value"])]:
                                # Sole survivor is the converted scalar —
                                # unwind the one-way scalar->set conversion.
                                self.set_remove(row["entity"],
                                                row["attribute"],
                                                row["prev_value"])
                                outcome = _rewrite_prev(row)
                            else:
                                outcome = "partial:set_retained"
                    elif action == "member_removed":
                        self.set_add(
                            row["entity"], row["attribute"],
                            row["prev_value"],
                            confidence=float(row["prev_confidence"] or 0.55),
                            origin=row["prev_support"] or "agent")
                        outcome = "reverted"
                    elif action == "member_confirmed":
                        outcome = "skipped:confirmed"
            except Exception as exc:  # noqa: BLE001 — keep unwinding
                logger.warning("rollback: reversal failed at seq %s (%s)",
                               row["seq"], exc)
                outcome = f"partial:error:{type(exc).__name__}"
            bucket = outcome.split(":", 1)[0]
            counts[bucket] = counts.get(bucket, 0) + 1
            details.append({"seq": row["seq"], "entity": row["entity"],
                            "attribute": row["attribute"],
                            "action": action, "outcome": outcome})
        with self._lock:
            self._save_cortex()
            self._storage.mark_dream_run_rolled_back(
                target["id"], _time.time())
        return {"run_id": target["id"], **counts, "details": details}

    def dream_run_auto(self, *, limit: int | None = None) -> dict[str, Any]:
        """dream_run with primary/fallback extractor selection (2026-07-11
        sonnet-sidecar-cutover spec): probe-and-choose per invocation, record
        which side served for dream_status, and stamp the result. The single
        entry point for every LIVE dream trigger (sweep, console, MCP tool,
        session-end); the bench harness keeps calling dream_run directly."""
        from pseudolife_memory.memory.dream import build_extractor_with_fallback
        try:
            extractor, which = build_extractor_with_fallback(
                self.config.memory.dream)
        except ValueError as e:
            return {"error": str(e), "pulled": 0, "claims": 0}
        import time as _t
        self._last_dream_extractor = {
            "which": which,
            "base_url": getattr(extractor, "base_url", None),
            "at": _t.time(),
        }
        result = self.dream_run(extractor, limit=limit)
        result["extractor"] = which
        return result

    def _dream_reflush_stale(self, entries: list[dict]) -> int:
        """After a claim/trace write failure, verify the pulled batch's
        in-memory→PG entry mapping and re-insert entries whose rows are gone
        (a connection lost mid-store could hand out a RETURNING id for an
        insert that never committed — see PostgresStorage._txn). Without
        this, a memory_traces FK violation recurs verbatim every sweep: a
        permanent dream stall. Healing turns it into a single held sweep —
        the next pull sees the fresh ids. Never raises."""
        if self._storage is None:
            return 0
        ids = [e["db_id"] for e in entries if e.get("db_id") is not None]
        if not ids:
            return 0
        try:
            with self._lock:
                assert self._cms is not None
                missing = set(ids) - self._storage.existing_entry_ids(ids)
                if not missing:
                    return 0
                return self._cms.reflush_entries(missing)
        except Exception as exc:  # noqa: BLE001 — healing must never mask the hold
            logger.warning("dream stale-id heal failed (%s)", exc)
            return 0

    def dream_status(self) -> dict[str, Any]:
        """Backlog (eligible unconsolidated memories), idle seconds since the most
        recent store, and whether the trigger would fire. Read-only — safe for a
        SessionStart nudge hook."""
        import time as _t
        cfg = self.config.memory.dream
        backlog = self.dream_pull(limit=10**9)["count"]

        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._cortex is not None
            latest = max(
                (e.timestamp for b in self._cms.bands for e in b.entries),
                default=0.0,
            )
            cursor = self._cortex.dream_cursor

            lessons_cfg = self.config.memory.lessons
            from pseudolife_memory.memory.dream import resolve_endpoints
            _r = resolve_endpoints(cfg)
            _has_extractor = bool(
                (_r["primary_url"] and _r["primary_model"])
                or (_r["fallback_url"] and _r["fallback_model"]))
            if (lessons_cfg.enabled and lessons_cfg.infer_outcomes
                    and lessons_cfg.infer_outcomes_max_signals > 0
                    and _has_extractor):
                infer_pending = len(self._pending_inference_candidates())
                retry_pending = len(self._load_infer_cursor()["retry"])
            else:
                infer_pending = retry_pending = 0
            if cfg.digest_enabled and _has_extractor:
                digest_pending = len(self._pending_digest_candidates())
                digest_retry = len(self._load_digest_cursor()["retry"])
            else:
                digest_pending = digest_retry = 0

        idle = (_t.time() - latest) if latest else 0.0
        would_fire = bool(cfg.enabled and (
            backlog >= cfg.min_batch
            or (backlog >= 1 and idle >= cfg.idle_seconds)
            or infer_pending >= 1
            # Digests run only on the empty-pull branch (idle cycle, by
            # design), so a digest backlog fires the sweep only once the
            # pull would be empty. Unconditional firing consolidated a
            # partial batch every tick while entries were pending — zero
            # digest progress at broken cadence (pre-PR review, 2026-08-27).
            or (digest_pending >= 1 and backlog == 0)
        ))
        from pseudolife_memory.memory.dream import _status_extractor_fields
        return {"backlog": backlog, "idle_seconds": idle,
                "dream_cursor": cursor, "would_fire": would_fire,
                "infer_outcomes": {"pending": infer_pending,
                                   "retry_pending": retry_pending},
                "digests": {"pending": digest_pending,
                            "retry_pending": digest_retry},
                # Harness-agnostic deep-dream nudge: any MCP client polling
                # status (or a SessionStart hook) can surface this to its
                # user. Computed outside the lock above — deep_dream_need
                # takes the (non-reentrant) service lock itself.
                "deep_dream": self.deep_dream_need(),
                **_status_extractor_fields(
                    cfg, getattr(self, "_last_dream_extractor", None))}

    def _fire_and_forget_dream(self) -> None:
        """Run one dream cycle in a daemon thread so SessionEnd never blocks on
        the extractor. Errors are logged, never raised."""
        import threading

        def _run() -> None:
            try:
                self.dream_run_auto()
            except Exception:  # noqa: BLE001 — background best-effort
                logger.warning("session-end dream failed", exc_info=True)

        threading.Thread(target=_run, name="session-end-dream",
                         daemon=True).start()

    def _load_infer_cursor(self) -> dict:
        """Meta-backed cursor for the auto-outcome-inference scan. Shape
        ``{"ts": float, "retry": {episode_id: attempt_count}}``. File mode
        (no storage) always sees the default — the scan is a no-op there."""
        raw = self._storage.get_meta(self._INFER_CURSOR_KEY) \
            if self._storage else None
        if isinstance(raw, dict):
            return {"ts": float(raw.get("ts", 0.0)),
                    "retry": dict(raw.get("retry", {}))}
        return {"ts": 0.0, "retry": {}}

    def _save_infer_cursor(self, cur: dict) -> None:
        if self._storage is not None:
            self._storage.set_meta(self._INFER_CURSOR_KEY, cur)

    def _episode_inference_context(self, root, subtree: set[str]) -> str:
        """All daemon-visible session context, INCLUDING status/log-source
        entries — dream.exclude_sources protects fact extraction, not this
        (spec 2026-07-18, decision 2)."""
        assert self._cms is not None
        em = self._cms.episodes
        lines = [f"Session: {root.title or '(untitled)'}"]
        for e in em.episodes.values():
            if e.id in subtree and e.id != root.id and e.title:
                lines.append(f"Sub-task: {e.title}")
        entries = [en for band in self._cms.bands for en in band.entries
                   if en.episode_id in subtree]
        entries.sort(key=lambda en: en.timestamp)
        for en in entries:
            mark = " [superseded]" if en.superseded_at else ""
            lines.append(f"- ({en.source}){mark} {en.text}")
        return "\n".join(lines)

    def _pending_inference_candidates(self, *, limit: int = 8) -> list[dict]:
        """Caller MUST hold the lock. Closed session roots past the cursor
        with >=1 subtree entry and zero subtree outcome signals."""
        assert self._cms is not None
        if self._storage is None:
            return []
        cur = self._load_infer_cursor()
        em = self._cms.episodes
        counts = self._episode_entry_counts()
        roots = sorted(
            (e for e in em.episodes.values()
             if e.parent_id is None and e.session_key
             and e.ended_at is not None and e.ended_at > cur["ts"]),
            key=lambda e: e.ended_at)
        out: list[dict] = []
        for root in roots:
            subtree = {root.id} | {
                e.id for e in em.episodes.values()
                if em._descends_from(e, root.id)}
            if sum(counts.get(i, 0) for i in subtree) == 0:
                continue
            if self._storage.count_signals_for_episodes(list(subtree)) > 0:
                continue
            out.append({"root_id": root.id, "ended_at": root.ended_at,
                        "context": self._episode_inference_context(
                            root, subtree)})
            if len(out) >= limit:
                break
        return out

    def _load_digest_cursor(self) -> dict:
        """Meta-backed cursor for the session-digest scan (spec 2026-08-24).
        Shape ``{"ts": float, "retry": {episode_id: attempt_count}}``. The
        zero start is load-bearing: enabling the feature backfills every
        historical closed session (ratified decision 2)."""
        raw = self._storage.get_meta(self._DIGEST_CURSOR_KEY) \
            if self._storage else None
        if isinstance(raw, dict):
            return {"ts": float(raw.get("ts", 0.0)),
                    "retry": dict(raw.get("retry", {}))}
        return {"ts": 0.0, "retry": {}}

    def _save_digest_cursor(self, cur: dict) -> None:
        if self._storage is not None:
            self._storage.set_meta(self._DIGEST_CURSOR_KEY, cur)

    def _pending_digest_candidates(
            self, *, limit: int | None = None) -> list[dict]:
        """Caller MUST hold the lock. Closed session roots past the digest
        cursor with >=1 subtree entry and no existing digest entry, oldest
        first, capped at ``digest_max_per_cycle`` (bounds the backfill)."""
        assert self._cms is not None
        if self._storage is None:
            return []
        cfg = self.config.memory.dream
        cap = int(limit if limit is not None else cfg.digest_max_per_cycle)
        cur = self._load_digest_cursor()
        em = self._cms.episodes
        counts = self._episode_entry_counts()
        digested = {e.episode_id for band in self._cms.bands
                    for e in band.entries
                    if e.source == "digest" and e.episode_id}
        roots = sorted(
            (e for e in em.episodes.values()
             if e.parent_id is None and e.session_key
             and e.ended_at is not None and e.ended_at > cur["ts"]),
            key=lambda e: e.ended_at)
        out: list[dict] = []
        for root in roots:
            if root.id in digested:
                continue
            subtree = {root.id} | {
                e.id for e in em.episodes.values()
                if em._descends_from(e, root.id)}
            n_entries = sum(counts.get(i, 0) for i in subtree)
            if n_entries == 0:
                continue
            out.append({"root_id": root.id, "ended_at": root.ended_at,
                        "started_at": root.started_at,
                        "title": root.title or "(untitled)",
                        "n_entries": n_entries,
                        "context": self._episode_inference_context(
                            root, subtree)})
            if len(out) >= cap:
                break
        return out

    def _store_digest(self, text: str, episode_id: str,
                      episode_title: str) -> None:
        """Write one digest entry OUTSIDE the CMS gate pipeline (spec
        2026-08-24, decision 4): no surprise gate (a digest restates
        session content, so it is low-surprise by construction), no
        contradiction decay (it must never supersede the turns it
        summarizes), no slot extraction (narrative, not a fact source).
        Stamps the SUMMARIZED episode, not the currently open one — retitle
        and merge then treat it like the episode's other entries. Caller
        MUST hold the lock."""
        assert self._cms is not None
        emb = self._embedder.encode_single(text)
        band = self._cms.bands[0]
        band.store(text, emb, source="digest", surprise=1.0)
        entry = band.entries[-1]
        entry.episode_id = episode_id
        entry.episode_title = episode_title
        entry.tags = ["digest"]
        if self._storage is not None:
            from pseudolife_memory.storage.sync import entry_to_row
            entry.db_id = self._storage.insert_entry(entry_to_row(entry))

    def _contested_facts(self) -> list[dict]:
        """Contested cortex facts shaped for graph_insight.suggest_questions.
        Mirrors how cortex_search detects contention: current_records() +
        contenders_for(). CortexRecord exposes .entity/.attribute/.value."""
        out = []
        with self._lock:
            self._ensure_init()
            if self._cortex is None:
                return out
            for r in self._cortex.current_records():
                conts = self._cortex.contenders_for(r.entity, r.attribute)
                if conts:
                    out.append({
                        "entity": r.entity, "attribute": r.attribute, "value": r.value,
                        "contender_value": conts[0].value,
                        "contender_origin": conts[0].origin,
                    })
        return out

    def _refresh_graph_insight(self) -> dict[str, Any]:
        """Recompute communities + digest from the live graph and persist. Read
        inputs under the lock, compute lock-free, persist under the lock."""
        import time as _time
        from pseudolife_memory.memory import graph_insight as gi
        cfg = self.config.memory.graph_insight
        if not cfg.enabled:
            return {"refreshed": False, "reason": "disabled"}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"refreshed": False, "reason": "no_storage"}
            g = self._storage.load_graph()
            prior = self._storage.load_communities()["assignment"]
        if not g["edges"]:
            return {"refreshed": False, "reason": "empty_graph"}
        contested = self._contested_facts()
        communities = gi.detect_communities(
            g["edges"], resolution=cfg.resolution,
            max_community_fraction=cfg.max_community_fraction, algorithm=cfg.algorithm)
        communities = gi.remap_to_previous(communities, prior)
        summaries = gi.summarize_communities(communities, g["edges"], g["entities"])
        assignment = {eid: cid for cid, ids in communities.items() for eid in ids}
        computed_at = _time.time()
        digest = gi.build_digest(
            communities, summaries, g["edges"], g["entities"], contested, computed_at,
            god_top_n=cfg.god_nodes_top_n, surprises_top_n=cfg.surprises_top_n,
            questions_top_n=cfg.questions_top_n, betweenness_sample=cfg.betweenness_sample)
        with self._lock:
            self._storage.replace_communities(assignment, summaries, computed_at)
            self._storage.set_meta("graph_digest", digest)
        return {"refreshed": True, "communities": len(summaries)}

    def _safe_refresh_graph_insight(self) -> dict[str, Any]:
        """Run _refresh_graph_insight, swallowing any failure so a refresh can
        never break a dream. Shared by both dream_run paths."""
        try:
            return self._refresh_graph_insight()
        except Exception as exc:  # noqa: BLE001 — insight must never break a dream
            logger.warning("graph-insight refresh failed (%s); dream unaffected", exc)
            return {"refreshed": False, "error": str(exc)}

    def deep_dream(self, *, apply: bool = False,
                   include_snippets: bool = True) -> dict[str, Any]:
        """Manual full-corpus graph consolidation. Step A (self-clean) + Step B
        (candidate generation), both deterministic. dry-run (default) computes and
        returns a preview + candidates without writing; apply first writes a graph
        snapshot (undo artifact — refuses with ``snapshot_failed`` if it can't),
        then commits the re-score and (when auto_apply_safe) the provably-safe
        supersede/merge class. Discovered links are NOT written here — Step C (the
        /dream agent) proposes them via graph_propose_links.
        ``include_snippets=False`` omits candidate evidence for callers who only
        want the scores."""
        from pseudolife_memory.memory import graph_consolidation as gc
        cfg = self.config.memory.deep_dream
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            g = self._storage.load_graph()
            scope_map = self._storage.entity_sources_map()
            traces = self._storage.traces_by_entity_norm()
            entries = self._storage.load_entries()
            dismissed = self._storage.dismissed_pairs()
            prop_keys = self._storage.entity_proposal_keys()
            pending_props = self._storage.pending_entity_proposals()
            pending_links = self._storage.pending_proposals()
            lesson_refs = self._storage.lesson_entity_ids()
            fact_counts = self._storage.entity_fact_counts()
            fact_texts = self._storage.current_fact_counts_by_entity_text()
            lesson_recs = self._curation_records("lesson", cfg.snippet_max_chars)
            world_recs = self._curation_records("world", cfg.snippet_max_chars)
        entities, edges = g["entities"], g["edges"]

        rescore = gc.rescore_edges(edges, entities)
        violations = gc.hard_violation_edges(edges, entities)
        dups = gc.exact_duplicate_pairs(entities, edges)
        from pseudolife_memory.graph import norm_name as _nn
        known_norms = frozenset(
            {e["canonical"] for e in entities}
            | {_nn(a) for als in g["aliases"].values() for a in als})
        junk = gc.junk_entities(entities, edges, max_degree=cfg.junk_max_degree,
                                known_norms=known_norms)
        # Keep tombstones: a name whose junk proposal was rejected is a real
        # referent by verdict — never re-filed, never auto-deleted, even
        # after the entity churned and re-minted (the rejected proposal
        # row CASCADEs away; the text-keyed tombstone does not).
        from pseudolife_memory.service import _kept_junk_norms
        kept = _kept_junk_norms(dismissed)
        if kept:
            _canon = {e["id"]: e["canonical"] for e in entities}
            junk = [j for j in junk if _canon.get(j["entity_id"]) not in kept]
        # Junk-first routing: a junk-flagged side — this pass or a pending
        # proposal — belongs to the junk queue; neither a merge nor a
        # candidate slot should double-handle it.
        junk_owned = ({j["entity_id"] for j in junk}
                      | {p["entity_id"] for p in pending_props
                         if p.get("kind") == "junk"})
        vectors, mentions = gc.entity_context_vectors(
            entities, entries, traces, min_mentions=cfg.min_entity_mentions,
            max_fallback_mentions=cfg.max_fallback_mentions or None)
        # Lesson-minted <task> <aspect> nodes are not graph entities — the
        # same exclusion graph_review has always applied; without it they
        # paired with the artifacts they mention and burned candidate slots
        # (five pairs in one 2026-08-12 session).
        from pseudolife_memory.memory.graph_review import lesson_only_ids
        near = gc.candidate_pairs(
            vectors, edges, entities, scope_map, mentions,
            min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates,
            dismissed=dismissed, max_support_overlap=cfg.max_support_overlap,
            pending_pairs={frozenset((p["src_id"], p["dst_id"]))
                           for p in pending_links},
            excluded_ids=junk_owned | lesson_only_ids(edges, lesson_refs))
        merge_cands, link_cands = gc.partition_candidates(
            near, entities, edges, merge_min_similarity=cfg.merge_min_similarity,
            fact_counts=fact_counts)
        # Same name-shape vetoes the write-dedup filing applies — dry-run
        # display and the apply-time filing loop both consume merge_cands,
        # so filtering here keeps them agreeing.
        from pseudolife_memory.memory.graph_review import merge_veto as _mv
        merge_cands = [m for m in merge_cands
                       if _mv(m["from"], m["into"]) is None]
        # Belt over the candidate-level exclusion: merge filing must stay
        # junk-clean even if candidate generation changes shape later.
        merge_cands = [m for m in merge_cands
                       if m["from_id"] not in junk_owned
                       and m["into_id"] not in junk_owned]
        if include_snippets:
            candidates = self._attach_candidate_snippets(
                link_cands, entities, entries, traces, cfg.max_context_snippets,
                mentions=mentions, max_chars=cfg.snippet_max_chars)
        else:
            candidates = link_cands
        merge_proposals = self._enrich_merge_proposals(
            pending_props, entities, edges, entries, traces, mentions,
            scope_map, cfg.max_context_snippets, cfg.snippet_max_chars,
            include_snippets, fact_counts=fact_counts)
        # Store curation (listing-only, both dry-run and apply): cross-key
        # near-duplicate lesson/world slots. Settled by the reviewer via
        # curation_dismiss_duplicate (distinct) or the existing forget /
        # re-write tools (duplicate) — the deep dream never deletes them.
        lesson_dups, world_dups = self._slot_duplicate_listings(
            lesson_recs, world_recs, dismissed)

        totals = {"entities": len(entities), "edges": len(edges),
                  "candidates": len(candidates)}
        # Fact tally by graph-normalized subject NAME, folded from the raw
        # cortex text (see the junk tombstone note in the apply branch for
        # why the id cross-index alone is not enough).
        from pseudolife_memory.graph import norm_name as _nn2
        facts_by_norm: dict[str, int] = {}
        for _text, _n in fact_texts.items():
            _k = _nn2(_text)
            if _k:
                facts_by_norm[_k] = facts_by_norm.get(_k, 0) + _n
        if not apply:
            # The orphan sweep's census, so an operator can read what
            # `orphan_sweep: true` would delete before switching it on.
            with self._lock:
                would_orphan = self._unreachable_orphans(
                    entries, mentions, facts_by_norm, set())
            import time as _t0
            return {"dry_run": True, "rescored": len(rescore),
                    "would_orphan_count": len(would_orphan),
                    "would_orphan": [
                        {"entity": c["display"],
                         "age_days": round((_t0.time() - float(c.get("created_at") or _t0.time())) / 86400.0, 1)}
                        for c in would_orphan[:200]],
                    "would_supersede": [self._edge_label(e, entities) for e in violations],
                    "would_merge": self._merge_labels(dups, entities),
                    "would_merge_propose": [
                        {"from": m["from"], "into": m["into"],
                         "similarity": m["similarity"], "reason": m["reason"],
                         "already_proposed": ("merge",
                                              min(m["from_id"], m["into_id"]),
                                              max(m["from_id"], m["into_id"])) in prop_keys}
                        for m in merge_cands],
                    "would_junk": [{"entity": j["display"], "reason": j["reason"],
                                    "already_proposed": ("junk", j["entity_id"]) in prop_keys}
                                   for j in junk],
                    "merge_proposals": merge_proposals,
                    "lesson_duplicates": lesson_dups,
                    "world_duplicates": world_dups,
                    "candidates": candidates, "totals": totals}

        snapshot = self._write_graph_snapshot()
        if snapshot is None:
            return {"error": "snapshot_failed", "applied": False,
                    "detail": "pre-apply graph snapshot could not be written; nothing changed"}
        import time as _t
        superseded = merged = merge_proposed = junk_proposed = 0
        junk_deleted = scoped = analyzer_filed = orphans_deleted = 0
        with self._lock:
            # Writes apply against the snapshot read above; like the dream's
            # graph-relation extraction, a concurrent edit between the two lock
            # windows is tolerated — supersede/merge/rescore are no-ops on a row
            # that has since changed.
            # Entities removed by this apply (merged-away or junk-deleted):
            # later steps must not reference their ids — entity_sources and
            # entity_proposals FK-cascade with the row.
            dropped: set[int] = set()
            for eid, conf in rescore:
                self._storage.set_edge_confidence(eid, conf)
            if cfg.auto_apply_safe:
                for e in violations:
                    if self._storage.supersede_edge(e["src_id"], e["relation"], e["dst_id"]):
                        superseded += 1
                for frm, into in dups:
                    if self._storage.merge_entity(frm, into):
                        merged += 1
                        dropped.add(frm)
            # Junk auto-apply: a flagged entity with no edges and at most the
            # one fact slot it was minted from carries no structure a wrong
            # deletion could lose (the node re-mints on next mention; the
            # snapshot above is the undo). Anything evidence-bearing stays a
            # proposal for review — 68 of 70 junk verdicts on 2026-08-05 were
            # mechanical accepts of exactly the guard-passing class. Runs
            # BEFORE the merge-proposal inserts so a proposal never lands on
            # an id this pass is about to delete.
            from pseudolife_memory.graph import degree_counts as _dc
            deg = _dc(edges)
            now = _t.time()
            for j in junk:
                if self._storage.insert_entity_proposal(
                        "junk", j["entity_id"], None, None, j["reason"], _t.time()) is not None:
                    junk_proposed += 1
            disp_by_id = {e["id"]: e["display"] for e in entities}
            canon_by_id = {e["id"]: e["canonical"] for e in entities}
            # Junk tombstones: names a prior verdict (reviewed or auto)
            # already deleted. A re-mint of such a name that the detector
            # flags AGAIN is auto-deleted at the detector's own degree bar
            # instead of re-queueing for a second verdict — 'G:' was
            # deleted on 2026-08-16 and re-minted into the same queue the
            # same week. The zero-structure guard below still protects
            # never-judged names.
            tombstones = {_nn2(d)
                          for d in self._storage.junk_accepted_displays()}
            # facts_by_norm (computed above the branch): the entity_id
            # cross-index reads zero for facts an earlier delete_entity
            # orphaned (it NULLs facts.entity_id, and only a slot write
            # re-links one), which is precisely the already-damaged
            # population: a name deleted once WHILE carrying facts would
            # otherwise look contentless on every later re-mint and be
            # deleted again unattended. Folded in Python, not SQL, because
            # facts.entity_norm is the cortex norm and the entity side is
            # the graph norm.
            for p in self._storage.pending_entity_proposals():
                if p.get("kind") != "junk":
                    continue
                eid = p["entity_id"]
                display = disp_by_id.get(eid, p.get("entity") or "?")
                # A tombstone relaxes the DEGREE bar only; the fact-count
                # half of the evidence bar holds either way. Tombstones are
                # permanent (nothing removes a merge_decisions row), so a
                # short name auto-deleted once stayed deletable forever —
                # months later the same name can be a real entity with a
                # dozen cortex facts and one edge, and the unattended delete
                # would take its edges, aliases, sources and fact
                # cross-index with it (#177).
                nd = _nn2(display)
                contentless = max(fact_counts.get(eid, 0),
                                  facts_by_norm.get(nd, 0),
                                  facts_by_norm.get(
                                      canon_by_id.get(eid, ""), 0)) <= 1
                zero_structure = deg.get(eid, 0) == 0 and contentless
                tombstoned = (contentless
                              and nd in tombstones
                              and deg.get(eid, 0) <= cfg.junk_max_degree)
                if not (zero_structure or tombstoned):
                    continue
                if self._storage.delete_entity(eid):
                    # The proposal row CASCADEs away with the entity, so the
                    # denormalized merge_decisions row (no FK) is the only
                    # durable record of an unattended deletion.
                    suffix = ("" if zero_structure else " (tombstoned)")
                    self._storage.record_merge_decision(
                        p["id"], display,
                        None, "accepted", p.get("score"),
                        f"junk auto-delete: {p.get('reason')}{suffix}",
                        "dream-auto", now)
                    junk_deleted += 1
                    dropped.add(eid)
            # Non-destructive: populate the review queue regardless of auto_apply_safe.
            for m in merge_cands:
                if m["from_id"] in dropped or m["into_id"] in dropped:
                    continue
                if self._storage.insert_entity_proposal(
                        "merge", m["from_id"], m["into_id"], m["similarity"], m["reason"], _t.time()) is not None:
                    merge_proposed += 1
            # Scope stamping: attribute still-unattributed entities from the
            # sources of the entries that mention them (the mentions map is
            # already computed for the context vectors). backfill_entity_
            # sources only reaches entities with a current fact, which left
            # 327 entities projectless on 2026-08-05.
            scopes_cfg = self.config.memory.scopes
            excl = {str(s).strip().lower() for s in scopes_cfg.exclude}
            roll = {str(k).strip().lower(): str(v).strip().lower()
                    for k, v in scopes_cfg.rollup.items()}
            entry_source = {en["id"]: str(en.get("source") or "")
                           for en in entries}
            for e in entities:
                eid = e["id"]
                if eid in dropped:
                    continue                     # deleted this pass; FK is gone
                if scope_map.get(eid) or not mentions.get(eid):
                    continue
                keys: set[str] = set()
                for entry_id in mentions[eid]:
                    key = entry_source.get(entry_id, "").strip().lower()
                    if not key or key in excl:
                        continue
                    keys.add(key)
                    umb = roll.get(key)
                    if umb and umb != key and umb not in excl:
                        keys.add(umb)
                for key in sorted(keys):
                    self._storage.upsert_entity_source(eid, key, "derived", now)
                if keys:
                    scoped += 1
            # Analyzer duplicates -> the queues (2026-09-02). graph_review's
            # live token-Jaccard findings were displayed on every Console
            # load and filed nowhere, so no judge ever saw them (~90 pairs
            # re-listed daily on the live bank). merge pairs go to the
            # merge queue (same vetoes as the write-dedup detector; the
            # dedupe index keeps rejected pairs sticky), file/concept pairs
            # go to the link queue as `<file> implements <concept>`.
            if cfg.analyzer_file_duplicates:
                analyzer_filed = self._file_analyzer_duplicates(
                    entities, edges, dismissed, lesson_refs, fact_counts,
                    dropped | junk_owned, prop_keys, now)
            # Unreachable orphans (2026-09-02): an entity with no evidence
            # at all — the storage census excludes anything with an edge
            # (superseded included), fact, lesson, alias, scope or
            # proposal — that no current entry mentions either cannot be
            # reached by any read path. Older than orphan_min_age_days so a
            # node mid-construction is never swept.
            if cfg.orphan_sweep:
                orphans_deleted = self._sweep_unreachable_orphans(
                    entries, mentions, facts_by_norm, dropped, now)
        # Re-read pending merges: the apply loop above may have just inserted
        # fresh proposals the Step-C triage should see in the same response.
        # The watermark stamp makes EVERY apply (manual or tick) reset the
        # deep-dream need signal — see deep_dream_need.
        with self._lock:
            self._storage.set_meta(
                "deep_last_apply",
                {"ts": _t.time(),
                 "max_entity_id": self._storage.max_entity_id()})
            pending_props = self._storage.pending_entity_proposals()
        merge_proposals = self._enrich_merge_proposals(
            pending_props, entities, edges, entries, traces, mentions,
            scope_map, cfg.max_context_snippets, cfg.snippet_max_chars,
            include_snippets, fact_counts=fact_counts)
        return {"applied": True, "rescored": len(rescore), "superseded": superseded,
                "merged": merged, "merge_proposed": merge_proposed,
                "junk_proposed": junk_proposed, "junk_deleted": junk_deleted,
                "scoped": scoped, "analyzer_filed": analyzer_filed,
                "orphans_deleted": orphans_deleted, "snapshot": snapshot,
                "merge_proposals": merge_proposals,
                "lesson_duplicates": lesson_dups,
                "world_duplicates": world_dups,
                "candidates": candidates, "totals": totals}

    def deep_dream_need(self) -> dict[str, Any]:
        """Cheap need signal for the deep dream's mechanical half — and the
        harness-agnostic "deep dream recommended" flag: it rides
        ``dream_status`` (hence ``memory_dream(action="status")``), so ANY
        MCP client can read it and nudge its user or schedule a pass,
        without Claude-specific machinery. Watermark = the meta row every
        ``deep_dream(apply=True)`` stamps."""
        import time as _t
        cfg = self.config.memory.deep_dream
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"recommended": False, "reason": "no_storage"}
            mark = self._storage.get_meta("deep_last_apply")
            max_id = self._storage.max_entity_id()
            if mark is None:
                if max_id == 0:
                    return {"recommended": False, "reason": "empty graph",
                            "last_apply_at": None}
                return {"recommended": True,
                        "reason": f"never deep-dreamed ({max_id} entities)",
                        "last_apply_at": None, "new_entities": max_id}
            new = self._storage.entities_above(int(mark["max_entity_id"]))
        days = (_t.time() - float(mark["ts"])) / 86400.0
        out = {"recommended": False, "reason": "below thresholds",
               "last_apply_at": mark["ts"], "new_entities": new,
               "days_since": round(days, 2)}
        if cfg.auto_min_new_entities and new >= cfg.auto_min_new_entities:
            out.update(recommended=True,
                       reason=f"{new} new entities since the last deep apply")
        elif cfg.auto_interval_days and days >= cfg.auto_interval_days:
            out.update(recommended=True,
                       reason=f"{days:.1f} days since the last deep apply")
        return out

    def deep_dream_tick(self) -> dict[str, Any]:
        """Sweep-tick automation of the deep dream's MECHANICAL half: when
        the need signal fires, run ``deep_dream(apply=True)`` — rescore,
        guard-passing junk auto-delete, scope stamping, proposal filing,
        snapshot-first. Step C (judgment) is never run here; the queues
        fill and wait for an agent or the Atlas. Never raises into the
        sweep timer; returns a slim summary for the sweep log."""
        cfg = self.config.memory.deep_dream
        if not cfg.auto_tick:
            return {"fired": False, "reason": "disabled"}
        try:
            need = self.deep_dream_need()
            if not need.get("recommended"):
                return {"fired": False, "reason": "below_threshold",
                        "need": need}
            result = self.deep_dream(apply=True, include_snippets=False)
        except Exception as exc:  # noqa: BLE001 — a tick must never kill the sweep
            logger.warning("deep-dream tick failed: %s", exc)
            return {"fired": False, "reason": f"error: {exc}"}
        slim = {k: v for k, v in result.items() if k in (
            "applied", "error", "rescored", "superseded", "merged",
            "merge_proposed", "junk_proposed", "junk_deleted", "scoped",
            "analyzer_filed", "orphans_deleted", "snapshot")}
        return {"fired": True, "reason": need["reason"], **slim}

    def _judge_extractor(self, extractor=None, method: str = "judge_merges",
                         model: str | None = None):
        """The endpoint a review-queue judge calls: an explicit override
        (``judge_url``), the passed extractor, or the daemon's dream
        extractor — whichever first supports ``method``. ``model`` swaps
        the model name on a constructed endpoint (the merge judge's
        second-opinion model); it never alters a passed extractor."""
        cfg = self.config.memory.deep_dream
        dream_cfg = self.config.memory.dream
        if extractor is not None:
            return extractor if hasattr(extractor, method) else None
        from pseudolife_memory.memory.dream import OpenAICompatExtractor
        if cfg.judge_url:
            ex = OpenAICompatExtractor(
                cfg.judge_url, model or cfg.judge_model or "judge",
                # Explicit, not the constructor default: the judge endpoint
                # follows the same config knob as the dream extractor, so a
                # default change can never silently alter this shipped payload.
                max_tokens=dream_cfg.extractor_max_tokens,
                timeout_seconds=dream_cfg.extractor_timeout_seconds)
            return ex if hasattr(ex, method) else None
        from pseudolife_memory.memory.dream import (
            build_extractor_with_fallback,
        )
        try:
            ex, _which = build_extractor_with_fallback(dream_cfg)
        except ValueError:
            return None
        if model and isinstance(ex, OpenAICompatExtractor) and ex.model != model:
            ex = OpenAICompatExtractor(
                ex.base_url, model, api_key=ex.api_key,
                max_tokens=ex.max_tokens, timeout_seconds=ex.timeout,
                extra_body=ex.extra_body)
        return ex if hasattr(ex, method) else None

    def _judge_enrich(self, pending: list[dict]) -> list[dict]:
        """The merge-judge evidence pack for ``pending`` rows — the same
        snippets/scopes/degree the review surfaces show, with the
        ``low_differential`` stamp."""
        cfg = self.config.memory.deep_dream
        with self._lock:
            g = self._storage.load_graph()
            scope_map = self._storage.entity_sources_map()
            traces = self._storage.traces_by_entity_norm()
            entries = self._storage.load_entries()
            fact_counts = self._storage.entity_fact_counts()
        from pseudolife_memory.memory import graph_consolidation as gc
        _, mentions = gc.entity_context_vectors(
            g["entities"], entries, traces,
            min_mentions=cfg.min_entity_mentions,
            max_fallback_mentions=cfg.max_fallback_mentions or None)
        # Built at the JUDGE's cap, not the review surface's: the
        # 2026-09-02 panel judged 305/309 merge snippets clipped to 240
        # chars at build time and lost guidance in three folds.
        return self._enrich_merge_proposals(
            pending, g["entities"], g["edges"], entries, traces,
            mentions, scope_map, cfg.max_context_snippets,
            cfg.judge_snippet_max_chars, True, fact_counts=fact_counts)

    @staticmethod
    def _model_name(ex, fallback: str | None = None) -> str:
        """The model a judge endpoint reported serving on its last call,
        else its configured name, else the class — stamped on verdicts."""
        return (getattr(ex, "served_model", None) or fallback
                or getattr(ex, "model", None) or type(ex).__name__)

    def _snapshot_once(self, state: dict) -> bool:
        """Graph snapshot before the FIRST destructive auto-apply of a judge
        call (every apply the spec covers is snapshot-first; the deep apply
        already is). False = the snapshot could not be written, so the
        caller must refuse the apply."""
        if "snapshot" not in state:
            state["snapshot"] = self._write_graph_snapshot()
        return state["snapshot"] is not None

    @staticmethod
    def _stamp_skipped(verdicts: list[dict], rows: list[dict],
                       **leave_fields) -> None:
        """Rows the model silently skipped in an otherwise-successful call
        are recorded as zero-confidence leaves: without this they are
        re-sent every sweep and a stubborn batch head starves the rest of
        the queue (the sidecar arm returned no verdict for 33% of rows on
        the 2026-08-16 ladder). Transport failures raise above and mark
        nothing."""
        returned = {v["n"] for v in verdicts}
        for r in rows:
            if r["n"] not in returned:
                verdicts.append({"n": r["n"], "verdict": "leave",
                                 "confidence": 0.0,
                                 "note": "model returned no verdict",
                                 **leave_fields})

    def deep_dream_judge(self, extractor=None, *,
                         limit: int | None = None,
                         second_extractor=None) -> dict[str, Any]:
        """Autonomous Step C over pending MERGE proposals (2026-08-16
        design, extended 2026-09-02). First opinion: judge a bounded batch
        of not-yet-judged rows, recording each verdict on the row; in
        ``auto-reject`` / ``auto`` modes apply reject verdicts at/above
        ``judge_reject_min_confidence``. Second opinion
        (``judge_second_opinion``): rows already carrying a first verdict
        but still pending are re-judged once in a fresh batch (optionally
        ``judge_second_model``); two rejects at mean confidence >=
        ``judge_reject_min_confidence_2`` apply, and in ``auto`` mode two
        accepts on a non-low-differential row at mean >=
        ``judge_accept_min_confidence`` fold the entity — the only path
        that ever auto-applies an accept, and only when the two opinions
        come from DIFFERENT models (``judge_second_model``; the same model
        at temperature 0 mostly repeats itself — batch composition alone
        flipped 2/129 verdicts on the 2026-08-16 ladder — so a same-model
        second vote is not independent enough to authorize a fold). Name
        vetoes (``merge_veto``, ``variant_conflict``) hold at apply time
        too. Disagreement stamps ``split`` on the note and leaves the row
        for a human. ``second_extractor`` is the test hook for the second
        opinion's endpoint. Never raises into the sweep timer."""
        import time as _t
        cfg = self.config.memory.deep_dream
        if not cfg.judges_enabled:
            return {"judged": 0, "skipped": "judges_disabled"}
        if cfg.judge_mode not in ("shadow", "auto-reject", "auto"):
            return {"judged": 0, "skipped": "disabled"}
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return {"judged": 0, "skipped": "no_storage"}
                pending = [p for p in self._storage.pending_entity_proposals()
                           if p.get("kind") == "merge"]
            first = [p for p in pending if not p.get("judge_verdict")]
            second = ([p for p in pending
                       if p.get("judge_verdict") and not p.get("judge2_verdict")]
                      if cfg.judge_second_opinion else [])
            if not first and not second:
                return {"judged": 0}
            ex = self._judge_extractor(extractor)
            if ex is None:
                return {"judged": 0, "skipped": "no_judge_extractor"}
            cap = max(1, int(limit if limit is not None else cfg.judge_batch))
            out = {"judged": 0, "auto_rejected": 0, "auto_accepted": 0,
                   "second_opinions": 0, "pending_unjudged": 0,
                   "model": getattr(ex, "model", None) or type(ex).__name__,
                   "mode": cfg.judge_mode}
            # Second opinions first: these rows were judged on an EARLIER
            # sweep, so the batch they now share is a different one, which
            # is what makes the second vote an independent sample.
            if second:
                if second_extractor is not None:
                    ex2 = second_extractor
                else:
                    ex2 = (self._judge_extractor(
                               None, model=cfg.judge_second_model)
                           if cfg.judge_second_model and extractor is None
                           else ex) or ex
                from pseudolife_memory.memory.graph_consolidation import (
                    variant_conflict)
                from pseudolife_memory.memory.graph_review import merge_veto
                batch = second[:cap]
                logger.info("deep-dream judge: second opinion on %d pending "
                            "merge proposal(s) (mode %s)", len(batch),
                            cfg.judge_mode)
                enriched = self._judge_enrich(batch)
                proposals = [{"n": i + 1, "from": e["from"], "into": e["into"],
                              "reason": e.get("reason"), "score": e.get("score"),
                              "low_differential": e.get("low_differential"),
                              "snippet_chars": cfg.judge_snippet_max_chars}
                             for i, e in enumerate(enriched)]
                verdicts = ex2.judge_merges(proposals)
                self._stamp_skipped(verdicts, proposals)
                model2 = self._model_name(ex2)
                now = _t.time()
                with self._lock:
                    dismissed = self._storage.dismissed_pairs()
                    # dismissed_pairs is keyed by the entity's STORED
                    # canonical (graph_dismiss_duplicate resolves display
                    # names to it): an entity minted from a bare name and
                    # later display-enriched — 'GND (Enshrouded server)'
                    # over canonical 'gnd' — has a canonical norm_name(display)
                    # never reproduces, so the guard resolves through the
                    # proposal's entity ids, never through names.
                    canon_by_id = {e["id"]: e["canonical"]
                                   for e in self._storage.load_graph()["entities"]}
                # Same-model detection must survive rows judged before this
                # build (stamped with the CONFIGURED name) and a shared
                # extractor (ex2 is ex): distinct only when the second
                # opinion came from another endpoint object AND its served
                # and configured names both differ from the first stamp.
                same_endpoint = ex2 is ex
                configured2 = getattr(ex2, "model", None)
                snap: dict = {}
                for v in verdicts:
                    e = enriched[v["n"] - 1]
                    row = batch[v["n"] - 1]
                    v1, c1 = row.get("judge_verdict"), float(row.get("judge_confidence") or 0.0)
                    v2, c2 = v["verdict"], float(v["confidence"])
                    # The first opinion's note is the prefix; the verdict
                    # tail (and any refusal reason appended below) must
                    # survive the 400-char cap, so the prefix is what gets
                    # truncated.
                    note1 = (row.get("judge_note") or "")[:160]
                    tag = ("split" if v1 != v2 else "agree")
                    note = f"{note1} | 2nd ({model2}): {v2} {c2:.2f} [{tag}]"
                    with self._lock:
                        ok = self._storage.set_entity_proposal_second_judgment(
                            e["id"], verdict=v2, confidence=c2, model=model2,
                            at=now, note=note)
                    if not ok:
                        continue
                    out["second_opinions"] += 1
                    mean = (c1 + c2) / 2.0
                    if (v1 == v2 == "reject"
                            and cfg.judge_mode in ("auto-reject", "auto")
                            and mean >= cfg.judge_reject_min_confidence_2):
                        res = self.graph_reject_entity_proposal(
                            e["id"], decided_by="dream-judge")
                        if res.get("rejected"):
                            out["auto_rejected"] += 1
                    elif (v1 == v2 == "accept" and cfg.judge_mode == "auto"
                            and e.get("low_differential") is False
                            and mean >= cfg.judge_accept_min_confidence):
                        a_name = e["from"]["display"] or ""
                        b_name = e["into"]["display"] or ""
                        pair = tuple(sorted((
                            canon_by_id.get(row["entity_id"], ""),
                            canon_by_id.get(row["into_id"], ""))))
                        if pair in dismissed:
                            # An earlier verdict (relate / dismiss_pair)
                            # settled these as distinct; the pending merge
                            # row is moot — close it instead of folding
                            # over a human's decision.
                            res = self.graph_reject_entity_proposal(
                                e["id"], decided_by="dream-judge")
                            out["auto_rejected"] += bool(res.get("rejected"))
                            out["auto_accept_refused"] = out.get(
                                "auto_accept_refused", 0) + 1
                            continue
                        first_model = row.get("judge_model") or ""
                        if (same_endpoint or first_model == model2
                                or (configured2 and first_model == configured2)):
                            reason = "auto-accept needs a distinct second model"
                        elif str(row.get("reason") or "").startswith("analyzer-duplicate"):
                            # The accept gate's evidence holds no analyzer-
                            # filed rows (2026-09-02 panel: write-dedup 56,
                            # dream-alias 4, token-subset 3); unmeasured
                            # class stays pending with the verdicts attached.
                            reason = "auto-accept refused: analyzer-filed rows are unmeasured"
                        elif merge_veto(a_name, b_name) is not None:
                            reason = "auto-accept refused: name veto"
                        elif variant_conflict(a_name, b_name):
                            reason = "auto-accept refused: variant conflict"
                        elif not self._snapshot_once(snap):
                            reason = "auto-accept refused: graph snapshot failed"
                        else:
                            reason = None
                        if reason is not None:
                            with self._lock:
                                self._storage.set_entity_proposal_second_judgment(
                                    e["id"], verdict=v2, confidence=c2,
                                    model=model2, at=now,
                                    note=f"{note} | {reason}"[:400])
                            out["auto_accept_refused"] = out.get(
                                "auto_accept_refused", 0) + 1
                            continue
                        res = self.graph_accept_entity_merge(
                            e["id"], decided_by="dream-judge")
                        if res.get("accepted"):
                            out["auto_accepted"] += 1
            if first:
                batch = first[:cap]
                # Announce the batch BEFORE the enrichment + model call: the
                # completion line alone let the 2026-08-31 forensics misplace
                # a ~50s window inside this (mostly lock-free) phase.
                logger.info("deep-dream judge: judging %d pending merge "
                            "proposal(s) (mode %s)", len(batch), cfg.judge_mode)
                enriched = self._judge_enrich(batch)
                proposals = [{"n": i + 1, "from": e["from"], "into": e["into"],
                              "reason": e.get("reason"), "score": e.get("score"),
                              "low_differential": e.get("low_differential"),
                              "snippet_chars": cfg.judge_snippet_max_chars}
                             for i, e in enumerate(enriched)]
                verdicts = ex.judge_merges(proposals)
                self._stamp_skipped(verdicts, proposals)
                model = self._model_name(ex, out["model"])
                out["model"] = model
                now = _t.time()
                for v in verdicts:
                    e = enriched[v["n"] - 1]
                    with self._lock:
                        ok = self._storage.set_entity_proposal_judgment(
                            e["id"], verdict=v["verdict"],
                            confidence=v["confidence"], note=v["note"] or None,
                            model=model, at=now)
                    if not ok:
                        continue
                    out["judged"] += 1
                    if (cfg.judge_mode in ("auto-reject", "auto")
                            and v["verdict"] == "reject"
                            and v["confidence"] >= cfg.judge_reject_min_confidence):
                        res = self.graph_reject_entity_proposal(
                            e["id"], decided_by="dream-judge")
                        if res.get("rejected"):
                            out["auto_rejected"] += 1
            out["pending_unjudged"] = max(0, len(first) - out["judged"])
            return out
        except Exception as exc:  # noqa: BLE001 — the judge must never kill the sweep
            logger.warning("deep-dream judge failed: %s", exc)
            return {"judged": 0, "error": str(exc)}

    # ── link judge (2026-09-02) ───────────────────────────────────────────

    def _enrich_link_proposals(self, pending: list[dict]) -> list[dict]:
        """Evidence pack for pending edge proposals: each side's live
        edges and scopes, the detector's rationale, and the notes naming
        BOTH entities (per-side notes when nothing names both)."""
        from pseudolife_memory.memory import graph_consolidation as gc
        from pseudolife_memory.memory.graph_review import _token_set
        cfg = self.config.memory.deep_dream
        cap = cfg.snippet_max_chars
        with self._lock:
            g = self._storage.load_graph()
            scope_map = self._storage.entity_sources_map()
            entries = self._storage.load_entries()
        disp = {e["id"]: e["display"] for e in g["entities"]}
        outs: dict[int, list[str]] = {}
        ins: dict[int, list[str]] = {}
        for ed in g["edges"]:
            outs.setdefault(ed["src_id"], []).append(
                f"{ed['relation']}>{disp.get(ed['dst_id'], '?')}")
            ins.setdefault(ed["dst_id"], []).append(
                f"{disp.get(ed['src_id'], '?')}>{ed['relation']}")

        def edges_of(eid):
            return (outs.get(eid, []) + ins.get(eid, []))[:8]

        def stamp(e):
            src = e.get("source")
            return (f"[{src}] " if src else "") + str(e.get("text", ""))[:cap]

        entry_tokens = None

        def mentions_of(display, k=2):
            nonlocal entry_tokens
            want = _token_set(display)
            if not want:
                return []
            if entry_tokens is None:
                entry_tokens = [(e, _token_set(e.get("text", ""))) for e in entries]
            found = []
            for e, toks in entry_tokens:
                if want <= toks:
                    found.append(stamp(e))
                    if len(found) >= k:
                        break
            return found

        rows = []
        for i, p in enumerate(pending):
            both = [t[:cap] for t in gc.shared_mention_entries(
                entries, p["src"], p["dst"], limit=3)]
            rows.append({
                "n": i + 1, "src": p["src"], "relation": p["relation"],
                "dst": p["dst"], "rationale": p.get("rationale"),
                "src_edges": edges_of(p["src_id"]), "dst_edges": edges_of(p["dst_id"]),
                "src_scopes": sorted(scope_map.get(p["src_id"], [])),
                "dst_scopes": sorted(scope_map.get(p["dst_id"], [])),
                "co_mentions": both,
                "src_mentions": [] if both else mentions_of(p["src"]),
                "dst_mentions": [] if both else mentions_of(p["dst"])})
        return rows

    def deep_dream_judge_links(self, extractor=None, *,
                               limit: int | None = None) -> dict[str, Any]:
        """Judge a bounded batch of not-yet-judged pending LINK proposals,
        recording each verdict on the row (schema v36). In ``auto``:
        accept at/above ``link_accept_min_confidence`` promotes the edge
        (origin ``action``); reject at/above ``link_reject_min_confidence``
        marks the row rejected. A retype is recorded (``judge_relation``)
        but never auto-applied — see the comment at the retype branch
        below. Never raises into the sweep."""
        import time as _t
        cfg = self.config.memory.deep_dream
        if not cfg.judges_enabled:
            return {"judged": 0, "skipped": "judges_disabled"}
        mode = cfg.link_judge_mode
        if mode not in ("shadow", "auto"):
            return {"judged": 0, "skipped": "disabled"}
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return {"judged": 0, "skipped": "no_storage"}
                pending = [p for p in self._storage.pending_proposals()
                           if not p.get("judge_verdict")]
            if not pending:
                return {"judged": 0}
            ex = self._judge_extractor(extractor, method="judge_links")
            if ex is None:
                return {"judged": 0, "skipped": "no_judge_extractor"}
            cap = max(1, int(limit if limit is not None else cfg.judge_batch))
            batch = pending[:cap]
            logger.info("deep-dream link judge: judging %d pending link "
                        "proposal(s) (mode %s)", len(batch), mode)
            rows = self._enrich_link_proposals(batch)
            verdicts = ex.judge_links(rows)
            self._stamp_skipped(verdicts, rows, relation=None)
            model = self._model_name(ex)
            judged = applied = 0
            now = _t.time()
            for v in verdicts:
                p = batch[v["n"] - 1]
                with self._lock:
                    ok = self._storage.set_proposal_judgment(
                        p["id"], verdict=v["verdict"], confidence=v["confidence"],
                        note=v.get("note"), model=model,
                        relation=v.get("relation"), at=now)
                if not ok:
                    continue
                judged += 1
                if mode != "auto":
                    continue
                conf = float(v["confidence"])
                if v["verdict"] == "accept" and conf >= cfg.link_accept_min_confidence:
                    res = self.graph_accept_proposal(p["id"], decided_by="dream-judge")
                    applied += bool(res.get("accepted"))
                # A retype is recorded, never auto-written: the first ladder
                # run scored the judge's relation choice at 0/1 on retypes
                # (and 6/10 on candidate proposals), so the corrected edge
                # waits for a reviewer — the verdict and judge_relation are
                # on the row for the Console / an agent to apply.
                elif v["verdict"] == "reject" and conf >= cfg.link_reject_min_confidence:
                    res = self.graph_reject_proposal(p["id"], decided_by="dream-judge")
                    applied += bool(res.get("rejected"))
            return {"judged": judged, "applied": applied,
                    "pending_unjudged": max(0, len(pending) - judged),
                    "model": model, "mode": mode}
        except Exception as exc:  # noqa: BLE001 — never kill the sweep
            logger.warning("deep-dream link judge failed: %s", exc)
            return {"judged": 0, "error": str(exc)}

    # ── junk judge (2026-09-02) ───────────────────────────────────────────

    def _enrich_junk_proposals(self, pending: list[dict]) -> list[dict]:
        """Evidence pack for pending junk proposals: detector class, live
        degree and edges (with origin), fact count and text, whether the
        node is a lesson-minted object, scopes, mentioning notes."""
        from pseudolife_memory.graph import degree_counts, norm_name as _nn
        from pseudolife_memory.memory.graph_review import _token_set
        cfg = self.config.memory.deep_dream
        cap = cfg.snippet_max_chars
        with self._lock:
            g = self._storage.load_graph()
            scope_map = self._storage.entity_sources_map()
            entries = self._storage.load_entries()
            fact_counts = self._storage.entity_fact_counts()
            fact_texts = self._storage.current_fact_counts_by_entity_text()
            fact_rows = {p["entity_id"]: self._storage.entity_fact_rows(
                p["entity_id"], next((e["canonical"] for e in g["entities"]
                                      if e["id"] == p["entity_id"]), ""))
                for p in pending}
        disp = {e["id"]: e["display"] for e in g["entities"]}
        canon = {e["id"]: e["canonical"] for e in g["entities"]}
        deg = degree_counts(g["edges"])
        by_ent: dict[int, list[dict]] = {}
        for ed in g["edges"]:
            by_ent.setdefault(ed["src_id"], []).append(ed)
            by_ent.setdefault(ed["dst_id"], []).append(ed)
        facts_by_norm: dict[str, int] = {}
        for text, n in fact_texts.items():
            k = _nn(text)
            if k:
                facts_by_norm[k] = facts_by_norm.get(k, 0) + n
        entry_tokens = None

        def mentions_of(display, k=3):
            nonlocal entry_tokens
            want = _token_set(display)
            if not want:
                return []
            if entry_tokens is None:
                entry_tokens = [(e, _token_set(e.get("text", ""))) for e in entries]
            found = []
            for e, toks in entry_tokens:
                if want <= toks:
                    src = e.get("source")
                    found.append((f"[{src}] " if src else "") + str(e.get("text", ""))[:cap])
                    if len(found) >= k:
                        break
            return found

        rows = []
        for i, p in enumerate(pending):
            eid = p["entity_id"]
            display = disp.get(eid, p.get("entity") or "?")
            edges = by_ent.get(eid, [])
            lesson_object = bool(edges) and all(
                ed.get("relation") in ("prefers", "avoids")
                and (ed.get("origin") or "") == "action" for ed in edges)
            facts = max(fact_counts.get(eid, 0),
                        facts_by_norm.get(_nn(display), 0),
                        facts_by_norm.get(canon.get(eid, ""), 0))
            rows.append({
                "n": i + 1, "display": display, "reason": p.get("reason"),
                "degree": deg.get(eid, 0),
                "edges": [f"{disp.get(ed['src_id'], '?')} -{ed['relation']}-> "
                          f"{disp.get(ed['dst_id'], '?')} [{ed.get('origin') or '?'}]"
                          for ed in edges[:8]],
                "facts": facts,
                "fact_text": [f"{a}={str(v)[:120]}" for a, v in fact_rows.get(eid, [])],
                "lesson_object": lesson_object,
                "scopes": sorted(scope_map.get(eid, [])),
                "mentions": mentions_of(display)})
        return rows

    def deep_dream_judge_junk(self, extractor=None, *,
                              limit: int | None = None) -> dict[str, Any]:
        """Judge a bounded batch of not-yet-judged pending JUNK proposals.
        In ``auto``: keep at/above ``junk_keep_min_confidence`` closes the
        proposal (entity kept); delete at/above
        ``junk_delete_min_confidence`` deletes the entity ONLY under the
        evidence bar (degree <= ``junk_max_auto_degree``, at most one fact
        slot) — anything richer stays pending with the verdict attached.
        Never raises into the sweep."""
        import time as _t
        cfg = self.config.memory.deep_dream
        if not cfg.judges_enabled:
            return {"judged": 0, "skipped": "judges_disabled"}
        mode = cfg.junk_judge_mode
        if mode not in ("shadow", "auto"):
            return {"judged": 0, "skipped": "disabled"}
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return {"judged": 0, "skipped": "no_storage"}
                pending = [p for p in self._storage.pending_entity_proposals()
                           if p.get("kind") == "junk" and not p.get("judge_verdict")]
            if not pending:
                return {"judged": 0}
            ex = self._judge_extractor(extractor, method="judge_junk")
            if ex is None:
                return {"judged": 0, "skipped": "no_judge_extractor"}
            cap = max(1, int(limit if limit is not None else cfg.judge_batch))
            batch = pending[:cap]
            logger.info("deep-dream junk judge: judging %d pending junk "
                        "proposal(s) (mode %s)", len(batch), mode)
            rows = self._enrich_junk_proposals(batch)
            verdicts = ex.judge_junk(rows)
            self._stamp_skipped(verdicts, rows)
            model = self._model_name(ex)
            judged = applied = 0
            now = _t.time()
            snap: dict = {}
            for v in verdicts:
                p = batch[v["n"] - 1]
                r = rows[v["n"] - 1]
                with self._lock:
                    ok = self._storage.set_entity_proposal_judgment(
                        p["id"], verdict=v["verdict"], confidence=v["confidence"],
                        note=v.get("note"), model=model, at=now)
                if not ok:
                    continue
                judged += 1
                if mode != "auto":
                    continue
                conf = float(v["confidence"])
                if v["verdict"] == "keep" and conf >= cfg.junk_keep_min_confidence:
                    res = self.graph_reject_entity_proposal(
                        p["id"], decided_by="dream-judge")
                    applied += bool(res.get("rejected"))
                elif (v["verdict"] == "delete"
                        and conf >= cfg.junk_delete_min_confidence
                        and r["degree"] <= cfg.junk_max_auto_degree
                        and r["facts"] <= 1
                        and self._snapshot_once(snap)):
                    res = self.graph_accept_entity_junk(
                        p["id"], decided_by="dream-judge")
                    applied += bool(res.get("accepted"))
            return {"judged": judged, "applied": applied,
                    "pending_unjudged": max(0, len(pending) - judged),
                    "model": model, "mode": mode}
        except Exception as exc:  # noqa: BLE001 — never kill the sweep
            logger.warning("deep-dream junk judge failed: %s", exc)
            return {"judged": 0, "error": str(exc)}

    # ── store-curation judge (2026-09-02) ─────────────────────────────────

    def deep_dream_judge_curation(self, extractor=None, *,
                                  limit: int | None = None) -> dict[str, Any]:
        """Judge a bounded batch of the lesson/world duplicate listings
        (pairs without a memo younger than ``curation_rejudge_days``),
        recording every verdict in ``curation_judgments``. ``auto-distinct``
        applies distinct verdicts at/above
        ``curation_distinct_min_confidence`` as a (reversible) dismissal;
        ``auto`` additionally applies duplicate verdicts at/above
        ``curation_forget_min_confidence``: the survivor is re-written with
        the judge's fold (lessons only) and the loser forgotten. Never
        raises into the sweep."""
        import time as _t
        cfg = self.config.memory.deep_dream
        if not cfg.judges_enabled:
            return {"judged": 0, "skipped": "judges_disabled"}
        mode = cfg.curation_judge_mode
        if mode not in ("shadow", "auto-distinct", "auto"):
            return {"judged": 0, "skipped": "disabled"}
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return {"judged": 0, "skipped": "no_storage"}
                dismissed = self._storage.dismissed_pairs()
                lesson_recs = self._curation_records("lesson", cfg.snippet_max_chars)
                world_recs = self._curation_records("world", cfg.snippet_max_chars)
                # The judge reads FULL values: the 2026-09-02 panel judged
                # 240-char clipped values and lost guidance in three
                # "duplicate" folds — a destructive queue never gets
                # truncated evidence.
                full = {store: {r["key"]: r["value"]
                                for r in self._curation_records(store, 0)}
                        for store in ("lesson", "world")}
                memo = {"lesson": self._storage.curation_judgments("lesson"),
                        "world": self._storage.curation_judgments("world")}
            lesson_dups, world_dups = self._slot_duplicate_listings(
                lesson_recs, world_recs, dismissed)
            now = _t.time()
            horizon = float(cfg.curation_rejudge_days) * 86400.0

            def fresh(store, pair):
                key = tuple(sorted((pair["a_key"], pair["b_key"])))
                m = memo[store].get(key)
                return m is not None and (now - float(m["judged_at"])) < horizon

            todo = [("lesson", c) for c in lesson_dups if not fresh("lesson", c)]
            todo += [("world", c) for c in world_dups if not fresh("world", c)]
            if not todo:
                return {"judged": 0}
            ex = self._judge_extractor(extractor, method="judge_slot_pairs")
            if ex is None:
                return {"judged": 0, "skipped": "no_judge_extractor"}
            cap = max(1, int(limit if limit is not None else cfg.judge_batch))
            batch = todo[:cap]
            logger.info("deep-dream curation judge: judging %d duplicate "
                        "listing(s) (mode %s)", len(batch), mode)
            def side(store, c, k):
                d = dict(c[k])
                d["value"] = full[store].get(c[f"{k}_key"], d.get("value"))
                return d
            rows = [{"n": i + 1, "store": store, "similarity": c.get("similarity"),
                     "a": side(store, c, "a"), "b": side(store, c, "b"),
                     "a_key": c["a_key"], "b_key": c["b_key"]}
                    for i, (store, c) in enumerate(batch)]
            verdicts = ex.judge_slot_pairs(rows)
            self._stamp_skipped(verdicts, rows, keep=None, fold=None)
            model = self._model_name(ex)
            judged = applied = 0
            for v in verdicts:
                store, c = batch[v["n"] - 1]
                with self._lock:
                    self._storage.record_curation_judgment(
                        store, c["a_key"], c["b_key"], verdict=v["verdict"],
                        keep=v.get("keep"), fold=v.get("fold"),
                        confidence=v["confidence"], note=v.get("note"),
                        model=model, at=now)
                judged += 1
                conf = float(v["confidence"])
                if (v["verdict"] == "distinct" and mode in ("auto-distinct", "auto")
                        and conf >= cfg.curation_distinct_min_confidence):
                    res = self.curation_dismiss_duplicate(
                        store, c["a"]["entity"], c["a"]["attribute"],
                        c["b"]["entity"], c["b"]["attribute"])
                    applied += bool(res.get("dismissed"))
                elif (v["verdict"] == "duplicate" and mode == "auto"
                        and conf >= cfg.curation_forget_min_confidence):
                    applied += bool(self._apply_slot_duplicate(
                        store, c, v.get("keep"), v.get("fold"),
                        reason=v.get("note") or None))
            return {"judged": judged, "applied": applied,
                    "pending_unjudged": max(0, len(todo) - judged),
                    "model": model, "mode": mode}
        except Exception as exc:  # noqa: BLE001 — never kill the sweep
            logger.warning("deep-dream curation judge failed: %s", exc)
            return {"judged": 0, "error": str(exc)}

    def _apply_slot_duplicate(self, store: str, pair: dict, keep: str | None,
                              fold: str | None, *,
                              reason: str | None = None) -> bool:
        """Settle a ratified duplicate listing: fold the judge's carry-over
        into the surviving LESSON slot (world facts are cited values and
        are never concatenated), then RETIRE the loser (``decided_by``
        ``dream-judge``, the judge's note as the reason) — reversible
        through ``lesson_restore`` / ``world_restore``."""
        if keep not in ("a", "b"):
            return False
        survivor = pair["a"] if keep == "a" else pair["b"]
        loser = pair["b"] if keep == "a" else pair["a"]
        if store == "lesson":
            if fold:
                with self._lock:
                    self._ensure_init()
                    from pseudolife_memory.service import _slot_key
                    full = {_slot_key(*r.key): r
                            for r in self._lessons.current_records()}
                key = pair["a_key"] if keep == "a" else pair["b_key"]
                rec = full.get(key)
                if rec is not None and fold not in rec.value:
                    self.lesson_write(
                        rec.entity, rec.attribute,
                        f"{rec.value.rstrip()} Also: {fold}",
                        about=rec.about, outcome=rec.outcome,
                        polarity=rec.polarity, confidence=rec.confidence,
                        origin="agent")
            out = self.lesson_forget(loser["entity"], loser["attribute"],
                                     decided_by="dream-judge", reason=reason)
        else:
            out = self.world_forget(loser["entity"], loser["attribute"],
                                    decided_by="dream-judge", reason=reason)
        return bool(out.get("removed"))

    # ── Step-C candidate judge (2026-09-02) ───────────────────────────────

    def deep_dream_judge_candidates(self, extractor=None, *,
                                    candidates: list[dict] | None = None,
                                    limit: int | None = None) -> dict[str, Any]:
        """Judge the deep dream's link CANDIDATES (unlinked near-pairs) ONE
        slice per tick (``judge_batch``), like every other judge:
        ``propose`` files an edge proposal (source ``deep-dream-judge``,
        then settled by the link judge like any other), ``dismiss`` marks
        the pair distinct, ``leave`` keeps the slot. Every judged pair is
        memoised (``deep_candidate_verdicts`` meta, bounded) so no pair is
        re-sent within ``candidate_rejudge_days`` — in ``shadow`` mode that
        memo is the only record. The scan (a dry-run pass) runs only while
        an apply's candidates are still unjudged; the per-apply watermark
        turns ``complete`` once every candidate has a fresh memo. Never
        raises into the sweep."""
        import time as _t
        from pseudolife_memory.graph import norm_name as _nn
        cfg = self.config.memory.deep_dream
        if not cfg.judges_enabled:
            return {"judged": 0, "skipped": "judges_disabled"}
        mode = cfg.candidate_judge_mode
        if mode not in ("shadow", "auto"):
            return {"judged": 0, "skipped": "disabled"}
        try:
            mark = None
            if candidates is None:
                with self._lock:
                    self._ensure_init()
                    if self._storage is None:
                        return {"judged": 0, "skipped": "no_storage"}
                    mark = self._storage.get_meta("deep_last_apply")
                    done = self._storage.get_meta("deep_candidates_judged")
                if mark is None or (done and done.get("ts") == mark.get("ts")
                                    and done.get("complete")):
                    return {"judged": 0, "reason": "no_new_apply"}
                candidates = self.deep_dream(apply=False).get("candidates", [])
            with self._lock:
                self._ensure_init()
                memo = self._storage.get_meta("deep_candidate_verdicts") or {}
            pairs = dict(memo.get("pairs") or {})
            now = _t.time()
            horizon = float(cfg.candidate_rejudge_days) * 86400.0

            def key(c):
                return "|".join(sorted((_nn(c["src"]), _nn(c["dst"]))))

            todo = [c for c in candidates
                    if key(c) not in pairs
                    or (now - float(pairs[key(c)].get("ts", 0))) >= horizon]
            if not todo:
                if mark is not None:
                    with self._lock:
                        self._storage.set_meta("deep_candidates_judged",
                                               {"ts": mark["ts"], "complete": True})
                return {"judged": 0, "reason": "all_judged"}
            ex = self._judge_extractor(extractor, method="judge_candidates")
            if ex is None:
                return {"judged": 0, "skipped": "no_judge_extractor"}
            cap = max(1, int(limit if limit is not None else cfg.judge_batch))
            chunk = todo[:cap]
            logger.info("deep-dream candidate judge: judging %d of %d unjudged "
                        "link candidate(s) (mode %s)", len(chunk), len(todo), mode)
            rows = [{"n": i + 1, "src": c["src"], "dst": c["dst"],
                     "similarity": c.get("similarity"),
                     "src_snippets": c.get("src_snippets") or [],
                     "dst_snippets": c.get("dst_snippets") or []}
                    for i, c in enumerate(chunk)]
            verdicts = ex.judge_candidates(rows)
            self._stamp_skipped(verdicts, rows, relation=None, src=None,
                                dst=None, rationale=None)
            model = self._model_name(ex)
            judged = proposed = dismissed = left = 0
            for v in verdicts:
                c = chunk[v["n"] - 1]
                judged += 1
                conf = float(v["confidence"])
                pairs[key(c)] = {"verdict": v["verdict"], "confidence": conf,
                                 "ts": now, "mode": mode}
                if mode != "auto":
                    left += 1                 # shadow: memo only
                    continue
                if (v["verdict"] == "propose" and v.get("relation")
                        and conf >= cfg.candidate_min_confidence):
                    res = self.graph_propose_links(
                        [{"src": v.get("src") or c["src"],
                          "relation": v["relation"],
                          "dst": v.get("dst") or c["dst"],
                          "similarity": c.get("similarity"),
                          "rationale": v.get("rationale")}],
                        source="deep-dream-judge")
                    proposed += int(res.get("proposed", 0))
                elif (v["verdict"] == "dismiss"
                        and conf >= cfg.candidate_min_confidence):
                    res = self.graph_dismiss_duplicate(c["src"], c["dst"])
                    dismissed += bool(res.get("dismissed"))
                else:
                    left += 1
            # Bounded memo: newest 500 pairs.
            if len(pairs) > 500:
                keep = sorted(pairs.items(), key=lambda kv: -float(kv[1].get("ts", 0)))[:500]
                pairs = dict(keep)
            remaining = max(0, len(todo) - len(chunk))
            with self._lock:
                self._storage.set_meta("deep_candidate_verdicts", {"pairs": pairs})
                if mark is not None:
                    self._storage.set_meta(
                        "deep_candidates_judged",
                        {"ts": mark["ts"], "complete": remaining == 0})
            return {"judged": judged, "proposed": proposed,
                    "dismissed": dismissed, "left": left,
                    "remaining": remaining, "model": model, "mode": mode}
        except Exception as exc:  # noqa: BLE001 — never kill the sweep
            logger.warning("deep-dream candidate judge failed: %s", exc)
            return {"judged": 0, "error": str(exc)}

    # ── apply-time mechanical additions (2026-09-02) ──────────────────────

    def _file_analyzer_duplicates(self, entities, edges, dismissed, lesson_refs,
                                  fact_counts, excluded: set[int],
                                  prop_keys: set[tuple], now: float) -> int:
        """File graph_review's live duplicate findings into the queues.
        Caller holds the lock. A pair the merge queue already holds (any
        status — the write-dedup detector files most fresh mints first, and
        a rejected pair stays rejected) is skipped, including its
        file/concept relate form: the merge judge settles that row as a
        relate or a reject. Returns the number of rows filed."""
        from pseudolife_memory.graph import degree_counts
        from pseudolife_memory.memory.graph_consolidation import variant_conflict
        from pseudolife_memory.memory.graph_review import (
            duplicate_candidates, lesson_only_ids)
        from pseudolife_memory.memory.relation_quality import edge_confidence
        alive = [e for e in entities if e["id"] not in excluded]
        findings = duplicate_candidates(
            alive, dismissed=dismissed,
            lesson_ids=lesson_only_ids(edges, lesson_refs))
        if not findings:
            return 0
        by_display = {e["display"]: e["id"] for e in alive}
        deg = degree_counts(edges)
        registry = {r["name"] for r in self._graph.load_relations()}
        filed = 0
        for f in findings:
            a, b = f["entities"]
            ia, ib = by_display.get(a), by_display.get(b)
            if ia is None or ib is None or ia == ib:
                continue
            if ("merge", min(ia, ib), max(ia, ib)) in prop_keys:
                continue
            if f.get("action") == "relate":
                rel = f.get("suggested_relation") or "related-to"
                if rel not in registry:
                    rel = "related-to"
                pid = self._storage.insert_proposal(
                    ia, rel, ib, edge_confidence(a, rel, b), None,
                    f"analyzer file/concept pair (jaccard {f.get('score')})",
                    "analyzer", now)
            else:
                # The screen every other merge-filing path applies: differing
                # size / quant / version tokens are sibling artifacts, never
                # a fold (E4B vs E2B, Q4_K_M vs Q4_K_XL — nine such rejected
                # by hand on 2026-07-11).
                if variant_conflict(a, b):
                    continue
                ea = deg.get(ia, 0) + fact_counts.get(ia, 0)
                eb = deg.get(ib, 0) + fact_counts.get(ib, 0)
                if ea > eb or (ea == eb and ia < ib):
                    into, frm = ia, ib
                else:
                    into, frm = ib, ia
                pid = self._storage.insert_entity_proposal(
                    "merge", frm, into, f.get("score"),
                    f"analyzer-duplicate: jaccard {f.get('score')}", now)
            filed += pid is not None
        return filed

    def _unreachable_orphans(self, entries, mentions, facts_by_norm,
                             dropped: set[int]) -> list[dict]:
        """The orphan sweep's census: zero-evidence entities (storage
        predicate) that no current entry mentions. Caller holds the lock.
        Read-only — the dry run reports it as ``would_orphan``."""
        from pseudolife_memory.graph import norm_name as _nn
        from pseudolife_memory.memory.graph_review import _token_set
        cfg = self.config.memory.deep_dream
        cands = self._storage.zero_evidence_entities(
            float(cfg.orphan_min_age_days) * 86400.0)
        if not cands:
            return []
        entry_tokens = [_token_set(e.get("text", "")) for e in entries]
        out: list[dict] = []
        for c in cands:
            eid = c["id"]
            if eid in dropped or mentions.get(eid):
                continue
            if (facts_by_norm.get(_nn(c["display"]), 0)
                    or facts_by_norm.get(c.get("canonical") or "", 0)):
                continue
            want = _token_set(c["display"])
            if not want or any(want <= toks for toks in entry_tokens):
                continue                      # mentioned (or unverifiable): keep
            out.append(c)
        return out

    def _sweep_unreachable_orphans(self, entries, mentions, facts_by_norm,
                                   dropped: set[int], now: float) -> int:
        """Delete the census (at most ``orphan_max_per_apply`` per pass).
        Caller holds the lock. Each delete and its audit row are ONE
        transaction; audited as ``dream-auto`` / status ``deleted`` —
        deliberately NOT a junk tombstone (``accepted`` + no fold target),
        which would relax the junk guard for any later re-mint."""
        cfg = self.config.memory.deep_dream
        cap = int(cfg.orphan_max_per_apply)
        deleted = 0
        for c in self._unreachable_orphans(entries, mentions, facts_by_norm, dropped):
            if cap > 0 and deleted >= cap:
                break                         # bounded blast radius per pass
            if self._storage.delete_entity_audited(
                    c["id"], c["display"], "deleted",
                    "orphan-unreachable auto-delete", "dream-auto", now):
                dropped.add(c["id"])
                deleted += 1
        return deleted

    def _write_graph_snapshot(self) -> str | None:
        """Timestamped JSON dump of the five graph tables the apply path is
        about to mutate — a targeted undo artifact (pg_dump backups remain the
        real recovery path). Keeps the newest ``snapshot_keep`` files under
        ``data_dir/graph_snapshots``. Returns the filename, or None when the
        snapshot could not be written — the caller must refuse to apply."""
        import json
        import time as _t
        keep = max(1, int(self.config.memory.deep_dream.snapshot_keep))
        try:
            with self._lock:
                tables = self._storage.dump_graph_tables()
            d = self.data_dir / "graph_snapshots"
            d.mkdir(parents=True, exist_ok=True)
            name = _t.strftime("graph-%Y%m%d-%H%M%S", _t.gmtime()) + ".json"
            (d / name).write_text(json.dumps(tables, default=str), encoding="utf-8")
            for old in sorted(d.glob("graph-*.json"))[:-keep]:
                old.unlink()
            return name
        except OSError:
            return None

    def _edge_label(self, e: dict, entities: list[dict]) -> dict:
        disp = {x["id"]: x["display"] for x in entities}
        return {"src": disp.get(e["src_id"], str(e["src_id"])), "relation": e["relation"],
                "dst": disp.get(e["dst_id"], str(e["dst_id"])), "confidence": e.get("confidence")}

    def _merge_labels(self, dups: list[tuple[int, int]], entities: list[dict]) -> list[dict]:
        disp = {x["id"]: x["display"] for x in entities}
        return [{"from": disp.get(f, str(f)), "into": disp.get(t, str(t))} for f, t in dups]

    # Shown-evidence share at/above which a pair is stamped low_differential.
    # 0.5 mirrors the 2026-08-21 live shadow comparison's metric
    # (evals/results/judge-shadow-live-20260821.json: 40/109 pending merge
    # proposals had an empty side or >=50% shared snippets, flagged
    # independently by four of the six slice judges) — the flag reproduces
    # exactly the measurement that defined the defect.
    _LOW_DIFFERENTIAL_SHARE = 0.5
    # Entries token-scanned into an evidence pool when an entity has neither
    # usable traces nor a mentions entry. Bounded so a hub name contributes
    # a sample, not a corpus dump (README.md subset-matched 300+ entries on
    # the 2026-08-30 live bank); big enough that exclusive-first selection
    # has room to look past a co-mention prefix.
    _SNIPPET_SCAN_CAP = 12

    def _attach_candidate_snippets(self, candidates, entities, entries, traces, k,
                                   mentions=None, max_chars=None,
                                   differential=False):
        """Attach up to k context snippets per side (each truncated to
        max_chars), for the Step-C agent prompt and the merge judge. Traces
        are the primary evidence; entities without USABLE traces (ids must
        resolve to live entries) fall back to their token-mention entries,
        and entities the vector pass excluded outright — below
        min_entity_mentions, or over max_fallback_mentions, caps that guard
        VECTORS, not display — fall back to a bounded token scan, so a
        candidate never ships as a bare similarity score merely because it
        was vector-ineligible (32 of 109 pending proposals shipped an empty
        side on 2026-08-21).

        ``differential=True`` (the MERGE path — "same referent?") makes each
        side lead with entries EXCLUSIVE to it, shared entries only filling
        remaining slots, and stamps ``evidence_overlap`` (shared share of the
        SHOWN snippets) plus ``low_differential`` — an empty side, shown
        overlap at/above ``_LOW_DIFFERENTIAL_SHARE``, or one side's evidence
        pool wholly contained in the other's (exclusive-first ordering would
        otherwise HIDE that a side has no evidence of its own — the
        bare-vs-qualified name shape). LINK candidates keep pool order and
        get no stamps: their question is "what relation holds?", which the
        co-occurrence notes answer — demoting shared entries there would
        strip the evidence (see shared_mention_entries)."""
        from pseudolife_memory.memory.graph_review import _token_set
        by_id = {e["id"]: e for e in entries}
        canon = {e["id"]: e["canonical"] for e in entities}
        disp = {e["id"]: e["display"] for e in entities}
        entry_tokens = None      # built once, only if some side needs the scan

        def pool(eid):
            nonlocal entry_tokens
            ids = [i for i in traces.get(canon.get(eid, ""), []) if i in by_id]
            seen = set(ids)
            for i in sorted(mentions.get(eid, ()) if mentions else ()):
                if i in by_id and i not in seen:
                    ids.append(i)
                    seen.add(i)
            if not ids:
                want = _token_set(disp.get(eid, ""))
                if want:
                    if entry_tokens is None:
                        entry_tokens = [(e["id"], _token_set(e.get("text", "")))
                                        for e in entries]
                    for i, toks in entry_tokens:
                        if want <= toks:
                            ids.append(i)
                            if len(ids) >= self._SNIPPET_SCAN_CAP:
                                break
            return ids

        def pick(own, other):
            if differential:
                ranked = ([i for i in own if i not in other]
                          + [i for i in own if i in other])
            else:
                ranked = own
            texts: list[str] = []
            seen_text: set[str] = set()
            for i in ranked:
                if len(texts) >= k:
                    break
                t = by_id[i]["text"]
                if max_chars:
                    t = t[:max_chars]
                if t in seen_text:
                    continue
                texts.append(t)
                seen_text.add(t)
            return texts

        for c in candidates:
            src_pool, dst_pool = pool(c["src_id"]), pool(c["dst_id"])
            src = pick(src_pool, set(dst_pool))
            dst = pick(dst_pool, set(src_pool))
            c["src_snippets"], c["dst_snippets"] = src, dst
            if not differential:
                continue
            overlap = (len(set(src) & set(dst)) / min(len(src), len(dst))
                       if src and dst else 0.0)
            one_sided = bool(src_pool and dst_pool
                             and (not set(src_pool) - set(dst_pool)
                                  or not set(dst_pool) - set(src_pool)))
            c["evidence_overlap"] = round(overlap, 2)
            c["low_differential"] = (not src or not dst or one_sided
                                     or overlap >= self._LOW_DIFFERENTIAL_SHARE)
        return candidates

    def _enrich_merge_proposals(self, pending, entities, edges, entries,
                                traces, mentions, scope_map, k, max_chars,
                                include_snippets, *, fact_counts=None):
        """Evidence payload for Step-C merge triage (write-dedup spec): each
        pending merge proposal with per-side display/etype/degree/scopes and
        snippets. Presentation and application both derive the fold direction
        from CURRENT evidence via ``_fold_direction`` — the stored direction
        was computed when the minted side had no history and goes stale as
        the graph grows (rows 981/983, 2026-08-12: the from-side reached
        degree 8 while its proposal sat pending against a degree-0 target).
        ``accept_merge`` applies the same rule, so what is shown is still
        exactly what an accept does."""
        from pseudolife_memory.graph import degree_counts
        deg = degree_counts(edges)
        by_id = {e["id"]: e for e in entities}
        facts = fact_counts or {}

        def ev(eid):
            return deg.get(eid, 0) + facts.get(eid, 0)

        from pseudolife_memory.memory.graph_review import shared_pair_groups
        rows = [p for p in pending if p.get("kind") == "merge"]
        oriented = [self._fold_direction(p["entity_id"], p["into_id"], ev)
                    for p in rows]
        # Group key on the STORED ids (direction-agnostic): rows sharing an
        # endpoint are one where-does-this-entity-belong decision.
        groups = shared_pair_groups(
            [(p["entity_id"], p["into_id"]) for p in rows])
        shaped = [{"src_id": f, "dst_id": i} for f, i in oriented]
        if include_snippets:
            self._attach_candidate_snippets(
                shaped, entities, entries, traces, k,
                mentions=mentions, max_chars=max_chars, differential=True)
        out = []
        for p, (frm, into), g, s in zip(rows, oriented, groups, shaped):
            def side(eid, snips):
                e = by_id.get(eid) or {}
                return {"display": e.get("display"), "etype": e.get("etype"),
                        "degree": deg.get(eid, 0),
                        "scopes": sorted(scope_map.get(eid, [])),
                        "snippets": snips if include_snippets else []}
            row = {"id": p["id"], "score": p.get("score"),
                   "reason": p.get("reason"),
                   "group": (by_id.get(g) or {}).get("display")
                   if g is not None else None,
                   "from": side(frm, s.get("src_snippets", [])),
                   "into": side(into, s.get("dst_snippets", []))}
            if include_snippets:
                # Evidence-quality stamp (2026-08-21 shadow finding): the
                # reviewer and the judge both need to know when the sides'
                # shown evidence cannot differentiate the pair.
                row["evidence_overlap"] = s.get("evidence_overlap")
                row["low_differential"] = s.get("low_differential")
            # Shadow pre-judgment (v30): an opinion for the reviewer, shown
            # beside the evidence it judged from.
            if p.get("judge_verdict"):
                row["judge"] = {"verdict": p["judge_verdict"],
                                "confidence": p.get("judge_confidence"),
                                "note": p.get("judge_note"),
                                "model": p.get("judge_model")}
            if p.get("judge2_verdict"):
                row["judge2"] = {"verdict": p["judge2_verdict"],
                                 "confidence": p.get("judge2_confidence"),
                                 "model": p.get("judge2_model")}
            out.append(row)
        return out
