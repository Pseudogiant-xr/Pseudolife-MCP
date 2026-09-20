"""``memory_dream(action="deep")`` bounds its lists so the MCP response stays a
usable size.

2026-09-20, live bank: 316 pending merge proposals with snippets made a 597 KB
tool result. The JSON-RPC envelope escapes that text and FastMCP duplicates it
as ``structuredContent``, so the server-sent event on the wire was 1,124,250
bytes, over the 1 MiB per-event cap in the SDK client's SSE decoder, and every
deep dream through the shim died as a phantom disconnect. The service method is
untouched (the Console and the sweep tick read it whole); the MCP tool caps
each list and reports the full counts.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers import invoke_tool


@pytest.fixture
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def _proposal(i: int, snippet: str) -> dict:
    return {"proposal_id": i,
            "from": {"display": f"from-{i}", "snippets": [snippet, snippet]},
            "into": {"display": f"into-{i}", "snippets": [snippet]}}


def _fat_deep(n_proposals: int = 300, n_candidates: int = 120, snippet_chars: int = 700) -> dict:
    snippet = "x" * snippet_chars
    return {
        "dry_run": True, "rescored": 0,
        "would_orphan_count": 0, "would_orphan": [],
        "would_supersede": [], "would_merge": [],
        "would_merge_propose": [
            {"from": f"a{i}", "into": f"b{i}", "similarity": 1.0,
             "reason": "token-subset", "already_proposed": False} for i in range(50)],
        "would_junk": [],
        "merge_proposals": [_proposal(i, snippet) for i in range(n_proposals)],
        "lesson_duplicates": [{"pair": i} for i in range(30)],
        "world_duplicates": [],
        "candidates": [
            {"src_id": i, "dst_id": i + 1, "src_snippets": [snippet], "dst_snippets": [snippet]}
            for i in range(n_candidates)],
        "totals": {"entities": 5000, "edges": 3000, "candidates": n_candidates},
    }


def test_deep_response_lists_are_capped_with_full_counts(mod, monkeypatch):
    fat = _fat_deep()
    monkeypatch.setattr(mod.service, "deep_dream", lambda **_kw: fat)
    assert len(json.dumps(fat)) > mod._DEEP_RESPONSE_BUDGET, "premise: the raw result is oversized"

    out = invoke_tool("memory_dream", {"action": "deep"})

    assert len(json.dumps(out)) <= mod._DEEP_RESPONSE_BUDGET
    head = mod._DEEP_LIST_HEAD
    assert out["truncated"] == {"merge_proposals": 300, "candidates": 120,
                                "would_merge_propose": 50}
    assert out["merge_proposals"] == fat["merge_proposals"][:head]
    assert out["candidates"] == fat["candidates"][:head]
    assert out["would_merge_propose"] == fat["would_merge_propose"][:head]
    # Lists under the cap, scalars and totals pass through untouched.
    assert out["lesson_duplicates"] == fat["lesson_duplicates"]
    assert out["totals"] == fat["totals"]
    assert out["dry_run"] is True
    assert "memory_graph_review(action='list')" in out["hint"] and "snippets" in out["hint"]


def test_deep_response_shrinks_the_head_until_it_fits_the_budget(mod, monkeypatch):
    # 40 items of ~20 KB each: the default head alone would still be ~800 KB.
    fat = _fat_deep(n_proposals=40, n_candidates=0, snippet_chars=7000)
    monkeypatch.setattr(mod.service, "deep_dream", lambda **_kw: fat)

    out = invoke_tool("memory_dream", {"action": "deep"})

    assert len(json.dumps(out)) <= mod._DEEP_RESPONSE_BUDGET
    assert 0 < len(out["merge_proposals"]) < mod._DEEP_LIST_HEAD
    assert out["truncated"]["merge_proposals"] == 40
    assert out["merge_proposals"] == fat["merge_proposals"][:len(out["merge_proposals"])]


def test_response_within_budget_passes_through_unchanged(mod, monkeypatch):
    # More candidates than the head (top_k_candidates defaults to 50) but a
    # small response: nothing is cut, because the head exists only to bring
    # an oversized response under budget.
    modest = _fat_deep(n_proposals=60, n_candidates=50, snippet_chars=20)
    monkeypatch.setattr(mod.service, "deep_dream", lambda **_kw: modest)
    assert len(json.dumps(modest)) <= mod._DEEP_RESPONSE_BUDGET, "premise: fits the budget"
    assert len(modest["candidates"]) > mod._DEEP_LIST_HEAD, "premise: longer than the head"

    out = invoke_tool("memory_dream", {"action": "deep"})

    assert out == modest
    assert "truncated" not in out and "hint" not in out


def test_deep_arguments_still_reach_the_service(mod, monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"dry_run": False, "applied": True}

    monkeypatch.setattr(mod.service, "deep_dream", fake)
    out = invoke_tool("memory_dream", {"action": "deep", "apply": True, "snippets": False})
    assert seen == {"apply": True, "include_snippets": False}
    assert out == {"dry_run": False, "applied": True}
