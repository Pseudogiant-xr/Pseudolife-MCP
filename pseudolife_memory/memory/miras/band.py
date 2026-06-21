"""The :class:`MIRASBand` — one tier in the continuum: a capacity-bounded
**cosine** vector store with a **novelty** surprise gate.

v0.5: the test-time-trained neural memory (per-store MLP update + the neural
retrieval blend + the HOPE chained read) was removed — it underperformed plain
cosine and was a regime mismatch for standalone embedding retrieval (see
``docs/2026-06-21-neural-memory-investigation.md``). The full machinery is
preserved on the ``archive/neural-memory-titans`` branch. Bands now rank purely
by cosine similarity and gate stores by novelty against existing entries.

Public surface (drop-in with the prior band where it matters):

* ``entries: list[MemoryEntry]`` — text + embeddings + metadata.
* ``size: int`` — len of ``entries``.
* ``name: str`` — band identifier (``instant`` / ``fast`` / …).
* ``surprise_ema: float`` — EMA of past surprise scores (telemetry).
* ``compute_surprise(embedding) -> float`` — novelty vs existing entries.
* ``store(text, embedding, source, surprise) -> None`` — append + evict.
* ``retrieve(query_embedding, top_k) -> RetrievalResult`` — cosine top-k.
* ``get_state_dict() / load_state_dict()`` — entry persistence (tolerant of
  legacy MLP-weight blocks, which are ignored).

Construction is via :func:`build_band` from a
:class:`src.utils.config.MIRASBandSpec`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.memory.miras.retention import build_policy, now_seconds

if TYPE_CHECKING:
    from pseudolife_memory.utils.config import MIRASBandSpec


class MIRASBand:
    """One tier in the Continuum Memory System: a cosine vector store with
    capacity eviction, recency, and a novelty surprise gate."""

    def __init__(
        self,
        name: str,
        embedding_dim: int,
        retention,
        max_entries: int,
        update_interval: int,
        promotion_access_count: int,
        promotion_surprise: float,
        device: str = "cuda",
    ):
        self.name = name
        self.embedding_dim = embedding_dim
        self.retention = retention
        self.max_entries = max_entries
        self.update_interval = update_interval
        self.promotion_access_count = promotion_access_count
        self.promotion_surprise = promotion_surprise
        self.device = device if torch.cuda.is_available() else "cpu"

        # Surprise-EMA bookkeeping — introspection / consolidation telemetry.
        self.surprise_ema: float = 0.0
        self.surprise_ema_decay: float = 0.95

        # Entry store with a lazy normalised pattern-matrix cache for cosine
        # retrieval + novelty scoring. The CMS mutates ``entries`` directly
        # during consolidation — flip ``_dirty`` whenever the list changes.
        self.entries: list[MemoryEntry] = []
        self._pattern_matrix: torch.Tensor | None = None
        self._dirty: bool = True

        # Optional eviction callback: the CMS sets this so a capacity eviction
        # also removes the entry's storage row.
        self.on_evict = None

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.entries)

    # ------------------------------------------------------------------
    # Surprise gate (novelty)
    # ------------------------------------------------------------------

    def compute_surprise(self, embedding: torch.Tensor) -> float:
        """Novelty of ``embedding`` vs what this band already holds.

        ``1 - max_j cos(embedding, entry_j)`` — a near-duplicate of an existing
        entry is unsurprising (→ 0), genuinely new content is surprising (→ 1).
        Returns ``1.0`` on an empty band (everything is novel before anything
        is stored). This is what the ``surprise_threshold`` gate has always
        meant: don't re-store what we already hold.
        """
        if not self.entries:
            return 1.0
        if self._dirty:
            self._rebuild_pattern_matrix()
        assert self._pattern_matrix is not None
        x = embedding.to(self.device)
        x = F.normalize(x.unsqueeze(0), p=2, dim=1).squeeze(0)
        max_sim = float((self._pattern_matrix @ x).max())
        return max(0.0, min(1.0, 1.0 - max_sim))

    # ------------------------------------------------------------------
    # Store + eviction
    # ------------------------------------------------------------------

    def store(
        self,
        text: str,
        embedding: torch.Tensor,
        source: str = "",
        surprise: float = 0.0,
    ) -> None:
        """Append an entry; evict under capacity pressure. No training."""
        entry = MemoryEntry(
            text=text,
            embedding=embedding.detach().to(self.device),
            surprise_score=surprise,
            source=source,
            bank=self.name,
        )
        if len(self.entries) >= self.max_entries:
            self._evict_one()
        self.entries.append(entry)
        self._dirty = True
        # Telemetry EMA over the (novelty) surprise the gate measured.
        self.surprise_ema = (
            self.surprise_ema_decay * self.surprise_ema
            + (1.0 - self.surprise_ema_decay) * float(surprise)
        )

    def _evict_one(self) -> None:
        """Drop the entry with the lowest source-weighted retention score
        (recency / surprise / balanced × per-source multiplier)."""
        if not self.entries:
            return
        now = now_seconds()
        scores = [self.retention.source_weighted_score(e, now) for e in self.entries]
        worst = min(range(len(scores)), key=lambda i: scores[i])
        evicted = self.entries.pop(worst)
        self._dirty = True
        if self.on_evict is not None:
            self.on_evict(evicted)

    # ------------------------------------------------------------------
    # Retrieval (pure cosine)
    # ------------------------------------------------------------------

    def retrieve(
        self, query_embedding: torch.Tensor, top_k: int = 5
    ) -> RetrievalResult:
        """Top-k entries by cosine similarity to the query."""
        if not self.entries:
            return RetrievalResult(entries=[], scores=[], surprises=[])

        if self._dirty:
            self._rebuild_pattern_matrix()
        assert self._pattern_matrix is not None  # implied by len(entries) > 0

        query = query_embedding.to(self.device)
        query = F.normalize(query.unsqueeze(0), p=2, dim=1).squeeze(0)
        scores = self._pattern_matrix @ query

        k = min(top_k, len(self.entries))
        top_scores, top_indices = torch.topk(scores, k)

        result_entries: list[MemoryEntry] = []
        result_surprises: list[float] = []
        for idx in top_indices.tolist():
            entry = self.entries[idx]
            entry.access_count += 1
            result_entries.append(entry)
            result_surprises.append(entry.surprise_score)

        return RetrievalResult(
            entries=result_entries,
            scores=top_scores.detach().cpu().tolist(),
            surprises=result_surprises,
        )

    def _rebuild_pattern_matrix(self) -> None:
        if not self.entries:
            self._pattern_matrix = None
            self._dirty = False
            return
        embeddings = [e.embedding.to(self.device) for e in self.entries]
        self._pattern_matrix = F.normalize(torch.stack(embeddings), p=2, dim=1)
        self._dirty = False

    # ------------------------------------------------------------------
    # Persistence — entries only (tolerant of legacy MLP-weight blocks)
    # ------------------------------------------------------------------

    def get_state_dict(self) -> dict:
        """Serialise this band's entries (no weights — there is no MLP)."""
        return {
            "entries": [
                {
                    "text": e.text,
                    "embedding": e.embedding.cpu(),
                    "surprise_score": e.surprise_score,
                    "timestamp": e.timestamp,
                    "access_count": e.access_count,
                    "source": e.source,
                    "superseded_at": e.superseded_at,
                    "superseded_by_text": e.superseded_by_text,
                    "last_logical_turn": e.last_logical_turn,
                    "slots": e.slots,
                    "episode_id": e.episode_id,
                    "episode_title": e.episode_title,
                    "tags": e.tags,
                }
                for e in self.entries
            ]
        }

    def load_state_dict(self, state: dict) -> None:
        """Restore entries from a saved band. Legacy ``memory_state`` /
        ``optimizer_state`` / ``update_count`` / ``axes`` blocks (pre-v0.5
        MLP weights) are ignored — only entries are restored."""
        self.entries = [
            MemoryEntry(
                text=e["text"],
                embedding=e["embedding"].to(self.device),
                surprise_score=e["surprise_score"],
                timestamp=e["timestamp"],
                access_count=e["access_count"],
                source=e.get("source", ""),
                bank=self.name,
                superseded_at=e.get("superseded_at"),
                superseded_by_text=e.get("superseded_by_text"),
                last_logical_turn=e.get("last_logical_turn"),
                slots=e.get("slots", []),
                episode_id=e.get("episode_id"),
                episode_title=e.get("episode_title"),
                tags=list(e.get("tags") or []),
            )
            for e in state.get("entries", [])
        ]
        self._dirty = True


def build_band(spec: "MIRASBandSpec", embedding_dim: int, device: str) -> MIRASBand:
    """Construct a :class:`MIRASBand` from a :class:`MIRASBandSpec` — a plain
    cosine store with the spec's capacity / cadence / promotion / eviction."""
    policy = build_policy(spec.retention_policy)
    return MIRASBand(
        name=spec.name,
        embedding_dim=embedding_dim,
        retention=policy,
        max_entries=spec.max_entries,
        update_interval=spec.update_interval,
        promotion_access_count=spec.promotion_access_count,
        promotion_surprise=spec.promotion_surprise,
        device=device,
    )
