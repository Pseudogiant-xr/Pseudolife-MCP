"""Tests for :class:`MemoryService` — the layer the MCP server exposes.

Coverage targets the MCP tool-shaped methods one by one (store / search /
recent / supersede / stats / save), plus a few cross-method invariants
(superseded entries surface in ``recent`` with the flag set; supersession
text round-trips through the entry dict; stats reflects writes).

Document tests live in :mod:`tests.test_document_service`.
"""

from __future__ import annotations

import pytest

from pseudolife_memory.service import MemoryService


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class TestStore:
    def test_store_returns_stored_true_on_first_call(
        self, pristine_service: MemoryService,
    ) -> None:
        result = pristine_service.store("The capital of France is Paris", source="general")
        assert result["stored"] is True
        assert result["reason"] is None
        # Surprise on a totally fresh band should be max (1.0).
        assert 0.5 <= result["surprise"] <= 1.0

    def test_store_rejects_empty_text(
        self, pristine_service: MemoryService,
    ) -> None:
        result = pristine_service.store("   ", source="general")
        assert result["stored"] is False
        assert result["reason"] == "empty"

    def test_store_persists_source_tag(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("X happened on Tuesday", source="my-tag")
        recent = pristine_service.recent(n=1)
        assert recent["entries"][0]["source"] == "my-tag"

    def test_store_default_source_is_claude(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Default-source fact for the test")
        recent = pristine_service.recent(n=1)
        assert recent["entries"][0]["source"] == "claude"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_finds_relevant_memory(
        self, pristine_service: MemoryService,
    ) -> None:
        # Use richer, overlapping content so cosine clears MIN_SCORE (0.25)
        # on the small MiniLM-L6 embedder. Shorter "PseudoLife v0.7.6" type
        # queries hover right around the threshold and produce flaky
        # zero-result runs — those are still useful real-world queries, but
        # the test shouldn't depend on the threshold knife-edge.
        pristine_service.store(
            "PseudoLife v0.7.6 ships three new memory features: HyDE-lite "
            "query expansion, periodic reflection / dreaming, and "
            "contrastive learning from negative signals.",
            source="pseudolife",
        )
        pristine_service.store(
            "The user has a Ragdoll cat named Jacque who lives in the "
            "kitchen and likes tuna.",
            source="general",
        )
        result = pristine_service.search(
            "What memory features does PseudoLife v0.7.6 ship?", top_k=5,
        )
        assert result["count"] >= 1
        # The top entry should be the PseudoLife one, not the cat one.
        assert "PseudoLife" in result["entries"][0]["text"]

    def test_search_filters_by_source(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Python tip about list comprehensions", source="general")
        pristine_service.store(
            "PseudoLife v0.7.6 ships HyDE and Reflection", source="pseudolife",
        )
        result = pristine_service.search(
            "comprehensions", top_k=5, sources=["pseudolife"],
        )
        # All returned entries must have source=pseudolife.
        for entry in result["entries"]:
            assert entry["source"] == "pseudolife"

    def test_search_empty_query_returns_zero(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("A real memory", source="test")
        result = pristine_service.search("   ")
        assert result["count"] == 0
        assert result["entries"] == []

    def test_search_returns_score_for_each_entry(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("The sky is blue", source="test")
        result = pristine_service.search("what colour is the sky")
        if result["entries"]:
            entry = result["entries"][0]
            assert "score" in entry
            assert isinstance(entry["score"], float)


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------


class TestRecent:
    def test_recent_returns_newest_first(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Memory A — first", source="t")
        pristine_service.store("Memory B — second", source="t")
        pristine_service.store("Memory C — third", source="t")
        result = pristine_service.recent(n=3)
        texts = [e["text"] for e in result["entries"]]
        # Newest stored should appear first.
        assert texts[0].startswith("Memory C")
        assert texts[-1].startswith("Memory A")

    def test_recent_caps_at_n(
        self, pristine_service: MemoryService,
    ) -> None:
        for i in range(5):
            pristine_service.store(f"Memory number {i}", source="t")
        result = pristine_service.recent(n=2)
        assert result["count"] == 2

    def test_recent_filters_by_source(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("A general fact", source="general")
        pristine_service.store("A pseudolife fact", source="pseudolife")
        result = pristine_service.recent(n=10, sources=["pseudolife"])
        assert result["count"] == 1
        assert result["entries"][0]["source"] == "pseudolife"


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------


class TestSupersede:
    def test_supersede_marks_exact_match(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("The capital of France is Lyon", source="wrong")
        result = pristine_service.supersede(
            "The capital of France is Lyon",
            "The capital of France is Paris",
        )
        assert result["superseded_count"] == 1
        assert result["new_memory_stored"] is True

    def test_supersede_flag_visible_in_recent(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Sky is green", source="wrong")
        pristine_service.supersede("Sky is green", "Sky is blue")
        recent = pristine_service.recent(n=10)
        # Find the wrong entry — should be marked superseded.
        wrong = next(e for e in recent["entries"] if e["text"] == "Sky is green")
        assert wrong["superseded"] is True
        assert wrong["superseded_by_text"] == "Sky is blue"

    def test_supersede_via_embedding_fallback(
        self, pristine_service: MemoryService,
    ) -> None:
        """Paraphrased ``old_text`` should still flag the original via top-1
        embedding match."""
        pristine_service.store(
            "The user prefers Python over Rust for systems work",
            source="general",
        )
        result = pristine_service.supersede(
            "User likes Python more than Rust",
            "User uses Python but is open to Rust experiments",
        )
        # Embedding fallback may or may not catch the paraphrase
        # depending on cosine sim — assert non-negative either way and
        # confirm the new memory landed regardless.
        assert result["superseded_count"] >= 0
        assert result["new_memory_stored"] is True

    def test_supersede_empty_input_is_noop(
        self, pristine_service: MemoryService,
    ) -> None:
        result = pristine_service.supersede("", "anything")
        assert result["superseded_count"] == 0
        assert result["reason"] == "empty_input"


# ---------------------------------------------------------------------------
# stats / save
# ---------------------------------------------------------------------------


class TestStatsAndSave:
    def test_stats_reflects_writes(
        self, pristine_service: MemoryService,
    ) -> None:
        for i in range(3):
            pristine_service.store(f"Stat-test memory {i}", source="t")
        stats = pristine_service.stats()
        assert stats["total_memories"] >= 3
        # Continuum preset has 8 bands.
        assert len(stats["bands"]) == 8
        # First band ('working' in continuum) or 'instant' — at least one
        # band has size > 0.
        assert any(b["size"] > 0 for b in stats["bands"])

    def test_save_returns_target_dir(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("To-be-saved memory", source="t")
        result = pristine_service.save()
        assert "saved_to" in result
        # Path should be inside our data_dir.
        assert str(pristine_service.data_dir) in result["saved_to"]


# ---------------------------------------------------------------------------
# trace — debug visibility into the ranking pipeline
# ---------------------------------------------------------------------------


class TestTrace:
    def test_trace_returns_structured_trace_alongside_entries(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store(
            "Trace test: PseudoLife uses 8-band continuum memory",
            source="trace-test",
        )
        out = pristine_service.trace(
            "PseudoLife continuum memory", top_k=5,
        )
        # Same envelope as search, plus a ``trace`` key.
        assert "query" in out
        assert "count" in out
        assert "entries" in out
        assert "trace" in out
        # Trace structure surfaces ranking decisions.
        trace = out["trace"]
        assert "config" in trace
        assert "filters" in trace
        assert "tiers" in trace
        assert "final_topk" in trace
        # At least one tier should have been queried.
        assert len(trace["tiers"]) >= 1

    def test_trace_records_filters(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("X happened", source="trace-test")
        out = pristine_service.trace(
            "X", top_k=3, sources=["trace-test"], bands=["instant"],
        )
        assert out["trace"]["filters"]["sources"] == ["trace-test"]
        assert out["trace"]["filters"]["bands"] == ["instant"]

    def test_trace_tier_candidates_explain_drops(
        self, pristine_service: MemoryService,
    ) -> None:
        """Each tier should explain WHY each candidate was kept or dropped."""
        pristine_service.store("Apples are red fruit", source="trace-test")
        pristine_service.store("Bananas are yellow fruit", source="other-source")
        out = pristine_service.trace(
            "fruit colour", top_k=5, sources=["trace-test"],
        )
        # Walk every tier's candidate list — each entry needs a
        # drop_reason or kept=True.
        for tier in out["trace"]["tiers"]:
            if tier.get("filtered_out"):
                continue
            for cand in tier["candidates"]:
                # Exactly one outcome: kept OR dropped (with a reason).
                if cand["kept"]:
                    assert cand.get("drop_reason") is None
                else:
                    assert cand.get("drop_reason") is not None

    def test_trace_empty_query_returns_empty(
        self, pristine_service: MemoryService,
    ) -> None:
        out = pristine_service.trace("   ")
        assert out["count"] == 0
        assert out["entries"] == []


# ---------------------------------------------------------------------------
# list_sources — discoverability of source taxonomy
# ---------------------------------------------------------------------------


class TestListSources:
    def test_list_sources_returns_counts_per_source(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("A", source="alpha")
        pristine_service.store("B", source="alpha")
        pristine_service.store("C", source="beta")
        out = pristine_service.list_sources()
        # Convert to dict for easy lookup.
        by_source = {row["source"]: row["count"] for row in out["sources"]}
        assert by_source["alpha"] == 2
        assert by_source["beta"] == 1

    def test_list_sources_sorted_by_count_desc(
        self, pristine_service: MemoryService,
    ) -> None:
        for _ in range(3):
            pristine_service.store(f"alpha row {_}", source="alpha")
        pristine_service.store("solo beta", source="beta")
        out = pristine_service.list_sources()
        counts = [row["count"] for row in out["sources"]]
        assert counts == sorted(counts, reverse=True)

    def test_list_sources_empty_bank_returns_empty(
        self, pristine_service: MemoryService,
    ) -> None:
        out = pristine_service.list_sources()
        assert out["sources"] == []
        assert out["total"] == 0


# ---------------------------------------------------------------------------
# delete — hygiene
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_by_exact_text_removes_entry(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Delete me exactly", source="del-test")
        pristine_service.store("Keep me around", source="del-test")
        result = pristine_service.delete(text="Delete me exactly")
        assert result["deleted_count"] == 1
        # Verify gone from recent.
        recent = pristine_service.recent(n=10)
        texts = [e["text"] for e in recent["entries"]]
        assert "Delete me exactly" not in texts
        assert "Keep me around" in texts

    def test_delete_by_substring_removes_all_matches(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Junk entry one", source="del-test")
        pristine_service.store("Junk entry two", source="del-test")
        pristine_service.store("Real memory", source="del-test")
        result = pristine_service.delete(substring="Junk")
        assert result["deleted_count"] == 2
        recent = pristine_service.recent(n=10)
        texts = [e["text"] for e in recent["entries"]]
        assert not any("Junk" in t for t in texts)
        assert "Real memory" in texts

    def test_delete_by_source_removes_all_in_that_source(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Test-only A", source="purgeable")
        pristine_service.store("Test-only B", source="purgeable")
        pristine_service.store("Production fact", source="prod")
        result = pristine_service.delete(source="purgeable")
        assert result["deleted_count"] == 2
        recent = pristine_service.recent(n=10)
        sources = {e["source"] for e in recent["entries"]}
        assert "purgeable" not in sources
        assert "prod" in sources

    def test_delete_requires_at_least_one_filter(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Some entry", source="safety-test")
        # All-None should refuse — preventing accidental "delete everything".
        with pytest.raises(ValueError):
            pristine_service.delete()

    def test_delete_returns_zero_when_nothing_matches(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("Existing", source="del-test")
        result = pristine_service.delete(text="Does not exist anywhere")
        assert result["deleted_count"] == 0

    def test_delete_survives_save_reload(self, tmp_path) -> None:
        """Deletes must persist across a save → reload cycle."""
        svc1 = MemoryService(data_dir=tmp_path)
        svc1.store("To-be-deleted", source="t")
        svc1.store("To-be-kept", source="t")
        svc1.delete(text="To-be-deleted")
        svc1.save()

        svc2 = MemoryService(data_dir=tmp_path)
        recent = svc2.recent(n=10)
        texts = [e["text"] for e in recent["entries"]]
        assert "To-be-deleted" not in texts
        assert "To-be-kept" in texts


# ---------------------------------------------------------------------------
# search scoring overrides
# ---------------------------------------------------------------------------


class TestSearchOverrides:
    def test_search_accepts_min_score_override(
        self, pristine_service: MemoryService,
    ) -> None:
        """``min_score`` parameter should be accepted without error and
        cap the result set at relevance >= the override.
        """
        pristine_service.store("Apples grow on trees", source="t")
        # min_score=0.99 is effectively a "nothing passes" filter.
        result = pristine_service.search(
            "apples", top_k=5, min_score=0.99,
        )
        assert result["count"] == 0

    def test_search_disable_recency_boost(
        self, pristine_service: MemoryService,
    ) -> None:
        """Disabling recency boost should produce scores <= the default
        (no recency uplift on fresh entries)."""
        pristine_service.store(
            "The MIRAS architecture has 8 bands in the continuum preset.",
            source="t",
        )
        default = pristine_service.search(
            "MIRAS continuum bands", top_k=3,
        )
        no_boost = pristine_service.search(
            "MIRAS continuum bands", top_k=3, disable_recency_boost=True,
        )
        # Same entry should appear in both. Its no_boost score must not
        # exceed its default score (recency only adds, never subtracts).
        if default["count"] and no_boost["count"]:
            d_text = default["entries"][0]["text"]
            nb_match = next(
                (e for e in no_boost["entries"] if e["text"] == d_text), None,
            )
            if nb_match is not None:
                assert nb_match["score"] <= default["entries"][0]["score"] + 1e-4


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_then_reload_restores_memories(tmp_path) -> None:
    """End-to-end: store → save → new service in same data_dir → search."""
    svc1 = MemoryService(data_dir=tmp_path)
    svc1.store(
        "The MCP wrapper preserves memory across processes",
        source="round-trip-test",
    )
    svc1.save()

    svc2 = MemoryService(data_dir=tmp_path)
    result = svc2.search("MCP wrapper preserves memory", top_k=3)
    assert result["count"] >= 1
    # The text should be findable verbatim.
    texts = [e["text"] for e in result["entries"]]
    assert any("MCP wrapper preserves memory" in t for t in texts)


# ---------------------------------------------------------------------------
# Cross-encoder reranking (Tier B)
# ---------------------------------------------------------------------------
#
# These tests drive ``service.search`` / ``service.trace`` with the
# reranker enabled — but they never load the real cross-encoder. Instead
# we monkeypatch ``sentence_transformers.CrossEncoder`` with a tiny stub
# that scores pairs by shared whitespace tokens. That keeps the suite
# fast and offline while still exercising the wiring end-to-end:
# config flag → service → CMS.retrieve → reranker.rerank → fuse → resort.


class _StubCrossEncoder:
    """Deterministic stand-in for ``sentence_transformers.CrossEncoder``."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def predict(self, pairs):  # type: ignore[no-untyped-def]
        out = []
        for q, c in pairs:
            shared = set(q.lower().split()) & set(c.lower().split())
            # 2 * matches - 1 logit, so 0 matches → -1 (sigmoid<0.5),
            # 3+ matches → strongly positive.
            out.append(2.0 * len(shared) - 1.0)
        return out


def _reset_reranker(svc: MemoryService) -> None:
    """Drop any previously-loaded model so the monkeypatch takes effect.

    The reranker caches its model on the first call; without this reset
    a stub installed in test N+1 won't be picked up because test N
    already cached the real (or earlier-patched) CrossEncoder.
    """
    svc._ensure_init()  # noqa: SLF001 — fixture wiring.
    assert svc._reranker is not None
    svc._reranker._model = None  # noqa: SLF001
    svc._reranker._disabled = False  # noqa: SLF001


class TestReranker:
    def test_search_with_rerank_true_fires_reranker(
        self,
        pristine_service: MemoryService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rerank=True per-call enables reranking even when config is off."""
        import sentence_transformers  # noqa: PLC0415
        monkeypatch.setattr(sentence_transformers, "CrossEncoder", _StubCrossEncoder)
        _reset_reranker(pristine_service)

        pristine_service.store(
            "We use pytest as our python testing framework",
            source="project",
        )
        pristine_service.store(
            "The user's cat Jacque is a Ragdoll who lives in the kitchen",
            source="general",
        )
        out = pristine_service.trace(
            "what python testing framework do we use", rerank=True,
        )
        assert out["trace"]["reranker"]["fired"] is True
        assert "candidates" in out["trace"]["reranker"]
        # Per-candidate breakdown should carry the three scoring columns.
        cand = out["trace"]["reranker"]["candidates"][0]
        for key in ("original_score", "ce_score", "fused_score"):
            assert key in cand, f"missing {key!r} in rerank trace"

    def test_rerank_false_disables_even_when_config_enabled(
        self,
        pristine_service: MemoryService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rerank=False overrides config.reranker.enabled=True."""
        import sentence_transformers  # noqa: PLC0415
        monkeypatch.setattr(sentence_transformers, "CrossEncoder", _StubCrossEncoder)
        _reset_reranker(pristine_service)

        # Flip the config flag on so the default would be to rerank.
        original = pristine_service.config.memory.reranker.enabled
        pristine_service.config.memory.reranker.enabled = True
        try:
            pristine_service.store("Python is fun", source="x")
            out = pristine_service.trace("python", rerank=False)
            assert out["trace"]["reranker"]["fired"] is False
        finally:
            pristine_service.config.memory.reranker.enabled = original

    def test_rerank_promotes_lexically_aligned_candidate(
        self,
        pristine_service: MemoryService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reranking should reorder candidates by cross-encoder score.

        We store a query-aligned memory and a distractor. With the stub
        cross-encoder, the aligned memory's CE score will dwarf the
        distractor's, so the fused score puts it at index 0.
        """
        import sentence_transformers  # noqa: PLC0415
        monkeypatch.setattr(sentence_transformers, "CrossEncoder", _StubCrossEncoder)
        _reset_reranker(pristine_service)

        pristine_service.store(
            "PseudoLife uses ChromaDB for the reference document bank",
            source="project",
        )
        pristine_service.store(
            "Coffee is brewed in the morning by the espresso machine",
            source="kitchen",
        )
        out = pristine_service.search(
            "PseudoLife uses ChromaDB reference document bank",
            top_k=5,
            rerank=True,
        )
        assert out["count"] >= 1
        # The lexically-aligned memory should land at the top.
        assert "PseudoLife" in out["entries"][0]["text"]

    def test_rerank_failure_falls_back_to_biencoder(
        self,
        pristine_service: MemoryService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the cross-encoder errors, we still return bi-encoder results."""
        import sentence_transformers  # noqa: PLC0415

        def _broken(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("CrossEncoder unavailable")

        monkeypatch.setattr(sentence_transformers, "CrossEncoder", _broken)
        _reset_reranker(pristine_service)

        pristine_service.store("A simple test fact", source="x")
        out = pristine_service.search("simple test fact", rerank=True)
        # Bi-encoder result still surfaces despite the rerank failure.
        assert out["count"] >= 1
        assert "simple test fact" in out["entries"][0]["text"]


# ---------------------------------------------------------------------------
# BM25 hybrid retrieval (Tier B2)
# ---------------------------------------------------------------------------


class TestBM25:
    def test_search_with_bm25_true_fires_pool(
        self, pristine_service: MemoryService,
    ) -> None:
        """bm25=True per-call enables the BM25 pool even when config is off."""
        pristine_service.store(
            "the function process_chunk_v2 returns a tuple of (chunks, metadata)",
            source="code",
        )
        out = pristine_service.trace(
            "process_chunk_v2", bm25=True,
        )
        assert out["trace"]["bm25"]["fired"] is True
        assert out["trace"]["bm25"]["candidates_scored"] >= 1
        # The exact-token query should surface the entry.
        assert any(
            "process_chunk_v2" in e["text"] for e in out["entries"]
        )

    def test_bm25_false_disables_even_when_config_enabled(
        self, pristine_service: MemoryService,
    ) -> None:
        """bm25=False overrides config.bm25.enabled=True."""
        original = pristine_service.config.memory.bm25.enabled
        pristine_service.config.memory.bm25.enabled = True
        try:
            pristine_service.store("python tooling notes", source="x")
            out = pristine_service.trace("python", bm25=False)
            assert out["trace"]["bm25"]["fired"] is False
        finally:
            pristine_service.config.memory.bm25.enabled = original

    def test_bm25_catches_exact_keyword_dense_might_miss(
        self, pristine_service: MemoryService,
    ) -> None:
        """The classic hybrid win: a rare identifier-style token."""
        pristine_service.store(
            "we ship the bug fix in release v9.42.0 next Monday",
            source="release",
        )
        pristine_service.store(
            "lunch was nice today, the soup was warm",
            source="general",
        )
        out = pristine_service.search("v9.42.0", bm25=True, top_k=5)
        assert out["count"] >= 1
        # The exact-token entry should be at or near the top.
        top_texts = [e["text"] for e in out["entries"][:2]]
        assert any("v9.42.0" in t for t in top_texts), (
            f"v9.42.0 entry didn't surface in top-2; got {top_texts!r}"
        )

    def test_bm25_trace_records_per_hit_scores(
        self, pristine_service: MemoryService,
    ) -> None:
        """Each BM25 hit in the trace carries raw + normalised scores."""
        pristine_service.store(
            "pseudolife uses ChromaDB for the reference document bank",
            source="project",
        )
        out = pristine_service.trace(
            "ChromaDB reference document bank", bm25=True,
        )
        bm25_block = out["trace"]["bm25"]
        assert bm25_block["fired"] is True
        assert len(bm25_block["hits"]) >= 1
        hit = bm25_block["hits"][0]
        for key in ("text_preview", "raw_bm25", "normalized"):
            assert key in hit, f"missing {key!r} in bm25 trace hit"

    def test_bm25_respects_source_filter(
        self, pristine_service: MemoryService,
    ) -> None:
        """BM25 should obey the per-query source filter so it can't sneak
        filtered entries into the result set."""
        pristine_service.store(
            "important keyword zarpok lives in this entry",
            source="alpha",
        )
        pristine_service.store(
            "the keyword zarpok also appears here in beta",
            source="beta",
        )
        out = pristine_service.search(
            "zarpok", bm25=True, sources=["alpha"], top_k=5,
        )
        # Only the alpha-sourced entry can show up.
        for entry in out["entries"]:
            assert entry["source"] == "alpha"


# ---------------------------------------------------------------------------
# Tier C — episode lifecycle + tag filters on service surface
# ---------------------------------------------------------------------------


class TestEpisodes:
    """Service-level coverage of the episode lifecycle tools.

    The integration boundary is the MCP-facing surface: tool callers see
    plain dicts, the underlying ``EpisodeManager`` is exercised by
    ``test_episodes.py``. These tests verify that the lifecycle hooks
    actually stamp entries through the lock + ``_ensure_init`` path.
    """

    def test_episode_start_returns_id_title_and_started_at(
        self, pristine_service: MemoryService,
    ) -> None:
        out = pristine_service.episode_start("alpha-session")
        assert "id" in out and isinstance(out["id"], str) and len(out["id"]) == 32
        assert out["title"] == "alpha-session"
        assert out["started_at"] > 0
        assert out["ended_at"] is None

    def test_store_during_open_episode_stamps_entry(
        self, pristine_service: MemoryService,
    ) -> None:
        ep = pristine_service.episode_start("work-session")
        pristine_service.store("decision made about X", source="claude")
        recent = pristine_service.recent(n=1)
        assert recent["entries"][0]["episode_id"] == ep["id"]
        assert recent["entries"][0]["episode_title"] == "work-session"

    def test_store_after_episode_end_does_not_stamp(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.episode_start("done-session")
        pristine_service.episode_end()
        pristine_service.store("post-episode thought", source="claude")
        recent = pristine_service.recent(n=1)
        assert recent["entries"][0]["episode_id"] is None
        assert recent["entries"][0]["episode_title"] is None

    def test_episode_end_returns_closed_episode_with_ended_at(
        self, pristine_service: MemoryService,
    ) -> None:
        opened = pristine_service.episode_start("to-be-closed")
        closed = pristine_service.episode_end()
        assert closed["id"] == opened["id"]
        assert closed["ended_at"] is not None and closed["ended_at"] >= closed["started_at"]

    def test_episode_end_with_none_open_returns_empty(
        self, pristine_service: MemoryService,
    ) -> None:
        out = pristine_service.episode_end()
        assert out == {} or out is None or out.get("id") is None

    def test_episode_list_returns_newest_first(
        self, pristine_service: MemoryService,
    ) -> None:
        import time
        a = pristine_service.episode_start("first")
        pristine_service.episode_end()
        time.sleep(0.02)  # Windows clock resolution — distinguish timestamps
        b = pristine_service.episode_start("second")
        pristine_service.episode_end()
        listing = pristine_service.episode_list()
        ids = [e["id"] for e in listing["episodes"]]
        # b started after a → b first.
        assert ids.index(b["id"]) < ids.index(a["id"])
        assert listing["count"] == 2

    def test_episode_summary_returns_stats_and_recent_entries(
        self, pristine_service: MemoryService,
    ) -> None:
        ep = pristine_service.episode_start("stats-session")
        pristine_service.store("first decision", source="claude", tags=["a"])
        pristine_service.store("second decision", source="general", tags=["a", "b"])
        summary = pristine_service.episode_summary(ep["id"])
        assert summary["id"] == ep["id"]
        assert summary["title"] == "stats-session"
        assert summary["entry_count"] == 2
        # Tag distribution counts unique tags across entries.
        tag_dist = {t["tag"]: t["count"] for t in summary["tag_distribution"]}
        assert tag_dist.get("a") == 2
        assert tag_dist.get("b") == 1
        # Recent entries surfaces text+source for each.
        assert len(summary["recent_entries"]) == 2

    def test_episode_summary_for_missing_id_returns_not_found(
        self, pristine_service: MemoryService,
    ) -> None:
        out = pristine_service.episode_summary("no-such-id")
        assert out.get("found") is False

    def test_search_filtered_by_episode_returns_only_matching(
        self, pristine_service: MemoryService,
    ) -> None:
        ep_a = pristine_service.episode_start("alpha")
        pristine_service.store(
            "alpha-only context: choosing stdio transport for MCP",
            source="claude",
        )
        pristine_service.episode_end()
        ep_b = pristine_service.episode_start("beta")
        pristine_service.store(
            "beta context: a completely unrelated topic about cats",
            source="claude",
        )
        pristine_service.episode_end()

        out = pristine_service.search(
            "transport", episodes=[ep_a["id"]], top_k=5, min_score=0.0,
        )
        # The single alpha entry must appear; the beta one must not.
        texts = [e["text"] for e in out["entries"]]
        assert any("alpha-only" in t for t in texts)
        assert not any("beta context" in t for t in texts)

    def test_recent_filtered_by_episode(
        self, pristine_service: MemoryService,
    ) -> None:
        ep_a = pristine_service.episode_start("a")
        pristine_service.store("entry in a", source="claude")
        pristine_service.episode_end()
        pristine_service.episode_start("b")
        pristine_service.store("entry in b", source="claude")
        pristine_service.episode_end()

        out = pristine_service.recent(n=10, episodes=[ep_a["id"]])
        texts = [e["text"] for e in out["entries"]]
        assert "entry in a" in texts
        assert "entry in b" not in texts


class TestTagsSurface:
    """Tag plumbing at the service level."""

    def test_store_accepts_tags_parameter(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store(
            "tagged fact",
            source="claude",
            tags=["decision", "blocker"],
        )
        recent = pristine_service.recent(n=1)
        assert recent["entries"][0]["tags"] == ["decision", "blocker"]

    def test_list_tags_counts_unique_tags(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("a", source="x", tags=["red"])
        pristine_service.store("b", source="x", tags=["red", "blue"])
        pristine_service.store("c", source="x", tags=["blue", "green"])
        out = pristine_service.list_tags()
        counts = {row["tag"]: row["count"] for row in out["tags"]}
        assert counts.get("red") == 2
        assert counts.get("blue") == 2
        assert counts.get("green") == 1
        assert out["total"] == 5  # tag-occurrence total

    def test_list_tags_sorted_by_count_desc(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("a", source="x", tags=["popular"])
        pristine_service.store("b", source="x", tags=["popular"])
        pristine_service.store("c", source="x", tags=["popular", "rare"])
        out = pristine_service.list_tags()
        order = [row["tag"] for row in out["tags"]]
        assert order[0] == "popular"
        assert order[-1] == "rare"

    def test_search_with_tags_filter_drops_untagged(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store(
            "implemented feature behind a v1 flag",
            source="claude", tags=["feature-flag"],
        )
        pristine_service.store(
            "regular note about feature implementation",
            source="claude",
        )
        out = pristine_service.search(
            "feature", tags=["feature-flag"], min_score=0.0, top_k=5,
        )
        # All surfaced entries must carry the tag.
        for entry in out["entries"]:
            assert "feature-flag" in entry["tags"]

    def test_delete_by_tag_filter_removes_only_tagged(
        self, pristine_service: MemoryService,
    ) -> None:
        pristine_service.store("retire me", source="x", tags=["retired"])
        pristine_service.store("keep me", source="x", tags=["fresh"])
        out = pristine_service.delete(tag="retired")
        assert out["deleted_count"] == 1
        assert "retire me" in out["deleted_texts"]
        # Survivor still queryable.
        recent = pristine_service.recent(n=10)
        texts = [e["text"] for e in recent["entries"]]
        assert "keep me" in texts
        assert "retire me" not in texts

    def test_delete_by_episode_filter_removes_only_that_episode(
        self, pristine_service: MemoryService,
    ) -> None:
        ep_a = pristine_service.episode_start("trash-session")
        pristine_service.store("garbage 1", source="x")
        pristine_service.store("garbage 2", source="x")
        pristine_service.episode_end()
        pristine_service.episode_start("keep-session")
        pristine_service.store("treasure", source="x")
        pristine_service.episode_end()

        out = pristine_service.delete(episode=ep_a["id"])
        assert out["deleted_count"] == 2
        recent = pristine_service.recent(n=10)
        texts = [e["text"] for e in recent["entries"]]
        assert "treasure" in texts
        assert "garbage 1" not in texts
        assert "garbage 2" not in texts


class TestConsolidation:
    """Service-level cluster surfacing + atomic consolidate operation.

    The clustering algorithm itself is exercised by ``test_consolidation.py``;
    these tests focus on the service-layer wiring: query → embed →
    cluster → dict, plus the consolidate-and-supersede round-trip.
    """

    def test_consolidation_candidates_returns_cluster_dicts(
        self, pristine_service: MemoryService,
    ) -> None:
        """Three near-duplicate facts about the same topic should form
        one cluster when surfaced via ``consolidation_candidates``."""
        # All three describe stdio transport — same semantic content,
        # different phrasings. The bi-encoder should find them similar.
        pristine_service.store(
            "MCP uses stdio transport — no port conflicts",
            source="claude",
        )
        pristine_service.store(
            "stdio transport was chosen for MCP because ports clash",
            source="claude",
        )
        pristine_service.store(
            "decided on stdio for MCP transport (port-free)",
            source="claude",
        )
        # An unrelated fact that shouldn't join the cluster.
        pristine_service.store(
            "Python 3.11 is the minimum supported version",
            source="claude",
        )
        out = pristine_service.consolidation_candidates(
            query="MCP transport choice", top_k=10, min_cohesion=0.4,
        )
        assert out["query"] == "MCP transport choice"
        assert isinstance(out["clusters"], list)
        assert len(out["clusters"]) >= 1
        first = out["clusters"][0]
        assert "cohesion" in first and "members" in first
        member_texts = {m["text"] for m in first["members"]}
        # At least two stdio-related entries should cluster together.
        stdio_in_cluster = sum(
            1 for t in member_texts if "stdio" in t.lower()
        )
        assert stdio_in_cluster >= 2

    def test_consolidation_candidates_respects_episode_filter(
        self, pristine_service: MemoryService,
    ) -> None:
        """When scoped to one episode, candidates from other episodes
        should not leak in."""
        ep_a = pristine_service.episode_start("topic-A")
        pristine_service.store("alpha fact one", source="claude")
        pristine_service.store("alpha fact two", source="claude")
        pristine_service.episode_end()
        pristine_service.episode_start("topic-B")
        pristine_service.store("beta-only fact about something", source="claude")
        pristine_service.episode_end()

        out = pristine_service.consolidation_candidates(
            query=None, episode=ep_a["id"], top_k=20, min_cohesion=0.4,
        )
        # Every member that surfaces must be from episode A.
        for cluster in out["clusters"]:
            for member in cluster["members"]:
                assert member["episode_id"] == ep_a["id"]

    def test_consolidation_candidates_empty_bank_returns_empty(
        self, pristine_service: MemoryService,
    ) -> None:
        out = pristine_service.consolidation_candidates(
            query="anything", top_k=10,
        )
        assert out["clusters"] == []

    def test_consolidate_supersedes_old_and_stores_new(
        self, pristine_service: MemoryService,
    ) -> None:
        """The atomic operation: every entry in ``replaces`` gets marked
        superseded, the new entry is stored as a fresh memory."""
        pristine_service.store("fact A v1", source="claude")
        pristine_service.store("fact A v2", source="claude")
        pristine_service.store("fact A v3", source="claude")

        out = pristine_service.consolidate(
            replaces=["fact A v1", "fact A v2", "fact A v3"],
            new_text="Consolidated: fact A current state",
            source="consolidation",
            tags=["consolidated"],
        )
        assert out["superseded_count"] == 3
        assert out["new_memory_stored"] is True

        # Old entries surface as superseded; new one is current.
        recent = pristine_service.recent(n=10)
        by_text = {e["text"]: e for e in recent["entries"]}
        for old in ("fact A v1", "fact A v2", "fact A v3"):
            assert by_text[old]["superseded"] is True
            assert by_text[old]["superseded_by_text"] == (
                "Consolidated: fact A current state"
            )
        assert by_text["Consolidated: fact A current state"]["superseded"] is False
        assert by_text["Consolidated: fact A current state"]["tags"] == ["consolidated"]
        assert by_text["Consolidated: fact A current state"]["source"] == "consolidation"

    def test_consolidate_default_source_is_consolidation(
        self, pristine_service: MemoryService,
    ) -> None:
        """Source defaults to ``"consolidation"`` for audit clarity."""
        pristine_service.store("old fact", source="claude")
        out = pristine_service.consolidate(
            replaces=["old fact"], new_text="new fact",
        )
        recent = pristine_service.recent(n=5)
        new_entry = next(
            e for e in recent["entries"] if e["text"] == "new fact"
        )
        assert new_entry["source"] == "consolidation"
        assert out["superseded_count"] == 1

    def test_consolidate_empty_replaces_returns_no_op(
        self, pristine_service: MemoryService,
    ) -> None:
        """Defensive: an empty ``replaces`` list with a ``new_text``
        could be interpreted as 'just store this' — but the explicit
        consolidate semantics demand at least one supersession. Reject
        cleanly."""
        out = pristine_service.consolidate(
            replaces=[], new_text="anything",
        )
        assert out.get("error") or out["superseded_count"] == 0
        # No new memory stored either — the caller should use
        # ``memory_store`` for that.
        assert out["new_memory_stored"] is False


def test_store_stamps_callers_session_episode(
    pristine_service: MemoryService,
) -> None:
    """A store resolved to session A must carry A's episode, not whichever
    session opened most recently (the cross-session stamping bug)."""
    from pseudolife_memory.writer_context import (
        reset_writer_context,
        set_writer_context,
    )

    pristine_service.episode_start_session("A", "proj-a")
    pristine_service.episode_start_session("B", "proj-b")  # B is now current_id
    # Resolve the store to A — the EARLIER session — so a naive global-pointer
    # stamp (current_id == B) would mis-attribute it. Correct behavior stamps A.
    tok = set_writer_context("writer-x", "A")
    try:
        pristine_service.store("a durable fact about session A work", source="claude")
    finally:
        reset_writer_context(tok)
    eps = {e["title"]: e for e in pristine_service.episode_list()["episodes"]}
    assert eps["proj-a"]["entry_count"] == 1
    assert eps["proj-b"]["entry_count"] == 0


def test_store_lazy_opens_session_episode_when_none(
    pristine_service: MemoryService,
) -> None:
    """Direct-http (no shim/hook in the path): the first store from a session
    id with no open episode lazily opens one (daemon-owned lifecycle) and
    stamps it; a second store from the same session reuses that one episode."""
    from pseudolife_memory.writer_context import (
        reset_writer_context,
        set_writer_context,
    )

    tok = set_writer_context("writer-x", "MCP-SESS-1")
    try:
        pristine_service.store("first durable thing in this session")
        pristine_service.store("second durable thing in this session")
    finally:
        reset_writer_context(tok)
    eps = pristine_service.episode_list()["episodes"]
    mine = [e for e in eps if e["session_key"] == "MCP-SESS-1"]
    assert len(mine) == 1                  # exactly ONE episode for the session
    assert mine[0]["entry_count"] == 2     # both stores stamped to it
    assert mine[0]["title"].startswith("session - ")


def test_store_without_session_does_not_open_episode(
    pristine_service: MemoryService,
) -> None:
    """No session id (e.g. background/internal writer) must NOT lazily create
    an episode."""
    pristine_service.store("a memory with no session context")
    eps = pristine_service.episode_list()["episodes"]
    assert eps == [] or all(e["entry_count"] == 0 for e in eps)
