"""Forget-cascade failure-path regressions; synthetic fixture databases only."""


import pytest


from tests.test_forget_cascade import svc, _stored_id


from tests.pg_fixtures import pg_conn, pg_url


from tests.test_session_digest import _FakeDigestExtractor


def _session(svc, key, forgotten, kept):
    svc.episode_start_session(key, key)
    source_id = _stored_id(svc, forgotten)
    _stored_id(svc, kept)
    root = svc.episode_end_session(key, run_dream=False)["id"]
    return root, source_id


def test_disconnect_during_forget_is_atomic(svc, monkeypatch):
    root, source_id = _session(svc, "disconnect", "Beacon Red uses Pump Alpha", "Beacon Blue remains")
    svc.cortex_write("Beacon Red", "pump", "Pump Alpha", support="agent")
    from pseudolife_memory.memory.cortex import _norm_key
    svc._storage.add_trace(_norm_key("Beacon Red"), "pump", source_id, 1.0)
    assert svc.generate_digests_stage(_FakeDigestExtractor(["Beacon Red uses Pump Alpha."]))["written"] == 1
    original = svc._storage.set_meta

    def disconnect_then_meta(key, value):
        if key == "session_digest_cursor":
            svc._storage._conn.close()
        return original(key, value)

    monkeypatch.setattr(svc._storage, "set_meta", disconnect_then_meta)
    with pytest.raises(Exception):
        svc.delete(text="Beacon Red uses Pump Alpha")
    # Emit only synthetic state, never DSNs or metadata keys containing secrets.
    state = {
        "source_exists": svc._storage.get_entry(source_id) is not None,
        "fact_status": svc._storage.conn.execute("SELECT status FROM facts WHERE entity='Beacon Red'").fetchone()[0],
        "digest_retired": svc._storage.conn.execute("SELECT superseded_at IS NOT NULL FROM entries WHERE source='digest'").fetchone()[0],
        "resident_source_exists": any(e.db_id == source_id for b in svc._cms.bands for e in b.entries),
    }
    print("disconnect_state", state)
    assert state == {"source_exists": True, "fact_status": "current",
                     "digest_retired": False, "resident_source_exists": True}


def test_regeneration_retry_exhaustion_does_not_starve_later_session(svc):
    root, _ = _session(svc, "old", "Beacon Cyan is gone", "Beacon Green remains")
    assert svc.generate_digests_stage(_FakeDigestExtractor(["Cyan and Green."]))["written"] == 1
    svc.delete(text="Beacon Cyan is gone")
    later, _ = _session(svc, "new", "Beacon Yellow remains", "Beacon Orange remains")
    svc.config.memory.dream.digest_max_per_cycle = 1

    class MalformedOld:
        def summarize_session(self, context_text, *, target_chars):
            return None if "Green" in context_text else "Yellow and Orange."

    for _ in range(5):
        svc.generate_digests_stage(MalformedOld())
    assert svc._episode_digest_body(later) == "Yellow and Orange."


def test_ordinary_exception_rolls_back_all_derived_updates(svc, monkeypatch):
    root, source_id = _session(svc, "rollback", "Beacon Teal uses Pump Zeta", "Beacon Rose remains")
    svc.cortex_write("Beacon Teal", "pump", "Pump Zeta", support="agent")
    from pseudolife_memory.memory.cortex import _norm_key
    svc._storage.add_trace(_norm_key("Beacon Teal"), "pump", source_id, 1.0)
    assert svc.generate_digests_stage(_FakeDigestExtractor(["Teal uses Zeta."]))["written"] == 1
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic late failure")
    monkeypatch.setattr(svc._storage, "set_meta", fail)
    with pytest.raises(RuntimeError, match="synthetic late failure"):
        svc.delete(text="Beacon Teal uses Pump Zeta")
    assert svc._storage.get_entry(source_id) is not None
    assert svc.cortex_lookup("Beacon Teal", "pump")["value"] == "Pump Zeta"
    assert svc._storage.conn.execute("SELECT status FROM facts WHERE entity='Beacon Teal'").fetchone()[0] == "current"
    assert svc._episode_digest_body(root) == "Teal uses Zeta."
    assert not svc._storage.conn.execute("SELECT superseded_at IS NOT NULL FROM entries WHERE source='digest'").fetchone()[0]


def test_permanent_context_change_does_not_starve_other_sessions(svc):
    root, _ = _session(svc, "changing", "Beacon Slate stays", "Beacon Coral stays")
    later, _ = _session(svc, "later-stable", "Beacon Olive stays", "Beacon Indigo stays")
    svc.config.memory.dream.digest_max_per_cycle = 1
    class ChangingContext:
        def summarize_session(self, context_text, *, target_chars):
            if "Slate" in context_text:
                with svc._lock:
                    svc._cms.episodes.episodes[root].title += " changed"
                return "Slate and Coral."
            return "Olive and Indigo."
    for _ in range(5):
        svc.generate_digests_stage(ChangingContext())
    assert svc._episode_digest_body(later) == "Olive and Indigo."
    assert svc._episode_digest_body(root) is None
    # Deferral keeps the old root eligible once its context stabilizes.
    assert svc.generate_digests_stage(_FakeDigestExtractor(
        ["Slate and Coral remain."]))["written"] == 1
    assert svc._episode_digest_body(root) == "Slate and Coral remain."


def test_schema_49_50_51_upgrade(pg_conn):
    import subprocess
    import types
    from pseudolife_memory.storage import schema
    # Remove exactly the additive v50/v51 shapes in this fixture-owned DB.
    pg_conn.execute("DROP TABLE edge_evidence")
    pg_conn.execute("ALTER TABLE coordination_agents DROP COLUMN parent_thread, DROP COLUMN parent_agent_id")
    pg_conn.execute("UPDATE meta SET value='49'::jsonb WHERE key='schema_version'")
    for ref, expected in (("b81942cf^", 49), ("b81942cf", 50)):
        old = types.ModuleType("review_schema_" + str(expected))
        code = subprocess.check_output(["git", "show", ref + ":pseudolife_memory/storage/schema.py"], text=True, encoding="utf-8")
        exec(compile(code, old.__name__, "exec"), old.__dict__)
        old.ensure_schema(pg_conn)
        assert pg_conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == expected
        assert pg_conn.execute("SELECT to_regclass('public.edge_evidence')").fetchone()[0] is None
    schema.ensure_schema(pg_conn)
    assert pg_conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == 51
    assert pg_conn.execute("SELECT to_regclass('public.edge_evidence')").fetchone()[0] == "edge_evidence"
    assert pg_conn.execute("SELECT parent_thread, parent_agent_id FROM coordination_agents LIMIT 0").description


def test_legacy_edges_survive_and_fact_history_survives_hydration(svc):
    from pseudolife_memory.memory.cortex import _norm_key
    from pseudolife_memory.service import MemoryService
    text = "Beacon White uses Pump Eta"
    source_id = _stored_id(svc, text)
    svc.cortex_write("Beacon White", "pump", "Pump Eta", support="agent")
    svc._storage.add_trace(_norm_key("Beacon White"), "pump", source_id, 1.0)
    svc._link_dream_relations([{"src": "Beacon White", "relation": "uses", "dst": "Pump Eta"}], batch_sources={"synthetic"})
    edge_id = svc._storage.conn.execute("SELECT id FROM edges").fetchone()[0]
    svc.delete(text=text)
    svc._storage.close()
    reloaded = MemoryService(data_dir=svc.data_dir)
    try:
        reloaded._ensure_init()
        assert reloaded.cortex_lookup("Beacon White", "pump") is None
        records = [r for r in reloaded._cortex.records if r.entity == "Beacon White"]
        assert records[-1].status == "retired"
        assert str(source_id) in records[-1].superseded_by_value
        assert reloaded._storage.conn.execute("SELECT superseded_at FROM edges WHERE id=%s", (edge_id,)).fetchone()[0] is None
    finally:
        reloaded._storage.close()


@pytest.mark.parametrize("failure", ["malformed", "write"])
def test_exhausted_regeneration_preserves_advanced_cursor(svc, monkeypatch, failure):
    root, _ = _session(svc, "exhaustion", "Beacon Mint is gone", "Beacon Pearl remains")
    assert svc.generate_digests_stage(_FakeDigestExtractor(["Mint and Pearl."]))["written"] == 1
    svc.delete(text="Beacon Mint is gone")
    with svc._lock:
        cur = svc._load_digest_cursor()
        advanced = cur["ts"] + 100
        cur["ts"] = advanced
        svc._save_digest_cursor(cur)
    if failure == "write":
        def fail_write(*args, **kwargs):
            raise RuntimeError("synthetic digest write failure")
        monkeypatch.setattr(svc, "_store_digest", fail_write)
    for _ in range(2):
        svc.generate_digests_stage(_FakeDigestExtractor(
            [None if failure == "malformed" else "Pearl remains."]))
    with svc._lock:
        cur = svc._load_digest_cursor()
        assert cur["ts"] == advanced
        assert root not in cur.get("regenerate", [])
        assert root not in cur["retry"]
