"""Tests for evals/lesson_synthesis_bench.py — the procedural-path bench.

Pure-function tests only: no endpoints, no GPU. The bench is stdlib-only
by design, so it imports freely here.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import lesson_synthesis_bench as bench  # noqa: E402


def test_infer_rung_fixture_shape():
    """Pins the denominator every published --infer score divides by.

    Scores are published as bare fractions (0.875), so the fixture count is
    what makes them readable at all — 7/8, not "0.875 of something".
    """
    assert len(bench.INFER_FIXTURES) == 8
    abstain = [f for f in bench.INFER_FIXTURES if f["expect"] == "abstain"]
    assert len(abstain) == 2


def test_write_result_persists_json(tmp_path):
    """A bench number that only reaches stdout was never really measured.

    Both rungs printed and forgot until 2026-07-21, so the E4B 0.562 -> 0.875
    result had no artifact behind it anywhere in the repo.
    """
    out = tmp_path / "nested" / "infer.json"
    returned = bench.write_result({"rung": "infer", "score": 0.875}, out)
    assert returned == out
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "rung": "infer", "score": 0.875}


def test_cli_accepts_out_for_both_rungs():
    parser = bench.build_parser()
    assert parser.parse_args(["--infer", "--out", "x.json"]).out == Path(
        "x.json")
    assert parser.parse_args(["--target", "gemma", "--out", "y.json"]).out == (
        Path("y.json"))
    assert parser.parse_args(["--infer"]).out is None


def test_infer_dry_run_writes_manifest_when_out_given(tmp_path, capsys):
    """--dry-run needs no endpoint, so it is the one end-to-end path a test
    can exercise; it proves main() actually wires --out through."""
    out = tmp_path / "manifest.json"
    bench.main(["--infer", "--dry-run", "--out", str(out)])
    capsys.readouterr()
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["rung"] == "infer"
    assert saved["n_fixtures"] == 8
    assert [f["name"] for f in bench.INFER_FIXTURES] == saved["fixtures"]
