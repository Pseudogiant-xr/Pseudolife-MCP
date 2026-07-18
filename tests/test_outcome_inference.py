"""Auto-outcome inference (spec 2026-07-18): parser + config units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pseudolife_memory.memory.dream import _parse_outcome_claims
from pseudolife_memory.utils.config import LessonsConfig


def test_parse_outcome_claims_happy_path():
    content = ('noise before {"outcomes": [{"task": "deploy daemon", '
               '"outcome": "success", "about": "ops/update.ps1", '
               '"detail": "health check passed"}]} noise after')
    claims = _parse_outcome_claims(content, cap=3)
    assert claims == [{"task": "deploy daemon", "outcome": "success",
                       "about": "ops/update.ps1",
                       "detail": "health check passed"}]


def test_parse_outcome_claims_refuses_bad_enum_and_empty_task():
    content = ('{"outcomes": ['
               '{"task": "x", "outcome": "failed"},'
               '{"task": "", "outcome": "success"},'
               '{"task": "y", "outcome": "correction"}]}')
    claims = _parse_outcome_claims(content, cap=3)
    assert claims == [{"task": "y", "outcome": "correction",
                       "about": None, "detail": None}]


def test_parse_outcome_claims_cap():
    items = ",".join(f'{{"task": "t{i}", "outcome": "success"}}'
                     for i in range(5))
    claims = _parse_outcome_claims(f'{{"outcomes": [{items}]}}', cap=2)
    assert [c["task"] for c in claims] == ["t0", "t1"]


def test_parse_outcome_claims_malformed_vs_empty():
    assert _parse_outcome_claims("total garbage", cap=3) is None
    assert _parse_outcome_claims('{"wrong_key": []}', cap=3) is None
    assert _parse_outcome_claims('{"outcomes": []}', cap=3) == []


def test_lessons_config_defaults():
    cfg = LessonsConfig()
    assert cfg.infer_outcomes is True
    assert cfg.infer_outcomes_max_signals == 3


# ── Candidate scan / episode context / cursor IO (PG-backed) ────────────────
#
# The candidate scan needs real PostgresStorage (get_meta/set_meta,
# add_signal, count_signals_for_episodes) — the module-scoped
# ``pristine_service`` fixture in tests/conftest.py stays in file mode
# (no PSEUDOLIFE_MCP_DATABASE_URL), so it can't exercise this path. Mirrors
# the wiring tests/test_loop_health.py::test_service_loop_health_rates uses:
# monkeypatch the DSN in, build a fresh MemoryService bound to the bench
# Postgres. The session-episode API itself (episode_start_session / store /
# episode_end_session) is the same one tests/test_episode_service.py uses.

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture
def pg_service(pg_url, pg_conn, tmp_path, monkeypatch):
    """A MemoryService bound to the real bench Postgres (schema ensured +
    tables truncated by ``pg_conn``)."""
    from pseudolife_memory.service import MemoryService

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()
    return svc


@pytest.fixture
def closed_zero_signal_episode(pg_service):
    """One session episode holding two entries (one status-source) and no
    outcome signals, then closed. Returns (service, root_episode_id)."""
    svc = pg_service
    svc.episode_start_session("sess-1", "Session A")
    svc.store("wrote the outcome-inference candidate scan", source="claude")
    svc.store("dream cycle finished cleanly", source="status")
    closed = svc.episode_end_session("sess-1", run_dream=False)
    assert closed  # non-empty subtree survives prune-on-empty-close
    return svc, closed["id"]


def test_candidates_finds_zero_signal_closed_episode(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    with svc._lock:
        cands = svc._pending_inference_candidates()
    assert [c["root_id"] for c in cands] == [root_id]
    ctx = cands[0]["context"]
    assert "status" in ctx          # status-source entries ARE included
    assert ctx.startswith("Session:")


def test_candidates_skips_episode_with_signals(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    svc._storage.add_signal(task="t", outcome="success",
                            episode_id=root_id)
    with svc._lock:
        assert svc._pending_inference_candidates() == []


def test_candidates_respects_cursor(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    with svc._lock:
        end = svc._pending_inference_candidates()[0]["ended_at"]
        svc._save_infer_cursor({"ts": end, "retry": {}})
        assert svc._pending_inference_candidates() == []


def test_cursor_roundtrip_defaults(pg_service):
    svc = pg_service
    with svc._lock:
        assert svc._load_infer_cursor() == {"ts": 0.0, "retry": {}}
        svc._save_infer_cursor({"ts": 12.5, "retry": {"e1": 1}})
        assert svc._load_infer_cursor() == {"ts": 12.5, "retry": {"e1": 1}}


# ── infer_outcomes_stage (dream stage) ───────────────────────────────────────


class _FakeInferExtractor:
    def __init__(self, script):
        self.script = list(script)   # each: list | None | Exception
        self.calls = 0

    def infer_outcomes(self, context_text, *, cap=3):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _pending_inferred(svc, root_id):
    return [s for s in svc._storage.pending_signals(limit=100)
            if s.get("episode_id") == root_id]


def test_stage_writes_inferred_signals_and_advances(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    fake = _FakeInferExtractor([[{"task": "deploy", "outcome": "success",
                                  "about": None, "detail": "verified"}]])
    stats = svc.infer_outcomes_stage(fake)
    assert stats == {"scanned": 1, "written": 1}
    sigs = _pending_inferred(svc, root_id)
    assert len(sigs) == 1 and sigs[0]["origin"] == "inferred"
    # structurally idempotent: episode now has signals, cursor advanced
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([])) == {"scanned": 0, "written": 0}


def test_stage_clean_empty_advances_without_writes(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([[]])) == {"scanned": 1, "written": 0}
    assert _pending_inferred(svc, root_id) == []
    with svc._lock:
        assert svc._pending_inference_candidates() == []   # cursor moved


def test_stage_malformed_retries_twice_then_advances(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([None])) == {"scanned": 1, "written": 0}
    with svc._lock:
        assert svc._load_infer_cursor()["retry"] == {root_id: 1}
        assert len(svc._pending_inference_candidates()) == 1   # still pending
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([None])) == {"scanned": 1, "written": 0}
    with svc._lock:
        cur = svc._load_infer_cursor()
        assert cur["retry"] == {}                # cleared
        assert svc._pending_inference_candidates() == []   # advanced past


def test_stage_transport_failure_holds_cursor(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    from pseudolife_memory.memory.dream import ExtractorError
    stats = svc.infer_outcomes_stage(
        _FakeInferExtractor([ExtractorError("down")]))
    assert stats["written"] == 0
    with svc._lock:
        assert len(svc._pending_inference_candidates()) == 1   # untouched


def test_stage_respects_kill_switch(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    svc.config.memory.lessons.infer_outcomes = False
    stats = svc.infer_outcomes_stage(_FakeInferExtractor([]))
    assert stats.get("skipped") == "disabled"


# ── dream_status outcome inference reporting ────────────────────────────────

def test_dream_status_counts_inference_pending(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    st = svc.dream_status()
    assert st["infer_outcomes"]["pending"] == 1
    assert st["would_fire"] is True
    svc.config.memory.lessons.infer_outcomes = False
    st = svc.dream_status()
    assert st["infer_outcomes"]["pending"] == 0


def test_stage_skips_extractor_without_method(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    stats = svc.infer_outcomes_stage(object())
    assert stats.get("skipped") == "no-extractor"


def test_stage_survives_malformed_claim_dict(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    fake = _FakeInferExtractor([[
        {"bad": "no task key"},
        {"task": "good claim", "outcome": "success",
         "about": None, "detail": None},
    ]])
    stats = svc.infer_outcomes_stage(fake)
    assert stats == {"scanned": 1, "written": 1}     # bad one skipped, not fatal
    sigs = _pending_inferred(svc, root_id)
    assert [s["task"] for s in sigs] == ["good claim"]
    with svc._lock:
        assert svc._pending_inference_candidates() == []   # cursor advanced
