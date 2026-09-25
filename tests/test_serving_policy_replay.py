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
    kept, counts = spr.select_agent_events(
        [_ev(3697, ts=T0), _ev(3701, ts=T0 + 100)], since=0)
    assert [e.id for e in kept] == [3701]
    assert counts["dropped_review_2026_09_23"] == 1


def test_the_window_is_half_open_and_counts_labelled_drops():
    evs = [_ev(1, ts=T0 - 1), _ev(2, (0,), ts=T0, warm=True),
           _ev(3, ts=T0 + 10), _ev(4, ts=T0 + 20)]
    kept, counts = spr.select_agent_events(evs, since=T0, until=T0 + 20)
    assert [e.id for e in kept] == [3]
    assert counts["events_in_window"] == 2
    assert counts["dropped_warmup_labelled"] == 1


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
        if "FROM retrieval_events" in sql and "retrieval_uses" not in sql:
            return _FakeCursor([(1, S1, T0, False, [
                {"entry_id": 7, "rank": 0, "score": 0.8,
                 "components": {"supersession_mult": 0.55}}],
                {"top_k": 8, "filters": {"sources": None}})])
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
    events, entries, db = spr.fetch("postgresql://x/y", T0 - 1, None)
    assert "default_transaction_read_only=on" in seen["options"]
    assert conn.sql[0] == "BEGIN READ ONLY"
    assert conn.sql[1] == "SHOW transaction_read_only"
    assert conn.sql[-1] == "ROLLBACK"
    assert events[0].used == {7} and events[0].rows[0].superseded
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
