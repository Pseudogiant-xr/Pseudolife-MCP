"""Unit tests for the ReFind agentic-lexical arm (no GPU, no server).

Every model call is injected, so the whole loop — planning, temporal
narrowing, skip-inspected, session-aware fusion, budgeting — is exercised
on CPU.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import refind_arm  # noqa: E402
from refind_arm import (  # noqa: E402
    ArchiveRecord, LexicalArchive, parse_plan, refind_search,
)


def _rec(text, session, ordinal, date=None):
    return ArchiveRecord(text=text, session=session, ordinal=ordinal,
                         date=date)


# Two sessions. Session "b" holds the strong hit (both query terms) and a
# weak sibling; session "a" holds a weak hit whose lexical score is exactly
# equal to b's weak one (same tokens, same length) — so any ordering
# difference between them comes from session-aware fusion alone.
_RECORDS = [
    _rec("zebra alpha beta", "a", 1, "2024-03-01"),
    _rec("zebra quokka numbat", "b", 2, "2024-05-01"),
    _rec("zebra gamma delta", "b", 3, "2024-05-02"),
    _rec("wombat only here", "b", 4, None),
]


def _archive():
    return LexicalArchive(_RECORDS)


def test_search_ranks_exact_token_matches():
    hits = _archive().search("zebra quokka", top_k=5)
    assert hits[0].record.text == "zebra quokka numbat"
    assert all(h.lexical > 0 for h in hits)
    assert "wombat only here" not in [h.record.text for h in hits]


def test_temporal_narrowing_drops_records_outside_the_window():
    """ReFind narrows by time range. A record that provably falls outside
    the window is dropped; an UNDATED record can never be shown to fall
    outside, so it stays eligible."""
    arc = _archive()
    hits = arc.search("zebra wombat", top_k=5, since="2024-04-01")
    texts = [h.record.text for h in hits]
    assert "zebra alpha beta" not in texts          # dated before the window
    assert "wombat only here" in texts              # undated: still eligible
    late = arc.search("zebra", top_k=5, until="2024-03-31")
    assert [h.record.text for h in late] == ["zebra alpha beta"]


def test_skip_inspected_does_not_move_the_remaining_scores():
    """Round N+1 must not re-serve what round N inspected, and excluding a
    document must not silently re-weight the ones that survive: the index
    is built over the temporal WINDOW and exclusion is applied to its
    results, so IDF is stable across rounds."""
    arc = _archive()
    first = {h.record.text: h.lexical for h in arc.search("zebra", top_k=5)}
    second = arc.search("zebra", top_k=5, exclude={0, 1})
    texts = [h.record.text for h in second]
    assert "zebra alpha beta" not in texts and "zebra quokka numbat" not in texts
    assert second[0].record.text == "zebra gamma delta"
    assert second[0].lexical == pytest.approx(first["zebra gamma delta"])


def test_session_fusion_promotes_the_corroborated_session():
    """Session-aware fusion: two turns with identical lexical scores are
    separated by the evidence their SESSION carries. Weight 0 keeps the
    plain lexical order (tie broken by session/ordinal, which favours "a")."""
    arc = _archive()
    plain = [h.record.text
             for h in arc.search("zebra quokka", top_k=5, session_weight=0.0)]
    fused = [h.record.text
             for h in arc.search("zebra quokka", top_k=5, session_weight=0.5)]
    assert plain.index("zebra alpha beta") < plain.index("zebra gamma delta")
    assert fused.index("zebra gamma delta") < fused.index("zebra alpha beta")


def test_search_tie_break_is_deterministic():
    """Equal fused scores order by (session, ordinal) — the same archive
    and query must produce the same context on a rerun."""
    arc = LexicalArchive([_rec("same tokens here", "b", 9),
                          _rec("same tokens here", "a", 4),
                          _rec("same tokens here", "a", 2)])
    hits = arc.search("same tokens", top_k=3, session_weight=0.0)
    assert [(h.record.session, h.record.ordinal) for h in hits] == [
        ("a", 2), ("a", 4), ("b", 9)]


def test_serve_ranking_fuses_across_rounds_not_per_query():
    """Review finding (2026-09-01): per-query min-max makes every query's
    best hit exactly 1.0, so a lone weak hit from a later round ties the
    strongest hits of the first — and wins the tie-break when its session
    sorts first. The served set must be ranked over the UNION of what was
    inspected, from the raw lexical scores."""
    archive = LexicalArchive([
        _rec("session zebra quokka alpha", "b", 1),
        _rec("session zebra quokka beta", "b", 2),
        _rec("session gamma delta", "a", 3),      # only the common term
    ])
    chat = _ScriptedChat(['{"queries": ["zebra quokka"]}',
                          '{"queries": ["session"]}'])
    ctx, trace = refind_search(archive, "q?", chat=chat, rounds=2, top_k=2)
    served = [t for t in ctx.split("\n\n") if t]
    assert trace["inspected"] == 3 and len(served) == 2
    assert "session gamma delta" not in served


def test_refind_search_falls_back_when_a_round_finds_nothing():
    """A planner window that is parseable but wrong (2025 against a 2024
    archive) empties the search. An empty context would score ~0 and look
    like a genuine miss, so the loop takes one unrestricted look at the
    question and says so in the trace."""
    chat = _ScriptedChat(['{"queries": ["zebra"], "since": "2025-01-01", '
                          '"until": "2025-12-31"}', '{"done": true}'])
    ctx, trace = refind_search(_archive(), "zebra quokka?", chat=chat,
                               rounds=2, top_k=3)
    assert trace["fallback"] is True
    assert "zebra quokka numbat" in ctx and trace["served"] > 0


def test_no_fallback_when_the_loop_found_something():
    chat = _ScriptedChat(['{"queries": ["zebra"]}', '{"done": true}'])
    _, trace = refind_search(_archive(), "q?", chat=chat, rounds=2, top_k=3)
    assert trace["fallback"] is False


def test_window_index_cache_is_bounded_and_keeps_the_full_window():
    """One BM25 index per temporal window, cached — but the archive lives
    for a whole chat and every planner-proposed window is a new key, so
    the cache is capped. The unnarrowed index is the one every
    non-narrowing query hits, so it is never evicted."""
    arc = _archive()
    arc.search("zebra", top_k=2)                       # the full window
    for month in range(1, 9):
        arc.search("zebra", top_k=2, since=f"2024-{month:02d}-01")
    assert (None, None) in arc._windows
    assert len(arc._windows) <= refind_arm.WINDOW_CACHE + 1


def test_parse_plan_coerces_a_bare_string_queries_value():
    """A small model answering "queries": "trek domane" would otherwise
    have its string iterated into ten single-character queries, each of
    which returns junk at a top rank instead of failing cleanly."""
    assert parse_plan('{"queries": "trek domane"}')["queries"] == [
        "trek domane"]
    assert parse_plan('{"queries": 7}')["queries"] == []


def test_parse_plan_tolerates_fences_and_prose():
    assert parse_plan('{"queries": ["a", "b"]}')["queries"] == ["a", "b"]
    fenced = parse_plan('```json\n{"queries": ["x"], "since": "2024-01-01"}\n```')
    assert fenced["queries"] == ["x"] and fenced["since"] == "2024-01-01"
    assert parse_plan('Sure! {"done": true, "queries": []}')["done"] is True
    assert parse_plan("no json at all") is None


class _ScriptedChat:
    """Returns canned planner replies in order; records every prompt."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, system, user, *, max_tokens=256, **_):
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else '{"done": true}'


def test_refind_search_dedups_across_rounds_and_stops_on_done():
    chat = _ScriptedChat(['{"queries": ["zebra"]}',
                          '{"queries": ["quokka"]}',
                          '{"done": true}'])
    ctx, trace = refind_search(_archive(), "where is the zebra?", chat=chat,
                               rounds=4, top_k=6)
    turns = [t for t in ctx.split("\n\n") if t]
    assert len(turns) == len(set(turns))              # no turn served twice
    assert trace["rounds"] == 3                       # stopped on done
    assert trace["queries"] == ["zebra", "quokka"]
    assert trace["inspected"] == len(turns)
    assert len(chat.prompts) == 3


def test_refind_search_honours_the_turn_budget():
    """The arm is budget-matched to the rag control by default — its win
    must come from the loop, not from a wider window."""
    chat = _ScriptedChat(['{"queries": ["zebra wombat"]}', '{"done": true}'])
    ctx, trace = refind_search(_archive(), "q?", chat=chat, rounds=3, top_k=2)
    assert len([t for t in ctx.split("\n\n") if t]) == 2
    assert trace["served"] == 2
    assert trace["inspected"] >= 3                    # inspected more than served


def test_refind_context_reads_in_session_and_turn_order():
    chat = _ScriptedChat(['{"queries": ["zebra"]}', '{"done": true}'])
    ctx, _ = refind_search(_archive(), "q?", chat=chat, rounds=2, top_k=3)
    served = [t for t in ctx.split("\n\n") if t]
    assert served == ["zebra alpha beta", "zebra quokka numbat",
                      "zebra gamma delta"]


def test_refind_search_falls_back_to_the_question_when_planning_fails():
    """An unparseable planner reply must not silently serve an empty
    context — the first round falls back to the question itself, and the
    trace records the failure so the artifact shows it."""
    chat = _ScriptedChat(["I am afraid I cannot do that",
                          "still not json"])
    ctx, trace = refind_search(_archive(), "zebra quokka?", chat=chat,
                               rounds=2, top_k=3)
    assert "zebra quokka numbat" in ctx
    assert trace["plan_failures"] == 2
    assert trace["queries"] == ["zebra quokka?"]


def test_refind_search_is_bounded_by_rounds():
    chat = _ScriptedChat(['{"queries": ["zebra"]}'] * 20)
    _, trace = refind_search(_archive(), "q?", chat=chat, rounds=2, top_k=6)
    assert trace["rounds"] == 2 and len(chat.prompts) == 2


def test_plan_prompt_carries_the_span_and_what_was_already_read():
    """The planner can only narrow temporally if it is told the archive's
    span, and can only vary its queries if it is told what it already
    issued and read."""
    chat = _ScriptedChat(['{"queries": ["zebra"]}', '{"queries": ["quokka"]}'])
    refind_search(_archive(), "q?", chat=chat, rounds=2, top_k=6)
    assert "2024-03-01" in chat.prompts[0] and "2024-05-02" in chat.prompts[0]
    assert "zebra" in chat.prompts[1]                 # the issued query
    assert "zebra quokka numbat" in chat.prompts[1]   # a snippet it read


def test_parse_anchor_handles_beam_and_lme_date_shapes():
    assert refind_arm.parse_anchor("March-15-2024") == "2024-03-15"
    assert refind_arm.parse_anchor("2023/04/10 (Mon) 02:03") == "2023-04-10"
    assert refind_arm.parse_anchor("2024-05-01") == "2024-05-01"
    assert refind_arm.parse_anchor("sometime last spring") is None
    assert refind_arm.parse_anchor(None) is None


def test_empty_archive_serves_empty_context_without_calling_the_model():
    chat = _ScriptedChat(['{"queries": ["zebra"]}'])
    ctx, trace = refind_search(LexicalArchive([]), "q?", chat=chat, rounds=2,
                               top_k=6)
    assert ctx == "" and trace["served"] == 0
    assert chat.prompts == []
