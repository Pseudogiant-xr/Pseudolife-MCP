"""Per-run test-database isolation (no PG server needed).

The ``pg_conn`` fixture reaps every other backend on the test database
before truncating — correct within one run, lethal across runs: two
concurrent ``pytest tests/`` invocations sharing one database terminate
each other's live connections (AdminShutdown, a different victim set
every run). The contract pinned here is that each pytest process gets
its own private database, so the reaper can only ever hit this run's
leaked backends. An explicit ``PSEUDOLIFE_TEST_DATABASE_URL`` still wins
verbatim (CI's isolated service container uses a fixed name).
"""

from __future__ import annotations

import os

from tests import pg_fixtures


def test_default_test_db_is_private_to_this_run(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    url = pg_fixtures.resolve_test_db_url()
    db = url.rsplit("/", 1)[1]
    assert db == f"pseudolife_memory_test_{os.getpid()}"


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
    assert bench_db_autopin(env) == f"pseudolife_memory_bench_{os.getpid()}"
    # Inherited from a parent process's autopin (xdist worker): re-pin.
    env = {"PSEUDOLIFE_BENCH_DB": "pseudolife_memory_bench_99999",
           "_PSEUDOLIFE_BENCH_DB_AUTOPIN": "pseudolife_memory_bench_99999"}
    assert bench_db_autopin(env) == f"pseudolife_memory_bench_{os.getpid()}"
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

    assert ladder_sweep.bench_url().endswith(
        f"/pseudolife_memory_bench_{os.getpid()}"
    )
