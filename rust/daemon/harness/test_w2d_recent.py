"""memory_recent answers compare in exact order: no scores, no tie alignment."""
import w2d_search


def answer(ids):
    return {"status": 200, "headers": {"content-type": "mcp"},
            "json": {"count": len(ids), "entries": [{"id": i, "text": f"t{i}"} for i in ids]}}


def test_a_reversed_recent_listing_is_reported():
    diffs, notes = w2d_search.compare(answer([3, 2, 1]), answer([1, 2, 3]), ordered=True)
    assert diffs and not notes


def test_without_ordering_the_unscored_swap_would_be_aligned_away():
    # The search default: entries with no score tie at zero and are realigned.
    diffs, _ = w2d_search.compare(answer([3, 2, 1]), answer([1, 2, 3]))
    assert not diffs


def test_recent_cases_are_compared_ordered():
    cases = w2d_search.SCENARIOS["mcp-recent"]().cases()
    assert cases and all(c["tool"] == "memory_recent" for c in cases)


def stats_answer(body):
    return {"status": 200, "headers": {"content-type": "mcp"}, "json": body}


def test_stats_answers_are_compared_key_order_and_all():
    a = stats_answer({"bands": [], "total_memories": 1, "read_audit": {"x": 1, "y": 2}})
    b = stats_answer({"total_memories": 1, "bands": [], "read_audit": {"y": 2, "x": 1}})
    diffs, _ = w2d_search.compare(a, b, stats=True)
    assert len(diffs) == 2 and all("key order" in d for d in diffs)
    diffs, _ = w2d_search.compare(a, b)
    assert not diffs


def test_worst_sources_ties_align_but_a_real_order_difference_shows():
    def answer(*rows):
        return stats_answer({"bands": [], "total_memories": 0, "read_audit": {
            "worst_sources": [{"source": s, "n": n, "never_read": k} for s, n, k in rows]}})
    py = answer(("a", 5, 5), ("b", 10, 10), ("c", 5, 1))
    tied = answer(("b", 10, 10), ("a", 5, 5), ("c", 5, 1))
    diffs, _ = w2d_search.compare(py, tied, stats=True)
    assert not diffs
    wrong = answer(("c", 5, 1), ("a", 5, 5), ("b", 10, 10))
    diffs, _ = w2d_search.compare(py, wrong, stats=True)
    assert diffs


def test_stats_clock_leaves_compare_presence_and_type_only():
    a = stats_answer({"bands": [], "total_memories": 0,
                      "retrieval_log": {"last_event_at": 1.5}, "last_true_drop": {"at": 2.5}})
    b = stats_answer({"bands": [], "total_memories": 0,
                      "retrieval_log": {"last_event_at": 9.5}, "last_true_drop": {"at": 7.25}})
    diffs, _ = w2d_search.compare(a, b, stats=True)
    assert not diffs
    c = stats_answer({"bands": [], "total_memories": 0,
                      "retrieval_log": {"last_event_at": None}, "last_true_drop": {"at": 7.25}})
    diffs, _ = w2d_search.compare(a, c, stats=True)
    assert diffs


def candidates(*items):
    return {"status": 200, "headers": {"content-type": "mcp"},
            "json": {"record": None, "contenders": [], "candidates": [
                {"entity": "box", "attribute": a, "value": a, "score": s, "why": w}
                for a, s, w in items]}}


def test_near_tied_similar_candidates_align_but_same_entity_order_is_exact():
    tied = candidates(("a", 0.5001, "similar_slot"), ("b", 0.5, "similar_slot"))
    swapped = candidates(("b", 0.5, "similar_slot"), ("a", 0.5001, "similar_slot"))
    diffs, notes = w2d_search.compare(tied, swapped)
    assert not diffs and notes
    same = candidates(("a", None, "same_entity"), ("b", None, "same_entity"))
    reordered = candidates(("b", None, "same_entity"), ("a", None, "same_entity"))
    diffs, _ = w2d_search.compare(same, reordered)
    assert diffs


def test_importing_w2e_keeps_this_harness_name_guard():
    import pgdisposable as pg
    before = pg.DISPOSABLE_NAME.pattern
    w2d_search.import_w2e()
    assert pg.DISPOSABLE_NAME.pattern == before


def test_stats_scenarios_refuse_golden_replay():
    import pytest
    for name in ("mcp-stats", "mcp-stats-continuum", "mcp-stats-capacity"):
        with pytest.raises(SystemExit, match="live only"):
            w2d_search.run_scenario(w2d_search.SCENARIOS[name](), None, "unused", "golden", False)
