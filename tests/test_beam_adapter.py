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
    arms_for, judge_response, load_chat_turns, load_judge_prompt,
    parse_judge_score,
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


def test_arms_for_only_filters_in_canonical_order():
    assert arms_for(False, only="hybrid,rag") == ("rag", "hybrid")
    assert arms_for(True, only="hybrid_ev") == ("hybrid_ev",)


def test_arms_for_unknown_arm_is_loud():
    with pytest.raises(SystemExit):
        arms_for(False, only="rag,hybrid_ev")   # ev needs --chronicle
    with pytest.raises(SystemExit):
        arms_for(False, only="ragg")


def test_judge_response_uses_injected_chat():
    """The rejudge script swaps the local-server judge for a frontier CLI
    judge by injecting ``chat``; the scoring/failure semantics must not
    change with the transport."""
    calls = []

    def fake_chat(system, user, *, max_tokens=256, **_):
        calls.append(user)
        return '{"score": 0.5}' if len(calls) == 1 else "not json"

    v = judge_response("judge <question> <rubric_item> <llm_response>",
                       "q?", ["item one", "item two"], "an answer",
                       chat=fake_chat)
    assert v["llm_judge_score"] == 0.5           # mean over scored items only
    assert v["llm_judge_score_intfaithful"] == 0.0
    assert v["judge_failures"] == 1
    assert "item one" in calls[0] and "an answer" in calls[0]


def test_report_carries_hybrid_top_k_when_rows_do(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "abstention", "index": i,
             "hybrid_top_k": 6,
             "hybrid_score": 1.0, "hybrid_score_intfaithful": 1.0}
            for i in range(2)]
    out = tmp_path / "beam-100K-qwen-27b-hyb6.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "hyb6")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-hyb6.summary.json").read_text(
            encoding="utf-8"))
    assert summary["hybrid_top_k"] == 6


def test_report_omits_hybrid_top_k_for_legacy_rows(tmp_path, monkeypatch):
    """Pre-flag artifacts have no hybrid_top_k key; their summaries must not
    grow a null field on a --report re-run."""
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "abstention", "index": 0,
             "hybrid_score": 1.0, "hybrid_score_intfaithful": 1.0}]
    out = tmp_path / "beam-100K-qwen-27b-legacy.jsonl"
    out.write_text(json.dumps(rows[0]), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "legacy")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-legacy.summary.json").read_text(
            encoding="utf-8"))
    assert "hybrid_top_k" not in summary


def test_hybrid_top_k_is_read_at_call_time():
    """beam_adapter --hybrid-top-k works by setting lme.HYBRID_TOP_K before
    questions are answered; that only holds if build_contexts reads the
    module global at call time rather than binding it at import."""
    import longmemeval_bench as lme

    class _Svc:
        def search(self, q, top_k, **kw):
            return {"entries": [{"text": f"t{i}"} for i in range(top_k)]}

        def cortex_search(self, q, **kw):
            return {"entries": []}

    old = lme.HYBRID_TOP_K
    try:
        lme.HYBRID_TOP_K = 6
        ctx = lme.build_contexts(_Svc(), "q?")
        mems = ctx["hybrid"].split("Relevant memories:\n", 1)[1]
        assert [m for m in mems.split("\n\n") if m] == [
            f"t{i}" for i in range(6)]
    finally:
        lme.HYBRID_TOP_K = old


def test_hybrid_top_k_beyond_rag_budget_is_loud():
    """build_contexts slices mems[:HYBRID_TOP_K] from a top_k=RAG_TOP_K
    search, so a wider request is silently capped at 6 while the rows would
    record the wider number — an artifact asserting a budget that was never
    served (review finding 4). The validation must fire before any server
    probe so a bad flag dies instantly."""
    from pathlib import Path
    with pytest.raises(SystemExit, match="exceeds"):
        beam_adapter.run(Path("nowhere"), "100K", "qwen-27b", "t",
                         None, None, hybrid_top_k=12)
    with pytest.raises(SystemExit, match="positive"):
        beam_adapter.run(Path("nowhere"), "100K", "qwen-27b", "t",
                         None, None, hybrid_top_k=0)


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
