"""GET /api/trace answers: score ties may swap in the trace's ordered lists
as in a search's entries, and the trace's key order is compared."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import w2d_search


def answer(order, *, keys=("tiers", "final_topk")):
    hits = {"a": 0.5, "b": 0.5, "c": 0.1}
    cands = [{"text_preview": t, "raw_score": hits[t], "kept": True} for t in order]
    finals = [{"text_preview": t, "score": hits[t]} for t in order]
    trace = {}
    for k in keys:
        trace[k] = ([{"name": "flat", "candidates": cands}] if k == "tiers" else finals)
    return {"status": 200, "headers": {"content-type": "application/json"},
            "json": {"query": "q", "count": len(order),
                     "entries": [{"id": t, "text": t, "score": hits[t]} for t in order],
                     "trace": trace}}


def test_tied_trace_rows_align_as_entries_do():
    diffs, notes = w2d_search.compare(answer("abc"), answer("bac"))
    assert not diffs, diffs
    assert any("final_topk" in n for n in notes) and any("candidates" in n for n in notes)


def test_an_untied_swap_in_the_trace_is_reported():
    diffs, _ = w2d_search.compare(answer("abc"), answer("acb"))
    assert any("trace" in d for d in diffs)


def test_trace_key_order_is_compared():
    diffs, _ = w2d_search.compare(answer("abc"), answer("abc", keys=("final_topk", "tiers")))
    assert any("key order" in d for d in diffs)


def test_mcp_explain_template_without_frozen_package(monkeypatch):
    import psycopg
    from rust.harness_support.import_blocker import cold_harness_imports

    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    dsn = "postgresql://unused/pl_cf_w1a_explain"
    conn = MagicMock()
    conn.__enter__.return_value = conn
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(psycopg, "connect", connect)
    calls = []
    guard = MagicMock(side_effect=lambda target: calls.append(("guard", target)))
    harness = SimpleNamespace(Scenario=object, T_DEFAULT="fixture-token", assert_disposable=guard)
    with cold_harness_imports():
        from run_scenarios import mcp_search_explain
        from rust.harness_support import bank_fixture

        monkeypatch.setattr(bank_fixture, "initialize_bank",
                            lambda target: calls.append(("schema", target)))
        conn.execute.side_effect = lambda query, params: calls.append(("row", params))
        scenario, = mcp_search_explain.register(harness)
        with pytest.raises(RuntimeError, match="production bank"):
            scenario().prepare_template("dbname=pseudolife_memory")
        connect.assert_not_called()
        guard.side_effect = RuntimeError("connected database refused")
        with pytest.raises(RuntimeError, match="connected database refused"):
            scenario().prepare_template(dsn)
        assert calls == []
        guard.side_effect = lambda target: calls.append(("guard", target))
        connect.reset_mock()
        scenario().prepare_template(dsn)

    connect.assert_called_once_with(dsn)
    assert calls == [("guard", conn), ("schema", conn)] + [
        ("row", (f"memory {index}", json.dumps([*direction] + [0.0] * 1022), '["probe"]'))
        for index, direction in enumerate(((0.8, 0.6), (1.0, 0.0), (0.8, -0.6)))
    ]
