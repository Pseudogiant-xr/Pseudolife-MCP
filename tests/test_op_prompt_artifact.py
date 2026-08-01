"""The committed op-prompt artifact must stay byte-identical to the
programmatic construction the definitive C2-op gate ran (op_probe's
v0-appended-block: shipped _SYSTEM_PROMPT with the op block inserted
before the Return-empty line). Drift here would silently change what
`--system-prompt-file evals/prompts/ku_op_prompt_v0.txt` measures."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import op_probe  # noqa: E402


def test_op_prompt_file_matches_probe_construction():
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / "ku_op_prompt_v0.txt"
    assert path.read_text(encoding="utf-8") == op_probe.VARIANTS["v0-appended-block"]


def test_count_exclusion_prompt_file_matches_probe_construction():
    """Same pin for the count-exclusion arm's prompt (v5: v0 block + the
    counts-are-never-members rule with a single-claim worked example)."""
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / "ku_op_prompt_v5.txt"
    assert path.read_text(encoding="utf-8") == op_probe.VARIANTS["v5-count-exclusion-claim-example"]


def test_shipped_prompt_is_the_measured_v5_artifact():
    """The hold-reversal ship (2026-08-01): the live extraction prompt must
    be byte-identical to the artifact the count-exclusion gate measured
    (cascade at the op-less control, sidecar + ladder validated). Any drift
    between what runs and what was measured re-opens the gap all four
    verdict artifacts exist to close."""
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / "ku_op_prompt_v5.txt"
    assert _SYSTEM_PROMPT == path.read_text(encoding="utf-8")
