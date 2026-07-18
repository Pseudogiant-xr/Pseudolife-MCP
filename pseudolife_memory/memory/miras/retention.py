"""Named :class:`RetentionPolicy` factories.

Retention has three coupled responsibilities:

* ``weight_decay`` — vestigial since v0.5 (a band is a plain cosine store
  with no trained weights to decay). Kept as a factory parameter only so
  the named policies retain a uniform signature; see
  :class:`~pseudolife_memory.memory.miras.protocols.RetentionPolicy`.
* ``decay_factor_on_contradiction`` — multiplier applied by
  :func:`src.memory.contradiction.decay_contradicted_entries` to the
  embedding magnitude of an entry once it's been superseded.
* ``eviction_score`` — function used by :meth:`MIRASBand._evict_one` to
  pick the least-valuable entry when the bank is at capacity. Lower
  scores are evicted first.

Named policies ship with sensible defaults; users with a ``preset: custom``
config can roll their own.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pseudolife_memory.memory.miras.protocols import RetentionPolicy

if TYPE_CHECKING:
    from pseudolife_memory.memory.titans_memory import MemoryEntry


# ---------------------------------------------------------------------------
# Eviction scoring functions
# ---------------------------------------------------------------------------
# All scoring functions follow the convention: higher = keep, lower = evict.


def _balanced_score(entry: "MemoryEntry", now: float) -> float:
    """Access-rate plus a small surprise bonus.

    Reproduces the v0.4.x eviction heuristic from
    ``TitansMemoryBank._evict_one`` (titans_memory.py:248) verbatim:

    .. math:: score = \\frac{\\text{access\\_count}}{\\max(\\text{age}, 1)} + 0.1 \\cdot \\text{surprise}

    """
    age = max(now - entry.timestamp, 1.0)
    return entry.access_count / age + entry.surprise_score * 0.1


def _recency_heavy_score(entry: "MemoryEntry", now: float) -> float:
    """Strongly weighted toward recent entries.

    Uses an exponential recency multiplier (half-life one hour) on top
    of the access count. Surprise is ignored — the assumption is the
    band's caller cares about freshness over uniqueness.
    """
    age = max(now - entry.timestamp, 1.0)
    recency = 2.0 ** (-age / 3600.0)
    return entry.access_count * (1.0 + recency)


def _surprise_heavy_score(entry: "MemoryEntry", now: float) -> float:
    """Strongly weighted toward high-surprise entries.

    The opposite preference of ``recency_heavy``: pin novel facts
    even when they go stale and unread. The natural fit for the
    slowest (long-term) band.
    """
    age = max(now - entry.timestamp, 1.0)
    return entry.access_count / age + entry.surprise_score * 1.0


# ---------------------------------------------------------------------------
# Named policy factories
# ---------------------------------------------------------------------------


def balanced(weight_decay: float = 0.001, retention_boost: float = 0.0) -> RetentionPolicy:
    """The default eviction weighting: access-rate plus a surprise bonus.

    ``weight_decay`` is accepted only for signature parity with the other
    named policies — it is vestigial since v0.5 and unused by any code
    path. The behavioural default is
    ``decay_factor_on_contradiction=0.3`` (see ``cms.py:176``).
    """
    return RetentionPolicy(
        weight_decay=weight_decay,
        decay_factor_on_contradiction=0.3,
        eviction_score=_balanced_score,
        name="balanced",
        retention_boost=retention_boost,
    )


def recency_heavy(weight_decay: float = 0.005, retention_boost: float = 0.0) -> RetentionPolicy:
    """Recency-biased eviction + faster contradiction decay.

    ``weight_decay`` is vestigial (see :func:`balanced`) and has no effect;
    the recency bias comes entirely from :func:`_recency_heavy_score`.
    ``decay_factor_on_contradiction`` is smaller (0.2) — superseded facts
    are pushed lower in retrieval scores more aggressively.
    """
    return RetentionPolicy(
        weight_decay=weight_decay,
        decay_factor_on_contradiction=0.2,
        eviction_score=_recency_heavy_score,
        name="recency_heavy",
        retention_boost=retention_boost,
    )


def surprise_heavy(weight_decay: float = 0.0005, retention_boost: float = 0.0) -> RetentionPolicy:
    """Surprise-biased eviction + gentler contradiction decay.

    ``weight_decay`` is vestigial (see :func:`balanced`) and has no effect;
    high-surprise entries survive eviction even when access is low purely
    via :func:`_surprise_heavy_score`. The contradiction-decay factor is
    0.5 — superseded facts are still visible but down-weighted (useful for
    slow bands where the "old" pattern still has informational value).
    """
    return RetentionPolicy(
        weight_decay=weight_decay,
        decay_factor_on_contradiction=0.5,
        eviction_score=_surprise_heavy_score,
        name="surprise_heavy",
        retention_boost=retention_boost,
    )


POLICY_REGISTRY = {
    "balanced": balanced,
    "recency_heavy": recency_heavy,
    "surprise_heavy": surprise_heavy,
}


def build_policy(name: str, weight_decay: float | None = None,
                 retention_boost: float = 0.0) -> RetentionPolicy:
    """Construct a named policy. ``weight_decay`` overrides the default;
    ``retention_boost`` sets the MTT reinforcement-retention term."""
    try:
        factory = POLICY_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown retention_policy {name!r}. Available: {list(POLICY_REGISTRY)}"
        ) from exc
    if weight_decay is None:
        return factory(retention_boost=retention_boost)
    return factory(weight_decay=weight_decay, retention_boost=retention_boost)


def now_seconds() -> float:
    """Indirection so tests can monkeypatch the clock used by eviction."""
    return time.time()
