"""HybridLogicalClock (lite) — monotonic, immune to wall-clock steps."""

from pseudolife_memory.memory.hlc import HybridLogicalClock


def test_monotonic_within_same_ms():
    c = HybridLogicalClock(now_ms=lambda: 1000)
    assert c.tick() == (1000, 0)
    assert c.tick() == (1000, 1)          # logical bumps on same-ms ties


def test_advances_with_wall():
    seq = iter([1000, 1001])
    c = HybridLogicalClock(now_ms=lambda: next(seq))
    assert c.tick() == (1000, 0)
    assert c.tick() == (1001, 0)


def test_never_goes_backwards():
    seq = iter([1000, 990])               # wall steps BACK
    c = HybridLogicalClock(now_ms=lambda: next(seq))
    assert c.tick() == (1000, 0)
    assert c.tick() == (1000, 1)          # stays at 1000, bumps logical


def test_observe_advances_past_remote():
    c = HybridLogicalClock(now_ms=lambda: 1000)
    c.tick()                               # (1000, 0)
    c.observe(5000, 7)                     # a remote stamp from the future
    assert c.tick() == (5000, 8)           # local clock jumps past it
