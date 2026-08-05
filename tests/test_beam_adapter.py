"""Unit tests for the BEAM adapter's pure parts (no GPU, no BEAM data).

The judge-prompt extraction is tested against a synthetic prompts.py so the
test never depends on the (uncommitted) BEAM checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import beam_adapter  # noqa: E402
from beam_adapter import (  # noqa: E402
    arms_for, load_chat_turns, load_judge_prompt, parse_judge_score,
)


def _mini_beam(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "prompts.py").write_text(
        'other = "x"\n'
        'unified_llm_judge_base_prompt = """judge <question> '
        '<rubric_item> <llm_response>"""\n', encoding="utf-8")
    return tmp_path


def test_load_judge_prompt_extracts_without_import(tmp_path):
    prompt = load_judge_prompt(_mini_beam(tmp_path))
    assert "<rubric_item>" in prompt and "<llm_response>" in prompt


def test_load_judge_prompt_missing_is_loud(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "prompts.py").write_text('other = "x"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_judge_prompt(tmp_path)


def test_parse_judge_score_json_fenced_and_regex():
    assert parse_judge_score('{"score": 1.0, "reason": "ok"}') == 1.0
    assert parse_judge_score('```json\n{"score": 0.5}\n```') == 0.5
    assert parse_judge_score('Sure! {"score": "0.5", "x": 1}') == 0.5
    assert parse_judge_score("no score here") is None


def test_arms_for_chronicle_appends_hybrid_ev():
    from longmemeval_bench import ARMS
    assert arms_for(False) == ARMS
    assert arms_for(True) == (*ARMS, "hybrid_ev")


def test_report_derives_arms_from_rows(tmp_path, monkeypatch, capsys):
    """A chronicle run's summary must carry hybrid_ev; a vanilla run's must
    not — report reads the arms off the rows, not a static tuple."""
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "event_ordering", "index": i,
             "rag_score": 0.5, "rag_score_intfaithful": 0.0,
             "cortex_score": 0.0, "cortex_score_intfaithful": 0.0,
             "hybrid_score": 0.5, "hybrid_score_intfaithful": 0.0,
             "hybrid_ev_score": 1.0, "hybrid_ev_score_intfaithful": 1.0}
            for i in range(2)]
    out = tmp_path / "beam-100K-qwen-27b-t.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "t")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-t.summary.json").read_text(
            encoding="utf-8"))
    assert summary["arms"]["hybrid_ev"]["score"] == 1.0
    assert summary["types"]["event_ordering"]["hybrid_ev"] == 1.0
    assert summary["arms"]["hybrid"]["score"] == 0.5


def test_dream_tally_counts_events():
    class _Svc:
        def __init__(self):
            self.calls = 0
        def dream_run(self, extractor):
            self.calls += 1
            return {"pulled": 3, "claims": 2, "superseded": 0,
                    "literal_dropped": 1, "events_inserted": 2,
                    "events_pass_failed": False}
        def dream_status(self):
            return {"backlog": 0}
    tally = {"turns": 0, "dreams": 0, "claims": 0, "superseded": 0,
             "literal_dropped": 0, "events_inserted": 0,
             "events_pass_failures": 0}
    beam_adapter._dream_until_drained(_Svc(), None, tally)
    assert tally["events_inserted"] == 2
    assert tally["events_pass_failures"] == 0


def test_load_chat_turns_flattens_batches(tmp_path):
    # Real BEAM shape: a "turn" is a LIST of message dicts (an exchange);
    # the bare-dict tolerance is exercised by batch 2.
    import json
    (tmp_path / "chat.json").write_text(json.dumps([
        {"batch_number": 1, "time_anchor": "March-15-2024", "turns": [
            [{"role": "user", "content": "hello", "time_anchor": None},
             {"role": "assistant", "content": "hi there"},
             {"role": "assistant", "content": ""}],        # dropped: empty
        ]},
        {"batch_number": 2, "time_anchor": None, "turns": [
            {"role": "user", "content": "again",
             "time_anchor": "April-01-2024"},
        ]},
    ]), encoding="utf-8")
    turns = load_chat_turns(tmp_path)
    assert [(t["batch"], t["role"]) for t in turns] == [
        (1, "user"), (1, "assistant"), (2, "user")]
    # turn-level anchor wins; batch anchor is the fallback
    assert turns[0]["time_anchor"] == "March-15-2024"
    assert turns[1]["time_anchor"] == "March-15-2024"
    assert turns[2]["time_anchor"] == "April-01-2024"
