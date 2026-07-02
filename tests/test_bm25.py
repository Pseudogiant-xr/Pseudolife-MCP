"""Unit tests for the BM25 sparse-lexical retrieval module.

These tests run against synthetic ``MemoryEntry`` objects so they're
fast (no embedder, no torch) and isolated from the rest of the
retrieval pipeline. End-to-end tests that exercise BM25 through the
MemoryService live in ``test_service.py::TestBM25``.
"""

from __future__ import annotations

import time

import pytest
import torch

from pseudolife_memory.memory.bm25 import (
    BM25Index,
    normalize_scores,
    tokenize,
)
from pseudolife_memory.memory.titans_memory import MemoryEntry


def _make_entry(text: str, source: str = "test") -> MemoryEntry:
    """MemoryEntry minus the embedding (BM25 doesn't need it)."""
    return MemoryEntry(
        text=text,
        embedding=torch.zeros(4),
        timestamp=time.time(),
        surprise_score=0.5,
        access_count=0,
        source=source,
        bank="instant",
    )


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_lowercases(self) -> None:
        assert tokenize("Python Rocks") == ["python", "rocks"]

    def test_keeps_identifiers_whole(self) -> None:
        """Underscored identifiers stay one token — that's the whole
        point of running BM25 over code-shaped text."""
        toks = tokenize("call process_chunk_v2 with cfg")
        assert "process_chunk_v2" in toks

    def test_keeps_dotted_versions_whole(self) -> None:
        toks = tokenize("ship PseudoLife v0.7.6 today")
        assert "v0.7.6" in toks

    def test_drops_stop_words(self) -> None:
        toks = tokenize("the cat is the king")
        assert "the" not in toks
        assert "is" not in toks
        assert "cat" in toks
        assert "king" in toks

    def test_empty_input(self) -> None:
        assert tokenize("") == []
        assert tokenize("   ") == []


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


class TestIndexBuild:
    def test_invalid_k1_raises(self) -> None:
        with pytest.raises(ValueError, match="k1"):
            BM25Index([], k1=-0.1)

    def test_invalid_b_raises(self) -> None:
        with pytest.raises(ValueError, match="b"):
            BM25Index([], b=1.5)

    def test_empty_index_size(self) -> None:
        idx = BM25Index([])
        assert len(idx) == 0
        assert idx.avg_doc_length == 0.0

    def test_empty_index_score_returns_empty(self) -> None:
        idx = BM25Index([])
        assert idx.score("anything") == []

    def test_avg_doc_length(self) -> None:
        idx = BM25Index([
            _make_entry("a b c"),    # 3 tokens (after stop-list, "c" only? actually 'a' is stop)
            _make_entry("d e f g"),  # 4 tokens
        ])
        # "a" is a stop word → entry 1 has 2 effective tokens (b, c).
        # entry 2 has 4 (d, e, f, g). Average = 3.
        assert idx.avg_doc_length == 3.0


# ---------------------------------------------------------------------------
# IDF
# ---------------------------------------------------------------------------


class TestIDF:
    def test_rare_term_has_higher_idf_than_common_term(self) -> None:
        idx = BM25Index([
            _make_entry("python rocks"),
            _make_entry("python rules"),
            _make_entry("python wins"),
            _make_entry("rust gleams"),
        ])
        # "python" appears in 3/4 docs (common), "rust" in 1/4 (rare).
        assert idx.idf("rust") > idx.idf("python")

    def test_unseen_term_idf_is_zero(self) -> None:
        idx = BM25Index([_make_entry("hello world")])
        assert idx.idf("nonexistent") == 0.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScore:
    def test_exact_match_outranks_unrelated(self) -> None:
        idx = BM25Index([
            _make_entry("we use pytest for python testing"),
            _make_entry("the cat is on the mat"),
        ])
        hits = idx.score("pytest")
        assert len(hits) == 1
        assert "pytest" in hits[0][0].text

    def test_rare_token_query_finds_exact_entry(self) -> None:
        """The classic BM25 win: a token with no semantic neighbours."""
        idx = BM25Index([
            _make_entry("the cat sleeps in the kitchen"),
            _make_entry("the dog walks in the park"),
            _make_entry("function process_chunk_v2 returns a tuple"),
        ])
        hits = idx.score("process_chunk_v2", top_k=5)
        assert len(hits) >= 1
        assert "process_chunk_v2" in hits[0][0].text

    def test_dotted_version_token(self) -> None:
        idx = BM25Index([
            _make_entry("PseudoLife v0.7.6 ships new features"),
            _make_entry("PseudoLife v0.7.5 was the previous release"),
            _make_entry("the cat sat on the mat"),
        ])
        hits = idx.score("v0.7.6", top_k=5)
        assert len(hits) >= 1
        assert "v0.7.6" in hits[0][0].text

    def test_zero_score_entries_dropped(self) -> None:
        idx = BM25Index([
            _make_entry("hello world"),
            _make_entry("entirely unrelated text"),
        ])
        hits = idx.score("hello")
        # Only the matching entry shows up — score 0 entries are filtered.
        assert len(hits) == 1
        assert "hello" in hits[0][0].text

    def test_empty_query_returns_empty(self) -> None:
        idx = BM25Index([_make_entry("hello world")])
        assert idx.score("") == []
        assert idx.score("   ") == []

    def test_query_only_stop_words_returns_empty(self) -> None:
        idx = BM25Index([_make_entry("the cat sat on the mat")])
        assert idx.score("the is a an the") == []

    def test_top_k_respected(self) -> None:
        idx = BM25Index([
            _make_entry("python rocks"),
            _make_entry("python rules"),
            _make_entry("python wins"),
        ])
        hits = idx.score("python", top_k=2)
        assert len(hits) == 2


# ---------------------------------------------------------------------------
# Score normalisation
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_empty_returns_empty(self) -> None:
        assert normalize_scores([]) == []

    def test_single_collapses_to_one(self) -> None:
        e = _make_entry("hi")
        out = normalize_scores([(e, 7.5)])
        assert out == [(e, 1.0)]

    def test_min_max_normalises_range(self) -> None:
        a, b, c = _make_entry("a"), _make_entry("b"), _make_entry("c")
        out = normalize_scores([(a, 5.0), (b, 3.0), (c, 1.0)])
        # min=1, max=5, span=4 — so values become 1.0, 0.5, 0.0.
        out_scores = {e.text: s for e, s in out}
        assert out_scores["a"] == pytest.approx(1.0)
        assert out_scores["b"] == pytest.approx(0.5)
        assert out_scores["c"] == pytest.approx(0.0)

    def test_identical_scores_collapse_to_one(self) -> None:
        """All-tied input shouldn't divide by zero — it collapses to 1.0."""
        a, b = _make_entry("a"), _make_entry("b")
        out = normalize_scores([(a, 4.0), (b, 4.0)])
        assert all(s == 1.0 for _, s in out)


class TestTokenizerIntegers:
    """2026-07-02 review M2: the numeric alternative required a dot, so
    standalone integers vanished — defeating the module's own stated purpose
    (error codes, ports, model numbers)."""

    def test_standalone_integers_survive(self) -> None:
        from pseudolife_memory.memory.bm25 import tokenize
        assert tokenize("port 8080") == ["port", "8080"]
        assert tokenize("error 404 from nginx") == ["error", "404", "from", "nginx"]
        assert "4090" in tokenize("the RTX 4090 workstation")

    def test_dotted_versions_still_whole(self) -> None:
        from pseudolife_memory.memory.bm25 import tokenize
        assert "v0.7.6" in tokenize("shipped in v0.7.6")
