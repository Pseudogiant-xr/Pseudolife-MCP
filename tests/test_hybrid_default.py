"""Pin hybrid retrieval as the shipped default (LME-V2 slice evidence).

The 2026-07-20 LongMemEval-V2 procedure-slice replicates showed the hybrid
context (cortex facts ahead of associative entries) beating BOTH single
channels in every replicate under both answer prompts (agg: hybrid 0.633
[0.60-0.70] vs rag 0.500 / cortex 0.233 compose). These tests pin the config
defaults that make ``memory_search`` hybrid out of the box, so a future
default flip is a deliberate, test-visible decision — not a silent edit.
"""
from __future__ import annotations

from pseudolife_memory.utils.config import AppConfig


def test_cortex_search_first_defaults_on():
    cfg = AppConfig()
    cc = cfg.memory.cortex
    assert cc.enabled is True, (
        "cortex.enabled must default True — hybrid retrieval (facts ahead of "
        "entries) beat both single channels in every LME-V2 slice replicate")
    assert cc.search_first is True, (
        "cortex.search_first must default True — memory_search's hybrid shape "
        "(cortex facts surfaced above associative recall) is the validated "
        "default, not an optional extra")
