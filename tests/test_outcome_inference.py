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
    # dream_status's infer_pending gate (fix 3) also requires a configured
    # extractor endpoint — set one directly on the config (no env vars, no
    # network) so this test keeps exercising the kill-switch behavior it
    # was written for.
    svc.config.memory.dream.extractor_base_url = "http://example.test/v1"
    svc.config.memory.dream.extractor_model = "test-model"
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


# ── all-inferred batch confidence discount ──────────────────────────────────

def test_all_inferred_batch_discounts_lesson_confidence(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    svc._storage.add_signal(task="deploy daemon", outcome="failure",
                            detail="rollback needed", origin="inferred",
                            episode_id=root_id)

    class _LessonExtractor:
        def extract_lessons(self, signals):
            assert all(s.get("origin") == "inferred" for s in signals)
            return [{"task": "deploy daemon", "aspect": "process",
                     "lesson": "verify health before rollback",
                     "polarity": "-", "outcome": "failure"}]

    res = svc.synthesize_lessons(_LessonExtractor())
    assert res["lessons"] == 1
    row = svc.lesson_search("deploy daemon", top_k=1)
    lesson = row["entries"][0]
    assert lesson["confidence"] == pytest.approx(0.4)
    assert "inferred" in (lesson.get("provenance") or [])


def test_mixed_batch_keeps_default_confidence(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    svc._storage.add_signal(task="deploy daemon", outcome="failure",
                            detail="rollback needed", origin="inferred",
                            episode_id=root_id)
    svc._storage.add_signal(task="deploy daemon", outcome="success",
                            detail="explicit report", origin="agent",
                            episode_id=root_id)

    class _LessonExtractor:
        def extract_lessons(self, signals):
            return [{"task": "deploy daemon", "aspect": "process",
                     "lesson": "verify health before rollback",
                     "polarity": "-", "outcome": "failure"}]

    res = svc.synthesize_lessons(_LessonExtractor())
    assert res["lessons"] == 1
    row = svc.lesson_search("deploy daemon", top_k=1)
    lesson = row["entries"][0]
    assert lesson["confidence"] == pytest.approx(0.6)
    assert "inferred" not in (lesson.get("provenance") or [])


# ── final-review fixes: cap<=0 disables, commit-time re-check, status gate ──


def test_cap_zero_disables_feature(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    svc.config.memory.lessons.infer_outcomes_max_signals = 0
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([])).get("skipped") == "disabled"
    st = svc.dream_status()
    assert st["infer_outcomes"]["pending"] == 0


def test_parse_outcome_claims_cap_zero():
    from pseudolife_memory.memory.dream import _parse_outcome_claims
    assert _parse_outcome_claims(
        '{"outcomes": [{"task": "t", "outcome": "success"}]}', cap=0) == []


def test_commit_recheck_skips_already_processed(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode

    class _RacingExtractor:
        def infer_outcomes(self, context_text, *, cap=3):
            # simulate a concurrent dream finishing while we were unlocked
            svc._storage.add_signal(task="raced", outcome="success",
                                    episode_id=root_id)
            return [{"task": "dup", "outcome": "success",
                     "about": None, "detail": None}]

    stats = svc.infer_outcomes_stage(_RacingExtractor())
    assert stats["written"] == 0                    # duplicate write skipped
    sigs = [s for s in svc._storage.pending_signals(limit=100)
            if s.get("episode_id") == root_id]
    assert [s["task"] for s in sigs] == ["raced"]   # only the racer's signal


def test_dream_status_gates_pending_on_configured_extractor(
        closed_zero_signal_episode):
    """Fix 3: a NoOp/extractor-less deploy (no primary or fallback endpoint
    configured — the default here) must not pin ``would_fire`` forever via
    a phantom infer_pending count, even though a real candidate exists and
    the feature is otherwise on. Config-only check, offline — no monkeypatch
    of env vars needed since the default config already has no endpoint."""
    svc, _root = closed_zero_signal_episode
    assert svc.config.memory.dream.extractor_base_url is None
    assert svc.config.memory.dream.fallback_base_url is None
    with svc._lock:
        assert len(svc._pending_inference_candidates()) == 1   # real candidate
    st = svc.dream_status()
    assert st["infer_outcomes"]["pending"] == 0
    assert st["infer_outcomes"]["retry_pending"] == 0


# ── same-tick sibling collision (reap_idle_sessions closes multiple roots ──
# in one sweep with independent time.time() calls per close; they can land
# on the identical float) ────────────────────────────────────────────────────


@pytest.fixture
def two_same_tick_episodes(pg_service):
    """Two closed, zero-signal session episodes whose root ``ended_at`` is
    forced to the identical float value, reproducing what a single
    ``reap_idle_sessions`` sweep can produce for two idle roots closed back
    to back."""
    svc = pg_service
    svc.episode_start_session("sess-a", "Session A")
    svc.store("task a entry", source="claude")
    closed_a = svc.episode_end_session("sess-a", run_dream=False)
    svc.episode_start_session("sess-b", "Session B")
    svc.store("task b entry", source="claude")
    closed_b = svc.episode_end_session("sess-b", run_dream=False)
    assert closed_a and closed_b   # non-empty subtrees survive prune-on-close
    root_a, root_b = closed_a["id"], closed_b["id"]
    with svc._lock:
        em = svc._cms.episodes.episodes
        same_ts = em[root_a].ended_at
        em[root_b].ended_at = same_ts
        svc._persist_episodes()
    return svc, root_a, root_b


def test_same_tick_siblings_both_processed(two_same_tick_episodes):
    svc, root_a, root_b = two_same_tick_episodes
    fake = _FakeInferExtractor([
        [{"task": "task a", "outcome": "success", "about": None,
          "detail": None}],
        [{"task": "task b", "outcome": "failure", "about": None,
          "detail": None}],
    ])
    stats = svc.infer_outcomes_stage(fake)
    assert stats == {"scanned": 2, "written": 2}
    for rid, task in [(root_a, "task a"), (root_b, "task b")]:
        sigs = [s for s in svc._storage.pending_signals(limit=100)
                if s.get("episode_id") == rid]
        assert [s["task"] for s in sigs] == [task]
