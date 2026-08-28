"""Read-time freshness/decay — unit tests, no DB.

Two layers: the ``freshness`` helpers themselves, then the thin
``CortexRecord`` / ``CortexStore`` delegation over them (schema v23), where
the personal cortex deliberately DIVERGES from the helper's own default.
"""
import time

import pytest
import torch

from pseudolife_memory.memory.cortex import CortexRecord
from pseudolife_memory.memory.freshness import (
    FRESHNESS_CLASSES,
    decay_factor,
    describe_age,
    effective_confidence,
    is_stale,
    normalize_class,
    ttl_seconds,
)

DAY = 86400.0


def test_classes_known_and_default():
    assert FRESHNESS_CLASSES == ("evergreen", "slow", "volatile")
    assert normalize_class("Volatile") == "volatile"
    assert normalize_class(" SLOW ") == "slow"
    assert normalize_class("nonsense") == "volatile"   # conservative default
    assert normalize_class(None) == "volatile"


def test_evergreen_never_decays():
    assert decay_factor("evergreen", age_seconds=400 * DAY) == 1.0
    assert ttl_seconds("evergreen") is None
    assert is_stale("evergreen", retrieved_at=time.time() - 9999 * DAY) is False


def test_volatile_decays_to_floor_at_ttl():
    ttl = ttl_seconds("volatile")
    assert decay_factor("volatile", age_seconds=0.0) == 1.0
    f = decay_factor("volatile", age_seconds=ttl)
    assert 0.35 <= f <= 0.45
    # past TTL holds at the floor, never below
    assert decay_factor("volatile", age_seconds=10 * ttl) == decay_factor("volatile", age_seconds=ttl)


def test_slow_midpoint_decay():
    ttl = ttl_seconds("slow")
    f_half = decay_factor("slow", age_seconds=ttl / 2.0)
    assert 0.72 <= f_half <= 0.78   # linear 1.0 -> 0.5 across TTL, ~0.75 at half


def test_effective_confidence_scales_and_clamps():
    now = time.time()
    fresh = effective_confidence(0.9, retrieved_at=now, freshness_class="volatile", now=now)
    assert abs(fresh - 0.9) < 1e-9
    aged = effective_confidence(0.9, retrieved_at=now - ttl_seconds("volatile"),
                                freshness_class="volatile", now=now)
    assert 0.30 <= aged <= 0.42
    assert 0.0 <= effective_confidence(5.0, now, "evergreen", now=now) <= 1.0


def test_stale_past_two_ttl():
    now = time.time()
    ttl = ttl_seconds("volatile")
    assert is_stale("volatile", retrieved_at=now - 2.1 * ttl, now=now) is True
    assert is_stale("volatile", retrieved_at=now - 1.0 * ttl, now=now) is False


def test_describe_age():
    now = time.time()
    assert describe_age(now - 3 * 3600, now=now).endswith("h")
    assert describe_age(now - 3 * DAY, now=now) == "3d"
    assert describe_age(now - 100 * DAY, now=now).endswith("mo")
    assert describe_age(now - 800 * DAY, now=now).endswith("y")


# ── CortexRecord / CortexStore delegation (schema v23) ────────────────────
# Only the facts the delegation itself owns live here; the decay curves
# above are the same code and are not re-tested through the 3-line hop.

EMB = torch.zeros(1024)   # facts.embedding is vector(1024) — PG enforces it


def test_new_record_defaults_to_evergreen():
    """No behaviour change for a bank that never sets the field."""
    rec = CortexRecord(entity="project", attribute="language", value="Python")
    assert rec.freshness_class == "evergreen"


def test_last_confirmed_is_the_decay_anchor_not_asserted_at():
    """Re-confirming a fact should restore its trust — otherwise a
    long-standing fact that is still true reads as rotten."""
    now = time.time()
    rec = CortexRecord(entity="deploy", attribute="status", value="green",
                       confidence=0.9, asserted_at=now - 100 * DAY,
                       last_confirmed=now, freshness_class="volatile")

    assert rec.effective_confidence(now) == pytest.approx(0.9)
    assert rec.is_stale(now) is False


def test_unknown_class_falls_back_to_evergreen_not_volatile():
    """`freshness.normalize_class` sends unknown values to *volatile* — right
    for world facts, which rot by default. On the personal cortex that would
    invert the whole design: a typo'd class would silently start a durable
    fact decaying. The write path must land on evergreen instead."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot

    assert normalize_class("nonsense") == "volatile"   # the helper

    store = CortexStore()
    store.write_fact(Slot("x", "y", "z"), EMB, freshness_class="nonsense")
    assert store.lookup("x", "y").freshness_class == "evergreen"


def test_a_valid_class_still_reaches_the_record():
    """Guards the obvious over-correction: falling back to evergreen must not
    swallow the classes a writer explicitly asked for."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot

    store = CortexStore()
    store.write_fact(Slot("a", "b", "c"), EMB, freshness_class="volatile")
    store.write_fact(Slot("d", "e", "f"), EMB, freshness_class="SLOW")
    assert store.lookup("a", "b").freshness_class == "volatile"
    assert store.lookup("d", "e").freshness_class == "slow"
