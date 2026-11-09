"""Route scenarios whose fixtures must also work on a copy of a real bank.

The forced mirror run (``forced.py``) runs every route scenario a second time
on a copy of a restored real bank (``PL_HARNESS_BASE_DATABASE``), whose
tables already hold entries, episodes, close metadata, relations and edge
proposals. The synthetic arms passed while these errored on that copy:

* the reaper and sweep scenarios window-checked the base's own old close
  clocks ("close clock outside the arm's captured window");
* ``dim-mismatch`` created ``entries`` over the existing table;
* ``stamped`` deleted the ``uses`` relation that edge proposals reference;
* ``json-float-shortest-ties`` asked for the top 3 "memory" hits, and the
  bank's own entries took a place from a fixture row.

Each case seeds a disposable bank with the kind of rows a real bank holds,
and its near miss keeps the empty-bank behaviour or the check that matters.
Needs the disposable-bank login (``pgdisposable``); without one it skips.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[3]))

import database_gate  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable  # noqa: E402

OLD_ROOT, OLD_TOMB, NEW_ROOT = "o" * 32, "t" * 32, "a" * 32
REAL_TEXT = "The memory daemon keeps a durable memory of note {} for the bank."


def require_login():
    if not pgdisposable.LOGIN_FILE.exists():
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")


def unit(vector):
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def seed_like_a_real_bank(conn, entries=40):
    """Rows a real bank holds before any scenario writes: entries at this
    build's dimension whose text matches the scenarios' queries, entities
    with an edge and an edge proposal on builtin relations."""
    rnd = random.Random(1024)
    for i in range(entries):
        vector = unit([rnd.gauss(0, 1) for _ in range(1024)])
        if i == 0:
            # A real entry leaning toward the offline "memory" query (cosine
            # 0.5): above the old third fixture row's 0.28.
            vector = [0.4, 0.3] + [0.0] * 1022
            vector[2] = math.sqrt(1 - 0.25)
        conn.execute("INSERT INTO entries (band, text, embedding, ts, source) "
                     "VALUES ('flat', %s, %s::vector, %s, 'agent')",
                     (REAL_TEXT.format(i), json.dumps(vector), 1.7e9 + i))
    conn.execute("INSERT INTO entities (canonical, display, created_at) VALUES "
                 "('daemon', 'daemon', 1.6e9), ('bank', 'bank', 1.6e9), ('cli', 'cli', 1.6e9)")
    ids = dict(conn.execute("SELECT canonical, id FROM entities").fetchall())
    conn.execute("INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, created_at) "
                 "VALUES (%s, 'uses', %s, 0.7, 1.6e9)", (ids["daemon"], ids["bank"]))
    conn.execute("INSERT INTO edges (src_id, relation, dst_id, asserted_at) "
                 "VALUES (%s, 'part-of', %s, 1.6e9)", (ids["cli"], ids["daemon"]))
    # Builtin 'runs-on' already names 'hosts' as its inverse: a third reference.


@pytest.fixture
def route_bank(monkeypatch):
    """A bank in the route harness's own slice (its prepare_template refuses
    any other), initialized, with no entries unless the test seeds them."""
    require_login()
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    import run
    from pseudolife_memory.storage.postgres import PostgresStorage
    name = run.pg.name(run.pg.PREFIX + "realbase")
    dsn = run.pg.create(name)
    try:
        PostgresStorage(dsn).close()  # the schema and its builtin relations
        yield dsn
    finally:
        monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
        run.pg.drop(name)


@pytest.fixture
def empty_route_bank(monkeypatch):
    require_login()
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    import run
    name = run.pg.name(run.pg.PREFIX + "emptybase")
    dsn = run.pg.create(name)
    try:
        yield dsn
    finally:
        run.pg.drop(name)


def embedding_dim(conn):
    return conn.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = "
                        "to_regclass('public.entries') AND attname = 'embedding'").fetchone()[0]


def test_dim_mismatch_prepares_a_bank_that_already_has_entries(route_bank):
    import psycopg
    import run
    from pseudolife_memory.storage.postgres import PostgresStorage
    with psycopg.connect(route_bank, autocommit=True) as conn:
        seed_like_a_real_bank(conn)
    run.DimMismatch().prepare_template(route_bank)
    with psycopg.connect(route_bank, autocommit=True) as conn:
        assert embedding_dim(conn) == 384
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 40
    # What the scenario is for: the daemon refuses this bank before any DDL.
    with pytest.raises(RuntimeError, match=r"entries\.embedding is vector\(384\)"):
        PostgresStorage(route_bank)


def test_dim_mismatch_on_an_empty_bank_still_creates_its_one_table(empty_route_bank):
    import psycopg
    import run
    run.DimMismatch().prepare_template(empty_route_bank)
    with psycopg.connect(empty_route_bank, autocommit=True) as conn:
        assert embedding_dim(conn) == 384
        tables = conn.execute("SELECT table_name FROM information_schema.tables "
                              "WHERE table_schema = 'public'").fetchall()
        assert tables == [("entries",)]
        columns = conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                               "table_name = 'entries' ORDER BY ordinal_position").fetchall()
        assert columns == [("id",), ("embedding",)]


def relations(conn):
    return {r[0] for r in conn.execute("SELECT name FROM relations").fetchall()}


def test_stamped_prepares_a_bank_whose_relations_are_referenced(route_bank):
    import psycopg
    import run
    from pseudolife_memory.storage.postgres import PostgresStorage
    with psycopg.connect(route_bank, autocommit=True) as conn:
        seed_like_a_real_bank(conn)
        seeded = relations(conn)
    run.StampedBank().prepare_template(route_bank)
    with psycopg.connect(route_bank, autocommit=True) as conn:
        left = relations(conn)
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == 99
        assert conn.execute("SELECT count(*) FROM edge_proposals").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 1
    # The tripwire an initialization would put back is still one builtin
    # relation, now one nothing references.
    gone = seeded - left
    assert len(gone) == 1 and left < seeded
    assert {"uses", "part-of", "hosts"} <= left
    with pytest.raises(RuntimeError, match="newer than"):
        PostgresStorage(route_bank)


def test_stamped_on_an_empty_bank_still_deletes_uses(empty_route_bank):
    import psycopg
    import run
    run.StampedBank().prepare_template(empty_route_bank)
    with psycopg.connect(empty_route_bank, autocommit=True) as conn:
        assert "uses" not in relations(conn)
        assert "part-of" in relations(conn)


def fixture_search(dsn, tmp_path, monkeypatch):
    """The scenario's case through Python's real search route on ``dsn``,
    with an embedder answering the offline fixture's "memory" direction
    (the ONNX fixture itself needs ``onnx``, which CI's harness job lacks)."""
    pytest.importorskip("torch")
    import torch
    import run
    import pseudolife_memory.service as service_mod
    from pseudolife_memory.web.routes import ConsoleRoutes

    class FixtureDirection:
        backend, _device, dtype, embedding_dim = "fixture", "cpu", "fp32", 1024

        def __init__(self, config):
            self.config = config

        def describe(self):
            return {"backend": self.backend, "device": "cpu", "dtype": "fp32"}

        def encode(self, texts, normalize=True):
            texts = [texts] if isinstance(texts, str) else texts
            out = torch.zeros((len(texts), 1024))
            out[:, 0], out[:, 1] = 0.8, 0.6
            return out

        def encode_single(self, text, normalize=True):
            return self.encode(text).squeeze(0)

        def encode_query(self, text, normalize=True):
            return self.encode_single(text)

    scenario = run.JsonFloatTies()
    scenario.prepare_template(dsn)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(json.dumps({"memory": scenario.memory_config}), encoding="utf-8")
    monkeypatch.setattr(service_mod, "EmbeddingPipeline", FixtureDirection)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(data))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    svc = service_mod.MemoryService(data_dir=str(data))
    try:
        svc._ensure_init()
        (case,) = scenario.cases()
        path, _, query = case["path"].partition("?")
        found = ConsoleRoutes(svc).dispatch("GET", path, dict(urllib.parse.parse_qsl(query)), {})
    finally:
        svc._storage.close()
    return case, found


def test_float_ties_finds_its_fixed_rows_on_a_bank_with_matching_entries(route_bank, tmp_path,
                                                                         monkeypatch):
    import psycopg
    with psycopg.connect(route_bank, autocommit=True) as conn:
        seed_like_a_real_bank(conn)
    case, found = fixture_search(route_bank, tmp_path, monkeypatch)
    entries = found["entries"]
    assert sorted(e["timestamp"] for e in entries) == sorted(float(t) for t in case["expected_timestamps"])
    assert [e["text"] for e in entries] == [f"memory tie {i}" for i in range(3)]


def test_float_ties_on_an_empty_bank_keep_the_recorded_answer(route_bank, tmp_path, monkeypatch):
    """The near miss: the committed golden's rows, in its order and shape."""
    _, found = fixture_search(route_bank, tmp_path, monkeypatch)
    golden = run_golden("json-float-shortest-ties")["responses"][0]["json"]
    free = {"access_count", "score"}  # the golden's own "<free ...>" values
    assert found.keys() == golden.keys()
    assert ({k: v for k, v in found.items() if k != "entries"}
            == {k: v for k, v in golden.items() if k != "entries"})
    assert ([{k: v for k, v in e.items() if k not in free} for e in found["entries"]]
            == [{k: v for k, v in g.items() if k not in free} for g in golden["entries"]])


def run_golden(name):
    import run
    return run.load_golden(name)


# --- the reaper's close clocks on a real base --------------------------------

mir = pgdisposable.Slice("mir")


@pytest.fixture
def close_base(monkeypatch):
    """A base holding close metadata from long ago, as a real bank does."""
    require_login()
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    import psycopg
    from psycopg.types.json import Jsonb
    from pseudolife_memory.storage.schema import ensure_schema
    name = mir.name("pl_cf_mir_closebase")
    with psycopg.connect(mir.create(name), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        ensure_schema(conn)
        conn.execute("INSERT INTO meta (key, value) VALUES ('deferred_empty_roots', %s), "
                     "('episode_tombstones', %s)",
                     (Jsonb({OLD_ROOT: 1.6e9}),
                      Jsonb({OLD_TOMB: {"ended_at": 1.6e9 + 1, "session_key": "old"}})))
    dbstate._BASE_HASHES.clear()
    yield name
    dbstate._BASE_HASHES.clear()
    mir.drop(name)


def arm_after_close(base, monkeypatch, label, rewrite_old=False):
    """A copy of the base as one arm leaves it: one new deferred root closed
    now, beside the base's own entries; ``before`` is the copy beforehand."""
    import psycopg
    monkeypatch.setenv(pgdisposable.BASE_ENV, base)
    template = mir.name(f"pl_cf_mir_{label}_t")
    before = dbstate.dump(mir.create(template))
    arm = mir.name(f"pl_cf_mir_{label}_a")
    dsn = mir.create(arm, template=template)
    start = time.time()
    with psycopg.connect(dsn, autocommit=True) as conn:
        value = {NEW_ROOT: time.time()}
        if rewrite_old:
            value[OLD_ROOT] = time.time()
        conn.execute("UPDATE meta SET value = value || %s::jsonb WHERE key = 'deferred_empty_roots'",
                     (json.dumps(value),))
    state = dbstate.dump(dsn)
    for name in (arm, template):
        mir.drop(name)
    return state, before, start, time.time()


def test_reaper_closes_on_a_base_window_check_only_the_arms_own(close_base, monkeypatch):
    from background_sessions import normalize_closes
    state, before, start, end = arm_after_close(close_base, monkeypatch, "closekeep")
    assert before["rows"]["public.meta"]["rows"] == []  # the base's rows are left out
    projected = {r[0]: r[1] for r in normalize_closes(state, before, start, end)["rows"]["public.meta"]["rows"]}
    assert projected["deferred_empty_roots"] == {OLD_ROOT: 1.6e9, NEW_ROOT: f"<close:{NEW_ROOT}>"}


def test_reaper_closes_on_a_base_still_show_a_rewritten_base_entry(close_base, monkeypatch):
    from background_sessions import normalize_closes
    kept = normalize_closes(*arm_after_close(close_base, monkeypatch, "closekept"))
    rewrote = normalize_closes(*arm_after_close(close_base, monkeypatch, "closerw", rewrite_old=True))
    assert dbstate.diff(kept, rewrote)
