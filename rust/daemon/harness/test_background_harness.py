"""Rejecting controls for the background clock observation layer."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from background_sessions import normalize_closes, validate_restart_clocks, graceful_stop


def test_live_only_background_cases_remain_in_the_registered_corpus():
    import run as harness
    expected = {"session-reap", "session-sweep", "session-restart",
                "session-tombstone-restart", "session-resume-refusal", "sweep-recovery"}
    assert expected <= harness.SCENARIOS.keys()
    for name in expected:
        case = harness.SCENARIOS[name]()
        assert type(case).timeline is not harness.Scenario.timeline
        assert not case.golden_replay
    assert harness.SCENARIOS["sweep-pruning"].golden_replay


def state():
    return {"rows": {
        "public.episodes": {"columns": ["id", "parent_id", "ended_at"],
                            "rows": [["a" * 32, None, 100.0], ["b" * 32, "a" * 32, 100.0]]},
        "public.client_sessions": {"columns": ["session_key", "episode_ids", "ended_at"],
                                   "rows": [["fixture", ["a" * 32], 100.0]]},
        "public.meta": {"columns": ["key", "value"],
                        "rows": [["deferred_empty_roots", {"a" * 32: 100.0}]]},
    }}


def before():
    prior = state()
    for row in prior["rows"]["public.episodes"]["rows"]: row[2] = None
    prior["rows"]["public.client_sessions"]["rows"][0][2] = None
    prior["rows"]["public.meta"]["rows"] = []
    return prior


def test_clock_projection_preserves_raw_and_root_association():
    raw = state()
    projected = normalize_closes(raw, before(), 90.0, 110.0)
    assert raw == state()
    assert projected["rows"]["public.client_sessions"]["rows"][0][2] == "<close:" + "a" * 32 + ">"


@pytest.mark.parametrize("target,value", [("child", 100.1), ("client", 100.1), ("root", 120.0)])
def test_inconsistent_or_out_of_window_clock_is_rejected(target, value):
    broken = state()
    if target == "client": broken["rows"]["public.client_sessions"]["rows"][0][2] = value
    else: broken["rows"]["public.episodes"]["rows"][target == "child"][2] = value
    with pytest.raises(RuntimeError): normalize_closes(broken, before(), 90.0, 110.0)


def test_restart_cannot_rewrite_all_clocks_consistently():
    closed = state()
    changed = copy.deepcopy(closed)
    for row in changed["rows"]["public.episodes"]["rows"]: row[2] = 101.0
    changed["rows"]["public.client_sessions"]["rows"][0][2] = 101.0
    changed["rows"]["public.meta"]["rows"][0][1]["a" * 32] = 101.0
    with pytest.raises(RuntimeError): validate_restart_clocks(changed, closed)


def test_consistently_reversed_root_close_order_is_rejected_by_comparison():
    a, prior = state(), before()
    a["rows"]["public.episodes"]["rows"][1][1:] = [None, 101.0]
    prior["rows"]["public.episodes"]["rows"][1][1] = None
    b = copy.deepcopy(a)
    b["rows"]["public.episodes"]["rows"][0][2] = 101.0
    b["rows"]["public.episodes"]["rows"][1][2] = 100.0
    b["rows"]["public.client_sessions"]["rows"][0][2] = 101.0
    b["rows"]["public.meta"]["rows"][0][1]["a" * 32] = 101.0
    assert normalize_closes(a, prior, 90.0, 110.0) != normalize_closes(b, prior, 90.0, 110.0)


def test_signal_termination_is_not_a_controlled_clean_exit():
    import signal
    from types import SimpleNamespace
    proc = SimpleNamespace(poll=lambda: None, send_signal=lambda _: None,
                           wait=lambda timeout: -signal.SIGTERM)
    daemon = SimpleNamespace(proc=proc, log=Path("fixture.log"), stop=lambda: None)
    with pytest.raises(RuntimeError): graceful_stop(daemon)


def pruning_state():
    return {"catalog": {"sequences": [["public", "retrieval_events_id_seq", "bigint", 1, 1, 2]]},
            "rows": {"public.retrieval_events": {
                "columns": ["id", "query_text", "origin", "session_id", "episode_id", "served", "created_at"],
                "rows": [[1, "fixture", "search", None, None, [], 100.0],
                         [2, "fixture", "search", None, None, [], 9000000000000.0]]}}}


@pytest.mark.parametrize("change", ["rewrite", "sequence", "new-event", "warmup-extra-sequence"])
def test_pruning_scrub_rejects_rewrites_and_unexplained_sequence_changes(change):
    import run as harness
    import dbstate
    prior = pruning_state()
    correct = copy.deepcopy(prior)
    correct["rows"]["public.retrieval_events"]["rows"].pop(0)
    broken = copy.deepcopy(correct)
    if change == "rewrite":
        rewritten = copy.deepcopy(prior["rows"]["public.retrieval_events"]["rows"][0])
        rewritten[-1] = 200.0
        broken["rows"]["public.retrieval_events"]["rows"].insert(0, rewritten)
    elif change == "new-event":
        broken["rows"]["public.retrieval_events"]["rows"].append(
            [3, "unexpected", "search", None, None, [], 300.0])
    elif change == "warmup-extra-sequence":
        broken["rows"]["public.retrieval_events"]["rows"].append(
            [3, "warmup probe", "search", None, None, [], 300.0])
        broken["catalog"]["sequences"][0][-1] = 4
    else:
        broken["catalog"]["sequences"][0][-1] = 3
    a = harness.scrub_declared_rows(correct, prior)["state"]
    b = harness.scrub_declared_rows(broken, prior)["state"]
    assert dbstate.diff(a, b)


def test_dump_reads_every_table_from_one_snapshot(monkeypatch):
    # PR #882's Rust run 39422947014: a prune committed between the reads of
    # dream_run_slots and dream_runs, so one dump held 4 slots beside 2 runs.
    import dbstate
    import psycopg
    import pgdisposable as pg
    name = pg.name(pg.PREFIX + "dump_snapshot")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE dream_runs (id bigint PRIMARY KEY)")
            conn.execute("CREATE TABLE dream_run_slots (id bigint PRIMARY KEY, run_id bigint "
                          "NOT NULL REFERENCES dream_runs(id) ON DELETE CASCADE)")
            conn.execute("INSERT INTO dream_runs VALUES (1), (2), (3), (4)")
            conn.execute("INSERT INTO dream_run_slots VALUES (1, 1), (2, 2), (3, 3), (4, 4)")
        order_columns = dbstate._order_columns

        def prune_between_tables(conn, schema, table):
            if table == "dream_runs":  # read after dream_run_slots: commit the prune now
                with psycopg.connect(dsn, autocommit=True) as other:
                    other.execute("DELETE FROM dream_runs WHERE id NOT IN "
                                  "(SELECT id FROM dream_runs ORDER BY id DESC LIMIT 2)")
            return order_columns(conn, schema, table)

        monkeypatch.setattr(dbstate, "_order_columns", prune_between_tables)
        state = dbstate.dump(dsn)
        runs = state["rows"]["public.dream_runs"]["rows"]
        slots = state["rows"]["public.dream_run_slots"]["rows"]
        assert len(runs) == 4, "the dump's snapshot must predate the prune"
        assert {r[0] for r in runs} == {s[1] for s in slots}, (runs, slots)
    finally:
        pg.drop(name)


def test_dump_declares_tables_whose_writer_is_not_ported_yet():
    # cms_counters (schema v57, D2): Python's flush writes the row, Rust's
    # autosave is a no-op until B4. Its catalog is still dumped and compared;
    # its rows are not, until B4 deletes the entry.
    import dbstate
    import psycopg
    import pgdisposable as pg
    assert dbstate.UNPORTED_ROWS == {"cms_counters": "B4"}
    name = pg.name(pg.PREFIX + "dump_unported")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE cms_counters (id int PRIMARY KEY)")
            conn.execute("CREATE TABLE entries (id int PRIMARY KEY)")
            conn.execute("INSERT INTO cms_counters VALUES (1)")
            conn.execute("INSERT INTO entries VALUES (1)")
        state = dbstate.dump(dsn)
        assert state["rows"]["public.cms_counters"] == {"columns": ["id"], "rows": []}
        assert state["rows"]["public.entries"]["rows"] == [[1]]
        assert any(c[1] == "cms_counters" for c in state["catalog"]["columns"])
    finally:
        pg.drop(name)


def test_a_cloned_bank_starts_without_unported_rows():
    # A seeding Python daemon's exit flush writes the cms_counters row into
    # the template; each arm's clone starts without it, as both arms did
    # when the counters lived in the seeder's own weights.pt.
    import psycopg
    import pgdisposable as pg
    template, clone = pg.name(pg.PREFIX + "clone_t"), pg.name(pg.PREFIX + "clone_a")
    try:
        with psycopg.connect(pg.create(template), autocommit=True) as conn:
            conn.execute("CREATE TABLE cms_counters (id int PRIMARY KEY)")
            conn.execute("CREATE TABLE entries (id int PRIMARY KEY)")
            conn.execute("INSERT INTO cms_counters VALUES (1)")
            conn.execute("INSERT INTO entries VALUES (1)")
        with psycopg.connect(pg.create(clone, template=template)) as conn:
            assert conn.execute("SELECT count(*) FROM cms_counters").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
        with psycopg.connect(pg.dsn(template)) as conn:
            assert conn.execute("SELECT count(*) FROM cms_counters").fetchone()[0] == 1
    finally:
        pg.drop(clone)
        pg.drop(template)


def test_dump_never_deadlocks_a_concurrent_schema_transaction(monkeypatch):
    # A daemon's ensure_schema is one transaction taking ACCESS EXCLUSIVE
    # locks in schema order; the harness polls dumps (table-name order)
    # during it. The dump must give way, never hold a lock while it waits.
    import threading
    import dbstate
    import psycopg
    import pgdisposable as pg
    name = pg.name(pg.PREFIX + "dump_ddl")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            # No indexes: the catalog phase cannot block first, so an unsafe
            # dump reaches the row reads holding dream_run_slots.
            conn.execute("CREATE TABLE dream_runs (id bigint)")
            conn.execute("CREATE TABLE dream_run_slots (id bigint, run_id bigint)")
            conn.execute("INSERT INTO dream_runs VALUES (1)")
            conn.execute("INSERT INTO dream_run_slots VALUES (1, 1)")
        slots_read = threading.Event()
        order_columns = dbstate._order_columns

        def mark(conn, schema, table):
            if table == "dream_runs":
                slots_read.set()
            return order_columns(conn, schema, table)

        monkeypatch.setattr(dbstate, "_order_columns", mark)
        result = {}

        def run_dump():
            try:
                result["state"] = dbstate.dump(dsn)
            except Exception as exc:  # reported by the assertion below
                result["error"] = exc

        with psycopg.connect(dsn) as ddl:
            ddl.execute("SET lock_timeout = '10s'")
            ddl.execute("ALTER TABLE dream_runs ADD COLUMN IF NOT EXISTS note text")
            dumper = threading.Thread(target=run_dump)
            dumper.start()
            slots_read.wait(2)  # an unsafe dump now holds dream_run_slots and waits on dream_runs
            ddl.execute("ALTER TABLE dream_run_slots ADD COLUMN IF NOT EXISTS note text")
            ddl.commit()
        dumper.join(30)
        assert not dumper.is_alive()
        assert "error" not in result, result.get("error")
        columns = result["state"]["rows"]["public.dream_runs"]["columns"]
        assert "note" in columns, "the dump read the bank after the schema transaction"
    finally:
        pg.drop(name)


def test_only_one_proven_warmup_sequence_increment_is_waived():
    import run as harness
    prior = pruning_state()
    changed = copy.deepcopy(prior)
    changed["rows"]["public.retrieval_events"]["rows"].append(
        [3, "warmup probe", "search", None, None, [], 300.0])
    changed["catalog"]["sequences"][0][-1] = 3
    raw = copy.deepcopy(changed)
    assert harness.scrub_declared_rows(changed, prior)["state"] == prior
    assert changed == raw
