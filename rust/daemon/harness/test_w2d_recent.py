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
