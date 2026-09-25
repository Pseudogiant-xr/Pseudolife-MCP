"""Abstention signal for retrieval — a pure, torch-free helper.

``low_confidence`` is True when the result is empty or, with a positive
``floor``, when its top score is below it. At the shipped floor (0.0) it
therefore means only "nothing matched". It is not an answerability signal:
no floor is calibrated for the current embedder, and in-domain questions
whose answer is absent score like real hits (see
``memory.search_confidence_floor`` in ``utils/config.py``).
"""
from __future__ import annotations

from collections.abc import Sequence


def low_confidence(scores: Sequence[float], floor: float) -> bool:
    if not scores:
        return True
    if floor <= 0.0:
        return False
    return max(scores) < floor
