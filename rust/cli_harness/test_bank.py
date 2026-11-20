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


@pytest.fixture
def maintainer_fixture(monkeypatch):
    import atexit
    from importlib.util import module_from_spec
    from unittest.mock import patch

    from rust.cli_harness.rows import maintainer
    from rust.harness_support.import_blocker import cold_harness_imports

    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    spec = maintainer.__spec__
    row = module_from_spec(spec)
    with cold_harness_imports(), patch.dict(maintainer.normalize.RULES):
        try:
            spec.loader.exec_module(row)
            yield row
        finally:
            if hasattr(row, "_drop_all"):
                atexit.unregister(row._drop_all)


@pytest.mark.parametrize("kind", ["fresh", "stale", "pending", "rich"])
def test_maintainer_seed_runs_with_frozen_imports_blocked(maintainer_fixture, monkeypatch, kind):
    from contextlib import nullcontext
    from unittest.mock import MagicMock, call

    row = maintainer_fixture
    name = row._name(kind)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (name,)
    monkeypatch.setattr(_bank, "create", MagicMock())
    monkeypatch.setattr(_bank, "connect", lambda *a, **kw: nullcontext(conn))
    monkeypatch.setattr(_bank, "dump", lambda _: {"seeded": True})
    assert row.ensure(kind) == name
    assert conn.execute.call_args_list[0] == call("SELECT current_database()")
    conn.transaction.assert_called_once_with()
    assert conn.execute.call_count > 1


@pytest.mark.parametrize("server_name", ["pseudolife_memory", "pseudolife_bench", "pl_cf_other_run"])
@pytest.mark.parametrize("operation", ["seed", "redeem", "burn", "terminate", "lock", "prepare"])
def test_maintainer_fixture_refuses_wrong_server(maintainer_fixture, monkeypatch, server_name, operation):
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    from rust.harness_support.database_guard import ProductionDatabaseError

    row = maintainer_fixture
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (server_name,)
    monkeypatch.setattr(_bank, "connect", lambda *a, **kw: nullcontext(conn))
    with pytest.raises((ProductionDatabaseError, ValueError), match="production bank|different database"):
        if operation == "seed":
            row._seed(conn, row.ARM_DB, "pending")
        elif operation == "prepare":
            monkeypatch.setattr(row, "ensure", row._name)
            monkeypatch.setattr(row, "_copy", lambda *args: None)
            pending = row.case("fixture-preparation", "pending", [], dsn=False,
                               prepare=("SELECT 'fixture statement'",))
            pending.setup(MagicMock())
        else:
            getattr(row, operation + "_during")(None, MagicMock())
    conn.execute.assert_called_once_with("SELECT current_database()")
    conn.transaction.assert_not_called()


def test_maintainer_copy_refuses_wrong_admin_server(maintainer_fixture, monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    row = maintainer_fixture
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("pseudolife_bench",)
    drop = MagicMock()
    monkeypatch.setattr(row, "_drop", drop)
    monkeypatch.setattr(_bank, "_admin", lambda: nullcontext(conn))
    with pytest.raises(ValueError, match="requires the test admin database"):
        row._copy(row._name("pending"), row.ARM_DB)
    conn.execute.assert_called_once_with("SELECT current_database()")
    drop.assert_not_called()


def test_maintainer_copy_refuses_another_rows_bank(maintainer_fixture, monkeypatch):
    row = maintainer_fixture
    unrelated = _bank.name("pl_cf_other_row_test")
    monkeypatch.setattr(row, "_drop", lambda _: pytest.fail("drop reached with a foreign template"))
    with pytest.raises(ValueError, match="owned maintainer bank"):
        row._copy(unrelated, row.ARM_DB)


@pytest.mark.parametrize("operation", ["copy", "prepare", "terminate", "lock"])
def test_maintainer_successful_fixture_paths_are_import_free(maintainer_fixture, monkeypatch, operation):
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    import psycopg

    row = maintainer_fixture
    conn = MagicMock()
    process = MagicMock()
    process.poll.return_value = 0
    monkeypatch.setattr(row, "_live_code", lambda *_: "observed-hash")
    monkeypatch.setattr(row.time, "sleep", lambda _: None)
    if operation == "copy":
        source = row._name("pending")
        conn.execute.return_value.fetchone.side_effect = [("postgres",), (1,)]
        target = MagicMock()
        target.execute.return_value.fetchone.return_value = (row.ARM_DB,)
        admin = MagicMock(return_value=nullcontext(conn))
        monkeypatch.setattr(_bank, "_admin", admin)
        monkeypatch.setattr(_bank, "_connect", lambda _: nullcontext(target))
        monkeypatch.setattr(_bank, "_login", lambda: ("pseudolife_test", "fixture"))
        row._copy(source, row.ARM_DB)
        admin.assert_called_once_with()
        target.execute.assert_called_once_with("SELECT current_database()")
        assert "pg_terminate_backend" in conn.execute.call_args_list[-3].args[0]
        assert conn.execute.call_args_list[-2].args == (f'DROP DATABASE IF EXISTS "{row.ARM_DB}"',)
        last = f'CREATE DATABASE "{row.ARM_DB}" TEMPLATE "{source}"'
    else:
        conn.execute.return_value.fetchone.return_value = (row.ARM_DB,)
        monkeypatch.setattr(_bank, "connect", lambda *a, **kw: nullcontext(conn))
        if operation == "prepare":
            last = "SELECT 'fixture preparation'"
            monkeypatch.setattr(row, "ensure", row._name)
            monkeypatch.setattr(row, "_copy", lambda *args: None)
            monkeypatch.setattr(_bank, "dump", lambda _: {"prepared": True})
            case = row.case("fixture-preparation", "pending", [], dsn=False, prepare=(last,))
            arm = MagicMock()
            case.setup(arm)
            arm.state.__setitem__.assert_called_once_with("prepared", {"prepared": True})
        elif operation == "terminate":
            row.terminate_during(None, process)
            last = "SELECT pg_terminate_backend"
        else:
            with pytest.raises(psycopg.Rollback):
                row.lock_during(None, process)
            last = "LOCK TABLE maintainer_bootstrap"
    assert conn.execute.call_args_list[0].args == ("SELECT current_database()",)
    assert conn.execute.call_args.args[0].startswith(last)


def test_maintainer_seed_refuses_missing_pending_key(maintainer_fixture, monkeypatch):
    from unittest.mock import MagicMock

    row = maintainer_fixture
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (row.ARM_DB,)
    fixture = row._fixture()
    del fixture["seeds"]["pending"]["maintainer_passkeys"]
    monkeypatch.setattr(row, "_fixture", lambda: fixture)
    with pytest.raises(ValueError, match="incomplete or unexpected maintainer fixture rows"):
        row._seed(conn, row.ARM_DB, "pending")
    conn.execute.assert_called_once_with("SELECT current_database()")
    conn.transaction.assert_not_called()


@pytest.mark.parametrize("operation", ["redeem", "burn"])
def test_maintainer_wait_fixture_applies_to_the_waiting_code(maintainer_fixture, monkeypatch, operation):
    from contextlib import contextmanager, nullcontext
    from unittest.mock import MagicMock

    row = maintainer_fixture
    conn = MagicMock()
    observed = "fixture-code-hash"
    response = MagicMock(rowcount=1)
    response.fetchone.side_effect = [(row.ARM_DB,), (observed,)]
    in_transaction = False
    transactions = []

    @contextmanager
    def transaction():
        nonlocal in_transaction
        assert not in_transaction
        in_transaction = True
        try:
            yield
        finally:
            in_transaction = False

    def execute(statement, *args):
        if not isinstance(statement, str) or statement.startswith(("UPDATE ", "LOCK ")):
            transactions.append(in_transaction)
        return response

    conn.transaction.side_effect = transaction
    conn.execute.side_effect = execute
    connect = MagicMock(return_value=nullcontext(conn))
    monkeypatch.setattr(_bank, "connect", connect)
    insert = MagicMock(wraps=row._insert_rows)
    monkeypatch.setattr(row, "_insert_rows", insert)
    process = MagicMock()
    process.poll.return_value = None
    getattr(row, operation + "_during")(None, process)
    connect.assert_called_once_with(row.ARM_DB, autocommit=True)
    updates = [call.args[1] for call in conn.execute.call_args_list if isinstance(call.args[0], str)
               and call.args[0].startswith("UPDATE maintainer_bootstrap")]
    if operation == "redeem":
        assert updates == [(row.HOOK_T, "deskKeyHookAAAAAAAAAAA", observed, row.HOOK_T)]
        assert [call.args[1] for call in insert.call_args_list] == [
            "maintainer_nonces", "maintainer_passkeys", "coordination_events",
        ]
        conn.transaction.assert_called_once_with()
        assert transactions == [True] * 5
    else:
        assert updates == [(observed, row.HOOK_T, attempt) for attempt in range(5)]
        insert.assert_not_called()
        conn.transaction.assert_not_called()
        assert transactions == [False] * 5


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
    from rust.harness_support import database_guard as schema

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


def test_drop_retries_a_timed_out_admin_connect(monkeypatch):
    import psycopg

    statements = []
    admin = _teardown_fixture(monkeypatch, [None], statements)
    attempts = [psycopg.errors.ConnectionTimeout("admin timeout")]

    def admin_connect():
        if attempts:
            raise attempts.pop(0)
        return admin

    monkeypatch.setattr(_bank, "_admin", admin_connect)
    _bank.drop(_bank.name("pl_cf_teardown_admin_timeout"))
    assert attempts == [] and admin.exists is False


def test_teardown_propagates_when_the_check_fails_or_a_guard_refuses(monkeypatch):
    import psycopg
    from rust.harness_support.database_guard import ProductionDatabaseError

    failure = psycopg.OperationalError("server closed the connection")

    def broken_drop(_name):
        raise failure

    def no_admin():
        raise psycopg.OperationalError("cannot reach the server")

    # The absence check itself fails: the original failure propagates, with
    # its own context kept and the check's error noted.
    _teardown_fixture(monkeypatch, [], [])
    monkeypatch.setattr(_bank, "drop", broken_drop)
    monkeypatch.setattr(_bank, "_admin", no_admin)
    with pytest.raises(psycopg.OperationalError) as caught:
        _bank.teardown(_bank.name("pl_cf_teardown_unconfirmed"))
    assert caught.value is failure
    assert any("could not confirm" in note for note in caught.value.__notes__)

    # A guard refusal propagates even when the bank is gone.
    for refusal in (ValueError("not a disposable name"), ProductionDatabaseError("production")):
        _teardown_fixture(monkeypatch, [], [], exists=False)

        def refusing_drop(_name, refusal=refusal):
            raise refusal

        monkeypatch.setattr(_bank, "drop", refusing_drop)
        with pytest.raises(type(refusal)):
            _bank.teardown(_bank.name("pl_cf_teardown_guard"))
