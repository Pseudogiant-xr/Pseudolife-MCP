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
