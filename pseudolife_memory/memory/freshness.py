"""Read-time freshness / decay for world-knowledge facts.

Pure stdlib — no DB, no torch, no package siblings — so it unit-tests standalone
and can be loaded by file path from the gateway venv if ever needed.

The world cortex stores a *sourced* confidence (trust at retrieval time). Currency
is enforced at READ time, not by background jobs: a fact's **effective** confidence
is its stored confidence scaled by a decay factor that depends on how long ago it
was retrieved and how fast its class of fact rots. Evergreen facts never decay;
volatile facts (latest version, price, current role-holder) fall to a floor across
a short TTL and are flagged "stale, re-verify" past 2×TTL.

This module is deliberately the single source of truth for the decay curve so the
daemon (ranking/return) and the provider (rendering) agree.
"""
from __future__ import annotations

import time as _time

DAY = 86400.0

# Strongest currency guarantee → weakest. Unknown inputs normalise to ``volatile``
# so an unclassified fact is under-trusted rather than over-trusted.
FRESHNESS_CLASSES = ("evergreen", "slow", "volatile")

# Time-to-live per class (seconds). ``None`` = never expires (evergreen).
_TTL = {"evergreen": None, "slow": 270 * DAY, "volatile": 21 * DAY}  # ~9 months / ~3 weeks

# Decay floor reached at 1×TTL — confidence never falls below this multiplier, so a
# stale-but-sourced fact stays a weak lead rather than vanishing.
_FLOOR = {"evergreen": 1.0, "slow": 0.5, "volatile": 0.4}


def normalize_class(c: str | None) -> str:
    c = (c or "").strip().casefold()
    return c if c in FRESHNESS_CLASSES else "volatile"


def ttl_seconds(c: str | None):
    """TTL for a class, or ``None`` for evergreen."""
    return _TTL[normalize_class(c)]


def decay_factor(c: str | None, age_seconds: float) -> float:
    """Multiplier in [floor, 1.0]. Linear from 1.0 at age 0 to the class floor at
    1×TTL, then held at the floor. Evergreen is always 1.0."""
    c = normalize_class(c)
    ttl = _TTL[c]
    if ttl is None:
        return 1.0
    age = max(0.0, float(age_seconds))
    floor = _FLOOR[c]
    if age >= ttl:
        return floor
    return 1.0 - (1.0 - floor) * (age / ttl)


def effective_confidence(stored, retrieved_at, freshness_class, now=None) -> float:
    """Stored confidence scaled by age-based decay, clamped to [0, 1]."""
    now = _time.time() if now is None else float(now)
    age = now - float(retrieved_at if retrieved_at is not None else now)
    return max(0.0, min(1.0, float(stored) * decay_factor(freshness_class, age)))


def is_stale(freshness_class, retrieved_at, now=None) -> bool:
    """True once a fact is past 2×TTL — 'a lead, not truth; re-verify'. Evergreen
    facts are never stale."""
    ttl = ttl_seconds(freshness_class)
    if ttl is None:
        return False
    now = _time.time() if now is None else float(now)
    return (now - float(retrieved_at if retrieved_at is not None else now)) > 2.0 * ttl


def describe_age(retrieved_at, now=None) -> str:
    """Compact human age for the injected block, e.g. '3d' / '5mo' / '2y'."""
    now = _time.time() if now is None else float(now)
    age = max(0.0, now - float(retrieved_at if retrieved_at is not None else now))
    if age < 1.5 * DAY:
        return "%dh" % int(age // 3600)
    if age < 60 * DAY:
        return "%dd" % int(age // DAY)
    if age < 730 * DAY:
        return "%dmo" % int(age // (30 * DAY))
    return "%dy" % int(age // (365 * DAY))
