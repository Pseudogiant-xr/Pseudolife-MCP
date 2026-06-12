"""Read-time freshness/decay — pure unit tests (no DB, no torch)."""
import time

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
