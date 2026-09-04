"""Fixture tests for the retrieval telemetry review + offline replay.

CPU only, no database, no model: every function under test is pure, and
the DB/service seams (`fetch`, `build_service`, `run_arm`) are exercised
through injected fakes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import retrieval_replay as rr  # noqa: E402
import retrieval_telemetry_review as rtr  # noqa: E402


def _event(eid, served_ids, *, ts=1_780_000_000.0, session="s1",
           served_facts=None, params=None, query="q"):
    return {
        "id": eid, "query_text": query, "session_id": session,
        "episode_id": f"ep{eid}", "created_at": ts,
        "served": [{"entry_id": i, "rank": r, "score": 1.0 - 0.1 * r}
                   for r, i in enumerate(served_ids)],
        "served_facts": served_facts, "params": params,
    }


# ── guards ────────────────────────────────────────────────────────────────

# libpq accepts two DSN spellings and neither is case-sensitive in the
# database name; the 2026-09-04 review found the original guard matched only
# a lower-case URI path segment, so `dbname=pseudolife_memory` and a trailing
# slash both walked straight through onto the live bank.
_DSN_FORMS = [
    "postgresql://u:p@127.0.0.1:5433/{db}",
    "postgresql://u:p@127.0.0.1:5433/{db}/",
    "postgresql://u:p@127.0.0.1:5433/{DB}",
    "postgresql://u:p@127.0.0.1:5433/{db}?sslmode=disable",
    "host=127.0.0.1 port=5433 dbname={db}",
    "dbname={DB} user=u",
]


@pytest.mark.parametrize("dsn_form", _DSN_FORMS)
@pytest.mark.parametrize("db", ["pseudolife_memory", "pseudolife_memory_bench"])
@pytest.mark.parametrize("guard", [rr.guard_dsn, rtr.guard_dsn])
def test_guard_refuses_live_and_bench_dbs(guard, db, dsn_form):
    with pytest.raises(SystemExit):
        guard(dsn_form.format(db=db, DB=db.upper()))


@pytest.mark.parametrize("guard", [rr.guard_dsn, rtr.guard_dsn])
def test_guard_allows_a_replay_copy(guard):
    guard("postgresql://u:p@127.0.0.1:5433/pseudolife_memory_replay_20260904")
    guard("host=127.0.0.1 dbname=pseudolife_memory_replay_20260904")


# ── telemetry review ──────────────────────────────────────────────────────

def test_summarize_events_counts_days_sessions_and_coverage():
    day2 = 1_780_000_000.0 + 86_400 * 2
    events = [
        _event(1, [10, 11], params={"k": 1}),
        _event(2, [], session="s2"),
        _event(3, [12], ts=day2, session="s2",
               served_facts=[{"entity_norm": "a", "attribute_norm": "b"}]),
    ]
    s = rtr.summarize_events(events)
    assert s["n_events"] == 3
    assert s["distinct_sessions"] == 2
    assert s["zero_result_events"] == 1
    assert len(s["by_day"]) == 2
    assert s["by_day"][0]["events"] == 2
    assert s["params_coverage"]["rows"] == 1
    assert s["served_facts_coverage"]["rows"] == 1
    assert s["served_facts_coverage"]["total_facts_served"] == 1


def test_summarize_uses_reports_the_served_rank_it_credited():
    events = [_event(7, [10, 11, 12], ts=100.0)]
    uses = [{"event_id": 7, "entry_id": 12, "used_via": "get",
             "created_at": 160.0}]
    s = rtr.summarize_uses(events, uses)
    assert s["n_uses"] == 1
    assert s["by_via"] == {"get": 1}
    assert s["detail"][0]["served_rank"] == 2
    assert s["detail"][0]["latency_s"] == 60.0
    assert s["served_rank_histogram"] == {"rank_2": 1}


def test_access_count_is_never_treated_as_a_downstream_label():
    """cms.py bumps access_count for every entry in a merged result set,
    so a served-and-never-read entry has access_count > 0. Counting it as
    consumption would label almost the whole bank."""
    events = [_event(1, [10, 11])]
    signal = {10: {"exists": 1, "access_count": 99,
                   "explicit_reinforcements": 0},
              11: {"exists": 1, "access_count": 5,
                   "explicit_reinforcements": 0}}
    lb = rtr.summarize_labels(events, [], signal)
    assert lb["events_with_any_downstream_signal"] == 0
    assert lb["top1_consumed"] == 0


def test_explicit_reinforcement_counts_as_a_label():
    events = [_event(1, [10, 11])]
    signal = {10: {"exists": 1, "access_count": 0,
                   "explicit_reinforcements": 2},
              11: {"exists": 1, "access_count": 0,
                   "explicit_reinforcements": 0}}
    lb = rtr.summarize_labels(events, [], signal)
    assert lb["events_with_any_downstream_signal"] == 1
    assert lb["top1_consumed"] == 1
    assert lb["top3_consumed"] == 1


def test_dangling_served_ids_are_counted_not_dropped():
    """served carries no FK to entries — evicted ids must show up as
    dangling rather than silently vanishing from the join."""
    events = [_event(1, [10, 999])]
    signal = {10: {"exists": 1, "access_count": 0,
                   "explicit_reinforcements": 0}}
    lb = rtr.summarize_labels(events, [], signal)
    assert lb["served_id_join"] == {"served_ids_still_in_entries": 1,
                                    "served_ids_dangling": 1}


def test_verdict_measures_labelled_events_not_logged_events():
    lb = {"events_with_any_downstream_signal": 4}
    v = rtr.verdict(lb, n_events=5000, phase1_target=300)
    assert v["trainable"] is False
    assert v["shortfall"] == 296
    assert rtr.verdict({"events_with_any_downstream_signal": 300},
                       5000, 300)["trainable"] is True


# ── replay scoring ────────────────────────────────────────────────────────

def test_first_label_rank_and_score_case():
    assert rr.first_label_rank([5, 6, 7], {7}) == 2
    assert rr.first_label_rank([5, 6, 7], {8}) is None
    s = rr.score_case([5, 6, 7], {7})
    assert s["rr"] == pytest.approx(1 / 3)
    assert s["hit@1"] is False and s["hit@3"] is True and s["hit@6"] is True
    miss = rr.score_case([5, 6, 7], {8})
    assert miss["rr"] == 0.0 and miss["rank"] is None


def test_aggregate_survives_an_arm_with_no_cases():
    agg = rr.aggregate([], [])
    assert agg == {"n": 0, "mrr": 0.0, "hit@1": 0.0, "hit@3": 0.0,
                   "hit@6": 0.0, "median_latency_s": 0.0,
                   "mean_latency_s": 0.0}


def test_aggregate_mrr_and_latency():
    cases = [rr.score_case([1, 2], {1}), rr.score_case([1, 2], {2}),
             rr.score_case([1, 2], {9})]
    agg = rr.aggregate(cases, [0.1, 0.3, 0.2])
    assert agg["n"] == 3
    assert agg["mrr"] == pytest.approx((1.0 + 0.5 + 0.0) / 3, abs=1e-4)
    assert agg["hit@1"] == pytest.approx(1 / 3, abs=1e-4)
    assert agg["median_latency_s"] == 0.2


def test_build_cases_drops_events_with_no_label():
    events = [_event(1, [10, 11, 12]), _event(2, [])]
    uses = [{"event_id": 1, "entry_id": 11, "used_via": "get"}]
    assert [c["labels"] for c in rr.build_cases(events, uses, "uses")] == [{11}]
    assert [c["labels"] for c in
            rr.build_cases(events, uses, "logged-top1")] == [{10}]
    assert [c["labels"] for c in
            rr.build_cases(events, uses, "logged-top3")] == [{10, 11, 12}]


def test_sample_is_deterministic_and_spans_the_corpus():
    cases = [{"i": i} for i in range(100)]
    a = rr.sample(cases, 10)
    assert a == rr.sample(cases, 10)
    assert len(a) == 10
    assert a[0]["i"] == 0 and a[-1]["i"] >= 80  # not a head slice
    assert rr.sample(cases, None) is cases


def test_pool_knob_status_reports_absence_with_a_reason():
    class _S:
        pass

    class _Cfg:
        class memory:
            search = _S()

    st = rr.pool_knob_status(_Cfg)
    assert st["available"] is False and st["reason"]

    _Cfg.memory.search.candidate_pool_size = 200
    st2 = rr.pool_knob_status(_Cfg)
    assert st2["available"] is True
    assert st2["knobs_found"] == ["candidate_pool_size"]


def test_clear_query_embedding_cache_is_a_noop_without_a_pipeline():
    class _Svc:
        _embedder = None

    assert rr.clear_query_embedding_cache(_Svc()) is False


def test_clear_query_embedding_cache_empties_the_lru():
    import threading

    class _Emb:
        def __init__(self):
            self._cache = {("q", True): object()}
            self._cache_lock = threading.Lock()

    class _Svc:
        def __init__(self):
            self._embedder = _Emb()

    svc = _Svc()
    assert rr.clear_query_embedding_cache(svc) is True
    assert svc._embedder._cache == {}


@pytest.mark.parametrize("artifact", [
    "evals/results/retrieval-telemetry-review-20260904.json",
    "evals/results/retrieval-replay-20260904.json",
    "evals/results/graph-ablation-20260904.json",
])
def test_committed_artifacts_record_no_filesystem_paths(artifact):
    """An absolute path on the maintainer's machine embeds the OS
    username, which `test_release_ux.py` rejects for the whole tracked
    tree. The `--config` seed leaked exactly that once; record the file
    NAME, never the path."""
    p = Path(__file__).resolve().parents[1] / artifact
    if not p.exists():  # the graph run is optional on a fresh checkout
        pytest.skip(f"{artifact} not present")
    blob = p.read_text(encoding="utf-8")
    assert "c:\\" not in blob.lower()
    assert "/users/" not in blob.lower()


def test_run_arm_scores_a_fake_search_and_records_latency():
    calls = []

    def fake_search(q, top_k=6, **kw):
        calls.append((q, kw))
        return {"entries": [{"id": 10}, {"id": 11}]}

    cases = [{"event_id": 1, "query": "a", "labels": {11}},
             {"event_id": 2, "query": "b", "labels": {99}}]
    agg = rr.run_arm(fake_search, cases, {"bm25": False}, 6,
                     progress_every=0)
    assert agg["n"] == 2
    assert agg["mrr"] == pytest.approx(0.25)
    assert agg["rank_histogram"] == {"1": 1, "miss": 1}
    assert calls[0][1] == {"bm25": False}
