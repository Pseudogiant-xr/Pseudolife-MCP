"""Within-run context variants for the Phase-1 gate (amended design).

Design: docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md
(Amendment 2026-08-03). ``dump_bank`` persists cortex facts only — no band
entries — so per-knob bank-reuse runs are impossible for the retrieval
knobs; instead one run builds five hybrid context variants per question
from the same live service (identical bank ⇒ knob-only deltas,
within-question pairing, rag control byte-identical by construction).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import longmemeval_bench as lmb  # noqa: E402

VARIANT_KEYS = ("rag", "cortex", "hybrid", "hybrid_ctg", "hybrid_tl",
                "hybrid_enum", "hybrid_all")


class _StubSvc:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, question, **kw):
        self.calls.append(kw)
        return {"entries": [{"text": f"mem-{len(self.calls)}"}]}

    def cortex_search(self, *a, **kw):
        return {"entries": [
            {"entity": "user", "attribute": "store", "value": "Thrive"}]}

    def history(self, *a, **kw):
        return {"versions": [{"value": "Walmart", "tx_time": 1684108800.0},
                             {"value": "Thrive"}]}


def test_variant_contexts_and_knob_wiring():
    svc = _StubSvc()
    ctx = lmb.build_contexts(svc, "when did I first shop?", variants=True)
    for k in VARIANT_KEYS:
        assert k in ctx, k
    # Call 1 is the pinned control, shared by rag AND the vanilla hybrid
    # baseline (identical params — one call, byte-identical by construction).
    assert svc.calls[0] == {"top_k": lmb.RAG_TOP_K,
                            "contiguity_neighbors": 0, "timeline": False}
    # Each retrieval variant isolates its knob; hybrid_all combines.
    assert (svc.calls[1]["contiguity_neighbors"] == 1
            and svc.calls[1]["timeline"] is False)
    assert (svc.calls[2]["contiguity_neighbors"] == 0
            and svc.calls[2]["timeline"] is True)
    assert (svc.calls[3]["contiguity_neighbors"] == 1
            and svc.calls[3]["timeline"] is True)
    # hybrid_enum re-renders facts over the vanilla retrieval — no 5th call.
    assert len(svc.calls) == 4
    assert "  1. Walmart (2023-05-15)" in ctx["hybrid_enum"]
    assert "  1. Walmart (2023-05-15)" in ctx["hybrid_all"]
    assert "1. Walmart" not in ctx["hybrid"]
    # Memory portions come from the right calls.
    assert "mem-1" in ctx["rag"] and "mem-1" in ctx["hybrid"]
    assert "mem-1" in ctx["hybrid_enum"]
    assert "mem-2" in ctx["hybrid_ctg"]
    assert "mem-3" in ctx["hybrid_tl"]
    assert "mem-4" in ctx["hybrid_all"]


def test_non_variant_mode_unchanged():
    svc = _StubSvc()
    ctx = lmb.build_contexts(svc, "when did I first shop?")
    assert set(ctx) == {"rag", "cortex", "hybrid"}
    assert len(svc.calls) == 2  # pinned rag + config-following hybrid


def test_answer_and_judge_covers_all_context_arms(monkeypatch):
    monkeypatch.setattr(
        lmb, "_chat", lambda sys_p, prompt, max_tokens=512: "yes")
    row = {"question": "q", "answer": "a", "question_date": "d",
           "question_type": "multi-session",
           "contexts": {"rag": "r", "cortex": "c", "hybrid": "h",
                        "hybrid_ctg": "hc", "hybrid_all": "ha"}}
    out = lmb.answer_and_judge(row)
    for arm in ("rag", "cortex", "hybrid", "hybrid_ctg", "hybrid_all"):
        assert out[f"{arm}_correct"] is True
        assert f"{arm}_response" in out
        assert f"{arm}_context_tokens" in out


def _mk_row(qid, qtype, flags: dict) -> dict:
    row = {"question_id": qid, "question_type": qtype,
           "consolidation": {"superseded": 0},
           "cortex_response": "I don't know.", "abstention": False}
    for arm, ok in flags.items():
        row[f"{arm}_correct"] = ok
        row[f"{arm}_context_tokens"] = 100
        row.setdefault(f"{arm}_response", "x")
    return row


def test_report_covers_variant_arms(tmp_path, monkeypatch):
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    types = ("multi-session", "temporal-reasoning")
    out = lmb.out_file("oracle", "qwen-27b", "t", lmb.types_slug(types))
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        _mk_row("q1", "multi-session",
                {"rag": True, "cortex": False, "hybrid": False,
                 "hybrid_ctg": True}),
        _mk_row("q2", "temporal-reasoning",
                {"rag": True, "cortex": True, "hybrid": True,
                 "hybrid_ctg": True}),
    ]
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    lmb.report("oracle", "qwen-27b", "t", types)
    summary = json.loads(out.with_name(
        out.name.removesuffix(".jsonl") + ".summary.json").read_text())
    assert summary["arms"]["hybrid_ctg"]["accuracy"] == 1.0
    assert summary["arms"]["hybrid"]["accuracy"] == 0.5
    assert summary["types"]["multi-session"]["arms"]["hybrid_ctg"] == 1.0
