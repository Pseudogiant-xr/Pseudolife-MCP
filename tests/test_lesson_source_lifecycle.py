"""Source eligibility and retirement publication across ordinary service operations."""

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_correction_identity import _seed
from tests.test_lesson_source_lineage import Rules, backed_signal, rows, svc  # noqa: F401


@pytest.mark.parametrize("mutation", ["supersede", "forget", "evict"])
@pytest.mark.parametrize("limit", [1, 2])
def test_clustered_stale_source_stays_pending_without_vetoing_valid_neighbor(
        svc, mutation, limit):
    stale = _seed(svc, "Old clustered evidence")
    valid = _seed(svc, "Current clustered evidence")
    old_sid = backed_signal(svc, stale, "stale")
    new_sid = backed_signal(svc, valid, "valid")
    svc._storage.conn.execute(
        "UPDATE outcome_signals SET about = 'git' WHERE id = ANY(%s)",
        ([old_sid, new_sid],))
    svc._storage.conn.commit()
    assert svc.config.memory.lessons.rule_mode is False
    svc.lesson_write("prior", "rule", "Previously valid guidance")
    if mutation == "supersede":
        svc._storage.supersede_entries(
            [stale.db_id], superseded_at=12345.0,
            superseded_by_text="Corrected source")
    elif mutation == "forget":
        svc.delete(text=stale.text)
    else:
        svc._storage.delete_evicted_entry(
            stale.db_id, source=stale.source, superseded=False)
    offered = []

    class Cluster:
        def extract_lessons(self, signals):
            offered.extend(s["id"] for s in signals)
            return [dict(task="clustered", lesson="Use current evidence", about="git")]

    results = [svc.synthesize_lessons(Cluster(), limit=limit) for _ in range(3)]
    assert rows(svc).get("clustered", {}).get("status") == "current", results
    assert rows(svc)["prior"]["status"] == "current"
    assert offered == [new_sid]
    assert [s["id"] for s in svc._storage.pending_signals()] == [old_sid]
    assert rows(svc)["clustered"]["provenance"] == sorted({
        "lineage:batch", f"signal:{new_sid}", f"episode:{valid.episode_id}",
        f"entry:{valid.db_id}"})
    assert svc._storage.conn.execute(
        "SELECT consumed_at FROM outcome_signals WHERE id = %s", (old_sid,)
    ).fetchone()[0] is None


@pytest.mark.parametrize("mutation", ["forget", "supersede"])
def test_clustered_source_change_during_extraction_keeps_atomic_batch_pending(svc, mutation):
    first = _seed(svc, "First current clustered source")
    second = _seed(svc, "Second current clustered source")
    ids = [backed_signal(svc, first, "first"), backed_signal(svc, second, "second")]
    svc._storage.conn.execute(
        "UPDATE outcome_signals SET about = 'git' WHERE id = ANY(%s)", (ids,))
    svc._storage.conn.commit()
    svc.lesson_write("prior", "rule", "Previously valid guidance")

    class RacingCluster:
        def extract_lessons(self, signals):
            assert [s["id"] for s in signals] == ids
            if mutation == "forget":
                svc.delete(text=first.text)
            else:
                svc.supersede(entry_id=first.db_id, new_text="Corrected clustered source")
            return [dict(task="clustered", lesson="Race must not publish")]

    result = svc.synthesize_lessons(RacingCluster())
    assert result["lessons"] == 0
    assert result["skipped"] == "signals-changed"
    assert set(rows(svc)) == {"prior"}
    assert rows(svc)["prior"]["status"] == "current"
    assert [s["id"] for s in svc._storage.pending_signals()] == ids


def test_forget_reload_failure_finishes_cms_cortex_and_digest_publication(svc, monkeypatch):
    from pseudolife_memory.memory.cortex import _norm_key
    from pseudolife_memory.service import MemoryService

    root = svc._cms.episodes.start_session(title="Source publication", session_key="forget")
    source = _seed(svc, "Forgotten source evidence", episode=root.id)
    _seed(svc, "Surviving source evidence", episode=root.id)
    digest = _seed(svc, "Digest of forgotten evidence", source="digest", episode=root.id)
    backed_signal(svc, source)
    backed_signal(svc, digest, "digest-backed")
    svc.lesson_write("prior", "rule", "Unrelated guidance")
    assert svc.synthesize_lessons(Rules())["lessons"] == 2
    svc.cortex_write("Beacon", "color", "amber", support="agent")
    svc._storage.add_trace(_norm_key("Beacon"), "color", source.db_id, 1.0)
    original = svc._storage.load_lessons
    reads = []

    def unavailable():
        reads.append(True)
        raise RuntimeError("injected post-forget lesson reload failure")

    monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
    result = svc.delete(text=source.text)
    assert result["deleted_count"] == 1
    assert not any(e.db_id == source.db_id for b in svc._cms.bands for e in b.entries)
    assert svc._storage.get_entry(source.db_id) is None
    assert digest.superseded_at is not None
    assert root.id in svc._load_digest_cursor()["regenerate"]
    assert next(r for r in svc._cortex.records if r.entity == "Beacon").status == "retired"
    assert {r["entity"]: r["status"] for r in original()} == {
        "prior": "current", "backed": "retired", "digest-backed": "retired"}
    assert svc._lesson_source_refresh
    monkeypatch.setattr(svc, "_ensure_init", MemoryService._ensure_init.__get__(svc))
    with pytest.raises(RuntimeError, match="post-forget lesson reload failure"):
        svc.lessons_dump()
    assert len(reads) == 2
    monkeypatch.setattr(svc._storage, "load_lessons", original)
    assert [r["task"] for r in svc.lessons_dump()["entries"]] == ["prior"]
    assert not svc._lesson_source_refresh
    assert not any(e.db_id == source.db_id for b in svc._cms.bands for e in b.entries)


@pytest.mark.parametrize("reload_failure", [False, True])
def test_capacity_eviction_retirement_survives_actual_flush(svc, monkeypatch, reload_failure):
    terminal = len(svc._cms.bands) - 1
    svc._cms.bands[terminal].max_entries = 1
    source = _seed(svc, "Capacity victim source", band=terminal)
    backed_signal(svc, source)
    svc.lesson_write("prior", "rule", "Unrelated valid guidance")
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    _seed(svc, "Incoming replacement at capacity", band=terminal)
    assert svc._storage.get_entry(source.db_id) is None
    assert svc._storage._lesson_source_refresh
    assert next(r for r in svc._lessons.records if r.entity == "backed").status == "current"
    original_load = svc._storage.load_lessons
    original_replace = svc._storage.replace_lessons
    snapshots = []

    def snapshot(records):
        snapshots.append(True)
        return original_replace(records)

    monkeypatch.setattr(svc._storage, "replace_lessons", snapshot)
    if reload_failure:
        def unavailable():
            raise RuntimeError("injected eviction lesson reload failure")
        monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
        with pytest.raises(RuntimeError, match="eviction lesson reload failure"):
            svc.flush()
        assert snapshots == []
        assert {r["entity"]: r["status"] for r in original_load()} == {
            "backed": "retired", "prior": "current"}
        monkeypatch.setattr(svc._storage, "load_lessons", original_load)
    svc.flush()
    assert snapshots == [True]
    assert rows(svc)["backed"]["status"] == "retired"
    assert rows(svc)["prior"]["status"] == "current"
    assert next(r for r in svc._lessons.records if r.entity == "backed").status == "retired"


def test_forget_database_failure_still_preserves_resident_source(svc, monkeypatch):
    source = _seed(svc)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1

    def unavailable(*args, **kwargs):
        raise RuntimeError("injected forget database failure")

    monkeypatch.setattr(svc._storage, "forget_entry_ids", unavailable)
    with pytest.raises(RuntimeError, match="forget database failure"):
        svc.delete(text=source.text)
    assert svc._storage.get_entry(source.db_id) is not None
    assert any(e.db_id == source.db_id for b in svc._cms.bands for e in b.entries)
    assert rows(svc)["backed"]["status"] == "current"
