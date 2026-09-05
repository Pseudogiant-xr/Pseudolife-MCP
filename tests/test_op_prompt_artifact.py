"""The committed op-prompt artifact must stay byte-identical to the
programmatic construction the definitive C2-op gate ran (op_probe's
v0-appended-block: shipped _SYSTEM_PROMPT with the op block inserted
before the Return-empty line). Drift here would silently change what
`--system-prompt-file evals/prompts/ku_op_prompt_v0.txt` measures."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import op_probe  # noqa: E402


@pytest.mark.parametrize("filename,variant", [
    # v0: shipped _SYSTEM_PROMPT with the op block before the Return-empty line
    ("ku_op_prompt_v0.txt", "v0-appended-block"),
    # v5: v0 block + counts-are-never-members, with a single-claim example
    ("ku_op_prompt_v5.txt", "v5-count-exclusion-claim-example"),
    # v6: v5 + keep-literals-verbatim, with a single-claim example
    ("ku_op_prompt_v6.txt", "v6-literal-fidelity"),
    # v7: v5 + events extraction with a single-event example — Phase 2 of the
    # 2026-08-03 aggregation-aware-recall design
    ("ku_op_prompt_v7_events.txt", "v7-chronicle-events"),
    # v8: v5 + hedges-go-in-a-stance-field — Feature A of the 2026-08-12
    # stance+span-gate design
    ("ku_op_prompt_v8_stance.txt", "v8-stance"),
    # v9: v8 + cite-a-quote — Feature B of the same design; the v9-vs-v8
    # ladder gate isolates the quote field's claims tax
    ("ku_op_prompt_v9_stance_quote.txt", "v9-stance-quote"),
    # v10: v5 + the update-anchored stance rule — the sgku bank-diff
    # forensics traced v8's KU failure to a diluted consolidation anchor
    ("ku_op_prompt_v10_stance_update.txt", "v10-stance-update"),
    # v11: v10 with its two corpus-lifted worked examples re-cut on invented
    # tokens (2026-09-06); rules byte-identical, examples clean of LongMemEval
    ("ku_op_prompt_v11_example_recut.txt", "v11-example-recut"),
])
def test_prompt_file_matches_probe_construction(filename, variant):
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / filename
    assert path.read_text(encoding="utf-8") == op_probe.VARIANTS[variant]


def test_shipped_prompt_is_the_measured_v10_artifact():
    """The v10 stance ship (2026-08-14): the live extraction prompt must
    be byte-identical to the artifact its gates measured (probe + ladder
    + same-window KU-oracle paired control; ship decision recorded in
    the 2026-08-12 stance-span-gate spec's gate-outcomes section). Any
    drift between what runs and what was measured re-opens the gap the
    verdict artifacts exist to close."""
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / "ku_op_prompt_v10_stance_update.txt"
    assert _SYSTEM_PROMPT == path.read_text(encoding="utf-8")


def test_only_the_required_op_variant_scores_plain_facts_as_op_set():
    """The probe's decoy scorer treats "op":"set" as the only acceptable
    plain-fact shape for the v1 variant, whose schema REQUIRES op on every
    claim. Every other variant leaves plain facts op-less, so their decoys
    must be scored against {None, "set"} — a name-prefix match that also
    caught v10 and v11 read their count decoys as 0/7 for a month
    (op-probe-qwen38-0817.json) while the claims themselves were right."""
    assert op_probe.requires_op("v1-required-op")
    for name in op_probe.VARIANTS:
        if name != "v1-required-op":
            assert not op_probe.requires_op(name), name
