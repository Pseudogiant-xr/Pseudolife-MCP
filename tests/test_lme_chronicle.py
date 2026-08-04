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
        self.calls: list[dict] = []

    def search(self, question, **kw):
        self.calls.append(kw)
        out = {"entries": [{"text": f"mem-{len(self.calls)}"}]}
        if self._events is not None:
            out["events"] = self._events
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
