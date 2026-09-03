"""The within-run BEAM pairing artifact regenerates byte-exactly from its rows.

``evals/beam_within_run_pairs.py`` produces the paired column the
comparator-arms table in ``evals/README.md`` publishes (arm minus ``rag``,
95% CI, sign-flip permutation p, served characters). The artifact is a
claim about the committed ``.jsonl``; this test re-derives it from those
rows with the same seed so the two cannot drift apart, and pins the
five-arm shape the docs describe.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

from beam_within_run_pairs import CONTROL, pair_run  # noqa: E402

RESULTS = REPO / "evals" / "results"
ROWS = RESULTS / "beam-100K-qwen-27b-chip12-b16.jsonl"
ARTIFACT = RESULTS / "beam-100K-qwen-27b-chip12-b16.arms-vs-rag.json"
ARMS = ["refind", "hybrid", "cortex", "nomem"]


def _rows() -> list[dict]:
    return [json.loads(line) for line in ROWS.read_text(encoding="utf-8")
            .splitlines() if line.strip()]


@pytest.mark.skipif(not ROWS.exists(), reason="chip12-b16 rows not checked out")
def test_artifact_regenerates_byte_exactly():
    committed = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    regenerated = pair_run(_rows(), ARMS, perms=committed["perms"],
                           seed=committed["seed"])
    regenerated["source"] = ROWS.name
    assert (json.dumps(regenerated, indent=2, sort_keys=True) + "\n"
            == ARTIFACT.read_text(encoding="utf-8"))


def test_artifact_shape_matches_docs():
    d = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert d["control"] == CONTROL == "rag"
    assert d["n_rows"] == 400
    assert set(d["arms"]) == set(ARMS)
    for arm in ARMS:
        a = d["arms"][arm]
        assert a["n"] == 400
        assert a["wins"] + a["losses"] + a["ties"] == 400
        assert set(a["types"]) == set(d["control_types"])
    # The no-memory floor is served nothing, by construction.
    assert d["arms"]["nomem"]["context_chars_mean"] == 0
    assert d["arms"]["nomem"]["types"]["abstention"] == 1.0


def test_pair_run_on_a_tiny_run_is_exact():
    rows = [
        {"type": "t", "rag_score": 1.0, "x_score": 0.0,
         "contexts": {"rag": "abcd", "x": ""}},
        {"type": "t", "rag_score": 0.5, "x_score": 0.5,
         "contexts": {"rag": "ab", "x": "abcdef"}},
        {"type": "u", "rag_score": 0.0, "x_score": 1.0,
         "contexts": {"rag": "", "x": "abc"}},
    ]
    d = pair_run(rows, ["x"], perms=200, seed=0)
    x = d["arms"]["x"]
    assert x["n"] == 3
    assert x["delta_vs_control"] == 0.0
    assert (x["wins"], x["losses"], x["ties"]) == (1, 1, 1)
    assert x["full_marks_rows"] == 1
    assert x["types"] == {"t": 0.25, "u": 1.0}
    assert x["context_chars_mean"] == 3
    assert d["control_context_chars_mean"] == 2
    assert x["perm_p"] == 1.0
