"""Slot conflicts are detected independently of embedding similarity.

A matching slot key with a different value or polarity admits a potential
update through the CMS surprise gate. That conflict does not authorize
retiring the entire source note, which may contain other useful claims.
Canonical slot supersession remains the cortex's responsibility.

The other detector paths still handle entries without extracted slots.
"""

from __future__ import annotations

import numpy as np
import torch

from pseudolife_memory.memory import contradiction as C
from pseudolife_memory.memory.titans_memory import MemoryEntry


def _pair(cos: float) -> tuple[torch.Tensor, torch.Tensor]:
    a = np.zeros(1024, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(1024, dtype=np.float32)
    b[0] = cos
    b[1] = float(np.sqrt(max(0.0, 1.0 - cos * cos)))
    return torch.from_numpy(a), torch.from_numpy(b)


def _entry(text: str, emb: torch.Tensor, slots) -> MemoryEntry:
    return MemoryEntry(text=text, embedding=emb, surprise_score=0.5,
                       source="t", bank="b", slots=list(slots))


# Deliberately low cosine — the whole point is that slot identity decides,
# so this fires where every existing heuristic path is out of reach.
FAR = 0.05


def test_same_slot_different_value_is_a_contradiction():
    q, other = _pair(FAR)
    old = _entry("Jacque is a cat", other, [("Jacque", "type", "cat", "+")])

    out = C.detect_contradictions(
        "Jacque is a dog", q, [old],
        new_slots=[("Jacque", "type", "dog", "+")])

    assert out == [old]


def test_same_slot_same_value_is_not_a_contradiction():
    """A restatement is a duplicate, not a correction — and restatements are
    exactly what knowledge-update evidence looks like."""
    q, other = _pair(FAR)
    old = _entry("Jacque is a cat", other, [("Jacque", "type", "cat", "+")])

    out = C.detect_contradictions(
        "Jacque, who is a cat, sleeps a lot", q, [old],
        new_slots=[("Jacque", "type", "cat", "+")])

    assert out == []


def test_a_different_entity_is_not_a_contradiction():
    q, other = _pair(FAR)
    old = _entry("Jacque is a cat", other, [("Jacque", "type", "cat", "+")])

    out = C.detect_contradictions(
        "Rex is a dog", q, [old],
        new_slots=[("Rex", "type", "dog", "+")])

    assert out == []


def test_a_different_attribute_on_the_same_entity_is_not_a_contradiction():
    q, other = _pair(FAR)
    old = _entry("Jacque is a cat", other, [("Jacque", "type", "cat", "+")])

    out = C.detect_contradictions(
        "Jacque is a Ragdoll", q, [old],
        new_slots=[("Jacque", "breed", "Ragdoll", "+")])

    assert out == []


def test_slot_keys_match_case_and_separator_insensitively():
    """Same normalisation the cortex uses, so the two stores agree on what
    counts as the same slot."""
    q, other = _pair(FAR)
    old = _entry("x", other, [("My Dog", "Favourite Toy", "ball", "+")])

    out = C.detect_contradictions(
        "y", q, [old], new_slots=[("my  dog", "favourite-toy", "rope", "+")])

    assert out == [old]


def test_flipping_polarity_on_the_same_value_is_a_contradiction():
    """"I have a cat" -> "I no longer have a cat" keeps the value and flips
    the sign; keying on value alone would miss it."""
    q, other = _pair(FAR)
    old = _entry("I have a cat", other, [("me", "has", "cat", "+")])

    out = C.detect_contradictions(
        "I no longer have a cat", q, [old],
        new_slots=[("me", "has", "cat", "-")])

    assert out == [old]


def test_a_superseded_entry_is_not_re_flagged_by_the_slot_path():
    q, other = _pair(FAR)
    old = _entry("Jacque is a cat", other, [("Jacque", "type", "cat", "+")])
    old.superseded_at = 1.0

    out = C.detect_contradictions(
        "Jacque is a dog", q, [old],
        new_slots=[("Jacque", "type", "dog", "+")])

    assert out == []


def test_entries_without_slots_fall_through_to_the_heuristics():
    """The slot path is additive — it must not suppress the existing paths
    for the majority of entries, which carry no slots at all."""
    q, near = _pair(C.STATE_TRANSITION_SIM_THRESHOLD_SLOT + 0.05)
    old = _entry("I gave away Jacque last week", near, [])

    out = C.detect_contradictions(
        "I have a Ragdoll cat named Jacque", q, [old], new_slots=[])

    assert out == [old]


def test_a_store_admits_the_new_slot_value_and_preserves_prior_evidence():
    """End-to-end through ``cms.store``: the slots come from the new text's
    own extraction, so a correction lands without the caller doing anything.

    Uses near-orthogonal embeddings deliberately — none of the cosine-gated
    paths can reach this pair, so a pass proves the slot path is wired.
    """
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.utils.config import MemoryConfig

    cfg = MemoryConfig()
    cfg.surprise_threshold = 0.99  # above this pair's synthetic surprise (0.95)
    cms = ContinuumMemorySystem(cfg)
    a, b = _pair(FAR)
    old_text = "I have a Ragdoll cat named Jacque"
    new_text = "I have a Siamese cat named Jacque"   # same slots, new breed
    assert cms.store(old_text, a, source="user")[0]
    stored, surprise = cms.store(new_text, b, source="user")
    assert stored and surprise < cfg.surprise_threshold

    by_text = {e.text: e for band in cms.bands for e in band.entries}
    old = by_text[old_text]
    assert old.superseded_at is None
    assert old.superseded_by_text is None
    assert old.surprise_score == 1.0
    assert new_text in by_text


def test_omitting_new_slots_preserves_the_previous_behaviour():
    """Callers that don't pass slots get exactly what they got before."""
    q, other = _pair(FAR)
    old = _entry("Jacque is a cat", other, [("Jacque", "type", "cat", "+")])

    assert C.detect_contradictions("Jacque is a dog", q, [old]) == []
