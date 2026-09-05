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
])
def test_prompt_file_matches_probe_construction(filename, variant):
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / filename
    assert path.read_text(encoding="utf-8") == op_probe.VARIANTS[variant]


def test_the_prompt_base_is_the_measured_v10_artifact():
    """The v10 stance ship (2026-08-14): the live extraction prompt must
    be byte-identical to the artifact its gates measured (probe + ladder
    + same-window KU-oracle paired control; ship decision recorded in
    the 2026-08-12 stance-span-gate spec's gate-outcomes section). Any
    drift between what runs and what was measured re-opens the gap the
    verdict artifacts exist to close.

    Since 2026-09-05 the v10 text is the BASE of the shipped prompt
    rather than all of it — the assistant-facts blocks are appended after
    their own ladder gate — so the pin moved to `_BASE_SYSTEM_PROMPT`.
    The shipped constant has its own byte-exact pin, against the measured
    artifact `assistant_facts_provenance.txt`, in
    `tests/test_assistant_provenance.py`."""
    from pseudolife_memory.memory.dream import _BASE_SYSTEM_PROMPT
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / "ku_op_prompt_v10_stance_update.txt"
    assert _BASE_SYSTEM_PROMPT == path.read_text(encoding="utf-8")


def test_the_shipped_prompt_still_opens_with_the_v10_base():
    """The append is an append: nothing in the v10 block may be edited or
    reordered on its way into the shipped prompt."""
    from pseudolife_memory.memory.dream import (_BASE_SYSTEM_PROMPT,
                                                _SYSTEM_PROMPT)
    assert _SYSTEM_PROMPT.startswith(_BASE_SYSTEM_PROMPT)
    assert len(_SYSTEM_PROMPT) > len(_BASE_SYSTEM_PROMPT)
