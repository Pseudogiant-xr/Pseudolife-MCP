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
