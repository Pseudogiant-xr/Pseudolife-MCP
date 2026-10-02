"""Per-run test-database isolation (no PG server needed).

The ``pg_conn`` fixture reaps every other backend on the test database
before truncating — correct within one run, lethal across runs: two
concurrent ``pytest tests/`` invocations sharing one database terminate
each other's live connections (AdminShutdown, a different victim set
every run). The contract pinned here is that each pytest process gets
its own private database, so the reaper can only ever hit this run's
leaked backends. An explicit ``PSEUDOLIFE_TEST_DATABASE_URL`` keeps its
connection settings and database name (CI's isolated service container uses a
fixed name).
"""

from __future__ import annotations

import os

from tests import pg_defaults, pg_fixtures


def test_default_test_db_is_private_to_this_run(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    url = pg_fixtures.resolve_test_db_url()
    db = url.rsplit("/", 1)[1]
    assert db == f"pseudolife_memory_test_{pg_defaults.run_suffix()}"


# ── Process namespaces: Windows and WSL share one dev server ────────────────
# A run names its database after its pid, and a later run drops the ones
# whose pid is gone. A WSL pid is invisible to Windows (and the reverse), so
# on 2026-10-02 a Windows pytest session dropped a running WSL suite's
# database mid-run (78 setup errors). A run inside WSL tags its names, and
# each run prunes only its own namespace's.

def test_a_run_inside_wsl_tags_its_database_names():
    assert pg_defaults.run_suffix(123, namespace="") == "123"
    assert pg_defaults.run_suffix(123, namespace="wsl") == "wsl123"
    assert pg_defaults.own_run_pid("123", namespace="") == 123
    assert pg_defaults.own_run_pid("wsl123", namespace="wsl") == 123
    # Never another namespace's, and never a name that is not a run's.
    assert pg_defaults.own_run_pid("wsl123", namespace="") is None
    assert pg_defaults.own_run_pid("123", namespace="wsl") is None
    for other in ("gw0", "proof", "", "wsl", "wslx1"):
        assert pg_defaults.own_run_pid(other, namespace="") is None
        assert pg_defaults.own_run_pid(other, namespace="wsl") is None


def test_a_private_bank_keeps_the_runs_suffix_last_so_pruning_finds_it():
    from tests.helpers import private_bank_name

    assert private_bank_name("pseudolife_memory_test_123", "shim") == \
        "pseudolife_memory_test_shim_123"
    assert private_bank_name("pseudolife_memory_test_wsl123", "shim") == \
        "pseudolife_memory_test_shim_wsl123"
    assert private_bank_name("ci_fixed_db", "shim") == "ci_fixed_db_shim"
    suffix = private_bank_name("pseudolife_memory_test_wsl123", "shim").rsplit("_", 1)[1]
    assert pg_defaults.own_run_pid(suffix, namespace="wsl") == 123


class _PruneConn:
    def __init__(self, names):
        self.names, self.dropped = names, []

    def execute(self, sql):
        if sql.startswith("DROP DATABASE"):
            self.dropped.append(sql.split('"')[1])
            return self
        self._rows = [(name,) for name in self.names]
        return self

    def fetchall(self):
        return self._rows


def test_pruning_drops_only_this_namespaces_dead_runs(monkeypatch):
    monkeypatch.setattr(pg_fixtures, "_pid_alive", lambda pid: False)
    names = ["pseudolife_memory_test_111", "pseudolife_memory_test_wsl222",
             "pseudolife_memory_bench_333", "pseudolife_memory_bench_wsl444",
             "pseudolife_memory_test_gw0"]
    native = _PruneConn(names)
    pg_fixtures._prune_dead_run_dbs(native, namespace="")
    assert native.dropped == ["pseudolife_memory_test_111", "pseudolife_memory_bench_333"]
    wsl = _PruneConn(names)
    pg_fixtures._prune_dead_run_dbs(wsl, namespace="wsl")
    assert wsl.dropped == ["pseudolife_memory_test_wsl222", "pseudolife_memory_bench_wsl444"]


def test_wsl_is_detected_without_microsoft_in_the_kernel_release(tmp_path):
    """A custom WSL kernel need not say "microsoft" in its release; the
    distro's environment or its interop binfmt entry still gives it away."""
    custom = "6.6.0-custom"
    nothing = tmp_path / "no-binfmt"
    assert pg_defaults._in_wsl(platform="linux", release="6.6.114.1-microsoft-standard-WSL2",
                               environ={}, binfmt=nothing)
    assert pg_defaults._in_wsl(platform="linux", release=custom,
                               environ={"WSL_DISTRO_NAME": "Ubuntu"}, binfmt=nothing)
    assert pg_defaults._in_wsl(platform="linux", release=custom,
                               environ={"WSL_INTEROP": "/run/WSL/1_interop"}, binfmt=nothing)
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()
    (binfmt / "WSLInterop-late").write_text("enabled\n")
    assert pg_defaults._in_wsl(platform="linux", release=custom, environ={}, binfmt=binfmt)
    # Plain Linux (the homelab box) and Windows, whatever its environment
    # forwards, are not WSL.
    assert not pg_defaults._in_wsl(platform="linux", release=custom, environ={}, binfmt=nothing)
    assert not pg_defaults._in_wsl(platform="win32", release="11",
                                   environ={"WSL_DISTRO_NAME": "Ubuntu"}, binfmt=binfmt)


# The eval harnesses make private databases on the same dev server. A name
# with a bare pid from a WSL run is one a Windows run's pruner would drop as
# a dead run's (and an untagged one could collide with a Windows pid).

def _eval_module(name):
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    return importlib.import_module(name)


def _windows_pruner_spares(name: str) -> bool:
    return pg_defaults.own_run_pid(name.rsplit("_", 1)[1], namespace="") is None


def test_the_epistemic_bench_names_its_database_in_the_run_namespace(monkeypatch):
    eb = _eval_module("epistemic_bench")
    monkeypatch.setattr(pg_defaults, "PID_NAMESPACE", "wsl")
    name = eb._bench_db_name()
    assert name == f"pseudolife_memory_bench_wsl{os.getpid()}"
    assert _windows_pruner_spares(name)
    # Its drop guard follows the tagged name, and a bare-pid twin (a
    # Windows run's) is not this run's to drop.
    with __import__("pytest").raises(SystemExit, match="did not create it"):
        eb.drop_bench_db(f"pseudolife_memory_bench_{os.getpid()}")


def test_the_audit_volume_scratch_database_is_named_in_the_run_namespace(monkeypatch):
    from contextlib import contextmanager

    import pytest

    from evals import coordination_audit_volume as harness

    monkeypatch.setattr(pg_defaults, "PID_NAMESPACE", "wsl")
    executed, connects = [], []

    class Admin:
        def execute(self, statement):
            executed.append(statement.as_string())

    @contextmanager
    def connect(dsn, **kwargs):
        connects.append(dsn)
        if len(connects) == 2:  # the scratch database itself: stop here
            raise RuntimeError("stop before the schema")
        yield Admin()

    monkeypatch.setattr(harness.psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="stop before the schema"):
        with harness.scratch_database("host=127.0.0.1 port=6543 dbname=postgres"):
            pass
    name = f"pseudolife_memory_bench_audit_wsl{os.getpid()}"
    assert executed == [f'CREATE DATABASE "{name}"',
                        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)']
    assert _windows_pruner_spares(name)


def test_the_coordination_bench_database_is_named_in_the_run_namespace(monkeypatch):
    from contextlib import contextmanager

    from psycopg.conninfo import conninfo_to_dict

    from evals import coordination_bench as harness

    monkeypatch.setattr(pg_defaults, "PID_NAMESPACE", "wsl")
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)

    class Admin:
        def execute(self, statement):
            pass

    @contextmanager
    def connect(dsn, **kwargs):
        yield Admin()

    monkeypatch.setattr(harness.psycopg, "connect", connect)
    with harness.disposable_database("host=127.0.0.1 port=6543 dbname=postgres") as dsn:
        name = conninfo_to_dict(dsn)["dbname"]
    assert name.startswith(f"coordination_bench_wsl{os.getpid()}_")


def test_env_override_wins_verbatim(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv(
        "PSEUDOLIFE_TEST_DATABASE_URL",
        "postgresql://u:p@10.0.0.5:5432/ci_fixed_db",
    )
    assert pg_fixtures.resolve_test_db_url().endswith("/ci_fixed_db")


def test_env_override_gets_a_worker_suffix_under_xdist(monkeypatch):
    """A verbatim override under xdist would put every worker's reaper on
    ONE database — the exact cross-run crossfire this module exists to
    prevent, moved inside a single CI job. Under a worker, the override's
    database name gets the worker id appended; single-process runs keep
    the verbatim contract above."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    monkeypatch.setenv(
        "PSEUDOLIFE_TEST_DATABASE_URL",
        "postgresql://u:p@10.0.0.5:5432/ci_fixed_db",
    )
    assert pg_fixtures.resolve_test_db_url().endswith("/ci_fixed_db_gw3")


def test_bench_autopin_decision_covers_all_three_origins():
    """The bench pin must re-pin in an xdist worker (the inherited value is
    the CONTROLLER's autopin — sharing it puts every worker's reset_bench
    reaper on one database) while leaving a user-set value alone."""
    from tests.conftest import bench_db_autopin

    # Unset: pin this process's name.
    env: dict[str, str] = {}
    assert bench_db_autopin(env) == f"pseudolife_memory_bench_{pg_defaults.run_suffix()}"
    # Inherited from a parent process's autopin (xdist worker): re-pin.
    env = {"PSEUDOLIFE_BENCH_DB": "pseudolife_memory_bench_99999",
           "_PSEUDOLIFE_BENCH_DB_AUTOPIN": "pseudolife_memory_bench_99999"}
    assert bench_db_autopin(env) == f"pseudolife_memory_bench_{pg_defaults.run_suffix()}"
    # Deliberately user-set (no matching autopin sentinel): keep it.
    env = {"PSEUDOLIFE_BENCH_DB": "my_bench"}
    assert bench_db_autopin(env) is None


def test_admin_url_targets_postgres_db(monkeypatch):
    monkeypatch.setenv(
        "PSEUDOLIFE_TEST_DATABASE_URL",
        "postgresql://u:p@10.0.0.5:5432/ci_fixed_db",
    )
    assert pg_fixtures._admin_url().endswith("/postgres")


def test_bench_db_is_private_to_this_run():
    """evals' reset_bench() reaps every backend on its database — the same
    crossfire class as pg_conn, on a second shared database. conftest pins a
    per-run bench name before any test imports ladder_sweep."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import ladder_sweep
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(ladder_sweep.bench_url())["dbname"] == (
        f"pseudolife_memory_bench_{pg_defaults.run_suffix()}")


# ── In-process reaper victim: the end-of-session dream thread ───────────────
# pg_conn's reaper also hits THIS process's own live dream thread (fired by
# episode_end_session / reap_idle_sessions, outliving its test). The storage
# layer reconnects it after the kill and the fresh transaction can deadlock
# the fixture's TRUNCATE (CI run 35556355319, 2026-09-21). The fixture waits
# for those threads first; these pin the helper it waits with.


def test_wait_for_background_dreams_joins_only_dream_threads():
    """A live dream-named thread is joined to completion; a thread with any
    other name is not the fixture's business and is left running."""
    import threading

    from pseudolife_memory.service_dream import SESSION_END_DREAM_THREAD_NAME

    finished = threading.Event()
    release_other = threading.Event()

    def _dream():
        import time
        time.sleep(0.3)
        finished.set()

    dream = threading.Thread(target=_dream, name=SESSION_END_DREAM_THREAD_NAME,
                             daemon=True)
    other = threading.Thread(target=release_other.wait, name="not-a-dream",
                             daemon=True)
    dream.start()
    other.start()
    try:
        stragglers = pg_fixtures.wait_for_background_dreams(timeout=10.0)
        assert stragglers == []
        assert finished.is_set() and not dream.is_alive()
        assert other.is_alive(), "a thread not named as a dream must be ignored"
    finally:
        release_other.set()
        other.join(5.0)


def test_wait_for_background_dreams_returns_stragglers_past_the_budget():
    """A dream that does not finish inside the budget comes back to the
    caller — the fixture fails loudly on it rather than racing it."""
    import threading
    import time

    from pseudolife_memory.service_dream import SESSION_END_DREAM_THREAD_NAME

    release = threading.Event()
    stuck = threading.Thread(target=release.wait, name=SESSION_END_DREAM_THREAD_NAME,
                             daemon=True)
    stuck.start()
    try:
        stragglers = pg_fixtures.wait_for_background_dreams(timeout=0.05)
        # `in`, not `==`: a real dream left by an earlier file on this
        # worker would share the budget and land in the list too.
        assert stuck in stragglers
        # Reported once, a straggler is returned again WITHOUT a second
        # wait — otherwise one hung dream costs the budget on every later
        # PG test. Well under the 5 s budget proves no join happened.
        started = time.monotonic()
        assert stuck in pg_fixtures.wait_for_background_dreams(timeout=5.0)
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        stuck.join(5.0)
        pg_fixtures._reported_stragglers.discard(stuck.ident)


def test_fire_and_forget_dream_starts_the_thread_the_fixture_waits_for():
    """The service's fire-and-forget thread carries the pinned name — if it
    were renamed, the fixture's wait would silently match nothing."""
    import threading
    from types import SimpleNamespace

    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.service_dream import SESSION_END_DREAM_THREAD_NAME

    seen: list[str] = []
    fake = SimpleNamespace(
        dream_run_auto=lambda: seen.append(threading.current_thread().name))
    MemoryService._fire_and_forget_dream(fake)
    assert pg_fixtures.wait_for_background_dreams(timeout=10.0) == []
    assert seen == [SESSION_END_DREAM_THREAD_NAME]


def test_suite_scrubs_the_extractor_endpoint_from_the_environment():
    """conftest drops the PSEUDOLIFE_DREAM_* endpoint selection at import, so
    a shell with the ops values exported cannot send the dreams tests fire to
    a live extractor — the no-op extractor is what keeps the fixture's dream
    wait in the milliseconds. Timeout/token budgets are left alone."""
    from tests.conftest import (
        EXTRACTOR_ENDPOINT_ENV, scrub_extractor_endpoint_env,
    )

    env = {name: "x" for name in EXTRACTOR_ENDPOINT_ENV}
    env["PSEUDOLIFE_DREAM_TIMEOUT_SECONDS"] = "30"
    assert sorted(scrub_extractor_endpoint_env(env)) == sorted(EXTRACTOR_ENDPOINT_ENV)
    assert env == {"PSEUDOLIFE_DREAM_TIMEOUT_SECONDS": "30"}
    assert scrub_extractor_endpoint_env(env) == []          # idempotent
    # And the live process really was scrubbed at conftest import.
    assert not any(name in os.environ for name in EXTRACTOR_ENDPOINT_ENV)
