"""Tests for evals/ladder_sweep.py — canonical-result overwrite guard.

A rerun once silently rewrote ``evals/results/sonnet-5.json`` in place
(2026-07-21), erasing the canonical run's timing fields. The rule (CLAUDE.md,
"Publishing a benchmark number") is: never overwrite a canonical result file
on a rerun — tag the run and promote deliberately. These tests pin the code
that enforces it.

Pure-function + CLI-wiring tests only: the run_* functions are monkeypatched,
so no endpoints, no Postgres, no GPU.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import ladder_sweep as ladder  # noqa: E402


class TestResolveOutPath:
    def test_tag_names_a_sibling_and_never_touches_canonical(self, tmp_path):
        base = tmp_path / "sonnet-5.json"
        base.write_text("{}", encoding="utf-8")
        assert ladder.resolve_out_path(base, "retrv1") == (
            tmp_path / "sonnet-5-retrv1.json")

    def test_untagged_first_run_uses_the_canonical_path(self, tmp_path):
        base = tmp_path / "e2b.json"
        assert ladder.resolve_out_path(base, None) == base

    def test_untagged_rerun_refuses_to_clobber(self, tmp_path):
        base = tmp_path / "sonnet-5.json"
        base.write_text("{}", encoding="utf-8")
        with pytest.raises(SystemExit, match="--out-tag"):
            ladder.resolve_out_path(base, None)


def test_cli_accepts_out_tag():
    parser = ladder.build_parser()
    assert parser.parse_args(["--rung", "naive-rag",
                              "--out-tag", "r2"]).out_tag == "r2"
    assert parser.parse_args(["--rung", "naive-rag"]).out_tag is None


@pytest.mark.parametrize("flag,canonical", [
    ("--rung", "naive-rag.json"),
    ("--abstain", "abstain.json"),
    ("--supersede", "supersede.json"),
])
def test_guard_fires_before_the_run_starts(tmp_path, monkeypatch,
                                           flag, canonical):
    """Refusing AFTER an overnight run would discard it — the guard must
    resolve the output path before any run function is entered."""
    (tmp_path / canonical).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ladder, "RESULTS_DIR", tmp_path)

    def _must_not_run(*a, **k):
        raise AssertionError("run started despite existing canonical file")

    monkeypatch.setattr(ladder, "run_rung", _must_not_run)
    monkeypatch.setattr(ladder, "run_abstain", _must_not_run)
    monkeypatch.setattr(ladder, "run_supersede", _must_not_run)
    with pytest.raises(SystemExit, match="--out-tag"):
        ladder.main([flag, "naive-rag"])


def test_tagged_rerun_writes_the_tagged_file_only(tmp_path, monkeypatch,
                                                  capsys):
    (tmp_path / "naive-rag.json").write_text('{"canonical": true}',
                                             encoding="utf-8")
    monkeypatch.setattr(ladder, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ladder, "run_rung", lambda rung: {"ok": 1})
    assert ladder.main(["--rung", "naive-rag", "--out-tag", "r2"]) == 0
    capsys.readouterr()
    assert json.loads((tmp_path / "naive-rag-r2.json").read_text(
        encoding="utf-8")) == {"ok": 1}
    assert json.loads((tmp_path / "naive-rag.json").read_text(
        encoding="utf-8")) == {"canonical": True}
