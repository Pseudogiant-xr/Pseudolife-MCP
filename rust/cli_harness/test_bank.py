"""CPU-only tests for deterministic disposable-bank dumps."""

import base64
import json
import subprocess
import sys

import pytest

from rust.cli_harness import normalize
from rust.cli_harness.rows import _bank


class _FakeConnection:
    def __init__(self, reverse: bool) -> None:
        self.reverse = reverse

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def _server_order(self, rows):
        return list(reversed(rows)) if self.reverse else list(rows)

    def execute(self, query: str):
        if "FROM pg_tables" in query:
            rows = [("coordination_lease_waiters",), ("coordination_leases",)]
        elif "AS j FROM public." in query:
            table = query.split('public."', 1)[1].split('"', 1)[0]
            rows = [(value,) for value in {
                "coordination_lease_waiters": ['{"id":2}', '{"id":1}'],
                "coordination_leases": ['{"id":4}', '{"id":3}'],
            }[table]]
        elif "FROM pg_sequences" in query:
            rows = [("coordination_lease_waiters_id_seq", 2),
                    ("coordination_leases_id_seq", 4)]
        elif "FROM information_schema.columns" in query:
            rows = [
                ("coordination_lease_waiters", "id", "bigint", "int8", "NO", None),
                ("coordination_leases", "id", "bigint", "int8", "NO", None),
            ]
        elif "FROM pg_indexes" in query:
            rows = [
                ("coordination_lease_waiters", "coordination_lease_waiters_pkey", "CREATE UNIQUE INDEX ..."),
                ("coordination_leases", "coordination_leases_pkey", "CREATE UNIQUE INDEX ..."),
            ]
        elif "FROM pg_constraint" in query:
            rows = [
                ("coordination_lease_waiters", "coordination_lease_waiters_pkey", "PRIMARY KEY (id)"),
                ("coordination_leases", "coordination_leases_pkey", "PRIMARY KEY (id)"),
            ]
        else:
            raise AssertionError(f"unexpected query: {query}")
        return self._server_order(rows)


def test_dump_is_independent_of_server_collation_order(monkeypatch):
    def serialized_dump(reverse: bool) -> str:
        monkeypatch.setattr(_bank, "_connect", lambda _name: _FakeConnection(reverse))
        return json.dumps(_bank.dump("pl_cf_fake"))

    assert serialized_dump(False) == serialized_dump(True)
def test_fixture_diagnostic_redacts_connection_credentials():
    from .lease_ci import diagnostic
    error = RuntimeError("initdb failed: postgresql://fixture:synthetic-password@127.0.0.1/db "
                         "password='synthetic-password' token=synthetic-token")
    detail = diagnostic(error)
    assert "initdb failed" in detail
    assert "synthetic-password" not in detail
    assert "synthetic-token" not in detail


def test_fixture_diagnostic_runs_without_tests_package(monkeypatch):
    import builtins
    from .lease_ci import diagnostic

    original_import = builtins.__import__

    def standalone_import(name, *args, **kwargs):
        if name == "tests" or name.startswith("tests."):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", standalone_import)
    dsn = "host=127.0.0.1 password='synthetic secret' passfile='synthetic-file'"
    detail = diagnostic(RuntimeError("initdb failed: synthetic secret synthetic-file"), dsn)
    assert "initdb failed" in detail
    assert "synthetic secret" not in detail
    assert "synthetic-file" not in detail


def test_two_interpreters_allocate_distinct_row_names():
    script = "from rust.cli_harness.rows import maintainer; print(maintainer.ARM_DB)"
    names = [subprocess.check_output([sys.executable, "-c", script], text=True).strip()
             for _ in range(2)]
    assert names[0] != names[1]
    assert all(_bank._NAME.fullmatch(name) for name in names)


def test_foreign_cleanup_refuses_before_connecting(monkeypatch):
    monkeypatch.setattr(_bank, "_admin", lambda: pytest.fail("foreign cleanup connected"))
    with pytest.raises(ValueError, match="owned"):
        _bank.drop("pl_cf_w1c_maint_arm")


def test_database_urls_still_refuse_the_owner_login(monkeypatch):
    from tests import pg_defaults

    monkeypatch.setattr(pg_defaults, "default_login", lambda: ("pseudolife", "fixture"))
    with pytest.raises(RuntimeError, match="test login only"):
        _bank.url(_bank.name("pl_cf_guard"))


def test_name_allocator_keeps_long_labels_distinct_and_stable():
    from rust.cli_harness.run_names import RunNames

    names = RunNames()
    a = names.name("pl_cf_" + "a" * 40, max_length=46)
    b = names.name("pl_cf_" + "a" * 39 + "b", max_length=46)
    assert a != b
    assert names.name("pl_cf_" + "a" * 40, max_length=46) == a
    assert all(_bank._NAME.fullmatch(name) for name in (a, b))
    assert names.owns(a) and not RunNames().owns(a)


def test_normalizer_only_replaces_exact_allocated_names():
    name = _bank.name("pl_cf_w1c_backup_absent")
    text = f'"{name}" {name}_other pl_cf_foreign_0123456789abcdef'
    enc = lambda value: base64.b64encode(value.encode()).decode()
    obs = {"stdout": enc(text), "stderr": enc(""),
           "files": {"config": "file:" + enc(json.dumps({"database": name}))},
           "db": {"name": name}}
    result = normalize.apply(obs, (), None)
    assert base64.b64decode(result["stdout"]).decode() == (
        f'"pl_cf_w1c_backup_absent" {name}_other pl_cf_foreign_0123456789abcdef')
    assert json.loads(base64.b64decode(result["files"]["config"][5:])) == {
        "database": "pl_cf_w1c_backup_absent"}
    assert result["db"]["name"] == "pl_cf_w1c_backup_absent"
    assert obs["db"]["name"] == name


def test_default_template_busy_retries_without_terminating_sessions(monkeypatch):
    import psycopg
    import time

    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, statement):
            statements.append(statement)
            if len(statements) < 3:
                raise psycopg.errors.ObjectInUse("template1 has another session")

    monkeypatch.setattr(_bank, "drop", lambda _name: None)
    monkeypatch.setattr(_bank, "_admin", Connection)
    monkeypatch.setattr(_bank, "url", lambda name: name)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    name = _bank.name("pl_cf_template_busy")
    assert _bank.create(name, schema=False) == name
    assert statements == [f'CREATE DATABASE "{name}"'] * 3


@pytest.mark.parametrize("error, attempts", [("ObjectInUse", 5), ("InsufficientPrivilege", 1)])
def test_default_template_retry_is_bounded_and_preserves_other_errors(monkeypatch, error, attempts):
    import psycopg
    import time
    from .pg_create import create_from_default_template

    statements = []
    failure = getattr(psycopg.errors, error)("fixture failure")

    class Connection:
        def execute(self, statement):
            statements.append(statement)
            raise failure

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(type(failure)) as caught:
        create_from_default_template(Connection(), "CREATE DATABASE fixture")
    assert caught.value is failure
    assert len(statements) == attempts


class _TeardownAdmin:
    """The admin connection drop() reaps through: a catalog that holds the
    bank until DROP runs, recording every statement."""

    def __init__(self, statements, exists=True, catalog=(0, True)):
        self.statements, self.exists, self.catalog = statements, exists, catalog

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        self.statements.append(statement)
        result = self

        class _Rows:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        if statement.startswith("SELECT 1 FROM pg_database"):
            return _Rows((1,) if result.exists else None)
        if statement.startswith("SELECT datconnlimit"):
            return _Rows(result.catalog if result.exists else None)
        if statement.startswith("DROP DATABASE"):
            result.exists = False
        return _Rows(None)


def _teardown_fixture(monkeypatch, connects, statements, **admin):
    import time
    from pseudolife_memory.storage import schema

    monkeypatch.setattr(_bank, "url", lambda name: name)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(schema, "assert_disposable_database", lambda _conn: None)
    monkeypatch.setattr(schema, "refuse_production_database", lambda _name: None)
    admin_conn = _TeardownAdmin(statements, **admin)
    monkeypatch.setattr(_bank, "_admin", lambda: admin_conn)

    class _Target:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def connect(_name, autocommit=True):
        failure = connects.pop(0) if connects else None
        if failure is not None:
            raise failure
        return _Target()

    monkeypatch.setattr(_bank, "_connect", connect)
    return admin_conn


def test_teardown_drop_retries_a_connect_timeout(monkeypatch):
    """A connect that times out on the first teardown attempt (hosted
    windows-latest, run 42348225338) no longer fails the case: drop() retries
    it and reaps the bank."""
    import psycopg

    connects = [psycopg.errors.ConnectionTimeout("connection timeout expired"), None]
    statements = []
    admin = _teardown_fixture(monkeypatch, connects, statements)
    name = _bank.name("pl_cf_teardown_timeout")
    _bank.drop(name)
    assert connects == []
    assert any(s.startswith("DROP DATABASE") for s in statements)
    assert admin.exists is False


def test_connect_retry_is_bounded_and_only_for_timeouts(monkeypatch):
    import psycopg

    # Timeouts every time: three attempts, then the catalog check decides,
    # and an ordinary (not half-dropped) bank still raises.
    timeouts = [psycopg.errors.ConnectionTimeout("timeout")] * 5
    statements = []
    _teardown_fixture(monkeypatch, timeouts, statements)
    name = _bank.name("pl_cf_teardown_bounded")
    with pytest.raises(psycopg.errors.ConnectionTimeout):
        _bank.drop(name)
    assert len(timeouts) == 5 - _bank.CONNECT_ATTEMPTS

    # A refusal that is not a timeout (a half-dropped bank) is not retried:
    # the catalog check sends it on to DROP, as before.
    refusals = [psycopg.OperationalError("database is not accepting connections"), None]
    statements = []
    admin = _teardown_fixture(monkeypatch, refusals, statements, catalog=(-2, False))
    name = _bank.name("pl_cf_teardown_half_dropped")
    _bank.drop(name)
    assert refusals == [None]
    assert admin.exists is False


def test_teardown_keeps_the_case_only_when_the_bank_is_gone(monkeypatch, capsys):
    import psycopg

    failure = psycopg.OperationalError("server closed the connection")

    def broken_drop(_name):
        raise failure

    # The drop failed but the bank is gone: printed, the case keeps its result.
    statements = []
    _teardown_fixture(monkeypatch, [], statements, exists=False)
    monkeypatch.setattr(_bank, "drop", broken_drop)
    name = _bank.name("pl_cf_teardown_gone")
    _bank.teardown(name)
    assert "the bank is gone; the case keeps its result" in capsys.readouterr().err

    # The bank is still there: a leak, so the failure propagates.
    _teardown_fixture(monkeypatch, [], [], exists=True)
    monkeypatch.setattr(_bank, "drop", broken_drop)
    with pytest.raises(psycopg.OperationalError) as caught:
        _bank.teardown(_bank.name("pl_cf_teardown_leaked"))
    assert caught.value is failure
