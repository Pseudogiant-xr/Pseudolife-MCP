"""Core memory data classes.

Despite the legacy filename, this module no longer owns any bank or neural
implementation — it defines only the :class:`MemoryEntry` and
:class:`RetrievalResult` dataclasses, which are referenced throughout the
codebase (and persisted in saved state). The episodic store lives in
:mod:`src.memory.miras` (plain cosine bands as of v0.5); the removed neural
machinery is archived on the ``archive/neural-memory-titans`` branch.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

import torch

# Process-wide creation counter backing ``MemoryEntry.seq``. ``next()`` on an
# ``itertools.count`` is atomic under the GIL, so concurrent stores get
# distinct, ordered stamps without extra locking.
_seq_counter = itertools.count(1)


@dataclass
class MemoryEntry:
    """A memory entry with text and metadata.

    ``superseded_at`` is set by the contradiction-detection path in
    :mod:`src.memory.contradiction` when a newer memory replaces this one.
    Retrieval filters superseded entries by default so the LLM sees only
    current facts (see ``ContinuumMemorySystem.retrieve``).

    ``last_logical_turn`` is stamped by the CMS at store time when a
    logical turn is open, and read by the ``min_logical_turn=`` retrieval
    filter for "what changed this session" queries. ``None`` for entries
    created outside a logical turn — which, since the turn-opening methods
    were removed as uncalled (see ``_in_logical_turn`` in ``cms.py``), is
    currently every entry.

    ``superseded_by_text`` (schema v5, v0.7.6) records the text of the
    memory that triggered this entry's supersession — populated by
    :func:`src.memory.contradiction.decay_contradicted_entries`. Used by
    the context builder to render *both* the superseded fact and its
    correction together so the LLM can answer state-probe questions
    correctly even when the correction's own embedding misses
    retrieval. ``None`` for pre-v5 entries and for entries that have
    never been superseded.

    ``episode_id`` / ``episode_title`` (schema v6, Pseudolife-MCP Tier C)
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
    # Write-time label pair (schema v35; memory/labels.py). ``authority``
    # = the speech act ("directive" | "observation" | "quoted"; None =
    # observation, the plainly-asserted default) and
    # ``distortion_tolerance`` = how exactly the text must survive
    # consolidation ("constraint" | "procedural" | "belief" |
    # "preference" | "episodic"; None = unlabelled). Persisted on the
    # entries row and carried through supersede / consolidate / band
    # relocation; NULL everywhere is exactly the pre-v35 behaviour.
    authority: str | None = None
    distortion_tolerance: str | None = None
    # Storage row id (schema v8, transient — NOT persisted in .pt saves).
    # None in file mode or before the write-through insert returns.
    db_id: int | None = None
    # Reinforcement strength (schema v13). DB-authoritative read-cache: loaded at
    # hydrate, bumped in-memory on each bump path, never written back via a save
    # path. Read by RetentionPolicy.source_weighted_score (MTT retention).
    reinforcements: int = 0
    # Process-monotonic creation sequence (transient, like ``db_id`` — NOT
    # persisted; hydration re-stamps entries in load order, which is insertion
    # order). Breaks ordering ties when wall-clock timestamps collide within
    # one ``time.time()`` tick — the wall clock alone cannot order same-tick
    # stores, and band promotion relocates entries so list position can't
    # either. Preserved across promotion (a relocation, not a re-creation).
    seq: int = 0
    # Memoised ``(has_gain_cue, has_loss_cue)`` for ``text``, filled lazily
    # by contradiction detection — which scans every entry of every band on
    # every write, and for most entries the cue check IS the cost. Transient
    # like ``db_id`` / ``seq``: never persisted, excluded from equality.
    # Safe to cache because ``text`` is never mutated after construction
    # (a relocation re-creates the entry rather than editing it).
    cue_flags: tuple[bool, bool] | None = field(
        default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        if self.seq == 0:
            self.seq = next(_seq_counter)


@dataclass
class RetrievalResult:
    """Result from memory retrieval.

    ``via`` (agg-recall Phase 1) is an optional per-entry channel marker
    aligned with ``entries`` — e.g. ``"timeline"`` for entries the timeline
    channel injected. ``None`` (the default, and every pre-Phase-1 caller)
    means no markers; a list uses ``None`` for ordinary dense/slot/BM25
    hits so consumers can tell structural context from scored hits.

    ``components`` / ``params`` (retrieval-log Phase 1 features) are the
    ranking inputs the fusion consumed: a per-entry dict aligned with
    ``entries`` (bi-encoder score, recency, multipliers, BM25 boost,
    cross-encoder score) and the per-query knob snapshot. Both are
    ``None`` for band-level results — only :meth:`ContinuumMemorySystem.
    retrieve` fuses, so only it can describe the fusion."""
    entries: list[MemoryEntry]
    scores: list[float]
    surprises: list[float]
    via: list[str | None] | None = None
    components: list[dict | None] | None = None
    params: dict | None = None


# v0.5: the deprecated ``MemoryMLP`` / ``TitansMemoryBank`` compat shims were
# removed with the neural memory. This module now only defines the
# ``MemoryEntry`` / ``RetrievalResult`` dataclasses that the rest of the package
# imports. The neural machinery lives on the ``archive/neural-memory-titans`` branch.
