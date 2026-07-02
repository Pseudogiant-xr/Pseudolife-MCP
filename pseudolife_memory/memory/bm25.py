"""BM25 lexical retrieval over the CMS memory pool (Tier B2).

The dense MiniLM-L6 embedder is excellent for semantic recall but
underweights exact-token matches. A query like ``process_chunk_v2`` —
a function name with no real semantic neighbours — can be drowned by
entries with more "natural" tokens that happen to share embedding
neighbours with the query. BM25, the canonical sparse-retrieval
scorer, is the standard fix: it weights tokens by inverse document
frequency, so rare-but-exact tokens count for a lot.

We run BM25 in parallel with the bi-encoder dense retrieval and
weighted-sum-fuse the two pools. This is a *hybrid retrieval* pipeline,
the same shape that powers modern production search stacks (Vespa,
Elasticsearch+kNN, Pinecone Hybrid). The fusion is intentionally
simple — normalised BM25 scores get scaled by ``weight`` and added to
the bi-encoder's adjusted score. Entries that appear in only one pool
still surface; entries that appear in both get boosted.

Design notes
------------
* **Pure stdlib.** No new dependencies. The whole module is one tokenizer
  + one BM25Okapi-style scorer + a fusion helper.
* **Rebuild per query.** Index construction is O(N tokens) which for a
  40K-entry bank with ~50 tokens per entry is ~20-50ms — well below the
  budget for an MCP tool call. We can introduce incremental
  maintenance later if the bank grows enough to matter.
* **Off by default.** ``BM25Config.enabled = False`` ships disabled so
  Tier-A users see no behaviour change. Flip the config flag or pass
  ``bm25=True`` per call to enable.

Tokenizer
---------
Whitespace + punctuation split, lowercased, ASCII-folded enough to
work on identifier-style tokens (``process_chunk_v2`` stays whole;
``Don't`` becomes ``don``, ``t``). A tiny stop-list filters ``a``,
``an``, ``the``, ``is``, ``are`` — enough to keep "the" from polluting
IDF without overreaching into rare-but-real tokens.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pseudolife_memory.memory.titans_memory import MemoryEntry


# Minimal stop list — five tokens that show up so frequently they
# dominate IDF without carrying signal. Deliberately small: aggressive
# stop-listing kills recall on questions like "is X true" where ``is``
# is actually informative once you weight by IDF.
_STOP = frozenset({"a", "an", "the", "is", "are"})

# Identifier-friendly tokeniser. Captures `process_chunk_v2`,
# `v0.7.6`, `--rerank`, `MIRAS`, while dropping standalone punctuation.
# Underscores stay; periods inside version-like tokens stay. The numeric
# alternative is ``*`` not ``+``: standalone integers (ports, HTTP codes,
# model numbers) must survive — they're the whole point of the lexical
# channel (2026-07-02 review M2 fixed the dot-required variant).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*|\d+(?:\.\d+)*")


def tokenize(text: str) -> list[str]:
    """Lowercase + identifier-aware split. Filters the tiny stop list.

    Returns an empty list for empty / whitespace-only input.
    """
    if not text:
        return []
    return [
        t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text))
        if t not in _STOP
    ]


@dataclass
class _Doc:
    """Single indexed document — its tokens, length, and source entry."""
    entry: "MemoryEntry"
    tokens: list[str]
    length: int


class BM25Index:
    """Okapi BM25 over a list of :class:`MemoryEntry` objects.

    Parameters
    ----------
    k1:
        Term-frequency saturation. Standard default 1.5; higher
        values make the scorer more sensitive to repeated terms.
    b:
        Length normalisation. Standard default 0.75; 0 disables
        length penalty, 1 fully normalises by doc length.

    Use
    ---
    >>> idx = BM25Index(entries, k1=1.5, b=0.75)
    >>> hits = idx.score("function process_chunk_v2", top_k=5)
    >>> for entry, score in hits:
    ...     ...

    The index is built eagerly in :meth:`__init__`. For incremental
    maintenance, drop the old index and construct a new one — that
    keeps the implementation honest.
    """

    def __init__(
        self,
        entries: list["MemoryEntry"],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if k1 < 0:
            raise ValueError(f"k1 must be >= 0, got {k1!r}")
        if not 0.0 <= b <= 1.0:
            raise ValueError(f"b must be in [0, 1], got {b!r}")
        self.k1 = float(k1)
        self.b = float(b)

        self._docs: list[_Doc] = [
            _Doc(entry=e, tokens=(toks := tokenize(e.text)), length=len(toks))
            for e in entries
        ]
        self._N = len(self._docs)
        # Avoid divide-by-zero when the bank is empty or has only
        # zero-token documents.
        total_len = sum(d.length for d in self._docs)
        self._avg_len = (total_len / self._N) if self._N else 0.0

        # Document frequency per term (how many docs contain the term).
        self._df: dict[str, int] = {}
        for doc in self._docs:
            for term in set(doc.tokens):
                self._df[term] = self._df.get(term, 0) + 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return self._N

    @property
    def avg_doc_length(self) -> float:
        return self._avg_len

    def idf(self, term: str) -> float:
        """Robertson-Spärck-Jones IDF with the +1 smoothing in the
        numerator so single-occurrence terms still get a positive
        weight. Returns 0 for unseen terms (no negative contributions).
        """
        df = self._df.get(term, 0)
        if df == 0 or self._N == 0:
            return 0.0
        # log((N - df + 0.5) / (df + 0.5) + 1) — Lucene's variant,
        # always non-negative.
        return math.log(1.0 + (self._N - df + 0.5) / (df + 0.5))

    def _doc_score(self, doc: _Doc, query_tokens: list[str]) -> float:
        """BM25 score of a single document against the query tokens."""
        if doc.length == 0 or not query_tokens:
            return 0.0
        score = 0.0
        # Term frequency cache for this doc — counted once.
        tf: dict[str, int] = {}
        for tok in doc.tokens:
            tf[tok] = tf.get(tok, 0) + 1
        norm = 1.0 - self.b + self.b * (doc.length / self._avg_len if self._avg_len else 0.0)
        for q in query_tokens:
            f = tf.get(q, 0)
            if f == 0:
                continue
            idf = self.idf(q)
            if idf <= 0.0:
                continue
            score += idf * (f * (self.k1 + 1.0)) / (f + self.k1 * norm)
        return score

    def score(
        self,
        query: str,
        top_k: int = 20,
    ) -> list[tuple["MemoryEntry", float]]:
        """Score the query against every document. Return the top-K.

        Documents with zero score (no query terms appear) are dropped
        so the caller never has to filter for them.
        """
        if not self._N:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scored: list[tuple["MemoryEntry", float]] = []
        for doc in self._docs:
            s = self._doc_score(doc, q_tokens)
            if s > 0.0:
                scored.append((doc.entry, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def normalize_scores(scored: list[tuple["MemoryEntry", float]]) -> list[tuple["MemoryEntry", float]]:
    """Min-max normalise a list of (entry, score) into the range [0, 1].

    Empty input returns empty. A single-element input collapses to 1.0
    (a single hit is, by definition, the top). Returns a fresh list —
    inputs are not mutated.
    """
    if not scored:
        return []
    if len(scored) == 1:
        e, _ = scored[0]
        return [(e, 1.0)]
    max_s = max(s for _, s in scored)
    min_s = min(s for _, s in scored)
    span = max_s - min_s
    if span <= 0.0:
        # All identical — collapse to 1.0 so the fusion still ranks
        # them above pure-dense-only hits.
        return [(e, 1.0) for e, _ in scored]
    return [(e, (s - min_s) / span) for e, s in scored]
