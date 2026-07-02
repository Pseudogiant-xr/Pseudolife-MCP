"""Retention policy for a MIRAS band — eviction + contradiction-decay.

v0.5: the ``MemoryModule`` / ``UpdateRule`` / ``RetentionObjective`` ABCs (the
test-time neural-memory axes) were removed along with the MLP. A band is now a
plain cosine store, so the only remaining pluggable axis is the retention policy
below (eviction scoring + contradiction-decay factor). The removed machinery is
archived on the ``archive/neural-memory-titans`` branch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pseudolife_memory.memory.titans_memory import MemoryEntry


def _default_source_weights() -> dict[str, float]:
    """Default per-source multipliers on eviction score.

    Designed for the agentic-deployment taxonomy that ``continuum`` targets:
    long-running agents emit far more bookkeeping memories (tool calls,
    thinking traces) than human-equivalent user statements, so the bank
    should preferentially evict the bookkeeping when full. For chat-only
    apps all entries get ``source="user"`` or ``"assistant"`` (back-compat
    aliases below) which map to neutral or near-neutral weights.
    """
    return {
        # Human input — highest retention. The user *is* ground truth.
        "user_msg": 1.5,
        "user": 1.5,           # v0.5.x back-compat alias
        # First-class observations and actions.
        "tool_result": 1.0,
        "agent_action": 1.0,
        "assistant": 1.0,      # v0.5.x back-compat alias
        # Cheap to lose — the tool call is reconstructible from its result,
        # and thinking is by design an ephemeral scratchpad.
        "tool_call": 0.5,
        "llm_thinking": 0.2,
        # Near-pinned configuration / system prompts.
        "system": 10.0,
    }


@dataclass
class RetentionPolicy:
    """Eviction + contradiction-decay parameters for a band.

    A plain dataclass — the behaviour is fully captured by data + a couple of
    pre-defined scoring callables. Named policies (``balanced`` /
    ``recency_heavy`` / ``surprise_heavy``) are constructed by factory functions
    in :mod:`src.memory.miras.retention`.

    Attributes
    ----------
    weight_decay:
        Vestigial since v0.5 (the band has no trained weights). Retained on the
        dataclass so the named-policy factories keep a uniform signature.
    decay_factor_on_contradiction:
        Embedding-magnitude multiplier applied by
        :func:`src.memory.contradiction.decay_contradicted_entries` when
        a memory entry is marked superseded. Smaller = more aggressive.
    eviction_score:
        Callable ``(entry, now_seconds) -> float`` used by
        :meth:`MIRASBand._evict_one`. Lower scores get evicted first; the
        per-source weighting is applied on top via :meth:`source_weighted_score`.
    name:
        Short stable identifier (surfaced in ``memory_stats``).
    source_weights:
        Per-``entry.source`` multipliers on eviction score. Higher = harder to
        evict. Unknown sources fall back to ``1.0``.
    """

    weight_decay: float
    decay_factor_on_contradiction: float
    eviction_score: Callable[["MemoryEntry", float], float]
    name: str
    source_weights: dict[str, float] = field(default_factory=_default_source_weights)
    # MTT retention (Phase 2). 0.0 = today's eviction exactly (log1p term vanishes).
    retention_boost: float = 0.0

    def source_weighted_score(self, entry: "MemoryEntry", now: float) -> float:
        """``(eviction_score + 1) × source_weights[source] + retention_boost ×
        log1p(reinforcements)``.

        The reinforcement term is added AFTER the source-weight multiply — an
        absolute, source-independent boost so a reinforced episode resists
        eviction regardless of its source tier. ``retention_boost = 0.0``
        (default) makes the term vanish → eviction is byte-identical to before.
        """
        base = self.eviction_score(entry, now)
        weight = self.source_weights.get(entry.source, 1.0)
        score = ((base + 1.0) * weight
                 + self.retention_boost * math.log1p(entry.reinforcements))
        # Superseded history is always cheaper to lose than current state.
        # Without this, a correction (near-zero surprise by construction)
        # scored below the stale fact it replaced and was evicted first,
        # permanently — while the stale fact survived.
        if getattr(entry, "superseded_at", None) is not None:
            score *= 0.05
        return score
