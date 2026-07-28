"""Contradiction detection: cost of the state-transition path, and the
behaviour that must survive making it cheaper.

`detect_contradictions` runs on every band on every write. Profiling a
store into a saturated 5,250-entry bank (2026-07-25) put **94%** of the
time here — 1.58M regex searches across five stores — against ~4% for the
capacity-eviction path that was assumed to be the problem.

Path 3 (the state-transition gain/loss detector) is the reason: unlike
paths 1 and 2 it does its text work before consulting similarity, so
every non-superseded entry costs four regex-list scans. The same strings
recur relentlessly — the new text is fixed across the whole loop, and the
bank's texts are fixed across stores — so the predicates are memoised.
Median store at 5,250 resident, real MiniLM embeddings and real
conversation text: **2,510 ms → 7.8 ms**.

There were no tests over `detect_contradictions` before this file; the
behavioural cases below are characterisation as much as regression guard.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from pseudolife_memory.memory import contradiction as C
from pseudolife_memory.memory.titans_memory import MemoryEntry

GAIN = "I have a Ragdoll cat named Jacque"
LOSS = "I gave away Jacque last week"


def _pair(cos: float) -> tuple[torch.Tensor, torch.Tensor]:
    """Two unit vectors with an exact cosine of ``cos``."""
    a = np.zeros(1024, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(1024, dtype=np.float32)
    b[0] = cos
    b[1] = float(np.sqrt(max(0.0, 1.0 - cos * cos)))
    return torch.from_numpy(a), torch.from_numpy(b)


def _entry(text: str, emb: torch.Tensor) -> MemoryEntry:
    return MemoryEntry(text=text, embedding=emb, surprise_score=0.5,
                       source="t", bank="b")


def _count_scans(monkeypatch) -> list[int]:
    calls = [0]
    real = C._matches_any

    def counting(patterns, text):
        calls[0] += 1
        return real(patterns, text)

    monkeypatch.setattr(C, "_matches_any", counting)
    return calls


def test_entry_cues_are_computed_once_per_entry(monkeypatch):
    """The shipped optimisation. The cue check is path 3's early exit, so
    for the overwhelming majority of entries it *is* the cost."""
    q, other = _pair(0.5)
    entries = [_entry(f"note number {i}", other) for i in range(40)]
    calls = _count_scans(monkeypatch)

    C.detect_contradictions(GAIN, q, entries)
    first = calls[0]
    C.detect_contradictions(GAIN, q, entries)
    second = calls[0] - first

    assert first >= 40, "expected a scan per entry on the cold pass"
    assert second <= 2, f"rescanned entry text on the warm pass ({second})"


def test_cue_cost_does_not_depend_on_bank_size(monkeypatch):
    """No cliff.

    A process-wide LRU would be 100% hit while resident <= maxsize and 0%
    the moment it isn't — path 3 sweeps every entry of every band on every
    write, and a cyclic scan is the worst case for LRU. That is reachable:
    ``rebalance_bands`` deliberately leaves the deepest band over capacity
    rather than truncating a bank at startup, and the shipped preset
    invites raising ``max_entries``. Caching on the entry makes the warm
    cost independent of how many entries there are.
    """
    q, other = _pair(0.5)
    warm_costs = []
    for n in (200, 12_000):
        entries = [_entry(f"note number {i}", other) for i in range(n)]
        calls = _count_scans(monkeypatch)
        C.detect_contradictions(GAIN, q, entries)  # cold
        before = calls[0]
        C.detect_contradictions(GAIN, q, entries)  # warm
        warm_costs.append(calls[0] - before)

    assert warm_costs[1] <= 2, (
        f"warm cost grew with bank size: {warm_costs[0]} at 200 entries, "
        f"{warm_costs[1]} at 12,000")


def test_cached_cues_match_a_freshly_computed_entry():
    """A stale or wrong cache silently changes which memories get
    superseded, so pin that the cached path agrees with a cold one."""
    q, near = _pair(C.STATE_TRANSITION_SIM_THRESHOLD_SLOT + 0.05)
    warmed = _entry(LOSS, near)
    C.detect_contradictions(GAIN, q, [warmed])          # populates the cache

    fresh = _entry(LOSS, near)
    assert (C.detect_contradictions(GAIN, q, [warmed])
            == [warmed])
    assert (C.detect_contradictions(GAIN, q, [fresh])
            == [fresh])


def test_a_slot_anchored_state_transition_is_flagged():
    """A shared named entity clears the low `slot` floor even though the
    two texts are not very similar."""
    q, near = _pair(C.STATE_TRANSITION_SIM_THRESHOLD_SLOT + 0.05)
    entry = _entry(LOSS, near)

    assert C.detect_contradictions(GAIN, q, [entry]) == [entry]


def test_a_state_transition_below_every_floor_is_rejected():
    q, far = _pair(C.STATE_TRANSITION_SIM_THRESHOLD_SLOT - 0.05)
    entry = _entry(LOSS, far)

    assert C.detect_contradictions(GAIN, q, [entry]) == []


def test_a_superseded_entry_is_never_re_contradicted():
    q, near = _pair(C.STATE_TRANSITION_SIM_THRESHOLD_SLOT + 0.05)
    entry = _entry(LOSS, near)
    entry.superseded_at = 1.0

    assert C.detect_contradictions(GAIN, q, [entry]) == []


@pytest.mark.parametrize("cos", [0.02, 0.14, 0.16, 0.5, 0.85])
def test_detection_matches_the_paths_it_documents(cos):
    """Characterisation across the interesting similarity range: the three
    heuristic paths, evaluated independently, agree with the function."""
    q, other = _pair(cos)
    entries = [_entry(LOSS, other), _entry("I still have Jacque", other),
               _entry("completely unrelated content here", other)]

    got = C.detect_contradictions(GAIN, q, entries)

    expected = []
    sims = [float(torch.dot(q / q.norm(), e.embedding / e.embedding.norm()))
            for e in entries]
    for entry, sim in zip(entries, sims):
        if sim >= C.NEGATION_SIM_THRESHOLD and C._negation_asymmetry(GAIN, entry.text):
            expected.append(entry)
            continue
        if sim >= C.REPLACEMENT_SIM_THRESHOLD and C._looks_like_replacement(GAIN, entry.text):
            expected.append(entry)
            continue
        kind = C._state_transition_anchor_kind(GAIN, entry.text)
        if kind is not None and sim >= C._STATE_TRANSITION_FLOOR_BY_KIND[kind]:
            expected.append(entry)
    assert got == expected
