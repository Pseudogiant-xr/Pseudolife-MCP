"""LME chronicle arm (Phase 2 gate harness).

``--chronicle`` adds a ``hybrid_ev`` context variant: the vanilla hybrid
context plus the ``events`` block the pinned control search already
returns (serving is cue-gated service-side, so rows whose question
carries no temporal cue — or whose bank yielded no events — get
``hybrid_ev == hybrid`` and pair to a zero delta, honestly). The events
come from the SAME pinned call as the rag control, so no extra search
and no knob leakage: the hybrid/hybrid_ev delta is the served events
block and nothing else.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import longmemeval_bench as lmb  # noqa: E402


def test_bench_reset_truncates_served_tables():
    """Regression lock (2026-08-04): ``ladder_sweep._ALL_TABLES`` is the
    BENCH reset's truncate list — a second mutation path beside
    ``pg_fixtures._ALL_TABLES``. ``chronicle_events`` has no FKs, so
    nothing cascades into it; leaving it off the list let events
    accumulate across all 266 questions of the ev-weak-0804 run and
    contaminate every served events block (verdict corrected in
    agg-recall-phase2-weak-verdict.json). Any future FK-free SERVED
    table must land in both lists."""
    import ladder_sweep
    assert "chronicle_events" in ladder_sweep._ALL_TABLES


class _StubSvc:
    def __init__(self, events=None):
        self._events = events
        self._extra: dict = {}     # e.g. events_total on agg-cued queries
        self.calls: list[dict] = []

    def search(self, question, **kw):
        self.calls.append(kw)
        out = {"entries": [{"text": f"mem-{len(self.calls)}"}]}
        if self._events is not None:
            out["events"] = self._events
        out.update(self._extra)
        return out

    def cortex_search(self, *a, **kw):
        return {"entries": [
            {"entity": "user", "attribute": "pet", "value": "kitten"}]}

    def history(self, *a, **kw):
        return {"versions": []}


_EVENTS = [
    {"description": "adopted a kitten", "actor": "user",
     "date": "2023-05-13", "phrase": "yesterday"},
    {"description": "kitten's first vet visit", "actor": "user",
     "date": None, "phrase": "a while back"},
]


def test_chronicle_adds_hybrid_ev_with_events_block(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    ctx = lmb.build_contexts(_StubSvc(events=_EVENTS),
                             "when did I adopt the kitten?", variants=True)
    assert "hybrid_ev" in ctx
    assert "Events (dated, oldest first):" in ctx["hybrid_ev"]
    assert "- 2023-05-13: adopted a kitten" in ctx["hybrid_ev"]
    assert "(undated: a while back): kitten's first vet visit" \
        in ctx["hybrid_ev"]
    # The events block is the ONLY delta vs the vanilla hybrid arm.
    assert "Events (" not in ctx["hybrid"]
    assert ctx["hybrid_ev"].startswith(ctx["hybrid"])


def test_no_events_makes_hybrid_ev_equal_hybrid(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    ctx = lmb.build_contexts(_StubSvc(events=None),
                             "what's my pet's name?", variants=True)
    assert ctx["hybrid_ev"] == ctx["hybrid"]


def test_chronicle_off_adds_no_arm(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", False)
    ctx = lmb.build_contexts(_StubSvc(events=_EVENTS),
                             "when did I adopt the kitten?", variants=True)
    assert "hybrid_ev" not in ctx


def test_chronicle_works_without_variants_mode(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    ctx = lmb.build_contexts(_StubSvc(events=_EVENTS),
                             "when did I adopt the kitten?")
    assert "hybrid_ev" in ctx and "adopted a kitten" in ctx["hybrid_ev"]
    assert set(ctx) == {"rag", "cortex", "hybrid", "hybrid_ev"}


# ── aggregation-serving variants (2026-08-06 design) ─────────────────────

_MANY_EVENTS = [
    {"description": f"went climbing at the {n} wall", "actor": "user",
     "date": f"2023-05-{11 + i:02d}", "phrase": f"the {n} day"}
    for i, n in enumerate(("alpha", "bravo", "charlie", "delta",
                           "echo", "foxtrot", "golf", "hotel"))
]


def test_hybrid_ev_reconstructs_old_gate_on_agg_only_query(monkeypatch):
    """The service now serves events on aggregation cues too, but the
    hybrid_ev arm must stay byte-comparable to the ev2 run: events shown
    only on a temporal cue, first 6 only — regardless of --ev-variants."""
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    svc = _StubSvc(events=_MANY_EVENTS)
    svc_out_total = dict(events_total=len(_MANY_EVENTS))
    svc._extra = svc_out_total
    ctx = lmb.build_contexts(svc, "how many walls did I climb?")
    assert ctx["hybrid_ev"] == ctx["hybrid"]      # no temporal cue -> no block


def test_hybrid_ev_truncates_to_six_on_temporal_query(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    ctx = lmb.build_contexts(_StubSvc(events=_MANY_EVENTS),
                             "when did I go climbing?")
    assert "alpha wall" in ctx["hybrid_ev"]
    assert "foxtrot wall" in ctx["hybrid_ev"]     # 6th event kept
    assert "golf wall" not in ctx["hybrid_ev"]    # 7th truncated


def test_ev_variants_add_agg_and_syn_arms(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    monkeypatch.setattr(lmb, "EV_VARIANTS", True)
    svc = _StubSvc(events=_MANY_EVENTS)
    svc._extra = {"events_total": len(_MANY_EVENTS)}
    ctx = lmb.build_contexts(svc, "how many walls did I climb?",
                             variants=False)
    # agg arm: full list on the aggregation cue, no tally
    assert "hotel wall" in ctx["hybrid_ev_agg"]   # all 8 served
    assert "Total events listed" not in ctx["hybrid_ev_agg"]
    # syn arm: agg + the computed tally line
    assert ctx["hybrid_ev_syn"].startswith(ctx["hybrid_ev_agg"])
    assert "Total events listed: 8" in ctx["hybrid_ev_syn"]
    # reconstruction arm unchanged beside them
    assert ctx["hybrid_ev"] == ctx["hybrid"]


def test_make_extractor_threads_events_prompt_file(tmp_path):
    """--events-prompt-file mirrors --system-prompt-file: candidate events
    prompts (events_pass_v2) run through the identical code path with the
    shipped constant untouched; omitted -> byte-identical v1 default."""
    from pseudolife_memory.memory.dream import _EVENTS_SYSTEM_PROMPT

    p = tmp_path / "candidate.txt"
    p.write_text("CANDIDATE EVENTS PROMPT", encoding="utf-8")
    ex = lmb._make_extractor("http://x/v1", None, events_prompt_file=str(p))
    assert ex.events_prompt == "CANDIDATE EVENTS PROMPT"
    default = lmb._make_extractor("http://x/v1", None)
    assert default.events_prompt == _EVENTS_SYSTEM_PROMPT


def test_ev_variants_off_adds_no_extra_arms(monkeypatch):
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    monkeypatch.setattr(lmb, "EV_VARIANTS", False)
    ctx = lmb.build_contexts(_StubSvc(events=_EVENTS),
                             "when did I adopt the kitten?")
    assert "hybrid_ev_agg" not in ctx and "hybrid_ev_syn" not in ctx


def test_ev_variants_add_hdr_arm_with_partial_record_header(monkeypatch):
    """Anti-suppression arm (2026-08-06 quantity+coverage design): same
    content as hybrid_ev_syn but the block header marks the list as a
    partial record, targeting the BEAM abstention-suppression regressions
    (6/8 losses were 'I don't know' on questions vanilla hybrid answered)."""
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    monkeypatch.setattr(lmb, "EV_VARIANTS", True)
    svc = _StubSvc(events=_MANY_EVENTS)
    svc._extra = {"events_total": len(_MANY_EVENTS)}
    ctx = lmb.build_contexts(svc, "how many walls did I climb?",
                             variants=False)
    assert "hybrid_ev_hdr" in ctx
    assert ("Events (dated, oldest first; partial record — other context "
            "may hold more):") in ctx["hybrid_ev_hdr"]
    # Same events and tally as syn, only the header differs.
    assert "hotel wall" in ctx["hybrid_ev_hdr"]
    assert "Total events listed: 8" in ctx["hybrid_ev_hdr"]
    assert ctx["hybrid_ev_hdr"].replace(
        "Events (dated, oldest first; partial record — other context "
        "may hold more):",
        "Events (dated, oldest first):") == ctx["hybrid_ev_syn"]


def test_ev_variants_add_ins_arm_with_directive_footer(monkeypatch):
    """Anti-suppression instruction arm (2026-08-07 evlora design): syn
    content plus one directive line AFTER the block. The descriptive hdr
    hedge rescued none of the three measured block-authority losses and
    flipped zero multi-session rows (evq-residual-decomposition-0807), so
    this arm tests the directive lever instead."""
    monkeypatch.setattr(lmb, "CHRONICLE", True)
    monkeypatch.setattr(lmb, "EV_VARIANTS", True)
    svc = _StubSvc(events=_MANY_EVENTS)
    svc._extra = {"events_total": len(_MANY_EVENTS)}
    ctx = lmb.build_contexts(svc, "how many walls did I climb?",
                             variants=False)
    assert "hybrid_ev_ins" in ctx
    assert ctx["hybrid_ev_ins"].startswith(ctx["hybrid_ev_syn"])
    assert ctx["hybrid_ev_ins"] == ctx["hybrid_ev_syn"] + (
        "\nThis list is an extracted index, not the complete record: when "
        "counting or totaling, re-scan the conversation above and include "
        "occurrences not listed here.")
