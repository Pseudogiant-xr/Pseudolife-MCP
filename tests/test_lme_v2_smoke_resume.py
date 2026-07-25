"""Tests for evals/lme_v2_smoke.py — the reanswer path's resume cursor.

`run_smoke` has always resumed from its JSONL (append + skip-done), so a
crashed model server costs one question. `reanswer` did not: it opened the
output with "w" and re-read every row from source, so each retry restarted
from row 1. On 2026-07-25 six server crashes discarded ~29 minutes each and
turned ~15 minutes of compute into 3.6 hours — only the one crash-free
attempt ever produced a file.

These pin the cursor. Pure-function: the endpoint probe and the
answer/judge call are both stubbed, so no server, no GPU, no Postgres.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import ladder_sweep  # noqa: E402
import lme_v2_smoke as smoke  # noqa: E402


def _row(qid: str) -> dict:
    return {"question_id": qid, "question": f"q-{qid}",
            "contexts": {"rag": "r", "cortex": "c", "hybrid": "h"}}


def _setup(tmp_path, monkeypatch, src_ids, done_ids=()):
    """Seed a source run and (optionally) a partial output, and stub out
    everything that would need a live answerer. Returns the call log."""
    monkeypatch.setattr(smoke, "RESULTS_DIR", tmp_path)
    out = tmp_path / "lme-v2-smoke-tgt.jsonl"
    monkeypatch.setattr(smoke, "OUT_FILE", out)
    monkeypatch.setattr(ladder_sweep, "probe", lambda url: True)

    (tmp_path / "lme-v2-smoke-src.jsonl").write_text(
        "".join(json.dumps(_row(i)) + "\n" for i in src_ids), encoding="utf-8")
    if done_ids:
        done_rows = []
        for i in done_ids:
            r = _row(i)
            r["reanswered_from"] = "src"
            r["marker"] = "pre-existing"
            for a in smoke.ARMS:
                r[f"{a}_correct"] = 1
            done_rows.append(json.dumps(r))
        out.write_text("\n".join(done_rows) + "\n", encoding="utf-8")

    calls: list[str] = []

    def _stub(row, answer_system=None):
        calls.append(row["question_id"])
        for a in smoke.ARMS:
            row[f"{a}_correct"] = 1
        return row

    monkeypatch.setattr(smoke, "answer_judge_score", _stub)
    return calls, out


def test_reanswer_skips_rows_already_in_its_output(tmp_path, monkeypatch,
                                                   capsys):
    """The cursor: a retry answers only what the crash left undone."""
    calls, out = _setup(tmp_path, monkeypatch, ["a", "b", "c"],
                        done_ids=["a"])
    smoke.reanswer("src", answer_system=None)
    capsys.readouterr()
    assert calls == ["b", "c"]
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    assert [r["question_id"] for r in rows] == ["a", "b", "c"]


def test_reanswer_preserves_the_work_it_resumed_onto(tmp_path, monkeypatch,
                                                     capsys):
    """Appending, not truncating — the already-answered row survives intact
    rather than being silently rewritten."""
    _, out = _setup(tmp_path, monkeypatch, ["a", "b"], done_ids=["a"])
    smoke.reanswer("src", answer_system=None)
    capsys.readouterr()
    first = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert first["marker"] == "pre-existing"


def test_reanswer_is_idempotent_once_complete(tmp_path, monkeypatch, capsys):
    """Re-running a finished pass must not re-burn GPU time on every row."""
    calls, out = _setup(tmp_path, monkeypatch, ["a", "b"], done_ids=["a", "b"])
    smoke.reanswer("src", answer_system=None)
    capsys.readouterr()
    assert calls == []


def test_reanswer_refuses_to_resume_across_a_prompt_change(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """Resuming made mixed-prompt files possible where truncation had made
    them impossible: same --out-tag, different --answer-prompt, and half
    the rows would answer under each. Refuse instead."""
    calls, out = _setup(tmp_path, monkeypatch, ["a", "b"], done_ids=["a"])
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    for r in rows:
        r["answer_prompt"] = "ku"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows),
                   encoding="utf-8")
    import pytest
    with pytest.raises(SystemExit, match="answer_prompt"):
        smoke.reanswer("src", answer_system="COMPOSE PROMPT")
    assert calls == []


def test_reanswer_records_the_prompt_variant_it_used(tmp_path, monkeypatch,
                                                     capsys):
    _, out = _setup(tmp_path, monkeypatch, ["a"])
    smoke.reanswer("src", answer_system=None)
    capsys.readouterr()
    assert json.loads(
        out.read_text(encoding="utf-8").splitlines()[0])["answer_prompt"] == "ku"


def test_reanswer_answers_everything_on_a_cold_start(tmp_path, monkeypatch,
                                                     capsys):
    calls, out = _setup(tmp_path, monkeypatch, ["a", "b", "c"])
    smoke.reanswer("src", answer_system=None)
    capsys.readouterr()
    assert calls == ["a", "b", "c"]
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 3
