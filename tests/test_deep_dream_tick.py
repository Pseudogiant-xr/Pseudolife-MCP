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
