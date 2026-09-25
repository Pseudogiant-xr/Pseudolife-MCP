"""Fixture tests for the serving-policy replay (evals/serving_policy_replay.py).

CPU only, no database, no model: the replay core is pure, and the one DB
seam (``fetch``) is exercised against a fake connection that records what
it was asked to run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import serving_policy_replay as spr  # noqa: E402

T0 = 1_790_000_000.0
S1 = "a" * 32                                   # Claude shim session
S2 = "01a0cd96-99c5-7d42-b881-871c1e8c2277"     # Codex UUID session


def _ev(eid, ranks_used=(), *, n=8, top_k=8, ts=None, session=S1,
        warm=False, sources=None, ids=None, scores=None, superseded=()):
    ids = list(ids) if ids is not None else [eid * 100 + r for r in range(n)]
    rows = [spr.Row(entry_id=i, rank=r,
                    score=(scores[r] if scores else 0.9 - 0.05 * r),
                    superseded=r in superseded)
            for r, i in enumerate(ids)]
    return spr.Event(
        id=eid, session_id=session,
        created_at=T0 + 60 * eid if ts is None else ts,
        is_warmup=warm, top_k=top_k, sources_filter=sources, rows=rows,
        used={ids[r] for r in ranks_used})


def _entries(events, *, length=1000, source="pseudolife-mcp", overrides=None):
    out = {}
    for e in events:
        for r in e.rows:
            out[r.entry_id] = spr.EntryInfo(source=source, text_len=length)
    out.update(overrides or {})
    return out


# ── the agent-origin filter ──────────────────────────────────────────────

def test_the_daemon_warmup_probe_is_not_an_agent_search():
    kept, counts = spr.select_agent_events(
        [_ev(1), _ev(2, warm=True)], since=0)
    assert [e.id for e in kept] == [1]
    assert counts["dropped_warmup"] == 1


@pytest.mark.parametrize("session", [None, "", "review-agent-experience-x1",
                                     "verify-pr285-x2", "A" * 32])
def test_sessionless_and_hand_set_probe_sessions_are_dropped(session):
    kept, _ = spr.select_agent_events([_ev(1, session=session)], since=0)
    assert kept == []


@pytest.mark.parametrize("session", [S1, S2])
def test_client_issued_sessions_are_kept(session):
    kept, _ = spr.select_agent_events([_ev(1, session=session)], since=0)
    assert [e.id for e in kept] == [1]


def test_a_recall_burst_is_dropped_whole_and_spaced_searches_survive():
    burst = [_ev(i, ts=T0 + 2 * i) for i in (1, 2, 3)]   # gaps of 2 s
    pair = [_ev(10, ts=T0 + 500), _ev(11, ts=T0 + 502)]  # only two
    other = [_ev(20, ts=T0 + 4, session=S2)]            # other session
    kept, counts = spr.select_agent_events(burst + pair + other, since=0)
    assert sorted(e.id for e in kept) == [10, 11, 20]
    assert counts["dropped_recall_burst"] == 3


def test_a_burst_of_searches_with_served_facts_is_kept():
    """Recall never attaches the cortex block's facts; the MCP
    memory_search handler does. Parallel searches (subagents share their
    parent's MCP session) trip the timing rule but carry facts."""
    burst = [_ev(i, (0,), ts=T0 + i) for i in (1, 2, 3)]
    burst[0].fact_scores = [0.5]
    burst[1].fact_scores = [0.4]
    kept, counts = spr.select_agent_events(burst, since=0)
    assert sorted(e.id for e in kept) == [1, 2]
    assert counts["dropped_recall_burst"] == 1
    assert counts["burst_kept_with_facts"] == 2
    assert counts["burst_kept_with_facts_labelled"] == 2


def test_a_burst_needs_every_gap_inside_the_limit():
    evs = [_ev(1, ts=T0), _ev(2, ts=T0 + 2), _ev(3, ts=T0 + 6)]
    assert spr.burst_ids(evs) == set()
    assert spr.burst_ids(evs, gap_s=4.0) == {1, 2, 3}


def test_the_2026_09_23_review_probes_are_excluded_by_id():
    # The probes the review's REPORT.md names, plus ids from inside its
    # window that belong to OTHER sessions or to a warmup, which must not
    # be swallowed by the ranges.
    for eid in (3660, 3661, 3662, 3663, 3697, 3634, 3664, 3607, 3706):
        assert eid in spr.REVIEW_2026_09_23
    for eid in (3606, 3611, 3669, 3701, 3704, 3707):
        assert eid not in spr.REVIEW_2026_09_23
    inside = spr.parse_ts("2026-09-23T05:19:20Z")
    kept, counts = spr.select_agent_events(
        [_ev(3697, ts=inside), _ev(3701, ts=inside + 400)], since=0)
    assert [e.id for e in kept] == [3701]
    assert counts["dropped_review_2026_09_23"] == 1
    # The same id outside the review's window is another bank's event.
    kept, _ = spr.select_agent_events([_ev(3697, ts=T0)], since=0)
    assert [e.id for e in kept] == [3697]


def test_the_window_is_half_open_and_counts_labelled_drops():
    evs = [_ev(1, ts=T0 - 1), _ev(2, (0,), ts=T0, warm=True),
           _ev(3, ts=T0 + 10), _ev(4, ts=T0 + 20)]
    kept, counts = spr.select_agent_events(evs, since=T0, until=T0 + 20)
    assert [e.id for e in kept] == [3]
    assert counts["events_in_window"] == 2
    assert counts["dropped_warmup_labelled"] == 1


# ── width simulation ─────────────────────────────────────────────────────

def _logged(eid, rows, *, top_k=8, pool_size=8, bm25_weight=0.3,
            used=()):
    """A width-``top_k`` event whose rows are (entry_id, cosine, bm25,
    superseded, channel), logged in served (score) order as cms.retrieve
    computes it: cosine x 0.55 if superseded, + weight x bm25; slot rows
    carry their own score in the cosine position."""
    built = []
    for eid_, cos, bm, sup, ch in rows:
        if ch == "dense":
            score = cos * (0.55 if sup else 1.0) + bm25_weight * bm
            built.append((score, spr.Row(eid_, 0, score, sup, cos, ch, bm)))
        else:
            built.append((cos, spr.Row(eid_, 0, cos, sup, None, ch, bm)))
    built.sort(key=lambda t: -t[0])
    served = [spr.Row(r.entry_id, i, r.score, r.superseded, r.dense,
                      r.channel, r.bm25) for i, (_, r) in enumerate(built)]
    return spr.Event(id=eid, session_id=S1, created_at=T0, is_warmup=False,
                     top_k=top_k, sources_filter=None, rows=served,
                     used=set(used), pool_size=pool_size,
                     bm25_weight=bm25_weight, bm25_min=0.1, simulable=True)


def test_a_narrower_search_is_not_a_prefix_of_a_wider_one():
    """The review's prefix method keeps the entry at cosine rank 6 that a
    width-6 search never pools, and drops the superseded entry at cosine
    rank 0 that width 8 merely pushed below its cut."""
    ev = _logged(1, [
        (10, 0.90, 0.0, True, "dense"),     # superseded: 0.495 as served
        (11, 0.85, 0.0, False, "dense"), (12, 0.80, 0.0, False, "dense"),
        (13, 0.75, 0.0, False, "dense"), (14, 0.70, 0.0, False, "dense"),
        (15, 0.65, 0.0, False, "dense"), (16, 0.62, 0.0, False, "dense"),
        (17, 0.60, 0.0, False, "dense")], used=(10, 16))
    assert [r.entry_id for r in ev.rows][-1] == 10     # served last at 8
    sim, simulated = spr.simulate_width(ev, 6)
    assert simulated
    assert [r.entry_id for r in sim] == [11, 12, 13, 14, 15, 10]
    prefix, _ = spr.simulate_width(ev, 6, "prefix")
    assert [r.entry_id for r in prefix] == [11, 12, 13, 14, 15, 16]
    rows = spr.policy_table([ev], {}, [spr.Policy("k6", top_k=6),
                                       spr.Policy("p", top_k=6,
                                                  width_method="prefix")],
                            default_top_k=8, text_cap=600, reps=0, seed=1)
    assert [(r["used_hits"], r["used_kept"]) for r in rows] == [(2, 1),
                                                                (2, 1)]
    assert rows[0]["rows_kept"] == 6 and rows[0]["events_prefix_fallback"] == 0


def test_a_lexical_hit_leaving_the_pool_returns_as_an_injection():
    """An entry pushed out of the dense pool keeps only its BM25-only
    score, ``weight x normalised``; one with no lexical score is gone."""
    sup = [(20 + i, 0.54 - 0.01 * i, 0.0, True, "dense") for i in range(6)]
    ev = _logged(2, sup + [(30, 0.48, 1.0, False, "dense"),
                           (31, 0.47, 0.0, False, "dense")])
    assert ev.rows[0].entry_id == 30                   # 0.78 at width 8
    sim, _ = spr.simulate_width(ev, 6)
    ids = [r.entry_id for r in sim]
    assert ids[0] == 30 and 31 not in ids and 25 not in ids
    assert (sim[0].score, sim[0].channel) == (0.3, "bm25")


def test_the_pessimistic_bound_moves_logged_rows_down_the_cosine_order():
    """Pool members the wider cut dropped are not logged, so a logged row's
    cosine rank can be understated; the bound assumes every unlogged pool
    slot outranks every logged row."""
    ev = _logged(4, [(50 + i, 0.9 - 0.05 * i, 0.0, False, "dense")
                     for i in range(7)] + [(60, 0.95, 0.0, False, "slot")])
    sim, _ = spr.simulate_width(ev, 6)
    low, _ = spr.simulate_width(ev, 6, "pessimistic")
    assert len([r for r in sim if r.channel == "dense"]) == 5
    assert len([r for r in low if r.channel == "dense"]) == 5
    assert [r.entry_id for r in low][-1] == 54     # one fewer deep row...
    ev.pool_size = 10                              # ...once 3 are unlogged
    low, _ = spr.simulate_width(ev, 6, "pessimistic")
    assert [r.entry_id for r in low] == [60, 50, 51, 52]


def test_the_simulation_is_scored_against_real_narrower_searches():
    ev = _logged(6, [
        (10, 0.90, 0.0, True, "dense"),
        (11, 0.85, 0.0, False, "dense"), (12, 0.80, 0.0, False, "dense"),
        (13, 0.75, 0.0, False, "dense")], top_k=4)
    # A real width-2 search served the two highest-cosine entries.
    check = spr.simulation_check([(ev, 2, [10, 11])])
    assert (check["simulate_exact_set"], check["prefix_exact_set"]) == (1, 0)
    assert check["simulate_rows_matched"] == 2
    assert check["prefix_rows_matched"] == 1      # prefix served 11, 12
    assert check["disagree_simulate_exact"] == 1
    assert check["widths"] == {"4->2": 1}


def test_an_unsimulable_search_falls_back_to_the_prefix_and_is_counted():
    ev = _logged(5, [(70 + i, 0.9 - 0.05 * i, 0.0, False, "dense")
                     for i in range(8)], used=(77,))
    ev.simulable = False                       # say, the reranker fired
    row = spr.policy_table([ev], {}, [spr.Policy("k6", top_k=6)],
                           default_top_k=8, text_cap=600, reps=0,
                           seed=1)[0]
    assert row["events_prefix_fallback"] == 1
    assert (row["used_hits"], row["used_kept"]) == (1, 0)


# ── policies ─────────────────────────────────────────────────────────────

def test_a_top_k_default_cuts_only_default_width_searches():
    default = _ev(1, (0, 6))                     # used rank 6 at width 8
    explicit = _ev(2, (0, 7), n=10, top_k=10)    # caller asked for 10
    rows = spr.policy_table(
        [default, explicit], _entries([default, explicit]),
        [spr.Policy("k6", top_k=6)], default_top_k=8, text_cap=600,
        reps=0, seed=1)[0]
    assert (rows["used_hits"], rows["used_kept"]) == (4, 3)
    assert rows["rows"] == 18 and rows["rows_kept"] == 16
    assert rows["events_changed"] == 1


def test_digest_exclusion_spares_a_caller_who_asked_for_digests():
    unfiltered = _ev(1, (1,))
    asked = _ev(2, (1,), sources=["digest"])
    ents = _entries([unfiltered, asked], overrides={
        101: spr.EntryInfo("digest", 500), 201: spr.EntryInfo("digest", 500)})
    row = spr.policy_table([unfiltered, asked], ents,
                           [spr.Policy("nd", exclude_sources=("digest",))],
                           default_top_k=8, text_cap=600, reps=0, seed=1)[0]
    assert (row["used_hits"], row["used_kept"]) == (2, 1)
    assert row["rows_kept"] == 15


def test_a_score_floor_drops_rows_below_it():
    ev = _ev(1, (0, 3), n=4, scores=[0.8, 0.6, 0.5, 0.4])
    row = spr.policy_table([ev], _entries([ev]),
                           [spr.Policy("f", min_score=0.5)],
                           default_top_k=8, text_cap=600, reps=0, seed=1)[0]
    assert (row["used_kept"], row["rows_kept"]) == (1, 3)


def test_chars_price_the_capped_text_the_projection_serves():
    assert spr.served_text_chars(spr.EntryInfo("s", 1000), 600) == 601
    assert spr.served_text_chars(spr.EntryInfo("s", 600), 600) == 600
    assert spr.served_text_chars(None, 600) is None
    ev = _ev(1, n=2)
    ents = {100: spr.EntryInfo("s", 1000)}      # entry 101 was evicted
    row = spr.policy_table([ev], ents, [spr.Policy("as")], default_top_k=8,
                           text_cap=600, reps=0, seed=1)[0]
    assert row["entry_text_chars"] == 601
    assert row["rows_without_length"] == 1


def test_the_text_cap_defaults_to_the_mcp_projection_cap():
    from pseudolife_memory.utils.config import McpConfig
    ev = _ev(1, (0,), n=1)
    rep = spr.build_report([ev], _entries([ev], length=5000), since=0,
                           until=None, reps=0)
    assert rep["config"]["entry_text_cap"] == McpConfig().entry_text_chars
    as_served = rep["strata"]["all_agent"]["policies"][0]
    assert as_served["entry_text_chars"] == McpConfig().entry_text_chars + 1


def test_wilson_interval_matches_the_textbook_value():
    lo, hi = spr.wilson(9, 10)
    assert round(lo, 3) == 0.596 and round(hi, 3) == 0.982
    assert spr.wilson(0, 0) is None


# ── digests ──────────────────────────────────────────────────────────────

def test_digest_use_is_compared_at_the_same_rank():
    evs = [_ev(1, (0,), n=4), _ev(2, (3,), n=4), _ev(3, (0, 3), n=4),
           _ev(4, (0,), n=4)]
    digest = {100, 200, 203}
    ents = _entries(evs, overrides={
        i: spr.EntryInfo("digest", 400) for i in digest})
    rm = spr.digest_rank_matched(evs, ents, live_only=False, reps=0, seed=1)
    assert rm["digest_rows"] == 3 and rm["digest_used"] == 2
    # Rank 0: digests 100 (used) and 200 (unused); others 300 and 400 both
    # used -> the non-digest rate there is 1.0, so 2 digest uses expected.
    # Rank 3: digest 203 (used); others 103, 303 (used), 403 -> rate 1/3,
    # so 1/3 of a use expected. Observed 2 against 2 + 1/3 expected.
    # Ranks 1-2 serve no digest and contribute nothing.
    assert rm["rank_matched_ratio"] == round(2 / (2 * 1.0 + 1 * (1 / 3)), 3)


def test_digest_comparison_ignores_filtered_searches():
    ev = _ev(1, (0,), sources=["digest"])
    ents = _entries([ev], source="digest")
    rm = spr.digest_rank_matched([ev], ents, live_only=False, reps=0, seed=1)
    assert rm["digest_rows"] == 0


# ── abstention ───────────────────────────────────────────────────────────

def _scored(eid, scores, dense, facts):
    ev = _ev(eid, n=len(scores), scores=scores)
    ev.rows = [spr.Row(r.entry_id, r.rank, r.score, False, d)
               for r, d in zip(ev.rows, dense)]
    ev.fact_scores = list(facts)
    return ev


def test_abstention_prices_the_flag_the_way_the_mcp_layer_sets_it():
    evs = [
        _scored(1, [], [], []),                       # nothing at all
        _scored(2, [], [], [0.5]),                    # facts only
        _scored(3, [0.8, 0.6], [0.62, 0.41], []),     # a strong entry
        _scored(4, [0.55], [0.55], [0.3]),            # weak entry + fact
    ]
    evs[3].used = {evs[3].rows[0].entry_id}           # ...that was used
    absent = _scored(3660, [0.49], [0.49], [0.58])
    rep = spr.abstention_report(evs, [absent])
    assert (rep["served_no_entries"], rep["served_nothing"]) == (2, 1)
    cells = {(g["search_confidence_floor"], g["guard_min_score"]): g
             for g in rep["floor_grid"]}
    grid = {k: g["flagged"] for k, g in cells.items()}
    # The old recommended pair drops every fact under 0.65, so a weak
    # search flags even when facts were served.
    assert grid[(0.70, 0.65)] == 3
    assert grid[(0.70, 0.2)] == 1      # the fact at 0.3/0.5 suppresses it
    assert grid[(0.50, 0.2)] == 1      # 0.55 clears a 0.5 floor
    # The floor reads the FUSED served score: a 0.8 fused hit is never
    # flagged at 0.70 even though its dense cosine is 0.62.
    assert not spr._flagged(evs[2], 0.70, 0.65)
    # Flagging a search whose hit was then used is a false abstention.
    assert cells[(0.70, 0.65)]["flagged_labelled"] == 1
    assert cells[(0.70, 0.2)]["flagged_labelled"] == 0
    assert cells[(0.70, 0.65)]["absent_probes_flagged"] == 1
    assert cells[(0.70, 0.2)]["absent_probes_flagged"] == 0
    assert rep["absent_answer_probes"]["top_dense_cosine"] == [0.49]
    assert rep["top_dense_cosine"]["searches"] == 2
    assert rep["lowest_served_dense_cosine"]["below_0.30"] == 0
    assert (rep["served_facts"], rep["served_facts_below_0.65"]) == (2, 2)


# ── privacy + read-only ──────────────────────────────────────────────────

def test_the_report_carries_aggregates_only():
    secret_session = "c0ffee" + "0" * 26
    evs = [_ev(1, (0,), session=secret_session), _ev(2, (1,), session=S2)]
    rep = spr.build_report(evs, _entries(evs), since=0, until=None, reps=50)
    blob = json.dumps(rep)
    assert secret_session not in blob and S2 not in blob
    for key in ('"query_text"', '"text"', '"session_id"', '"entry_id"'):
        assert key not in blob


def test_the_sql_reads_no_text_and_never_touches_meta():
    sql = " ".join((spr._EVENTS_SQL, spr._USES_SQL, spr._ENTRIES_SQL))
    assert "meta" not in sql.lower().split()
    assert "length(text)" in spr._ENTRIES_SQL
    assert "SELECT id, source, length(text) FROM entries" == spr._ENTRIES_SQL
    # query_text is only ever COMPARED, never selected.
    assert spr._EVENTS_SQL.count("query_text") == 1
    assert "(query_text = %(warmup)s)" in spr._EVENTS_SQL
    # The pair finder compares query texts and selects none of them.
    select = spr._PAIRS_SQL.split("FROM retrieval_events w")[0]
    assert "query_text" not in select


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, read_only="on"):
        self.sql: list[str] = []
        self._ro = read_only

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql.strip())
        if sql.startswith("SHOW transaction_read_only"):
            return _FakeCursor([(self._ro,)])
        if "current_database" in sql:
            return _FakeCursor([("pseudolife_memory",)])
        if "JOIN retrieval_events n" in sql:
            wide = [{"entry_id": 8, "rank": 0, "score": 0.9,
                     "components": {"channel": "dense", "dense": 0.9,
                                    "bm25": 0.0}},
                    {"entry_id": 9, "rank": 1, "score": 0.8,
                     "components": {"channel": "dense", "dense": 0.8,
                                    "bm25": 0.0}}]
            return _FakeCursor([(2, S1, T0, False, wide,
                                 {"top_k": 2, "filters": {}}, None, 1, [8])])
        if "FROM retrieval_events" in sql and "retrieval_uses" not in sql:
            return _FakeCursor([(1, S1, T0, False, [
                {"entry_id": 7, "rank": 0, "score": 0.8,
                 "components": {"supersession_mult": 0.55, "dense": 0.61}}],
                {"top_k": 8, "filters": {"sources": None}},
                [{"entity_norm": "e", "score": 0.52}])])
        if "FROM retrieval_uses" in sql:
            return _FakeCursor([(1, 7)])
        if "FROM entries" in sql:
            return _FakeCursor([(7, "status", 42)])
        return _FakeCursor([])


def _patch_connect(monkeypatch, conn, seen):
    import types

    def connect(dsn, **kw):
        seen.update(kw)
        return conn
    monkeypatch.setitem(sys.modules, "psycopg",
                        types.SimpleNamespace(connect=connect))


def test_fetch_reads_inside_a_read_only_transaction(monkeypatch):
    conn, seen = _FakeConn(), {}
    _patch_connect(monkeypatch, conn, seen)
    events, entries, db, pairs = spr.fetch("postgresql://x/y", T0 - 1,
                                           None)
    assert [(p[0].id, p[1], p[2]) for p in pairs] == [(2, 1, [8])]
    assert pairs[0][0].simulable        # pre-knob params: the shipped shape
    assert "default_transaction_read_only=on" in seen["options"]
    assert conn.sql[0] == "BEGIN READ ONLY"
    assert conn.sql[1] == "SHOW transaction_read_only"
    assert conn.sql[-1] == "ROLLBACK"
    assert events[0].used == {7} and events[0].rows[0].superseded
    assert events[0].rows[0].dense == 0.61
    assert events[0].fact_scores == [0.52]
    assert entries[7] == spr.EntryInfo("status", 42)


def test_fetch_refuses_a_writable_transaction(monkeypatch):
    conn, seen = _FakeConn(read_only="off"), {}
    _patch_connect(monkeypatch, conn, seen)
    with pytest.raises(SystemExit):
        spr.fetch("postgresql://x/y", T0 - 1, None)
    assert not any("FROM retrieval_events" in s for s in conn.sql)
    assert conn.sql[-1] == "ROLLBACK"


def test_main_refuses_to_overwrite_an_artifact(tmp_path):
    out = tmp_path / "a.json"
    out.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        spr.main(["--out", str(out)])
    assert out.read_text(encoding="utf-8") == "{}"
