"""The hybrid arm's served-context format — one definition, five users.

The two headers that join the cortex fact lines to the raw memory blocks
had been re-typed in five modules: the two producers (longmemeval_bench,
lme_v2_smoke), the two rebuilders that split a served context back apart
(rebuild_contexts, band_ablation) and answerability_probe's pathway
splitter. Nothing tied them together, so an edit to one that missed the
others would silently mis-split every context downstream instead of
failing (2026-09-01 post-merge review of PR #236).

Stdlib-only on purpose: answerability_probe imports this on CPU-only
machines, so it must never reach for the bench modules (and torch) the
producers live in.
"""
from __future__ import annotations

FACTS_HEADER = "Known facts:\n"
MEMS_HEADER = "\n\nRelevant memories:\n"


def hybrid_context(fact_lines, mem_texts) -> str:
    """The hybrid arm's served context: the fact lines one per line, then
    the raw memory blocks separated the way the rag arm separates them.
    Callers apply their own top-k to ``mem_texts`` first."""
    return (FACTS_HEADER + "\n".join(fact_lines)
            + MEMS_HEADER + "\n\n".join(mem_texts))
