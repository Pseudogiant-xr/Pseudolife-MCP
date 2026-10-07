"""Forgetting a source entry retracts only derived state it supported."""

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.fixture
def svc(pg_url, pg_conn, tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    service = MemoryService(data_dir=tmp_path)
    service._ensure_init()
    service.config.memory.dream.digest_enabled = True
    yield service
    service._storage.close()


def _stored_id(svc, text):
    svc.store(text, source="synthetic")
    return next(e.db_id for b in svc._cms.bands for e in b.entries
                if e.text == text)


def test_forget_retires_last_supported_fact_but_preserves_other_support(svc):
    solo = "Beacon Delta is amber"
    shared_a = "Beacon Echo is blue, report one"
    shared_b = "Beacon Echo is blue, report two"
    solo_id = _stored_id(svc, solo)
    a_id = _stored_id(svc, shared_a)
    b_id = _stored_id(svc, shared_b)
    svc.cortex_write("Beacon Delta", "color", "amber", support="agent")
    svc.cortex_write("Beacon Echo", "color", "blue", support="agent")
    from pseudolife_memory.memory.cortex import _norm_key
    for entity, entry_id in (("Beacon Delta", solo_id),
                             ("Beacon Echo", a_id), ("Beacon Echo", b_id)):
        svc._storage.add_trace(_norm_key(entity), "color", entry_id, 1.0)

    assert svc.cortex_lookup("Beacon Delta", "color")["value"] == "amber"
    assert svc.cortex_lookup("Beacon Echo", "color")["value"] == "blue"
    assert svc.delete(text=solo)["deleted_count"] == 1
    assert svc.delete(text=shared_a)["deleted_count"] == 1

    assert svc.cortex_lookup("Beacon Delta", "color") is None
    retired = [r for r in svc._cortex.records if r.entity == "Beacon Delta"]
    assert retired[-1].status == "retired"
    assert str(solo_id) in (retired[-1].superseded_by_value or "")
    echo = svc.cortex_lookup("Beacon Echo", "color")
    assert echo["value"] == "blue"
    assert echo["source_entries"] == [b_id]


def test_superseded_support_does_not_keep_forgotten_fact_current(svc):
    from pseudolife_memory.memory.cortex import _norm_key

    first = "Beacon Juliet is silver"
    stale = "Beacon Juliet was silver long ago"
    first_id = _stored_id(svc, first)
    stale_id = _stored_id(svc, stale)
    svc.cortex_write("Beacon Juliet", "color", "silver", support="agent")
    for entry_id in (first_id, stale_id):
        svc._storage.add_trace(_norm_key("Beacon Juliet"), "color", entry_id, 1.0)
    svc._storage.supersede_entries(
        [stale_id], superseded_at=12345.0,
        superseded_by_text="historical correction")
    svc.delete(text=first)
    assert svc.cortex_lookup("Beacon Juliet", "color") is None


def test_forget_retires_digest_and_queues_regeneration(svc):
    from tests.test_session_digest import _FakeDigestExtractor
    from pseudolife_memory.service import MemoryService

    svc.episode_start_session("synthetic-session", "Buoy colors")
    forgotten = "Beacon Foxtrot was violet"
    kept = "Beacon Golf was orange"
    source_id = _stored_id(svc, forgotten)
    _stored_id(svc, kept)
    root = svc.episode_end_session("synthetic-session", run_dream=False)["id"]
    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["Foxtrot violet; Golf orange."]))["written"] == 1
    assert svc._episode_digest_body(root) == "Foxtrot violet; Golf orange."

    svc.delete(text=forgotten)
    assert svc._episode_digest_body(root) is None
    assert not any(e["source"] == "digest" for e in svc.search(
        "Foxtrot violet", sources=["digest"])["entries"])
    with svc._lock:
        assert root in svc._load_digest_cursor()["regenerate"]
    svc._storage.close()
    reloaded = MemoryService(data_dir=svc.data_dir)
    reloaded._ensure_init()
    reloaded.config.memory.dream.digest_enabled = True
    assert reloaded._episode_digest_body(root) is None
    with reloaded._lock:
        assert root in reloaded._load_digest_cursor()["regenerate"]
    extractor = _FakeDigestExtractor(["Golf orange remains."])
    assert reloaded.generate_digests_stage(extractor)["written"] == 1
    assert all("Foxtrot" not in context for context in extractor.contexts)
    assert reloaded._episode_digest_body(root) == "Golf orange remains."
    assert any("Foxtrot violet" in e.text and e.superseded_at is not None
               for b in reloaded._cms.bands for e in b.entries
               if e.source == "digest")
    assert reloaded._storage.get_entry(source_id) is None
    reloaded._storage.close()


def test_forget_removes_source_edge_and_flags_orphan_entity(svc):
    text = "Beacon Hotel uses Gamma Pump"
    source_id = _stored_id(svc, text)
    assert svc._link_dream_relations(
        [{"src": "Beacon Hotel", "relation": "uses", "dst": "Gamma Pump"}],
        batch_sources={"synthetic"},
        batch_entries=[{"text": text, "db_id": source_id}]) == 1
    row = svc._storage.conn.execute(
        "SELECT g.id, g.src_id FROM edges g JOIN edge_evidence ev "
        "ON ev.edge_id = g.id WHERE ev.entry_id = %s", (source_id,)).fetchone()
    assert row is not None
    edge_id, src = row
    svc.delete(text=text)
    row = svc._storage.conn.execute(
        "SELECT superseded_at FROM edges WHERE id=%s", (edge_id,)).fetchone()
    assert row[0] is not None
    assert svc._storage.conn.execute(
        "SELECT 1 FROM entities WHERE id=%s", (src,)).fetchone()
    assert any(f["type"] == "orphan" and "Beacon Hotel" in f["entities"]
               for f in svc.graph_review()["findings"])


def test_forget_preserves_outcome_derived_lesson(svc):
    text = "Beacon India is green"
    _stored_id(svc, text)
    svc.lesson_write("painting", "method", "Check the primer", support="agent")
    before = [(r.entity, r.attribute, r.value, r.status)
              for r in svc._lessons.records]
    svc.delete(text=text)
    assert [(r.entity, r.attribute, r.value, r.status)
            for r in svc._lessons.records] == before


def test_forget_storage_failure_does_not_drop_in_memory_entry(svc, monkeypatch):
    text = "Beacon Kilo is copper"
    source_id = _stored_id(svc, text)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(svc._storage, "forget_entry_ids", fail)
    with pytest.raises(RuntimeError, match="synthetic storage failure"):
        svc.delete(text=text)
    assert svc._storage.get_entry(source_id) is not None
    assert any(e.text == text for b in svc._cms.bands for e in b.entries)


def test_forget_removes_quoted_digest_from_briefing(svc):
    from tests.test_session_digest import _FakeDigestExtractor

    svc.episode_start_session("briefing-session", "Buoy colors")
    forgotten = "Beacon Lima was violet"
    _stored_id(svc, forgotten)
    _stored_id(svc, "Beacon Mike was orange")
    svc.episode_end_session("briefing-session", run_dream=False)
    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["Lima violet; Mike orange."]))["written"] == 1

    before = svc.session_briefing(include_coordination=False)
    assert "Lima violet" in before["markdown"]
    assert "Lima violet" in before["recap"]["summary"]
    assert svc.delete(text=forgotten)["deleted_count"] == 1
    after = svc.session_briefing(include_coordination=False)
    assert after["recap"]["title"] == "Buoy colors"
    assert "Lima violet" not in after["markdown"]
    assert "summary" not in after["recap"]

    assert svc.generate_digests_stage(
        _FakeDigestExtractor(["Mike orange remains."]))["written"] == 1
    regenerated = svc.session_briefing(include_coordination=False)
    assert "Lima violet" not in regenerated["markdown"]
    assert "Mike orange remains." in regenerated["markdown"]

@pytest.mark.parametrize("regenerating", [False, True])
def test_forget_during_digest_extraction_discards_then_regenerates(svc, regenerating):
    from tests.test_session_digest import _FakeDigestExtractor

    svc.episode_start_session("racing-session", "Buoy inspection")
    forgotten = "Beacon November was violet"
    _stored_id(svc, forgotten)
    _stored_id(svc, "Beacon Oscar remains orange")
    if regenerating:
        _stored_id(svc, "Beacon Papa was silver")
    root = svc.episode_end_session("racing-session", run_dream=False)["id"]
    if regenerating:
        assert svc.generate_digests_stage(_FakeDigestExtractor(
            ["November violet; Oscar orange; Papa silver."]))["written"] == 1
        svc.delete(text="Beacon Papa was silver")

    # A later candidate advances the cursor while the first must stay queued.
    svc.episode_start_session("later-session", "Pump inspection")
    _stored_id(svc, "The Delta pump passed its pressure check")
    later = svc.episode_end_session("later-session", run_dream=False)["id"]

    class ForgettingExtractor:
        def summarize_session(self, context_text, *, target_chars):
            if "November" in context_text:
                assert "violet" in context_text
                assert svc.delete(text=forgotten)["deleted_count"] == 1
                return "November violet; Oscar orange."
            return "The Delta pump passed."

    result = svc.generate_digests_stage(ForgettingExtractor())
    assert result == {"scanned": 2, "written": 1}
    assert svc._episode_digest_body(root) is None
    assert svc._episode_digest_body(later) == "The Delta pump passed."
    assert not any(e.source == "digest" and e.superseded_at is None
                   and "November" in e.text
                   for band in svc._cms.bands for e in band.entries)
    with svc._lock:
        cursor = svc._load_digest_cursor()
        assert root in cursor["regenerate"]
        assert cursor["ts"] == svc._cms.episodes.episodes[later].ended_at

    fresh = _FakeDigestExtractor(["Oscar orange remains."])
    assert svc.generate_digests_stage(fresh)["written"] == 1
    assert len(fresh.contexts) == 1
    assert "November" not in fresh.contexts[0]
    assert "Oscar" in fresh.contexts[0]
    assert svc._episode_digest_body(root) == "Oscar orange remains."
    with svc._lock:
        assert root not in svc._load_digest_cursor()["regenerate"]
