"""No test or bench reset may reap, migrate or truncate a production bank.

The bundled stack's Postgres (127.0.0.1:5433) hosts the production bank
``pseudolife_memory`` beside the per-run test and bench databases, under the
same owning role. Every reset site reaps the other backends on its database,
runs this checkout's DDL, and TRUNCATEs ``BENCH_RESET_TABLES`` — all 31
tables. Until 2026-09-23 the only thing between a mistyped
``PSEUDOLIFE_TEST_DATABASE_URL`` / ``PSEUDOLIFE_BENCH_DB`` and a wiped bank was
the configured database NAME: nothing refused ``pseudolife_memory``.

Every test here drives the real reset code against a STUB connection that
records what it was sent and never reaches a server. That is deliberate:
while the guard was absent, the same test pointed at a real server would have
reaped the daemon and truncated the live bank.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from pseudolife_memory.storage import schema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import ladder_sweep  # noqa: E402

from tests import pg_fixtures  # noqa: E402
from tests import test_transfer_cli as transfer_tests  # noqa: E402

_PROBE = "SELECT current_database()"
# Anything a reset must not have sent before the guard refused it.
_DESTRUCTIVE = re.compile(
    r"pg_terminate_backend|\bTRUNCATE\b|\bCREATE\b|\bALTER\b|\bDROP\b"
    r"|\bINSERT\b|\bDELETE\b|\bUPDATE\b",
    re.IGNORECASE,
)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [] if self._row is None else [self._row]


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._result = self._conn.execute(sql, params)
        return self._result

    def fetchone(self):
        return self._result.fetchone()


class _StubConn:
    """psycopg-shaped stand-in. ``current_database()`` answers with the name
    the test chose — the SERVER's answer, independent of what the DSN said —
    and every statement is recorded. It holds no socket."""

    closed = False

    def __init__(self, server_db: str, log: list[tuple[str, str]]):
        self.server_db = server_db
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = str(sql).strip()
        self._log.append((self.server_db, text))
        if text == _PROBE:
            return _Result((self.server_db,))
        if text.startswith("SELECT 1 FROM pg_database"):
            return _Result((1,))  # the bench DB "exists": no CREATE DATABASE
        return _Result(None)

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


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

_STUB_URL = "postgresql://u:p@127.0.0.1:1/pl_guard_stub"


def _run_pg_conn_fixture():
    session = pg_fixtures._pg_conn_session(_STUB_URL)
    try:
        next(session)
    finally:
        session.close()


def _run_ladder_reset_bench():
    ladder_sweep.reset_bench()


def _run_transfer_bank():
    with transfer_tests._bank(_STUB_URL):
        pass


_RESET_SITES = {
    "tests/pg_fixtures.py::_pg_conn_session": _run_pg_conn_fixture,
    "evals/ladder_sweep.py::reset_bench": _run_ladder_reset_bench,
    "tests/test_transfer_cli.py::_bank": _run_transfer_bank,
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
    with pytest.raises(schema.ProductionDatabaseError):
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
    with pytest.raises(schema.ProductionDatabaseError):
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


# ── the helper itself ─────────────────────────────────────────────────────

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv(schema.PRODUCTION_DATABASE_ENV, raising=False)


@pytest.mark.parametrize("name", ["pseudolife_memory", "Pseudolife_Memory"])
def test_assert_disposable_refuses_the_default_bank(name, clean_env):
    log: list[tuple[str, str]] = []
    with pytest.raises(schema.ProductionDatabaseError, match="refusing"):
        schema.assert_disposable_database(_StubConn(name, log))
    assert log == [(name, _PROBE)]


@pytest.mark.parametrize("name", [
    "pseudolife_memory_test_123", "pseudolife_memory_test",
    "pseudolife_memory_test_gw0", "pseudolife_memory_bench",
    "pseudolife_memory_bench_99", "pl_bench_wabl_flat", "postgres",
])
def test_assert_disposable_accepts_test_bench_and_ci_names(name, clean_env):
    assert schema.assert_disposable_database(_StubConn(name, [])) == name


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1:5433/my_bank",
    "postgresql://u:p@127.0.0.1:5433/my_bank?sslmode=disable",
    "host=127.0.0.1 port=5433 dbname=my_bank user=u",
    "dbname='MY_BANK'",
])
def test_the_daemon_dsn_bank_is_refused_too(dsn, clean_env, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
    with pytest.raises(schema.ProductionDatabaseError):
        schema.assert_disposable_database(_StubConn("my_bank", []))
    assert schema.assert_disposable_database(
        _StubConn("pseudolife_memory_test_1", [])) == "pseudolife_memory_test_1"


def test_the_bank_conftest_recorded_is_refused(clean_env, monkeypatch):
    monkeypatch.setenv(schema.PRODUCTION_DATABASE_ENV, "Recorded_Bank")
    with pytest.raises(schema.ProductionDatabaseError):
        schema.assert_disposable_database(_StubConn("recorded_bank", []))


def test_inside_the_suite_the_snapshot_supersedes_the_live_dsn(
        clean_env, monkeypatch):
    """Tests point PSEUDOLIFE_MCP_DATABASE_URL at their OWN per-run database
    (pg_service, test_pg_fixture_dream_wait, test_session_digest, ...). The
    first cut read the live variable and so refused the suite's own database
    (2026-09-23 review). Inside the suite conftest has removed the operator's
    DSN and recorded its name, and that record is what counts."""
    monkeypatch.setenv(schema.PRODUCTION_DATABASE_ENV, "recorded_bank")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL",
                       "postgresql://u:p@127.0.0.1:5433/pseudolife_memory_test_7")
    assert schema.assert_disposable_database(
        _StubConn("pseudolife_memory_test_7", [])) == "pseudolife_memory_test_7"
    for refused in ("recorded_bank", "pseudolife_memory"):
        with pytest.raises(schema.ProductionDatabaseError):
            schema.assert_disposable_database(_StubConn(refused, []))


def test_conftest_left_a_snapshot_for_this_run():
    """conftest records one even when no daemon DSN was exported, so the
    guard never falls back to the live variable inside the suite."""
    assert os.environ.get(schema.PRODUCTION_DATABASE_ENV)


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


@pytest.mark.parametrize("name", ["pseudolife_memory/", "PSEUDOLIFE_MEMORY/"])
def test_a_trailing_slash_does_not_launder_the_bank_name(name, clean_env):
    assert schema.is_production_database(name)
    with pytest.raises(schema.ProductionDatabaseError):
        schema.refuse_production_database(name)


@pytest.mark.parametrize("dsn, name", [
    ("postgresql://u:p@h:5433/pseudolife_memory", "pseudolife_memory"),
    # libpq's own reading; is_production_database() treats it as the typo
    # it is (test above).
    ("postgresql://u:p@h:5433/pseudolife_memory/", "pseudolife_memory/"),
    ("postgresql://u:p@h:5433/other?dbname=pseudolife_memory",
     "pseudolife_memory"),
    ("postgresql://u:p@h:5433/pseudolife%5Fmemory", "pseudolife_memory"),
    ("host=h dbname='pseudolife_memory'", "pseudolife_memory"),
    ("host=h", None),
    ("not a dsn ===", None),
])
def test_dsn_database_name_parses_like_libpq(dsn, name, monkeypatch):
    monkeypatch.delenv("PGDATABASE", raising=False)
    assert schema.dsn_database_name(dsn) == name


def test_a_dsn_naming_no_database_falls_back_to_pgdatabase_like_libpq(
        clean_env, monkeypatch):
    monkeypatch.setenv("PGDATABASE", "pseudolife_memory")
    assert schema.dsn_database_name("host=h port=5433") == "pseudolife_memory"
    assert schema.dsn_database_name(
        "host=h dbname=replay_copy") == "replay_copy"
    for guard in _harness_guards().values():
        with pytest.raises(SystemExit):
            guard("host=h port=5433 user=u")


# ── name-level refusals where test and bench names resolve ────────────────

@pytest.fixture
def no_connect(monkeypatch):
    def _refuse(*a, **kw):
        raise AssertionError("name resolution must refuse before connecting")
    monkeypatch.setattr(psycopg, "connect", _refuse)


@pytest.mark.parametrize("override", [
    "postgresql://u:p@127.0.0.1:5433/pseudolife_memory",
    "postgresql://u:p@127.0.0.1:5433/pseudolife_memory/",
    "postgresql://u:p@127.0.0.1:5433/PSEUDOLIFE_MEMORY?sslmode=disable",
    "host=127.0.0.1 port=5433 dbname=pseudolife_memory user=u",
])
@pytest.mark.parametrize("worker", [None, "gw0"])
def test_test_db_override_naming_the_bank_is_refused(
        override, worker, clean_env, no_connect, monkeypatch):
    """Checked on the base name, before the xdist suffix: a single-process
    run (the CLAUDE.md full-suite form) uses the name verbatim."""
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", override)
    if worker:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    else:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    with pytest.raises(schema.ProductionDatabaseError):
        pg_fixtures._target_db_name()
    with pytest.raises(schema.ProductionDatabaseError):
        pg_fixtures.resolve_test_db_url()
    with pytest.raises(schema.ProductionDatabaseError):
        pg_fixtures.ensure_test_db()


def test_test_db_override_naming_a_disposable_db_still_resolves(
        clean_env, no_connect, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL",
                       "postgresql://u:p@127.0.0.1:5433/pseudolife_memory_test")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert pg_fixtures._target_db_name() == "pseudolife_memory_test"
    assert conninfo_to_dict(
        pg_fixtures.resolve_test_db_url())["dbname"] == "pseudolife_memory_test"


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


# ── structural: no reap or full-bank TRUNCATE without the guard ───────────

# The two statements that wreck a bank the reset did not own: the mass reap
# of every other backend on the database, and a TRUNCATE assembled from a
# table list (the tree's only spelling of a full-bank reset —
# BENCH_RESET_TABLES, or pg_fixtures'/ladder_sweep's _ALL_TABLES alias).
# Killing ONE known pid (a test's own storage connection) is not a reap.
_REAP_ALL = "pg_terminate_backend(pid) FROM pg_stat_activity"
_TRUNCATE_HEAD = "TRUNCATE "


def _candidate_python_files() -> list[Path]:
    """Tracked plus untracked-but-not-ignored, so a new reset site is held
    to the rule before it is ever staged."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard",
             "*.py"],
            cwd=ROOT, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git unavailable")
    return [ROOT / line for line in sorted(set(out.splitlines())) if line]


def _is_destructive(node) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and (node.value == _TRUNCATE_HEAD or _REAP_ALL in node.value))


def _is_guard_call(node) -> bool:
    return isinstance(node, ast.Call) and "assert_disposable_database" in (
        getattr(node.func, "id", None), getattr(node.func, "attr", None))


def _reset_functions():
    """(file, function, first destructive line, first guard line or None)
    for every function holding a reap or a full-bank TRUNCATE."""
    for path in _candidate_python_files():
        if not path.is_file():  # tracked but deleted in the working tree
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _REAP_ALL not in text and _TRUNCATE_HEAD not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # an untracked scratch file mid-edit
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hits = [n.lineno for n in ast.walk(func) if _is_destructive(n)]
            if not hits:
                continue
            guards = [n.lineno for n in ast.walk(func) if _is_guard_call(n)]
            yield (path.relative_to(ROOT).as_posix(), func.name, min(hits),
                   min(guards) if guards else None)


def test_every_reap_and_full_bank_truncate_is_guarded_first():
    """Per function, not per file: a file that guards one helper must not
    vouch for another. The guard call has to come before the first reap or
    TRUNCATE in the same function.

    A tripwire for the tree's existing spellings, not a proof: it matches the
    exact ``"TRUNCATE "`` head and the full reap string, and it does not
    check that the guard runs on the same connection or ahead of
    ``ensure_schema``. The stub-driven tests above pin that ordering at the
    three shared reset sites."""
    found = list(_reset_functions())
    # The scan must be finding the known sites, or it is guarding nothing.
    assert {(f, fn) for f, fn, _, _ in found} >= {
        ("tests/pg_fixtures.py", "_pg_conn_session"),
        ("evals/ladder_sweep.py", "reset_bench"),
        ("tests/test_transfer_cli.py", "_bank"),
        ("tests/test_transfer_cli.py", "_truncate_all"),
        ("tests/test_graph.py", "svc"),
        ("tests/test_dream_ack_storage.py", "_truncate"),
    }
    unguarded = [f"{f}::{fn} (line {line})" for f, fn, line, guard in found
                 if guard is None or guard > line]
    assert not unguarded, (
        "these functions reap backends or TRUNCATE the whole bank without "
        "calling schema.assert_disposable_database(conn) first:\n  "
        + "\n  ".join(unguarded))


# ── conftest keeps the daemon's DSN out of the suite ──────────────────────

def _import_conftest_in_child(**env_changes) -> dict:
    """Import tests/conftest.py in a fresh interpreter and report what it
    left in the environment. A child, because this machine's ambient
    environment has no daemon DSN: an in-process check would pass whether or
    not conftest removes one. ``None`` in ``env_changes`` unsets a name."""
    env = dict(os.environ)
    env.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
    env.pop(schema.PRODUCTION_DATABASE_ENV, None)
    # Hermetic child: an operator bench name disables the per-run autopin
    # (and its atexit DROP against the real server); the admin URL is inert.
    env["PSEUDOLIFE_BENCH_DB"] = "pl_guard_child_unused"
    env["PSEUDOLIFE_BENCH_ADMIN_URL"] = "postgresql://u:p@127.0.0.1:1/postgres"
    for name, value in env_changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    probe = (
        "import json, os, tests.conftest\n"
        "from pseudolife_memory.storage import schema\n"
        "print(json.dumps({\n"
        "  'dsn': os.environ.get('PSEUDOLIFE_MCP_DATABASE_URL'),\n"
        "  'recorded': os.environ.get(schema.PRODUCTION_DATABASE_ENV),\n"
        "  'protected': sorted(schema._production_database_names()),\n"
        "}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_conftest_removes_the_daemon_dsn_and_keeps_its_bank_name():
    """With PSEUDOLIFE_MCP_DATABASE_URL exported, every MemoryService a
    fixture builds without a database_url binds to that bank, and
    ``pristine_service.save()`` then snapshots a just-cleared cortex over it
    (replace_facts([]) -> DELETE FROM facts)."""
    got = _import_conftest_in_child(PSEUDOLIFE_MCP_DATABASE_URL=(
        "postgresql://u:secret@127.0.0.1:5433/Live_Bank_Name"))
    assert got["dsn"] is None
    assert got["recorded"] == "Live_Bank_Name"  # the name only: no credential
    assert "live_bank_name" in got["protected"]


def test_conftest_records_the_default_bank_when_no_dsn_was_exported():
    """Always a snapshot, so a test that points the live variable at its own
    database is never refused. Non-empty on purpose: Windows deletes an
    environment variable assigned the empty string."""
    got = _import_conftest_in_child()
    assert got["recorded"] == "pseudolife_memory"
    assert got["protected"] == ["pseudolife_memory"]


def test_conftest_keeps_an_inherited_snapshot():
    """An xdist worker inherits the controller's environment after the
    controller's conftest removed the DSN; the worker's own conftest import
    must not replace the recorded bank with the default."""
    got = _import_conftest_in_child(
        **{schema.PRODUCTION_DATABASE_ENV: "Inherited_Bank"})
    assert got["recorded"] == "Inherited_Bank"
    assert "inherited_bank" in got["protected"]


def test_the_exit_time_bench_drop_drops_only_the_pinned_name(monkeypatch):
    """conftest drops the per-run bench database at interpreter exit. It
    must drop the name it pinned, not whatever PSEUDOLIFE_BENCH_DB holds by
    then — a DROP DATABASE ... WITH (FORCE) on a production name would be
    the worst reset of all."""
    from tests import conftest

    if getattr(conftest, "_bench_pin", None) is None:
        pytest.skip("an operator PSEUDOLIFE_BENCH_DB disabled the autopin")
    executed: list[str] = []

    class _Admin(_StubConn):
        def execute(self, sql, params=None):
            executed.append(str(sql))
            return _Result(None)

    monkeypatch.setattr(psycopg, "connect",
                        lambda *a, **kw: _Admin("postgres", []))
    # Belt and braces: a bait that is no real database, and an admin URL no
    # server answers — so even if the stub stopped applying (say, a
    # `from psycopg import connect` refactor), nothing real could be dropped.
    monkeypatch.setenv("PSEUDOLIFE_BENCH_ADMIN_URL",
                       "postgresql://u:p@127.0.0.1:1/postgres")
    monkeypatch.setenv("PSEUDOLIFE_BENCH_DB", "pl_guard_exit_bait")
    conftest._drop_run_bench_db()
    assert executed == [
        f'DROP DATABASE IF EXISTS "{conftest._bench_pin}" WITH (FORCE)']
