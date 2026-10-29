"""GET /api/trace answers: score ties may swap in the trace's ordered lists
as in a search's entries, and the trace's key order is compared."""
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
