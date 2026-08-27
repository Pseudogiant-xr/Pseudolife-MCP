"""Session-digest layer (spec 2026-08-24): parser, config, segmentation
units, and the PG-backed dream stage.

Mirrors tests/test_outcome_inference.py — the digest stage is the same
locked-pull / unlocked-extract / locked-commit shape with a meta cursor,
but writes one narrative band entry per closed session root instead of
outcome signals.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pseudolife_memory.memory.dream import (_parse_digest,
                                            split_session_context)
from pseudolife_memory.utils.config import DreamConfig


# ── parser ───────────────────────────────────────────────────────────────────

def test_parse_digest_happy_path():
    content = 'noise before {"digest": "  The session set up X.  "} after'
    assert _parse_digest(content) == "The session set up X."


def test_parse_digest_malformed_vs_empty():
    assert _parse_digest("total garbage") is None
    assert _parse_digest('{"wrong_key": "text"}') is None
    assert _parse_digest('{"digest": ""}') is None
    assert _parse_digest('{"digest": "   "}') is None
    assert _parse_digest('{"digest": 42}') is None


# ── config ───────────────────────────────────────────────────────────────────

def test_digest_config_defaults():
    cfg = DreamConfig()
    assert cfg.digest_enabled is False          # ships off (design decision 6)
    assert cfg.digest_context_chars == 24000
    assert cfg.digest_target_chars == 1200      # re-targeted 2026-08-27
    assert cfg.digest_max_per_cycle == 4
    assert "digest" in cfg.exclude_sources      # never re-mined for facts


# ── segmentation (map-reduce over digest_context_chars) ─────────────────────

def test_split_session_context_under_cap_is_identity():
    text = "Session: A\n- (claude) one\n- (claude) two"
    assert split_session_context(text, 1000) == [text]


def test_split_session_context_splits_on_lines_preserving_order():
    lines = [f"- (claude) entry number {i}" for i in range(100)]
    text = "\n".join(lines)
    parts = split_session_context(text, 400)
    assert len(parts) > 1
    assert all(len(p) <= 400 for p in parts)
    assert "\n".join(parts) == text             # nothing lost, order kept


def test_split_session_context_oversize_single_line_hard_splits():
    text = "x" * 900
    parts = split_session_context(text, 400)
    assert "".join(parts) == text
    assert all(len(p) <= 400 for p in parts)


def test_split_session_context_nonpositive_cap_terminates():
    """cap<=0 must clamp, not spin forever on the hard-split path."""
    parts = split_session_context("abcdef", 0)
    assert "".join(parts) == "abcdef"


# ── PG-backed stage tests ───────────────────────────────────────────────────

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture
def pg_service(pg_url, pg_conn, tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()
    svc.config.memory.dream.digest_enabled = True
    return svc


@pytest.fixture
def closed_episode(pg_service):
    """One closed session episode with two entries. Returns (svc, root_id)."""
    svc = pg_service
    svc.episode_start_session("sess-1", "Budget tracker sprint")
    svc.store("implemented password hashing with Werkzeug", source="claude")
    svc.store("resolved the UNIQUE constraint error in SQLite", source="claude")
    closed = svc.episode_end_session("sess-1", run_dream=False)
    assert closed
    return svc, closed["id"]


class _FakeDigestExtractor:
    """Scripted summarize_session: each item is str | None | Exception."""

    def __init__(self, script):
        self.script = list(script)
        self.contexts: list[str] = []

    def summarize_session(self, context_text, *, target_chars=800):
        self.contexts.append(context_text)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _digest_entries(svc, root_id):
    with svc._lock:
        return [e for band in svc._cms.bands for e in band.entries
                if e.source == "digest" and e.episode_id == root_id]


def test_candidates_finds_closed_episode(closed_episode):
    svc, root_id = closed_episode
    with svc._lock:
        cands = svc._pending_digest_candidates()
    assert [c["root_id"] for c in cands] == [root_id]
    assert cands[0]["title"] == "Budget tracker sprint"
    assert cands[0]["n_entries"] == 2
    assert cands[0]["context"].startswith("Session:")


def test_candidates_skips_episode_with_existing_digest(closed_episode):
    svc, root_id = closed_episode
    svc.generate_digests_stage(_FakeDigestExtractor(["Did the things."]))
    with svc._lock:
        assert svc._pending_digest_candidates() == []


def test_digest_cursor_roundtrip(pg_service):
    svc = pg_service
    with svc._lock:
        assert svc._load_digest_cursor() == {"ts": 0.0, "retry": {}}
        svc._save_digest_cursor({"ts": 3.5, "retry": {"e": 1}})
        assert svc._load_digest_cursor() == {"ts": 3.5, "retry": {"e": 1}}


def test_stage_writes_digest_entry_stamped_and_headed(closed_episode):
    svc, root_id = closed_episode
    fake = _FakeDigestExtractor(["Implemented hashing; fixed constraint."])
    stats = svc.generate_digests_stage(fake)
    assert stats == {"scanned": 1, "written": 1}
    entries = _digest_entries(svc, root_id)
    assert len(entries) == 1
    e = entries[0]
    assert e.text.startswith("Session digest: Budget tracker sprint (")
    assert "2 entries" in e.text.splitlines()[0]
    assert e.text.endswith("Implemented hashing; fixed constraint.")
    assert e.episode_title == "Budget tracker sprint"
    assert "digest" in (e.tags or [])
    assert e.db_id is not None                      # write-through persisted
    # idempotent: candidate gone, cursor advanced
    assert svc.generate_digests_stage(
        _FakeDigestExtractor([])) == {"scanned": 0, "written": 0}


def test_digest_bypasses_surprise_gate(closed_episode):
    """A digest is low-surprise by construction (it restates session
    content) — the dedicated write path must not let the gate drop it."""
    svc, root_id = closed_episode
    fake = _FakeDigestExtractor(
        ["implemented password hashing with Werkzeug"])   # near-duplicate
    assert svc.generate_digests_stage(fake)["written"] == 1
    assert len(_digest_entries(svc, root_id)) == 1


def test_digest_never_supersedes_raw_turns(closed_episode):
    """The digest write must not run contradiction decay against the very
    turns it summarizes."""
    svc, root_id = closed_episode
    svc.generate_digests_stage(
        _FakeDigestExtractor(["Did not implement password hashing."]))
    with svc._lock:
        raw = [e for band in svc._cms.bands for e in band.entries
               if e.episode_id == root_id and e.source == "claude"]
    assert all(e.superseded_at is None for e in raw)


def test_digest_searchable_and_filterable(closed_episode):
    svc, root_id = closed_episode
    svc.generate_digests_stage(
        _FakeDigestExtractor(["Hashing done; constraint fixed."]))
    hit = svc.search("what happened in the budget tracker session",
                     sources=["digest"])
    assert any(e["source"] == "digest" for e in hit["entries"])
    excl = svc.search("what happened in the budget tracker session",
                      sources=["claude"])
    assert all(e["source"] != "digest" for e in excl["entries"])


def test_digest_excluded_from_dream_pull(closed_episode):
    svc, root_id = closed_episode
    svc.generate_digests_stage(_FakeDigestExtractor(["The digest text."]))
    pulled = svc.dream_pull(limit=100)      # takes the lock itself
    assert all("The digest text." not in e["text"] for e in pulled["entries"])


def test_stage_malformed_retries_twice_then_advances(closed_episode):
    svc, root_id = closed_episode
    assert svc.generate_digests_stage(
        _FakeDigestExtractor([None])) == {"scanned": 1, "written": 0}
    with svc._lock:
        assert svc._load_digest_cursor()["retry"] == {root_id: 1}
        assert len(svc._pending_digest_candidates()) == 1
    assert svc.generate_digests_stage(
        _FakeDigestExtractor([None])) == {"scanned": 1, "written": 0}
    with svc._lock:
        assert svc._load_digest_cursor()["retry"] == {}
        assert svc._pending_digest_candidates() == []       # advanced past
    assert _digest_entries(svc, root_id) == []


def test_stage_transport_failure_holds_cursor(closed_episode):
    svc, root_id = closed_episode
    from pseudolife_memory.memory.dream import ExtractorError
    stats = svc.generate_digests_stage(
        _FakeDigestExtractor([ExtractorError("down")]))
    assert stats["written"] == 0
    with svc._lock:
        assert len(svc._pending_digest_candidates()) == 1   # untouched


def test_stage_respects_kill_switch(closed_episode):
    svc, _root = closed_episode
    svc.config.memory.dream.digest_enabled = False
    stats = svc.generate_digests_stage(_FakeDigestExtractor([]))
    assert stats.get("skipped") == "disabled"


def test_stage_skips_extractor_without_method(closed_episode):
    svc, _root = closed_episode
    assert svc.generate_digests_stage(
        object()).get("skipped") == "no-extractor"


def test_commit_recheck_skips_when_digest_raced_in(closed_episode):
    svc, root_id = closed_episode

    class _RacingExtractor:
        def summarize_session(self, context_text, *, target_chars=800):
            # a concurrent dream digested this episode while we were unlocked
            svc._store_digest("Session digest: raced\nRaced text.",
                              root_id, "Budget tracker sprint")
            return "Loser text."

    stats = svc.generate_digests_stage(_RacingExtractor())
    assert stats["written"] == 0
    entries = _digest_entries(svc, root_id)
    assert len(entries) == 1 and "Raced text." in entries[0].text


def test_oversize_context_is_segmented_and_merged(closed_episode):
    svc, root_id = closed_episode
    svc.config.memory.dream.digest_context_chars = 80    # force segmentation
    with svc._lock:
        context = svc._pending_digest_candidates()[0]["context"]
    n_parts = len(split_session_context(context, 80))
    assert n_parts > 1                          # the cap actually bites
    fake = _FakeDigestExtractor(
        [f"seg {i}" for i in range(n_parts)] + ["merged digest"])
    stats = svc.generate_digests_stage(fake)
    assert stats == {"scanned": 1, "written": 1}
    assert len(fake.contexts) == n_parts + 1    # one per segment + the merge
    for i in range(n_parts):
        assert f"seg {i}" in fake.contexts[-1]  # merge call sees every part
    entries = _digest_entries(svc, root_id)
    assert len(entries) == 1
    assert entries[0].text.endswith("merged digest")


def test_backfill_processes_pre_enable_episodes_capped(pg_service):
    """Ratified decision 2: the zero-start cursor backfills history; the
    per-cycle cap bounds each dream pass."""
    svc = pg_service
    svc.config.memory.dream.digest_enabled = False       # feature off...
    for i in ("a", "b"):
        svc.episode_start_session(f"sess-{i}", f"Session {i}")
        svc.store(f"work item {i}", source="claude")
        assert svc.episode_end_session(f"sess-{i}", run_dream=False)
    svc.config.memory.dream.digest_enabled = True        # ...enabled later
    svc.config.memory.dream.digest_max_per_cycle = 1
    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["digest a"]))["written"] == 1
    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["digest b"]))["written"] == 1
    assert svc.generate_digests_stage(
        _FakeDigestExtractor([]))["written"] == 0        # drained


def test_briefing_recap_carries_digest_summary(closed_episode):
    """Design decision 7: the last closed session's digest fills the
    briefing recap; without one the recap stays the bare title/count."""
    svc, root_id = closed_episode
    recap = svc.session_briefing()["recap"]
    assert recap["title"] == "Budget tracker sprint"
    assert "summary" not in recap                        # no digest yet
    svc.generate_digests_stage(
        _FakeDigestExtractor(["Hashing was implemented and tested."]))
    recap = svc.session_briefing()["recap"]
    assert recap["summary"] == "Hashing was implemented and tested."


def _drain_dream_backlog(svc):
    """Pull/commit until empty (pull's "cursor" is the PRE-pull cursor —
    commit the max pulled timestamp; bounded so a regression fails loud)."""
    for _ in range(50):
        pulled = svc.dream_pull(limit=100)
        if not pulled["entries"]:
            return
        svc.dream_commit(max(e["timestamp"] for e in pulled["entries"]))
    pytest.fail("dream backlog did not drain in 50 pulls")


def test_dream_status_counts_digest_pending_and_fires(closed_episode):
    """The sweep gates dream_run on would_fire — a digest backlog on a
    QUIET bank must count, or it never backfills. Gated on a configured
    extractor endpoint, like infer_outcomes."""
    svc, _root = closed_episode
    svc.config.memory.lessons.infer_outcomes = False     # isolate digests
    _drain_dream_backlog(svc)
    st = svc.dream_status()
    assert st["digests"]["pending"] == 0          # no endpoint configured
    svc.config.memory.dream.extractor_base_url = "http://example.test/v1"
    svc.config.memory.dream.extractor_model = "test-model"
    st = svc.dream_status()
    assert st["digests"]["pending"] == 1
    assert st["would_fire"] is True
    svc.config.memory.dream.digest_enabled = False
    st = svc.dream_status()
    assert st["digests"]["pending"] == 0


def test_digest_backlog_defers_to_consolidation_cadence(closed_episode):
    """Digests run only on the empty-pull branch, so a digest backlog must
    NOT fire the sweep while entries are pending below the normal cadence
    thresholds — that would consolidate a partial batch every tick and
    make zero digest progress (pre-PR review finding, 2026-08-27)."""
    svc, _root = closed_episode
    svc.config.memory.lessons.infer_outcomes = False     # isolate digests
    svc.config.memory.dream.extractor_base_url = "http://example.test/v1"
    svc.config.memory.dream.extractor_model = "test-model"
    st = svc.dream_status()
    assert st["digests"]["pending"] == 1
    assert 1 <= st["backlog"] < svc.config.memory.dream.min_batch
    assert st["idle_seconds"] < svc.config.memory.dream.idle_seconds
    assert st["would_fire"] is False


def test_stage_write_failure_bounded_and_never_breaks_dream(closed_episode):
    """A failing digest write must not escape the stage (it would abort
    the stages after it and re-pay the map-reduce every dream): bounded
    like the malformed path — one held-cursor retry, then advance past."""
    svc, root_id = closed_episode

    def _boom(text, episode_id, title):
        raise RuntimeError("insert_entry: connection is broken")

    svc._store_digest = _boom
    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["The digest."])) == {"scanned": 1, "written": 0}
    with svc._lock:
        assert svc._load_digest_cursor()["retry"] == {root_id: 1}
        assert len(svc._pending_digest_candidates()) == 1   # cursor held
    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["The digest."])) == {"scanned": 1, "written": 0}
    with svc._lock:
        assert svc._load_digest_cursor()["retry"] == {}
        assert svc._pending_digest_candidates() == []       # advanced past
    assert _digest_entries(svc, root_id) == []


def test_dream_run_idle_cycle_runs_digest_stage(closed_episode):
    svc, root_id = closed_episode

    class _IdleExtractor(_FakeDigestExtractor):
        def extract(self, texts, vocab=None, known_facts=None):
            return []

    # Exhaust the normal backlog first so dream_run takes the idle branch.
    # dream_pull/dream_commit take the lock themselves, and pull's "cursor"
    # field is the PRE-pull cursor — committing it would never advance, so
    # commit the max pulled timestamp. Bounded so a cursor regression fails
    # the test instead of spinning.
    for _ in range(50):
        pulled = svc.dream_pull(limit=100)
        if not pulled["entries"]:
            break
        svc.dream_commit(max(e["timestamp"] for e in pulled["entries"]))
    else:
        pytest.fail("dream backlog did not drain in 50 pulls")
    res = svc.dream_run(_IdleExtractor(["The digest."]))
    assert res["pulled"] == 0
    assert res["digests"] == {"scanned": 1, "written": 1}
    assert len(_digest_entries(svc, root_id)) == 1
