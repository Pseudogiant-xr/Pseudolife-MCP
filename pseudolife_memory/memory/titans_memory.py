"""Core memory data classes.

Despite the legacy filename, this module no longer owns any bank or neural
implementation — it defines only the :class:`MemoryEntry` and
:class:`RetrievalResult` dataclasses, which are referenced throughout the
codebase (and persisted in saved state). The episodic store lives in
:mod:`src.memory.miras` (plain cosine bands as of v0.5); the removed neural
machinery is archived on the ``archive/neural-memory-titans`` branch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import torch


@dataclass
class MemoryEntry:
    """A memory entry with text and metadata.

    ``superseded_at`` is set by the contradiction-detection path in
    :mod:`src.memory.contradiction` when a newer memory replaces this one.
    Retrieval filters superseded entries by default so the LLM sees only
    current facts (see ``ContinuumMemorySystem.retrieve``).

    ``last_logical_turn`` is stamped by the CMS at store time when a
    logical turn is open (see :meth:`ContinuumMemorySystem.begin_logical_turn`).
    Used by the ``min_logical_turn=`` retrieval filter for "what changed
    this session" queries.  ``None`` for entries created outside a logical
    turn (v0.5.x chat flow falls into this case).

    ``superseded_by_text`` (schema v5, v0.7.6) records the text of the
    memory that triggered this entry's supersession — populated by
    :func:`src.memory.contradiction.decay_contradicted_entries`. Used by
    the context builder to render *both* the superseded fact and its
    correction together so the LLM can answer state-probe questions
    correctly even when the correction's own embedding misses
    retrieval. ``None`` for pre-v5 entries and for entries that have
    never been superseded.

    ``episode_id`` / ``episode_title`` (schema v6, PseudoLife-MCP Tier C)
    stamp the entry with the open episode at store time. ``None`` when no
    episode was active. ``episode_title`` is denormalised so retrieval
    responses can show the label without joining against the episode log.

    ``tags`` (schema v6) is an open-ended multi-valued tag list alongside
    the single-string ``source`` field. Tags are normalised at store time
    (lowercase / stripped / deduplicated) and used as an OR-style filter
    axis in retrieval. Empty list when the caller didn't set any.
    """
    text: str
    embedding: torch.Tensor
    surprise_score: float = 0.0
    timestamp: float = 0.0
    access_count: int = 0
    source: str = ""
    bank: str = ""
    superseded_at: float | None = None
    last_logical_turn: int | None = None
    # ``slots`` is a list of ``(entity, attribute, value, polarity)`` tuples
    # extracted by :mod:`src.memory.slots` at store time. Kept as plain
    # tuples (not dataclasses) so torch.save round-trips them losslessly.
    # Empty list when no slots were extractable from the text. Schema v4.
    slots: list[tuple[str, str, str, str]] = field(default_factory=list)
    # Text of the memory that triggered this entry's supersession. Schema v5.
    superseded_by_text: str | None = None
    # Episode anchoring (schema v6, Tier C). ``episode_id`` is a uuid4 hex
    # string; ``episode_title`` is the human label denormalised for display.
    # Both ``None`` when no episode was open at store time.
    episode_id: str | None = None
    episode_title: str | None = None
    # Multi-valued tag list (schema v6, Tier C). Normalised by the caller
    # (lowercase / stripped / deduplicated). Empty when no tags were set.
    tags: list[str] = field(default_factory=list)
    # Storage row id (schema v8, transient — NOT persisted in .pt saves).
    # None in file mode or before the write-through insert returns.
    db_id: int | None = None

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class RetrievalResult:
    """Result from memory retrieval."""
    entries: list[MemoryEntry]
    scores: list[float]
    surprises: list[float]


# v0.5: the deprecated ``MemoryMLP`` / ``TitansMemoryBank`` compat shims were
# removed with the neural memory. This module now only defines the
# ``MemoryEntry`` / ``RetrievalResult`` dataclasses that the rest of the package
# imports. The neural machinery lives on the ``archive/neural-memory-titans`` branch.
