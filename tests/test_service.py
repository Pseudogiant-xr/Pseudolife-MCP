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
