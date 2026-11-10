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


def test_a_daemon_gone_before_its_clean_stop_names_its_exit():
    """Partial forced run 870fb28fc: the restart pair failed with "owned
    daemon already exited before clean stop" and no record of which daemon
    or how it ended. The error now names the side and its exit code or
    signal (-9 is SIGKILL, as the OOM killer sends)."""
    from types import SimpleNamespace
    gone = SimpleNamespace(poll=lambda: -9, returncode=-9)
    daemon = SimpleNamespace(kind="rust", proc=gone, log=Path("rs.log"), stop=lambda: None)
    with pytest.raises(RuntimeError, match=r"rust daemon already exited \(code -9, SIGKILL\) before clean stop"):
        graceful_stop(daemon)
    gone = SimpleNamespace(poll=lambda: 3, returncode=3)
    daemon = SimpleNamespace(kind="python", proc=gone, log=Path("py.log"), stop=lambda: None)
    with pytest.raises(RuntimeError, match=r"python daemon already exited \(code 3\) before clean stop"):
        graceful_stop(daemon)


def pruning_state():
    return {"catalog": {"sequences": [["public", "retrieval_events_id_seq", "bigint", 1, 1, 2]]},
            "rows": {"public.retrieval_events": {
                "columns": ["id", "query_text", "origin", "session_id", "episode_id", "served", "created_at"],
                "rows": [[1, "fixture", "search", None, None, [], 100.0],
                         [2, "fixture", "search", None, None, [], 9000000000000.0]]}}}


def test_pruning_preserves_seeded_retrieval_clocks_in_golden_comparison():
    import run as harness
    prior = pruning_state()
    prior["rows"]["public.relations"] = {
        "columns": ["id", "created_at"], "rows": [[1, 123.0]]}
    after = copy.deepcopy(prior)
    after["rows"]["public.retrieval_events"]["rows"].pop(0)
    scenario = harness.SCENARIOS["sweep-pruning"]()
    normalized = scenario.normalize_db(after, prior, "rust")
    assert normalized["rows"]["public.retrieval_events"]["rows"][0][-1] == 9000000000000.0
    assert normalized["rows"]["public.relations"]["rows"][0][-1] == "<created_at>"
    changed = copy.deepcopy(after)
    changed["rows"]["public.retrieval_events"]["rows"][0][-1] -= 1
    assert scenario.normalize_db(changed, prior, "rust") != normalized


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
    assert dbstate.UNPORTED_ROWS == {"cms_counters": "B4", "principal_activity": "v58-clock",
                                     "rejected_bearers": "v58-clock"}
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


def titled_session_state(child_first):
    root = ["z-root", None, "work", "fixture - durable title"]
    child = ["a-child", "z-root", "work", "child"]
    history = ["h-history", None, "history", "history"]
    return {"rows": {
        "public.episodes": {"columns": ["id", "parent_id", "session_key", "title"],
                            "rows": [child, history, root] if child_first else [root, child, history]},
        "public.entries": {"columns": ["episode_id", "episode_title"],
                           "rows": [["z-root", root[3]]]},
        "public.meta": {"columns": ["key", "value"], "rows": []},
    }}


@pytest.mark.parametrize("scenario", ["session-restart", "session-resume-refusal"])
@pytest.mark.parametrize("child_first", [False, True])
def test_reaper_title_validation_selects_root_despite_inherited_session_key(scenario, child_first):
    import run as harness
    # PostgreSQL row order can put the child first; it shares the root's
    # session key but deliberately keeps its explicit child title.
    harness.SCENARIOS[scenario]().validate_expected(titled_session_state(child_first))


@pytest.mark.parametrize("scenario", ["session-restart", "session-resume-refusal"])
@pytest.mark.parametrize("broken", ["root-title", "entry-stamp"])
def test_reaper_title_validation_still_rejects_missing_durable_effect(scenario, broken):
    import run as harness
    snapshot = titled_session_state(True)
    if broken == "root-title":
        snapshot["rows"]["public.episodes"]["rows"][-1][3] = "work fixture"
        message = "content-derived root title"
    else:
        snapshot["rows"]["public.entries"]["rows"][0][1] = "work fixture"
        message = "root entry title stamps"
    with pytest.raises(RuntimeError, match=message):
        harness.SCENARIOS[scenario]().validate_expected(snapshot)


# A copy of a real bank (the forced mirror run's PL_HARNESS_BASE_DATABASE)
# already holds deferred_empty_roots and episode_tombstones entries with
# close clocks from long before either daemon started. Every reaper and
# sweep scenario raised "close clock outside the arm's captured window" on
# it, because normalize_closes window-checked every entry of those maps.
OLD_ROOT, OLD_TOMB, NEW_ROOT = "o" * 32, "t" * 32, "a" * 32


def inherited_meta():
    return [["deferred_empty_roots", {OLD_ROOT: 5.0}],
            ["episode_tombstones", {OLD_TOMB: {"ended_at": 6.0, "session_key": "old"}}]]


def state_over_inherited_meta(root_value=5.0, tomb_value=6.0):
    snapshot = state()
    snapshot["rows"]["public.meta"]["rows"] = [
        ["deferred_empty_roots", {OLD_ROOT: root_value, NEW_ROOT: 100.0}],
        ["episode_tombstones", {OLD_TOMB: {"ended_at": tomb_value, "session_key": "old"}}]]
    return snapshot


def meta_of(snapshot):
    return {row[0]: row[1] for row in snapshot["rows"]["public.meta"]["rows"]}


@pytest.fixture(params=["before", "base"])
def inherited(request, monkeypatch):
    """The old entries as ``before`` holds them (a synthetic template), or
    only in the base, which a base dump leaves out of ``before`` and
    ``dbstate.prior_rows`` supplies."""
    import dbstate
    prior = before()
    if request.param == "before":
        prior["rows"]["public.meta"]["rows"] = inherited_meta()
        return prior
    real = dbstate.prior_rows

    def with_base(snapshot, qualified, current):
        rows = real(snapshot, qualified, current)
        if qualified == "public.meta":
            have = {row[0] for row in rows}
            rows += [row for row in inherited_meta() if row[0] not in have]
        return rows

    monkeypatch.setattr(dbstate, "prior_rows", with_base)
    return prior


def test_inherited_close_metadata_is_not_window_checked_and_stays_exact(inherited):
    projected = meta_of(normalize_closes(state_over_inherited_meta(), inherited, 90.0, 110.0))
    assert projected["deferred_empty_roots"] == {OLD_ROOT: 5.0, NEW_ROOT: "<close:" + NEW_ROOT + ">"}
    assert projected["episode_tombstones"][OLD_TOMB]["ended_at"] == 6.0


def test_an_arm_written_close_outside_the_window_still_fails_beside_inherited_ones(inherited):
    broken = state_over_inherited_meta()
    meta_of(broken)["deferred_empty_roots"]["n" * 32] = 120.0
    with pytest.raises(RuntimeError, match="outside the arm's captured window"):
        normalize_closes(broken, inherited, 90.0, 110.0)


def test_an_inherited_close_an_arm_rewrote_is_its_own_write(inherited):
    import dbstate
    # Rewritten to a clock outside the window: refused like any arm write.
    with pytest.raises(RuntimeError, match="outside the arm's captured window"):
        normalize_closes(state_over_inherited_meta(root_value=7.0), inherited, 90.0, 110.0)
    # Rewritten inside it: tokenized, so it still differs from an arm that
    # left the inherited value alone.
    kept = normalize_closes(state_over_inherited_meta(), inherited, 90.0, 110.0)
    rewrote = normalize_closes(state_over_inherited_meta(tomb_value=101.0), inherited, 90.0, 110.0)
    assert dbstate.diff(kept, rewrote)


def test_a_sweep_must_write_its_own_tombstone_on_a_bank_that_has_some(inherited):
    import run as harness
    scenario = harness.SCENARIOS["session-sweep"]()
    scenario.before = inherited
    snapshot = titled_session_state(False)
    snapshot["rows"]["public.meta"]["rows"] = inherited_meta()
    with pytest.raises(RuntimeError, match="did not persist a tombstone"):
        scenario.validate_expected(snapshot)
    # Review (Opus): a new tombstone of the base's own expired deferred root
    # is not the scenario's; only its empty session's tombstone counts.
    meta_of(snapshot)["episode_tombstones"]["o" * 32] = {"ended_at": 5.0, "session_key": "older"}
    with pytest.raises(RuntimeError, match="did not persist a tombstone"):
        scenario.validate_expected(snapshot)
    meta_of(snapshot)["episode_tombstones"][NEW_ROOT] = {"ended_at": 100.0, "session_key": "empty"}
    scenario.validate_expected(snapshot)


def test_a_sweeps_tombstone_of_an_inherited_deferred_root_keeps_its_old_clock(inherited):
    # A sweep tombstones every expired deferred root, the base's too, with
    # that root's own close clock: a carried clock, compared exactly.
    swept = state_over_inherited_meta()
    meta = meta_of(swept)
    del meta["deferred_empty_roots"][OLD_ROOT]
    meta["episode_tombstones"][OLD_ROOT] = {"ended_at": 5.0, "session_key": "older"}
    projected = meta_of(normalize_closes(swept, inherited, 90.0, 110.0))
    assert projected["episode_tombstones"][OLD_ROOT]["ended_at"] == 5.0
    # The near miss: any other old clock on it is not the root's own.
    meta["episode_tombstones"][OLD_ROOT]["ended_at"] = 4.0
    with pytest.raises(RuntimeError, match="outside the arm's captured window"):
        normalize_closes(swept, inherited, 90.0, 110.0)


# ---- a session-end dream's graph insight refresh on a real bank's copy ----

WINDOW = [(90.0, 110.0)]
COMMUNITY_COLUMNS = ["id", "label", "size", "cohesion", "computed_at"]


def refreshed(stamp, digest_stamp=None):
    """state() after a graph insight refresh at ``stamp``: the communities,
    their memberships, a derived entity source and meta's graph_digest."""
    s = state()
    s["rows"]["public.communities"] = {"columns": COMMUNITY_COLUMNS, "rows": [[1, "c", 2, 0.5, stamp]]}
    s["rows"]["public.entity_communities"] = {"columns": ["entity_id", "community_id", "computed_at"],
                                              "rows": [[7, 1, stamp]]}
    s["rows"]["public.entity_sources"] = {
        "columns": ["entity_id", "source", "count", "origin", "updated_at"],
        "rows": [[7, "fixture", 1, "derived", stamp]]}
    s["rows"]["public.meta"]["rows"].append(
        ["graph_digest", {"computed_at": stamp if digest_stamp is None else digest_stamp,
                          "communities": [{"id": 1, "size": 2}]}])
    return s


def test_a_graph_insight_refresh_compares_without_each_daemons_clock():
    """Partial forced run 870fb28fc: on a real bank's copy a session-end
    dream refreshes graph insight on both daemons, each at its own now();
    the partition and the counts agreed and only these clocks differed."""
    from background_sessions import normalize_refresh_clocks
    a = normalize_refresh_clocks(refreshed(101.0), before(), WINDOW)
    assert a == normalize_refresh_clocks(refreshed(104.5), before(), WINDOW)
    assert a["rows"]["public.communities"]["rows"][0][4] == "<computed_at>"
    # Near misses: the digest's clock outside the window, a different
    # membership, and another meta row's clock all still differ.
    assert normalize_refresh_clocks(refreshed(101.0, digest_stamp=120.0), before(), WINDOW) != a
    other = refreshed(104.5)
    other["rows"]["public.entity_communities"]["rows"][0][1] = 2
    assert normalize_refresh_clocks(other, before(), WINDOW) != a
    kept = refreshed(101.0)
    kept["rows"]["public.meta"]["rows"].append(["graph_digest_v0", {"computed_at": 101.0}])
    assert ["graph_digest_v0", {"computed_at": 101.0}] in \
        normalize_refresh_clocks(kept, before(), WINDOW)["rows"]["public.meta"]["rows"]


def test_an_existing_rows_rewritten_refresh_clock_still_differs():
    """A row ``before`` already held is compared exactly (dbstate.normalize):
    one arm restamping it is a difference, not a refresh to excuse."""
    from background_sessions import normalize_refresh_clocks
    prior = before()
    prior["rows"]["public.communities"] = {"columns": COMMUNITY_COLUMNS, "rows": [[1, "c", 2, 0.5, 50.0]]}
    assert normalize_refresh_clocks(refreshed(101.0), prior, WINDOW) != \
        normalize_refresh_clocks(refreshed(104.5), prior, WINDOW)


@pytest.mark.parametrize("name", ["session-reap", "session-sweep", "session-restart",
                                  "session-tombstone-restart"])
def test_the_session_scenarios_final_bank_ignores_the_refresh_clocks(name):
    import run as harness
    scenario = harness.SCENARIOS[name]()
    scenario.starts = {"python": 90.0}
    scenario.clock_windows = {"python": WINDOW}
    assert scenario.normalize_db(refreshed(101.0), before(), "python") == \
        scenario.normalize_db(refreshed(104.5), before(), "python")


def test_a_bank_that_held_a_graph_waits_for_the_dreams_graph_refresh(monkeypatch):
    """Partial forced run 870fb28fc: at the session milestone Rust had
    rewritten every base community row and Python none; Python's graph
    refresh had not run yet. On a bank that held a graph the capture also
    waits for a digest computed at or after this daemon's start."""
    import dbstate
    from background_sessions import graph_refresh_pending
    start = 100.0
    without = state()
    assert not graph_refresh_pending(without, before(), start)        # no graph: never waits
    held = before()
    held["rows"]["public.meta"]["rows"].append(["graph_digest", {"computed_at": 50.0}])
    held["rows"]["public.edges"] = {"columns": ["id"], "rows": [[1]]}
    assert graph_refresh_pending(without, held, start)               # the digest not yet rewritten
    stale = state()
    stale["rows"]["public.meta"]["rows"].append(["graph_digest", {"computed_at": 50.0}])
    assert graph_refresh_pending(stale, held, start)                 # the copy's old digest
    done = state()
    done["rows"]["public.meta"]["rows"].append(["graph_digest", {"computed_at": 101.0}])
    assert not graph_refresh_pending(done, held, start)
    # Under a base, the digest row is the base's, so before leaves it out.
    monkeypatch.setattr(dbstate, "base_keys",
                        lambda qualified: {'"graph_digest"'} if qualified == "public.meta" else set())
    assert not graph_refresh_pending(without, before(), start)       # ...with no edges left
    monkeypatch.setattr(dbstate, "base_keys", lambda qualified: {
        "public.meta": {'"graph_digest"'}, "public.edges": {"1"}}.get(qualified, set()))
    assert graph_refresh_pending(without, before(), start)
    assert not graph_refresh_pending(done, before(), start)


def test_a_bank_with_a_digest_but_no_edges_never_waits_for_a_refresh(monkeypatch):
    """Review (Codex P2): both daemons skip a graph with no edges
    (empty_graph, before writing any digest), so a copy that kept its old
    digest after its last edge was deleted would wait out the deadline."""
    import dbstate
    from background_sessions import graph_refresh_pending
    held = before()
    held["rows"]["public.meta"]["rows"].append(["graph_digest", {"computed_at": 50.0}])
    assert not graph_refresh_pending(state(), held, 100.0)
    edged = copy.deepcopy(held)
    edged["rows"]["public.edges"] = {"columns": ["id", "src_id", "relation", "dst_id"], "rows": [[1, 1, "r", 2]]}
    assert graph_refresh_pending(state(), edged, 100.0)
    # Edges the arm's own state holds count too (a dump keeps new rows).
    now = state()
    now["rows"]["public.edges"] = edged["rows"]["public.edges"]
    assert graph_refresh_pending(now, held, 100.0)


def test_the_observed_banks_meta_rows_are_keyed_so_a_difference_names_its_row():
    """A response-side meta difference printed the row by index
    (.json.db.rows.public.meta.rows[5][1]...), so nothing could tell the
    graph_digest row from another without reading its value. The observed
    bank gives meta as a map by key, the same on both sides."""
    import run as harness
    from background_sessions import keyed_meta
    a = state()
    a["rows"]["public.meta"]["rows"].append(["graph_digest", {"totals": {"communities": 3}}])
    b = state()
    b["rows"]["public.meta"]["rows"].insert(0, ["graph_digest", {"totals": {"communities": 4}}])
    ka, kb = keyed_meta(a), keyed_meta(b)
    assert ka["rows"]["public.meta"]["by_key"]["graph_digest"] == {"totals": {"communities": 3}}
    assert ka["rows"]["public.episodes"] == a["rows"]["public.episodes"]       # other tables as they were
    diffs = harness.diff_values(ka, kb)
    assert diffs and all(".by_key.graph_digest." in d for d in diffs), diffs   # row order no longer matters
    assert harness.diff_values(keyed_meta(state()), keyed_meta(state())) == []
