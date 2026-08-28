"""Chronicle extraction + serving + rollback (schema v28, Phase 2 of the
2026-08-03 aggregation-aware-recall design).

The dream pass, when ``memory.dream.chronicle`` is on, routes extractor
events (dicts with ``kind: "event"`` riding the same batched call) into
``chronicle_events``: literal-gated like claims, date accepted only when
the batch corpus actually contains date information (fabrication guard),
exact-deduped, journaled as kind ``"event"`` for rollback-by-delete.
Serving: ``memory_search`` gains an ``events`` block on temporally-cued
queries. Extraction is ON by default since the 2026-08-12 soak review
(ev2 gates + 08-05..08-12 production soak); the knob remains for opt-out.

PG-backed service tests skip without the bench server.
"""
from __future__ import annotations

import pytest

from tests.dream_helpers import StubExtractor as _Stub
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.dream import events_from_parsed


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


def _event(description, date=None, phrase=None, source=0, actor="user"):
    return {"kind": "event", "description": description, "actor": actor,
            "date": date, "date_phrase": phrase, "source": source}


def _chronicle_rows(svc):
    return svc._storage.conn.execute(
        "SELECT description, to_char(occurred_at, 'YYYY-MM-DD'), "
        "occurred_phrase FROM chronicle_events ORDER BY id").fetchall()


# ── the bitemporal record shape ──────────────────────────────────────────
# Direct SQL: the storability and ordering contracts the serving layer
# assumes, pinned independently of whether any extractor happens to emit
# an undated or an invalidated event.


def test_occurred_at_is_nullable_and_orders_after_dated_rows(pg_conn):  # noqa: F811
    """Undated events (phrase-only) must be storable and sort AFTER dated
    ones under the serving order (occurred_at ASC NULLS LAST)."""
    pg_conn.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm) "
        "VALUES ('2023-05-14', 'on May 14', 1.0, 'user', 'user', "
        "'adopted a kitten', 'adopted a kitten')")
    pg_conn.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm) "
        "VALUES (NULL, 'a while back', 2.0, 'user', 'user', "
        "'visited Lisbon', 'visited lisbon')")
    pg_conn.commit()
    rows = pg_conn.execute(
        "SELECT description, occurred_at FROM chronicle_events "
        "ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC").fetchall()
    assert [r[0] for r in rows] == ["adopted a kitten", "visited Lisbon"]
    assert rows[1][1] is None


def test_invalidated_at_defaults_null_and_round_trips(pg_conn):  # noqa: F811
    """Contradiction handling is additive-only: invalidate, never delete."""
    pg_conn.execute(
        "INSERT INTO chronicle_events (occurred_phrase, recorded_at, actor, "
        "actor_norm, description, description_norm) "
        "VALUES ('yesterday', 3.0, 'user', 'user', 'sold the road bike', "
        "'sold the road bike')")
    pg_conn.commit()
    ev_id, inv = pg_conn.execute(
        "SELECT id, invalidated_at FROM chronicle_events "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert inv is None
    pg_conn.execute(
        "UPDATE chronicle_events SET invalidated_at = 4.0 WHERE id = %s",
        (ev_id,))
    pg_conn.commit()
    inv = pg_conn.execute(
        "SELECT invalidated_at FROM chronicle_events WHERE id = %s",
        (ev_id,)).fetchone()[0]
    assert inv == 4.0


# ── the pure parser ──────────────────────────────────────────────────────

def test_events_from_parsed_validates_shape():
    parsed = {"events": [
        {"description": "adopted a kitten", "actor": "user",
         "date": "2023-05-13", "date_phrase": "yesterday", "source": 1},
        {"description": "", "date": "2023-05-13"},          # no description
        {"description": "vague trip", "date": "May 2023"},  # bad date form
        {"description": "moved house", "source": 99},       # source OOB
        "not-a-dict",
    ]}
    out = events_from_parsed(parsed, 2)
    assert [e["description"] for e in out] == [
        "adopted a kitten", "vague trip", "moved house"]
    assert out[0]["kind"] == "event"
    assert out[0]["date"] == "2023-05-13" and out[0]["source"] == 0
    assert out[1]["date"] is None
    assert "source" not in out[2]


def test_events_from_parsed_handles_absent_key():
    assert events_from_parsed({"claims": []}, 3) == []
    assert events_from_parsed({"events": "nope"}, 3) == []


# ── the dream write path ─────────────────────────────────────────────────

def test_chronicle_on_by_default_writes_events(svc):
    # Default-on since 2026-08-12: ev2 gates all passed
    # (evals/results/ev2-separate-pass-verdict.json) and the 08-05..08-12
    # production soak reviewed clean (188 events, 0 wrong dates).
    assert svc.config.memory.dream.chronicle is True
    svc.store("[2023/05/14 (Sun) 10:02] user: adopted the kitten yesterday",
              source="notes")
    out = svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="yesterday")]))
    assert out["events_inserted"] == 1
    assert len(_chronicle_rows(svc)) == 1


def test_chronicle_explicit_off_ignores_events(svc):
    svc.config.memory.dream.chronicle = False
    svc.store("[2023/05/14 (Sun) 10:02] user: adopted the kitten yesterday",
              source="notes")
    out = svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="yesterday")]))
    assert out["events_inserted"] == 0
    assert _chronicle_rows(svc) == []


def test_event_written_journaled_and_counted(svc):
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14 (Sun) 10:02] user: adopted the kitten yesterday",
              source="notes")
    out = svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="yesterday")]))
    assert out["events_inserted"] == 1
    rows = _chronicle_rows(svc)
    assert rows == [("adopted a kitten", "2023-05-13", "yesterday")]
    runs = svc._storage.recent_dream_runs(limit=5)
    assert len(runs) == 1 and runs[0]["status"] == "committed"
    assert runs[0]["tallies"].get("events_inserted") == 1
    journal = svc._storage.dream_run_journal(runs[0]["id"])
    ev_rows = [r for r in journal if r["kind"] == "event"]
    assert len(ev_rows) == 1
    assert ev_rows[0]["action"] == "event_inserted"
    assert ev_rows[0]["chronicle_event_id"] is not None
    assert ev_rows[0]["new_value"] == "adopted a kitten"


def test_event_date_needs_grounding_in_batch(svc):
    """A dated event from a batch with NO date information stores undated
    (phrase kept) — the extractor cannot have resolved a real date."""
    svc.config.memory.dream.chronicle = True
    svc.store("user: adopted the kitten recently", source="notes")
    out = svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="recently")]))
    assert out["events_inserted"] == 1
    rows = _chronicle_rows(svc)
    assert rows == [("adopted a kitten", None, "recently")]


def test_event_description_is_literal_gated(svc):
    svc.config.memory.dream.chronicle = True
    assert svc.config.memory.dream.literal_gate == "enforce"
    svc.store("[2023/05/14] user: bought a new laptop", source="notes")
    out = svc.dream_run(_Stub([
        _event("bought a laptop for $4500", date="2023-05-14",
               phrase="on May 14")]))
    assert out["events_inserted"] == 0
    assert out["literal_dropped"] == 1
    assert _chronicle_rows(svc) == []


def test_event_dedup_is_not_rejournaled(svc):
    svc.config.memory.dream.chronicle = True
    ev = _event("adopted a kitten", date="2023-05-13", phrase="yesterday")
    svc.store("[2023/05/14] user: adopted the kitten yesterday",
              source="notes")
    out1 = svc.dream_run(_Stub([ev]))
    svc.store("[2023/05/15] user: as I said we adopted the kitten on the "
              "13th", source="notes")
    out2 = svc.dream_run(_Stub([ev]))
    assert out1["events_inserted"] == 1
    assert out2["events_inserted"] == 0 and out2["events_duplicate"] == 1
    assert len(_chronicle_rows(svc)) == 1
    runs = svc._storage.recent_dream_runs(limit=5)
    ev_rows = [r for run in runs
               for r in svc._storage.dream_run_journal(run["id"])
               if r["kind"] == "event"]
    assert len(ev_rows) == 1  # the duplicate journaled nothing


def test_malformed_event_is_skipped_not_crashed(svc):
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14] user: adopted the kitten", source="notes")
    out = svc.dream_run(_Stub([
        {"kind": "event", "actor": "user"},   # no description
        _event("adopted a kitten", date="2023-05-14", phrase="May 14")]))
    assert out["events_inserted"] == 1
    assert len(_chronicle_rows(svc)) == 1


# ── rollback ─────────────────────────────────────────────────────────────

def test_rollback_deletes_journaled_event(svc):
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14] user: adopted the kitten yesterday",
              source="notes")
    svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="yesterday"),
        {"entity": "user", "attribute": "pet", "value": "kitten",
         "confidence": 0.8, "origin": "agent", "source": 0}]))
    assert len(_chronicle_rows(svc)) == 1
    res = svc.dream_rollback()
    assert "error" not in res
    ev_detail = [d for d in res["details"] if d["action"] == "event_inserted"]
    assert ev_detail and ev_detail[0]["outcome"] == "reverted"
    assert _chronicle_rows(svc) == []


# ── serving ──────────────────────────────────────────────────────────────

def test_search_serves_events_on_temporal_cue(svc):
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14] user: adopted the kitten yesterday",
              source="notes")
    svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="yesterday"),
        _event("kitten's first vet visit", date="2023-06-01",
               phrase="June 1st", source=0)]))
    out = svc.search("when did I adopt the kitten?")
    assert "events" in out
    assert [e["description"] for e in out["events"]] == [
        "adopted a kitten", "kitten's first vet visit"]
    assert out["events"][0]["date"] == "2023-05-13"
    assert out["events"][0]["phrase"] == "yesterday"


def test_search_without_cue_serves_no_events_block(svc):
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/14] user: adopted the kitten yesterday",
              source="notes")
    svc.dream_run(_Stub([
        _event("adopted a kitten", date="2023-05-13", phrase="yesterday")]))
    out = svc.search("tell me about the kitten")
    assert "events" not in out


# ── aggregation-cued serving (2026-08-06-aggregation-serving-design.md) ──

def test_has_aggregation_cue_is_separate_from_temporal():
    """The aggregation predicate must NOT ride _TEMPORAL_CUE_RE — that
    regex also fires the (gate-failed) timeline channel."""
    from pseudolife_memory.memory.cms import (
        has_aggregation_cue, has_temporal_cue,
    )
    for q in ("how many books did I mention?", "how much did I spend?",
              "how often do I run?", "what percentage were done?",
              "in total, what did it cost?", "the total number of trips",
              "altogether how big?", "each time I visited",
              "every time we met"):
        assert has_aggregation_cue(q), q
        assert not has_temporal_cue(q), f"temporal RE must not widen: {q}"
    for q in ("count the money", "the total was fine",
              "all the things I like", "what is my favorite color?"):
        assert not has_aggregation_cue(q), q


def test_aggregation_cue_widening_covers_audited_phrasings():
    """Regression lock for the five cue-miss rows in
    events-coverage-audit-0806.json: 'total amount', 'total distance',
    'average', and 'the most' phrasings fired nothing while the sessions
    held the needed instances."""
    from pseudolife_memory.memory.cms import (
        has_aggregation_cue, has_temporal_cue,
    )
    for q in ("What is the total amount I spent on luxury items?",
              "What is the total distance of the hikes I did?",
              "What is the total cost of my orders?",
              "What is the average GPA of my studies?",
              "Which grocery store did I spend the most money at?"):
        assert has_aggregation_cue(q), q
        assert not has_temporal_cue(q), f"temporal RE must not widen: {q}"
    # Guards: bare 'total' without a quantity noun, and 'mostly', stay out.
    for q in ("the total was fine", "that room was mostly empty",
              "I totally agree"):
        assert not has_aggregation_cue(q), q


def _eight_events():
    # Digit-free descriptions: the enforce-mode literal gate correctly
    # drops fabricated numbers absent from the batch corpus.
    names = ("alpha", "bravo", "charlie", "delta",
             "echo", "foxtrot", "golf", "hotel")
    return [_event(f"went climbing at the {n} wall",
                   date=f"2023-05-{11+i:02d}", phrase=f"the {n} day")
            for i, n in enumerate(names)]


def test_aggregation_cue_serves_full_list_with_total(svc):
    """A counting question needs the whole set (limit 30, not 6) plus a
    computed list-length total the answerer can rely on for arithmetic."""
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/20] user: climbing log for May", source="notes")
    svc.dream_run(_Stub(_eight_events()))
    out = svc.search("how many climbing sessions did I do?")
    assert len(out["events"]) == 8
    assert out["events_total"] == 8


def test_temporal_only_cue_keeps_the_six_event_cap(svc):
    """Temporal-only queries are byte-identical to the shipped behavior:
    same gate, same limit-6 prefix, no total field."""
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/20] user: climbing log for May", source="notes")
    svc.dream_run(_Stub(_eight_events()))
    out = svc.search("when did I go climbing?")
    assert len(out["events"]) == 6
    assert "events_total" not in out


# ── explicit-date cue (2026-08-12 soak-review finding) ───────────────────

def test_has_date_cue_is_separate_from_temporal():
    """An explicit calendar date is a temporal cue for event serving, but
    must NOT ride _TEMPORAL_CUE_RE — that regex also fires the
    (gate-failed) timeline channel. Same isolation rule as the
    aggregation predicate above."""
    from pseudolife_memory.memory.cms import has_date_cue, has_temporal_cue
    for q in ("what happened on 2026-08-08?",
              "what did we ship on 2026-8-5?",
              "show the deploys from 2026/08/09"):
        assert has_date_cue(q), q
        assert not has_temporal_cue(q), f"temporal RE must not widen: {q}"
    for q in ("what is my favorite color?",
              "issue #2026 was closed",         # bare number, not a date
              "the 08-08 build broke",          # month-day only: ambiguous
              "call 0412-345-678 about it"):    # phone-number shape
        assert not has_date_cue(q), q


def test_iso_date_query_serves_events(svc):
    """Soak-review finding (2026-08-12): 'what happened on <ISO date>…?'
    carries no _TEMPORAL_CUE_RE word, so the events channel never fired
    on the strongest possible temporal cue (the live miss had a matching
    content word; only the cue gate blocked serving). The date predicate
    widens the serving condition only — cap and ordering match a
    temporal cue. Matching stays lexical, so a content word is still
    required; date-window matching against occurred_at is out of scope."""
    svc.config.memory.dream.chronicle = True
    svc.store("[2023/05/20] user: climbing log for May", source="notes")
    svc.dream_run(_Stub(_eight_events()))
    out = svc.search("what happened on 2023-05-12 at the climbing wall?")
    assert len(out["events"]) == 6
    assert "events_total" not in out
