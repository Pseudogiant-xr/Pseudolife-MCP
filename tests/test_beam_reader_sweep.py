"""Unit tests for evals/beam_reader_sweep.py's pure parts (no CLI, no GPU).

The Phase-0 reader/volume sweep serves top-48 raw-turn contexts once (CPU,
extraction-free) and answers/judges budget slices of them with a frontier
CLI model. Everything below runs offline with fake chat callables.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import beam_reader_sweep as brs  # noqa: E402
from beam_rejudge import build_cli_call  # noqa: E402


def test_build_cli_call_passes_short_system_as_argv():
    cmd, stdin = build_cli_call("claude", "claude-opus-5", "be brief", "q?")
    assert "--system-prompt" in cmd and "be brief" in cmd
    assert stdin == "q?"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5"


def test_build_cli_call_folds_oversized_system_into_stdin():
    big = "x" * 30000                       # over the argv margin
    cmd, stdin = build_cli_call("claude", "m", big, "q?")
    assert "--system-prompt" not in cmd
    assert stdin == f"{big}\n\nq?"


def test_build_cli_call_empty_system_has_no_flag():
    cmd, stdin = build_cli_call("claude", "m", "", "q?")
    assert "--system-prompt" not in cmd and stdin == "q?"


def test_serve_and_result_paths_are_distinct(tmp_path):
    serve = brs.serve_path("t")
    result = brs.result_path("t")
    assert serve != result
    assert serve.name.endswith(".serve.jsonl")
    assert result.name.endswith(".jsonl")


def test_assemble_context_slices_budget():
    raw = [f"t{i}" for i in range(48)]
    assert brs.assemble_context(raw, 6) == "\n\n".join(raw[:6])
    assert brs.assemble_context([], 6) == ""


def _serve_row(i: int = 0, qtype: str = "summarization") -> dict:
    return {"chat_id": "1", "tier": "100K", "type": qtype, "index": i,
            "question": "q?", "difficulty": "easy", "rubric": ["r1", "r2"],
            "raw_entries": [f"t{j}" for j in range(48)]}


def test_process_row_answers_and_judges_each_budget():
    calls = []

    def answer_fn(system, user, **_):
        calls.append(("answer", len(user)))
        return "an answer"

    def judge_fn(system, user, **_):
        return '{"score": 1.0}'

    out = brs.process_row(_serve_row(), (6, 48), "SYS", answer_fn,
                          "j <question> <rubric_item> <llm_response>",
                          judge_fn)
    assert out["rag6_score"] == 1.0 and out["rag48_score"] == 1.0
    assert out["rag6_response"] == "an answer"
    assert len(out["rag6_judge"]) == 2
    assert out["rag6_context_chars"] < out["rag48_context_chars"]
    assert (out["chat_id"], out["type"], out["index"]) == \
        ("1", "summarization", 0)
    # one answer call per budget; judge calls happen via judge_fn
    assert [c[0] for c in calls] == ["answer", "answer"]


def test_summarize_reports_arms_types_and_baseline():
    rows = [brs.process_row(_serve_row(0, "abstention"), (6,), "S",
                            lambda *a, **k: "ans",
                            "j", lambda *a, **k: '{"score": 1.0}'),
            brs.process_row(_serve_row(1, "summarization"), (6,), "S",
                            lambda *a, **k: "ans",
                            "j", lambda *a, **k: '{"score": 0.0}')]
    s = brs.summarize(rows, (6,), "claude-opus-5",
                      baseline={"rag": {"score": 0.4989}})
    assert s["arms"]["rag6"]["score"] == 0.5
    assert s["types"]["abstention"]["rag6"] == 1.0
    assert s["answerer"] == "claude-opus-5"
    assert s["baseline_qwen_reader_opus_judged"]["rag"]["score"] == 0.4989


def test_resolve_chat_fns_local_uses_bench_server_not_cli():
    """--answerer local answers AND judges through the bench Qwen server
    (zero subscription tokens); cli keeps the claude -p contract. The
    local pair must be plain callables with the (system, user) chat
    signature so process_row stays transport-blind."""
    a, j = brs.resolve_chat_fns("local", "claude-opus-5", "claude-opus-5",
                                "claude", 60.0)
    from beam_rejudge import CliJudge
    assert not isinstance(a, CliJudge) and not isinstance(j, CliJudge)
    assert callable(a) and callable(j)
    ca, cj = brs.resolve_chat_fns("cli", "claude-opus-5", "claude-opus-5",
                                  "claude", 60.0)
    assert isinstance(ca, CliJudge) and isinstance(cj, CliJudge)


def test_budget_beyond_recorded_serve_width_is_loud():
    """A budget wider than the row's serve_top_k would make the arm claim
    a retrieval width the serve never requested (the --hybrid-top-k
    silent-cap lesson). A bank legitimately returning FEWER entries than
    requested is fine — context_chars records the truth."""
    row = _serve_row()
    row["serve_top_k"] = 10
    with pytest.raises(SystemExit, match="serve_top_k"):
        brs.process_row(row, (48,), "S", lambda *a, **k: "x", "j",
                        lambda *a, **k: '{"score": 1.0}')
    short = _serve_row()
    short["raw_entries"] = short["raw_entries"][:10]   # short bank, ok
    out = brs.process_row(short, (48,), "S", lambda *a, **k: "x", "j",
                          lambda *a, **k: '{"score": 1.0}')
    assert out["rag48_context_chars"] == len("\n\n".join(
        short["raw_entries"]))
