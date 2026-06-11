"""TITANS Neural Long-Term Memory Module — compat shim over MIRAS.

In v0.4.x this module owned the bank implementation. In v0.5 the
implementation moved to :mod:`src.memory.miras`, which generalises the
v0.4.x design to a 4-axis configurable framework (see
:mod:`src.memory.miras` for details).

What's left here
----------------

* :class:`MemoryEntry` and :class:`RetrievalResult` — these are pure data
  classes referenced throughout the codebase (and persisted in saved
  state). They stay here as the source of truth.
* :class:`MemoryMLP` — a thin deprecated alias for
  :class:`src.memory.miras.modules.MLP3Module`. Kept so any external
  scripts that imported it keep working.
* :class:`TitansMemoryBank` — a deprecated subclass of
  :class:`src.memory.miras.MIRASBand` that translates the v0.4.x
  constructor kwargs into the ``titans`` preset's components.

When the v0.5.0 CMS refactor lands, ``cms.py`` stops instantiating
:class:`TitansMemoryBank` directly and uses
:func:`src.memory.miras.build_band` instead, at which point this shim
is only kept for the migration loader's benefit.

References:
    - Titans: Learning to Memorize at Test Time (arXiv:2501.00663)
    - Nested Learning: The Illusion of Deep Learning (arXiv:2512.24695)
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field

import torch

# Import the new home of the implementation. The cycle
# ``titans_memory → miras.band → titans_memory`` is broken by ``miras.band``
# importing only :class:`MemoryEntry` / :class:`RetrievalResult`, which are
# defined in this module before the import below runs.


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


# ---------------------------------------------------------------------------
# Backwards-compat shims — defined below the dataclasses so that
# ``miras.band`` can import ``MemoryEntry`` / ``RetrievalResult`` from here
# without circular-import grief.
# ---------------------------------------------------------------------------

from pseudolife_memory.memory.miras.band import MIRASBand  # noqa: E402
from pseudolife_memory.memory.miras.modules import MLP3Module  # noqa: E402
from pseudolife_memory.memory.miras.update_rules import SGDMomentumUpdate  # noqa: E402
from pseudolife_memory.memory.miras.objectives import L2ReconstructionObjective  # noqa: E402
from pseudolife_memory.memory.miras.retention import balanced  # noqa: E402


class MemoryMLP(MLP3Module):
    """Deprecated alias for :class:`src.memory.miras.modules.MLP3Module`.

    Kept so external scripts that ``from pseudolife_memory.memory.titans_memory import
    MemoryMLP`` continue to work. Behaviour is identical — MLP3Module is
    a verbatim reimplementation under the MIRAS protocol contract.
    """

    def __init__(self, dim: int, hidden_dim: int = 512):
        warnings.warn(
            "MemoryMLP is deprecated; use src.memory.miras.modules.MLP3Module instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(dim=dim, hidden_dim=hidden_dim)

    # The legacy class had an ``_init_weights`` private method (with a
    # leading underscore) and the rest of the codebase calls
    # ``bank.memory._init_weights()`` during ``ContinuumMemorySystem.clear``.
    # The MIRAS rename is :meth:`init_weights` (public). Provide the alias
    # so the existing CMS code keeps working until task 5 lands.
    def _init_weights(self) -> None:
        self.init_weights()


class TitansMemoryBank(MIRASBand):
    """Deprecated compat shim: builds a :class:`MIRASBand` with the
    ``titans`` preset components from the v0.4.x kwargs.

    Behaviour is bit-for-bit identical to v0.4.x — same MLP3, same
    SGD-momentum + weight_decay + grad clip 1.0, same L2 reconstruction
    loss, same balanced eviction policy.

    Direct instantiation is discouraged in v0.5+; use
    :func:`src.memory.miras.build_band` with a
    :class:`src.utils.config.MIRASBandSpec` instead.
    """

    def __init__(
        self,
        embedding_dim: int = 384,
        hidden_dim: int = 512,
        max_entries: int = 5000,
        learning_rate: float = 0.01,
        weight_decay: float = 0.001,
        device: str = "cuda",
        name: str = "memory",
        # Optional: the CMS calls ``store(entry)`` and tracks promotion
        # itself, so the band-level promotion thresholds aren't used here.
        update_interval: int = 1,
        promotion_access_count: int = 2,
        promotion_surprise: float = 0.5,
    ):
        device = device if torch.cuda.is_available() else "cpu"
        module = MLP3Module(dim=embedding_dim, hidden_dim=hidden_dim)
        rule = SGDMomentumUpdate(
            params=module.parameters(),
            base_lr=learning_rate,
            momentum=0.9,
            weight_decay=weight_decay,
            max_grad_norm=1.0,
        )
        objective = L2ReconstructionObjective()
        policy = balanced(weight_decay=weight_decay)

        super().__init__(
            name=name,
            embedding_dim=embedding_dim,
            memory_module=module,
            update_rule=rule,
            objective=objective,
            retention=policy,
            max_entries=max_entries,
            update_interval=update_interval,
            promotion_access_count=promotion_access_count,
            promotion_surprise=promotion_surprise,
            device=device,
        )

        # Legacy attributes some callers may read.
        self.hidden_dim = hidden_dim
