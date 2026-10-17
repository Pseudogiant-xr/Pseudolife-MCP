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
