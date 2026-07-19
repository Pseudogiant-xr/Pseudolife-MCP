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
    monkeypatch.setenv(
        "PSEUDOLIFE_TEST_DATABASE_URL",
        "postgresql://u:p@10.0.0.5:5432/ci_fixed_db",
    )
    assert pg_fixtures.resolve_test_db_url().endswith("/ci_fixed_db")


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
