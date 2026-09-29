"""Need-based deep-dream tick: the MECHANICAL half (Steps A/B apply — rescore,
guard-passing junk auto-delete, scope stamping, proposal filing) runs from the
sweep loop when the bank has grown enough or enough time has passed. Step C
(judgment) stays with agents/humans — the tick only fills the review queues.

Need signal: entities with id above the watermark stamped by the last deep
apply (id watermark, not count delta — merges and junk deletions shrink
counts and would mask growth), OR days since that apply. Every apply stamps
the watermark — manual and tick alike — so a manual pass resets the clock.
"""
from __future__ import annotations

import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


def _seed(svc, n=3):
    for i in range(n):
        svc.graph_relate(f"tick-node-{i}", "related-to", f"tick-anchor-{i}",
                         origin="agent")


def test_deep_apply_stamps_watermark(svc):
    _seed(svc)
    out = svc.deep_dream(apply=True, include_snippets=False)
    assert out["applied"] is True
    mark = svc._storage.get_meta("deep_last_apply")
    assert mark is not None
    assert mark["ts"] == pytest.approx(time.time(), abs=60)
    assert mark["max_entity_id"] >= 1


def test_need_fires_on_entity_growth_and_time(svc):
    _seed(svc)
    svc.deep_dream(apply=True, include_snippets=False)   # stamps watermark
    need = svc.deep_dream_need()
    assert need["recommended"] is False                  # freshly applied

    svc.config.memory.deep_dream.auto_min_new_entities = 2
    _seed(svc, n=4)                                      # 8 new entities
    need = svc.deep_dream_need()
    assert need["recommended"] is True
    assert "entities" in need["reason"]

    # Time backstop: age the watermark past the interval.
    svc.config.memory.deep_dream.auto_min_new_entities = 10**6
    mark = svc._storage.get_meta("deep_last_apply")
    svc._storage.set_meta("deep_last_apply",
                          {**mark, "ts": mark["ts"] - 8 * 86400.0})
    need = svc.deep_dream_need()
    assert need["recommended"] is True
    assert "days" in need["reason"]


def test_need_without_watermark_recommends(svc):
    # A bank that has never deep-dreamed is overdue by definition (provided
    # there is anything to consolidate).
    _seed(svc)
    need = svc.deep_dream_need()
    assert need["recommended"] is True


def test_dream_status_carries_deep_need(svc):
    st = svc.dream_status()
    assert "deep_dream" in st
    assert set(st["deep_dream"]) >= {"recommended", "reason"}


def test_tick_applies_when_needed_and_skips_when_not(svc):
    _seed(svc)
    svc.config.memory.deep_dream.auto_min_new_entities = 1
    out = svc.deep_dream_tick()
    assert out["fired"] is True and out.get("applied") is True
    # Watermark stamped by the apply → immediately after, no need.
    out2 = svc.deep_dream_tick()
    assert out2["fired"] is False

    svc.config.memory.deep_dream.auto_tick = False
    _seed(svc, n=5)
    out3 = svc.deep_dream_tick()
    assert out3["fired"] is False and out3["reason"] == "disabled"


def test_sweep_invokes_deep_tick():
    from pseudolife_memory.memory.dream import run_sweep_once

    calls = []

    class _FakeService:
        class config:  # noqa: D106 — mirrors test_dream.py's fake
            class memory:
                class dream:
                    enabled = True

        def compact_superseded(self):
            return {"total": 0}

        def prune_dream_runs(self):
            return 0

        def dream_status(self):
            return {"would_fire": False, "backlog": 0}

        def deep_dream_tick(self):
            calls.append(1)
            return {"fired": False, "reason": "below_threshold"}

    out = run_sweep_once(_FakeService())
    assert calls == [1]
    assert out["deep_tick"] == {"fired": False, "reason": "below_threshold"}


# -- review-queue health (dream_status["review_queue"]) ----------------------
# 2026-09-11..09-29: the merge judge sat in shadow and the pending merge queue
# grew from ~100 to 1,016 rows while nothing reported it. The block rides
# dream_status beside the deep-dream need signal: counts per queue, the age
# of the oldest pending merge, each judge's configured mode, and an
# attention flag.

DAY = 86400.0


def _file_queue(svc, *, merges=3, oldest_days=5.0):
    """``merges`` pending merge proposals (the first ``oldest_days`` old),
    one rejected merge, one pending junk and one pending link proposal.
    Returns the oldest pending merge's id."""
    _seed(svc, n=merges + 3)
    st = svc._storage
    ids = sorted(e["id"] for e in st.load_graph()["entities"])
    now = time.time()
    oldest = None
    for i in range(merges):
        created = now - oldest_days * DAY if i == 0 else now
        pid = st.insert_entity_proposal("merge", ids[i + 1], ids[0], 0.9,
                                        "test", created)
        assert pid is not None
        oldest = oldest or pid
    decided = st.insert_entity_proposal("merge", ids[merges + 1], ids[0], 0.9,
                                        "test", now - 40 * DAY)
    st.set_entity_proposal_status(decided, "rejected")
    assert st.insert_entity_proposal("junk", ids[merges + 2], None, None,
                                     "test", now) is not None
    assert st.insert_proposal(ids[merges + 3], "related-to", ids[merges + 4],
                              0.7, None, None, "test", now) is not None
    return oldest


def test_review_queue_counts_each_queue_and_the_oldest_merge(svc):
    _file_queue(svc, merges=3, oldest_days=5.0)
    rq = svc.dream_status()["review_queue"]
    assert rq["pending"] == {"merge": 3, "junk": 1, "link": 1}
    assert rq["oldest_merge_age_days"] == pytest.approx(5.0, abs=0.05)
    assert rq["oldest_unjudged_merge_age_days"] == pytest.approx(5.0, abs=0.05)
    cfg = svc.config.memory.deep_dream
    assert rq["judges"] == {
        "judges_enabled": cfg.judges_enabled,
        "judge_mode": cfg.judge_mode,
        "link_judge_mode": cfg.link_judge_mode,
        "junk_judge_mode": cfg.junk_judge_mode,
        "curation_judge_mode": cfg.curation_judge_mode,
        "candidate_judge_mode": cfg.candidate_judge_mode,
    }
    # Three rows on a fresh bank is healthy at the default thresholds.
    assert rq["attention"] == {"needed": False, "reasons": []}


def test_review_queue_empty_has_no_oldest_age(svc):
    rq = svc.review_queue_health()
    assert rq["pending"] == {"merge": 0, "junk": 0, "link": 0}
    assert rq["oldest_merge_age_days"] is None
    assert rq["oldest_unjudged_merge_age_days"] is None
    assert rq["attention"]["needed"] is False


def test_review_queue_attention_on_the_pending_count_in_any_mode(svc):
    _file_queue(svc, merges=3, oldest_days=0.0)
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"            # a draining judge: count rule only
    cfg.review_queue_alert_pending = 3
    rq = svc.review_queue_health()
    assert rq["attention"]["needed"] is True
    assert any("3 merge proposals pending" in r for r in rq["attention"]["reasons"])
    cfg.review_queue_alert_pending = 4
    assert svc.review_queue_health()["attention"]["needed"] is False
    cfg.review_queue_alert_pending = 0        # 0 disables the count rule
    assert svc.review_queue_health()["attention"]["needed"] is False


def test_review_queue_attention_when_nothing_drains_an_old_queue(svc):
    _file_queue(svc, merges=3, oldest_days=5.0)
    cfg = svc.config.memory.deep_dream
    cfg.review_queue_alert_pending = 0        # isolate the age rule
    cfg.review_queue_alert_age_days = 4.0
    cfg.review_queue_alert_min_pending = 3
    for mode in ("shadow", "off"):
        cfg.judge_mode = mode
        rq = svc.review_queue_health()
        assert rq["attention"]["needed"] is True, mode
        assert any(mode in r and "days" in r
                   for r in rq["attention"]["reasons"]), rq
    # A judge that applies verdicts, and has judged every row, leaves the
    # rest for a human: no age alert (the count rule still applies) ...
    for prop in svc._storage.pending_entity_proposals():
        svc._storage.set_entity_proposal_judgment(
            prop["id"], verdict="accept", confidence=0.7, note="t", model="m",
            at=time.time())
    cfg.judge_mode = "auto-reject"
    assert svc.review_queue_health()["attention"]["needed"] is False
    # ... unless every judge is switched off.
    cfg.judges_enabled = False
    assert svc.review_queue_health()["attention"]["needed"] is True
    cfg.judges_enabled = True
    cfg.judge_mode = "shadow"
    # Younger than the threshold, or fewer rows than the floor: healthy.
    cfg.review_queue_alert_age_days = 6.0
    assert svc.review_queue_health()["attention"]["needed"] is False
    cfg.review_queue_alert_age_days = 4.0
    cfg.review_queue_alert_min_pending = 4
    assert svc.review_queue_health()["attention"]["needed"] is False
    cfg.review_queue_alert_min_pending = 3
    cfg.review_queue_alert_age_days = 0       # 0 disables the age rule
    assert svc.review_queue_health()["attention"]["needed"] is False


def test_review_queue_failure_is_an_error_field_not_a_raise(svc, monkeypatch):
    def boom():
        raise RuntimeError("connection lost")
    svc.dream_status()                        # initialise the storage
    monkeypatch.setattr(svc._storage, "review_queue_counts", boom)
    st = svc.dream_status()
    rq = st["review_queue"]
    assert rq["error"] == "RuntimeError: connection lost"
    assert rq["attention"] == {"needed": False, "reasons": []}
    assert "deep_dream" in st                 # the rest of status still served


def test_review_queue_attention_when_a_draining_judge_leaves_rows_unjudged(svc):
    # "Configured to drain" is not "draining": an auto-reject judge with no
    # reachable endpoint never judges anything. A pending merge that no
    # judge has looked at for longer than the age threshold says so.
    oldest = _file_queue(svc, merges=3, oldest_days=5.0)
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.review_queue_alert_pending = 0
    cfg.review_queue_alert_age_days = 4.0
    cfg.review_queue_alert_min_pending = 3
    rq = svc.review_queue_health()
    assert rq["attention"]["needed"] is True
    assert any("unjudged" in r and "auto-reject" in r
               for r in rq["attention"]["reasons"]), rq
    # Once the judge has recorded a verdict on it, the row is waiting for
    # a human, not for the judge: the young unjudged rows stay quiet.
    assert svc._storage.set_entity_proposal_judgment(
        oldest, verdict="accept", confidence=0.7, note="t", model="m",
        at=time.time())
    rq = svc.review_queue_health()
    assert rq["oldest_merge_age_days"] == pytest.approx(5.0, abs=0.05)
    assert rq["oldest_unjudged_merge_age_days"] < 0.1
    assert rq["attention"]["needed"] is False


def test_review_queue_health_feeds_the_briefing_line(svc):
    # The real block, not a hand-built copy, renders the line.
    from pseudolife_memory.memory.briefing import review_queue_line

    _file_queue(svc, merges=3, oldest_days=5.0)
    svc.config.memory.deep_dream.review_queue_alert_pending = 3
    line = review_queue_line(svc.review_queue_health())
    assert line.startswith("Pseudolife-MCP: review queue has 3 merge proposals "
                           "pending, oldest 5 days; merge judge in ")
