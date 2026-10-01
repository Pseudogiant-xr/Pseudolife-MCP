"""Source eligibility and retirement publication across ordinary service operations."""

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_correction_identity import _seed
from tests.test_lesson_source_lineage import Rules, backed_signal, rows, svc  # noqa: F401


@pytest.mark.parametrize("mutation", ["forget", "evict"])
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
    if mutation == "forget":
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


def test_clustered_source_forget_during_extraction_keeps_atomic_batch_pending(svc):
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
            svc.delete(text=first.text)
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
        "prior": "current", "backed": "retired", "digest-backed": "current"}
    assert svc._storage._lesson_source_refresh
    monkeypatch.setattr(svc, "_ensure_init", MemoryService._ensure_init.__get__(svc))
    with pytest.raises(RuntimeError, match="post-forget lesson reload failure"):
        svc.lessons_dump()
    assert len(reads) == 2
    monkeypatch.setattr(svc._storage, "load_lessons", original)
    assert sorted(r["task"] for r in svc.lessons_dump()["entries"]) == [
        "digest-backed", "prior"]
    assert not svc._storage._lesson_source_refresh
    assert not any(e.db_id == source.db_id for b in svc._cms.bands for e in b.entries)


@pytest.mark.parametrize("reload_failure", [False, True])
def test_storage_forget_retirement_survives_actual_flush(svc, monkeypatch, reload_failure):
    source = _seed(svc, "Forgotten source")
    backed_signal(svc, source)
    svc.lesson_write("prior", "rule", "Unrelated valid guidance")
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    # A forget with no service callback (CMS delete_entries without a cascade).
    svc._storage.delete_entry_ids([source.db_id], forgotten=True)
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
            raise RuntimeError("injected forget lesson reload failure")
        monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
        with pytest.raises(RuntimeError, match="forget lesson reload failure"):
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


@pytest.mark.parametrize("victim", ["source", "unrelated"])
def test_capacity_eviction_never_retires_or_reloads_lessons(svc, monkeypatch, victim):
    terminal = len(svc._cms.bands) - 1
    svc._cms.bands[terminal].max_entries = 1
    source = _seed(svc, "Lesson source", band=terminal if victim == "source" else 0)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    doomed = source if victim == "source" else _seed(
        svc, "Unrelated entry at capacity", band=terminal)
    _seed(svc, "Incoming replacement at capacity", band=terminal)
    assert svc._storage.get_entry(doomed.db_id) is None
    assert svc._storage.get_meta("capacity_true_drops")["last_entry_id"] == doomed.db_id
    assert not getattr(svc._storage, "_lesson_source_refresh", False)

    def unavailable():
        raise AssertionError("eviction must not force a lesson reload")

    original_load = svc._storage.load_lessons
    monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
    svc.flush()
    monkeypatch.setattr(svc._storage, "load_lessons", original_load)
    assert rows(svc)["backed"]["status"] == "current"
    assert next(r for r in svc._lessons.records if r.entity == "backed").status == "current"
    assert not [d for d in svc._storage.store_decisions("lesson") if d["action"] == "retire"]


@pytest.mark.parametrize("mutation,lands", [
    ("supersede_before_selection", True),
    ("supersede_during_extraction", True),
    ("forget_before_selection", False),
])
def test_only_a_forgotten_credited_source_keeps_a_signal_pending(svc, mutation, lands):
    source = _seed(svc)
    sid = backed_signal(svc, source, "correction-rule")
    svc._storage.conn.execute(
        "UPDATE outcome_signals SET outcome = 'correction' WHERE id = %s", (sid,))
    svc._storage.conn.commit()
    if mutation == "supersede_before_selection":
        svc.supersede(entry_id=source.db_id, new_text="Corrected deployment evidence")
    elif mutation == "forget_before_selection":
        svc.delete(text=source.text)
    offered = []

    class Racing(Rules):
        def extract_rules(self, signals):
            offered.extend(s["id"] for s in signals)
            if mutation == "supersede_during_extraction":
                svc.supersede(entry_id=source.db_id, new_text="Corrected deployment evidence")
            return super().extract_rules(signals)

    result = svc.synthesize_lessons(Racing())
    if lands:
        assert result["lessons"] == 1, result
        assert offered == [sid]
        assert rows(svc)["correction-rule"]["status"] == "current"
        assert f"entry:{source.db_id}" in rows(svc)["correction-rule"]["provenance"]
        assert svc._storage.pending_signals() == []
    else:
        assert result["lessons"] == 0, result
        assert offered == []
        assert "correction-rule" not in rows(svc)
        assert [s["id"] for s in svc._storage.pending_signals()] == [sid]


@pytest.mark.parametrize("pending_refresh", [False, True],
                         ids=["no-retirement", "retirement-awaiting-reload"])
def test_correction_commits_and_publishes_despite_lesson_reload_failure(
        svc, monkeypatch, pending_refresh):
    source = _seed(svc)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    original_load = svc._storage.load_lessons
    original_supersede = svc._storage.supersede_entries

    def unavailable():
        raise RuntimeError("injected lesson reload failure")

    def supersede_with_pending_reload(*args, **kwargs):
        # Stands in for any committed retirement that still awaits a reload.
        persisted = original_supersede(*args, **kwargs)
        svc._storage._lesson_source_refresh = True
        return persisted

    monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
    if pending_refresh:
        monkeypatch.setattr(svc._storage, "supersede_entries", supersede_with_pending_reload)
    result = svc.supersede(entry_id=source.db_id, new_text="Corrected deployment evidence")
    assert result["superseded_ids"] == [source.db_id]
    assert svc._correction_recovery is None
    durable = svc._storage.get_entry(source.db_id)
    resident = {e.db_id: e for b in svc._cms.bands for e in b.entries}
    assert durable["superseded_at"] is not None
    assert resident[source.db_id].superseded_at == durable["superseded_at"]
    replacement = [e for e in resident.values() if e.text == "Corrected deployment evidence"]
    assert len(replacement) == 1
    assert svc._storage.get_entry(replacement[0].db_id) is not None
    # The barrier stays up until a reload succeeds; nothing was retired.
    assert bool(getattr(svc._storage, "_lesson_source_refresh", False)) is pending_refresh
    monkeypatch.setattr(svc._storage, "load_lessons", original_load)
    svc._save_lessons()
    assert not getattr(svc._storage, "_lesson_source_refresh", False)
    assert rows(svc)["backed"]["status"] == "current"
    assert svc._lessons.records[0].status == "current"


@pytest.mark.parametrize("when", ["before_selection", "during_extraction"])
def test_signal_with_a_surviving_credited_source_still_lands(svc, when):
    # Same last-source rule as retirement: forgetting one of a signal's
    # credited entries leaves it about something that still exists.
    kept = _seed(svc)
    gone = _seed(svc, "Retired deployment note", cosine=0.0)
    sid = backed_signal(svc, kept, "partly-forgotten")
    svc._storage.set_signal_used_ids(sid, {
        "credited": [kept.db_id, gone.db_id], "unmatched": [], "served_elsewhere": []})
    if when == "before_selection":
        svc.delete(text=gone.text)
    offered = []

    class Racing(Rules):
        def extract_rules(self, signals):
            offered.extend(s["id"] for s in signals)
            if when == "during_extraction":
                svc.delete(text=gone.text)
            return super().extract_rules(signals)

    result = svc.synthesize_lessons(Racing())
    assert svc._storage.get_entry(gone.db_id) is None
    assert result["lessons"] == 1, result
    assert offered == [sid]
    assert rows(svc)["partly-forgotten"]["status"] == "current"
    assert svc._storage.pending_signals() == []
