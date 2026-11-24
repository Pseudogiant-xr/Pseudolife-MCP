"""No eval or transfer-test reset may reap, migrate or truncate a production bank.

The reset sites in the Python evals (``_python_ladder_sweep.reset_bench``,
``coordination_audit_volume.measure``) and in tests/test_transfer_cli.py
(``_bank``), and the eval harnesses' own DSN guards, refuse through the
frozen package's guard (``pseudolife_memory.storage.schema``). These tests
drive them as tests/test_disposable_database_guard.py drives the suite's own
reset site, against the same STUB connection, which records what it was sent
and never reaches a server. They import the evals and the package, so they
retire with them; the suite's own site and the package-free guard stay pinned
in tests/test_disposable_database_guard.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from pseudolife_memory.storage import schema
from tests.test_disposable_database_guard import (
    _DESTRUCTIVE,
    _IMPLICIT_DAEMON_DSNS,
    _PROBE,
    _STUB_URL,
    _StubConn,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import _python_ladder_sweep as ladder_sweep  # noqa: E402

from tests import test_transfer_cli as transfer_tests  # noqa: E402


@pytest.fixture
def stub_server(monkeypatch):
    """Replace psycopg.connect for the test: an admin DSN (dbname=postgres)
    gets a stub reporting ``postgres``; every other DSN gets a stub
    reporting ``stub_server.target``. ensure_schema is replaced with a
    recorder so the DDL step is observable without a server."""
    log: list[tuple[str, str]] = []
    ddl: list[str] = []

    class _Server:
        target = "pseudolife_memory"
        statements = log
        ensure_schema_calls = ddl

    def _connect(conninfo, *args, **kwargs):
        dbname = conninfo_to_dict(str(conninfo)).get("dbname")
        server_db = "postgres" if dbname == "postgres" else _Server.target
        return _StubConn(server_db, log)

    def _ensure_schema(conn):
        ddl.append(conn.server_db)

    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setattr(schema, "ensure_schema", _ensure_schema)
    monkeypatch.setattr(transfer_tests, "ensure_schema", _ensure_schema)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv(schema.PRODUCTION_DATABASE_ENV, raising=False)
    return _Server


# ── the reset sites that open their own connection ────────────────────────

def _run_ladder_reset_bench():
    ladder_sweep.reset_bench()


def _run_transfer_bank():
    with transfer_tests._bank(_STUB_URL):
        pass


# The suite's own pg_conn site is driven by the same tests in
# tests/test_disposable_database_guard.py.
_RESET_SITES = {
    "evals/_python_ladder_sweep.py::reset_bench": _run_ladder_reset_bench,
    "tests/test_transfer_cli.py::_bank": _run_transfer_bank,
}
# The refusal each site raises: the frozen eval and the transfer test use the
# package's guard, which decides as the package-free one does.
_REFUSALS = {
    "evals/_python_ladder_sweep.py::reset_bench": schema.ProductionDatabaseError,
    "tests/test_transfer_cli.py::_bank": schema.ProductionDatabaseError,
}


@pytest.fixture
def allowed_bench_name(monkeypatch):
    # A name the name-level check accepts, so reset_bench reaches the
    # server-side check: the server, not the DSN, reports the bank.
    monkeypatch.setenv("PSEUDOLIFE_BENCH_DB", "pl_guard_stub")


@pytest.mark.parametrize("site", sorted(_RESET_SITES))
@pytest.mark.parametrize("server_db", ["pseudolife_memory", "PSEUDOLIFE_MEMORY"])
def test_reset_refuses_the_production_bank_before_touching_it(
        site, server_db, stub_server, allowed_bench_name):
    stub_server.target = server_db
    with pytest.raises(_REFUSALS[site]):
        _RESET_SITES[site]()
    sent_to_bank = [sql for db, sql in stub_server.statements if db == server_db]
    assert sent_to_bank == [_PROBE], (
        f"{site} sent statements to the production bank before (or instead "
        f"of) refusing it: {sent_to_bank}")
    destructive = [sql for _, sql in stub_server.statements
                   if _DESTRUCTIVE.search(sql)]
    assert not destructive, f"{site} ran {destructive} before refusing"
    assert stub_server.ensure_schema_calls == [], (
        f"{site} ran the DDL before refusing")


@pytest.mark.parametrize("site", sorted(_RESET_SITES))
def test_reset_refuses_the_bank_the_daemon_dsn_names(
        site, stub_server, allowed_bench_name, monkeypatch):
    stub_server.target = "renamed_bank"
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL",
                       "host=127.0.0.1 port=5433 dbname=renamed_bank user=u")
    with pytest.raises(_REFUSALS[site]):
        _RESET_SITES[site]()
    assert [sql for db, sql in stub_server.statements
            if db == "renamed_bank"] == [_PROBE]


@pytest.mark.parametrize("site", sorted(_RESET_SITES))
def test_reset_checks_the_server_first_then_proceeds_on_a_disposable_db(
        site, stub_server, allowed_bench_name):
    """The guard is the FIRST statement, ahead of the reap — a check that
    ran after pg_terminate_backend would already have killed the daemon."""
    stub_server.target = "pseudolife_memory_test_4242"
    _RESET_SITES[site]()
    sent = [sql for db, sql in stub_server.statements
            if db == "pseudolife_memory_test_4242"]
    assert sent[0] == _PROBE, sent
    reap = next(i for i, sql in enumerate(sent) if "pg_terminate_backend" in sql)
    truncate = next(i for i, sql in enumerate(sent) if sql.startswith("TRUNCATE"))
    assert 0 < reap < truncate
    assert stub_server.ensure_schema_calls


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv(schema.PRODUCTION_DATABASE_ENV, raising=False)


@pytest.mark.parametrize("site", sorted(_RESET_SITES))
def test_a_test_pointing_the_daemon_dsn_at_its_own_db_can_still_reset(
        site, stub_server, allowed_bench_name, monkeypatch):
    # What conftest leaves behind when no daemon DSN was exported.
    monkeypatch.setenv(schema.PRODUCTION_DATABASE_ENV, "pseudolife_memory")
    monkeypatch.setenv(
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "postgresql://u:p@127.0.0.1:5433/pseudolife_memory_test_4242")
    stub_server.target = "pseudolife_memory_test_4242"
    _RESET_SITES[site]()
    sent = [sql for db, sql in stub_server.statements
            if db == "pseudolife_memory_test_4242"]
    assert sent[0] == _PROBE
    assert any(sql.startswith("TRUNCATE") for sql in sent)


def test_a_dsn_naming_no_database_falls_back_to_pgdatabase_like_libpq(
        clean_env, monkeypatch):
    monkeypatch.setenv("PGDATABASE", "pseudolife_memory")
    monkeypatch.delenv("PGSERVICE", raising=False)
    assert schema.dsn_database_name("host=h port=5433") == "pseudolife_memory"
    assert schema.dsn_database_name(
        "host=h dbname=replay_copy") == "replay_copy"
    for guard in _harness_guards().values():
        with pytest.raises(SystemExit):
            guard("host=h port=5433 user=u")


@pytest.mark.parametrize("site", sorted(_RESET_SITES))
@pytest.mark.parametrize("dsn", _IMPLICIT_DAEMON_DSNS)
def test_reset_refuses_before_touching_a_bank_the_daemon_dsn_names_implicitly(
        site, dsn, stub_server, allowed_bench_name, monkeypatch):
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
    stub_server.target = "live_bank"
    with pytest.raises(_REFUSALS[site]):
        _RESET_SITES[site]()
    sent_to_bank = [sql for db, sql in stub_server.statements
                    if db == "live_bank"]
    assert sent_to_bank in ([], [_PROBE]), sent_to_bank
    assert not [sql for _, sql in stub_server.statements
                if _DESTRUCTIVE.search(sql)]
    assert stub_server.ensure_schema_calls == []


@pytest.mark.parametrize("harness", ["graph_ablation", "live_replay_flat_ab",
                                     "recall_fanout_bench", "retrieval_replay",
                                     "retrieval_telemetry_review"])
@pytest.mark.parametrize("dsn", ["host=h port=5433 user=u", "service=replay",
                                 "postgresql://u:p@h:5433"])
def test_every_harness_guard_refuses_a_dsn_whose_database_it_cannot_tell(
        harness, dsn, clean_env, monkeypatch):
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    with pytest.raises(SystemExit):
        _harness_guards()[harness](dsn)


# ── name-level refusals where bench names resolve ─────────────────────────

@pytest.fixture
def no_connect(monkeypatch):
    def _refuse(*a, **kw):
        raise AssertionError("name resolution must refuse before connecting")
    monkeypatch.setattr(psycopg, "connect", _refuse)


@pytest.mark.parametrize("bench_db", ["pseudolife_memory", "PseudoLife_Memory",
                                      "pseudolife_memory/"])
def test_bench_db_naming_the_bank_is_refused(bench_db, clean_env, no_connect,
                                             monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_BENCH_DB", bench_db)
    with pytest.raises(schema.ProductionDatabaseError):
        ladder_sweep._bench_db_name()
    with pytest.raises(schema.ProductionDatabaseError):
        ladder_sweep.reset_bench()


def test_bench_db_default_name_still_works(clean_env, monkeypatch):
    """The eval CLIs' fixed default must keep resolving."""
    monkeypatch.delenv("PSEUDOLIFE_BENCH_DB", raising=False)
    assert ladder_sweep._bench_db_name() == "pseudolife_memory_bench"


# ── the eval harnesses' own DSN guards share the parser ───────────────────

def _harness_guards():
    import graph_ablation
    import live_replay_flat_ab
    import recall_fanout_bench
    import retrieval_replay
    import retrieval_telemetry_review

    return {
        "graph_ablation": graph_ablation.guard_dsn,
        "live_replay_flat_ab": live_replay_flat_ab._guard_dsn,
        "recall_fanout_bench": recall_fanout_bench.guard_dsn,
        "retrieval_replay": retrieval_replay.guard_dsn,
        "retrieval_telemetry_review": retrieval_telemetry_review.guard_dsn,
    }


# Two spellings each of the hardened guards' regex missed: a ?dbname= query
# parameter (libpq lets it override the path) and a percent-encoded name.
_BYPASS_FORMS = [
    "postgresql://u:p@h:5433/{db}",
    "postgresql://u:p@h:5433/{db}/",
    "postgresql://u:p@h:5433/{DB}",
    "host=h port=5433 dbname={db}",
    "dbname={DB} user=u",
    "postgresql://u:p@h:5433/replay_copy?dbname={db}",
    "postgresql://u:p@h:5433/{pct}",
]


@pytest.mark.parametrize("harness", ["graph_ablation", "live_replay_flat_ab",
                                     "recall_fanout_bench", "retrieval_replay",
                                     "retrieval_telemetry_review"])
@pytest.mark.parametrize("form", _BYPASS_FORMS)
@pytest.mark.parametrize("db", ["pseudolife_memory", "pseudolife_memory_bench"])
def test_every_harness_guard_refuses_every_spelling(harness, form, db,
                                                    clean_env):
    guard = _harness_guards()[harness]
    dsn = form.format(db=db, DB=db.upper(), pct=db.replace("_", "%5F"))
    with pytest.raises(SystemExit):
        guard(dsn)


@pytest.mark.parametrize("harness", ["graph_ablation", "live_replay_flat_ab",
                                     "recall_fanout_bench", "retrieval_replay",
                                     "retrieval_telemetry_review"])
def test_every_harness_guard_allows_a_replay_copy(harness, clean_env):
    guard = _harness_guards()[harness]
    guard("postgresql://u:p@127.0.0.1:5433/pseudolife_memory_replay_20260904")
    guard("host=127.0.0.1 dbname=pseudolife_memory_replay_20260904")


# ── the coordination audit's volume bench ─────────────────────────────────

def test_audit_volume_refuses_configured_live_bank_with_scratch_prefix(monkeypatch):
    from types import SimpleNamespace
    from evals.coordination_audit_volume import measure
    from pseudolife_memory.storage import coordination

    name = "pseudolife_memory_bench_live"
    monkeypatch.delenv(schema.PRODUCTION_DATABASE_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", f"dbname={name}")
    sent = []
    closed = []
    conn = _StubConn(name, sent)
    monkeypatch.setattr(coordination, "CoordinationConnection",
                        lambda dsn: SimpleNamespace(conn=conn, close=lambda: closed.append(True)))
    with pytest.raises(schema.ProductionDatabaseError):
        measure("dbname=ignored", agents=0, messages=0, updates_per_agent=0,
                attaches_per_agent=0, seed=1)
    assert closed == [True]
    assert not any(_DESTRUCTIVE.search(sql) for _, sql in sent)
