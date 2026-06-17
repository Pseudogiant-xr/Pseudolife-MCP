"""Abstention signal for retrieval — a pure, torch-free helper.

``low_confidence`` is True when the search has no confident answer, so the
agent can decline instead of fabricating from weak/distractor hits. ``floor``
is the tunable confidence threshold (0.0 = off; only an empty result abstains).
"""
from __future__ import annotations

from collections.abc import Sequence


def low_confidence(scores: Sequence[float], floor: float) -> bool:
    if not scores:
        return True
    if floor <= 0.0:
        return False
    return max(scores) < floor
