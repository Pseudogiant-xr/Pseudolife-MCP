"""Enumerable fact rendering + control-arm pinning (agg-recall Phase 1, knob 3).

Design: docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md.
The 2026-08-03 autopsy showed the answerer miscounting values that were
fully present but rendered as inline chains ("a -> b -> c") — enumerated,
dated, one-per-line rendering is the fix, behind ``--fact-render enum``
(default ``inline`` stays byte-identical). ``build_contexts`` pins the rag
control arm to vanilla retrieval regardless of config/CLI so the
preregistered tripwire holds by construction.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import longmemeval_bench as lmb  # noqa: E402


def test_inline_default_unchanged():
    f = {"entity": "user", "attribute": "store", "value": "Thrive Market"}
    versions = [{"value": "Walmart"}, {"value": "Thrive Market"}]
    line = lmb._compose_fact_line(f, versions)
    assert line == ("user — store: Thrive Market"
                    "  (earlier values, oldest first: Walmart)")


def test_enum_scalar_chain_numbered_and_dated():
    f = {"entity": "user", "attribute": "store", "value": "Thrive Market"}
    versions = [
        {"value": "Walmart", "tx_time": 1684108800.0},       # 2023-05-15
        {"value": "Costco", "asserted_at": 1684713600.0},    # 2023-05-22
        {"value": "Thrive Market", "tx_time": 1685318400.0},
    ]
    out = lmb._compose_fact_line(f, versions, enumerated=True)
    lines = out.splitlines()
    assert lines[0] == "user — store: Thrive Market"
    assert lines[1] == "  earlier values, oldest first:"
    assert lines[2] == "  1. Walmart (2023-05-15)"
    assert lines[3] == "  2. Costco (2023-05-22)"
    assert len(lines) == 4  # current value never repeats in the chain


def test_enum_scalar_undated_version_has_no_parenthetical():
    f = {"entity": "user", "attribute": "city", "value": "Leeds"}
    out = lmb._compose_fact_line(
        f, [{"value": "York"}, {"value": "Leeds"}], enumerated=True)
    assert "  1. York" in out.splitlines()


def test_enum_scalar_no_history_is_single_line():
    f = {"entity": "user", "attribute": "city", "value": "Leeds"}
    assert lmb._compose_fact_line(f, [], enumerated=True) == \
        "user — city: Leeds"


def test_enum_set_members_numbered_with_former():
    f = {
        "entity": "user", "attribute": "cuisines-tried", "kind": "set",
        "value": "Ethiopian; Indian; Korean (3 members)",
        "members": [
            {"value": "Ethiopian", "asserted_at": 1684108800.0},
            {"value": "Indian"},
            {"value": "Korean", "asserted_at": 1685318400.0},
        ],
    }
    versions = [
        {"value": "vegan", "event": "added", "at": 1684108800.0},
        {"value": "vegan", "event": "removed", "at": 1684713600.0},
    ]
    out = lmb._compose_fact_line(f, versions, enumerated=True)
    lines = out.splitlines()
    assert lines[0].startswith("user — cuisines-tried:")
    assert "  members:" in lines
    mi = lines.index("  members:")
    assert lines[mi + 1] == "  1. Ethiopian (2023-05-15)"
    assert lines[mi + 2] == "  2. Indian"
    assert lines[mi + 3] == "  3. Korean (2023-05-29)"
    assert "  former members:" in lines
    fi = lines.index("  former members:")
    assert lines[fi + 1] == "  1. vegan (removed 2023-05-22)"


class _StubSvc:
    """Records search kwargs; returns canned entries."""

    def __init__(self):
        self.calls: list[dict] = []

    def search(self, question, **kw):
        self.calls.append(kw)
        return {"entries": [{"text": f"entry-{len(self.calls)}"}]}

    def cortex_search(self, *a, **kw):
        return {"entries": []}

    def history(self, *a, **kw):
        return {"versions": []}


def test_build_contexts_pins_rag_arm_to_vanilla_retrieval():
    svc = _StubSvc()
    ctx = lmb.build_contexts(svc, "when did I first shop at Thrive?")
    # First call builds the rag control arm: knobs explicitly pinned off.
    rag_call = svc.calls[0]
    assert rag_call.get("contiguity_neighbors") == 0
    assert rag_call.get("timeline") is False
    # The hybrid/memory call follows config/CLI (None = config default).
    hyb_call = svc.calls[1]
    assert hyb_call.get("contiguity_neighbors") is None
    assert hyb_call.get("timeline") is None
    # rag context comes from the pinned call, hybrid from the other.
    assert "entry-1" in ctx["rag"] and "entry-1" not in ctx["hybrid"]
    assert "entry-2" in ctx["hybrid"]
