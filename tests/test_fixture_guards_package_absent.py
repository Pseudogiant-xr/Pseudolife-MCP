"""The suite's fixtures load, and refuse the same production banks, without
the frozen package.

tests/conftest.py, tests/pg_fixtures.py, tests/pg_defaults.py and
tests/fake_embedder.py use the package-free guard in rust/harness_support,
so pytest can collect the suite once ``pseudolife_memory/`` is deleted. The
guard is what keeps the suite off the live bank, so each check below runs in a
fresh interpreter twice: once as the tree stands and once with the package
made unimportable (``sys.modules["pseudolife_memory"] = None``, which is how
an import of a missing package fails). Both runs must give the outcomes this
file writes down, which name the default bank, the bank an exported daemon
DSN names, and a disposable near-miss that must still be allowed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Outcomes name the exception class with its module, so the package's
# same-named class cannot stand in for the guard's.
REFUSED = "rust.harness_support.database_guard.ProductionDatabaseError"
LIVE_DSN = "postgresql://u:secret@127.0.0.1:5433/Live_Bank_Name"
IMPLICIT_DSN = "host=127.0.0.1 port=5433 user=u"
# "present" is a migration bridge: delete that arm with the package.
PACKAGE_STATES = ("present", "absent")


def _child(state: str) -> None:
    """Run in the child: import the fixtures, then print each outcome."""
    if state == "absent":
        sys.modules["pseudolife_memory"] = None  # an import now fails as for a missing package
    import psycopg

    import tests.conftest as conftest
    from rust.harness_support import database_guard as guard
    from tests import pg_defaults, pg_fixtures

    def name_of(exc):
        return f"{type(exc).__module__}.{type(exc).__qualname__}"

    def outcome(action):
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - the class is the outcome
            return name_of(exc)
        return "allowed" if result is None else result

    def no_connection(*args, **kwargs):
        raise AssertionError("a name-level refusal must come before any connection")

    report = {
        "recorded": os.environ.get(guard.PRODUCTION_DATABASE_ENV),
        "dsn_left": os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL"),
        "names": {},
        "override": {},
        "server": {},
        "dispatched": {},
    }
    names = ("pseudolife_memory", "PSEUDOLIFE_MEMORY/", "live_bank_name", "pseudolife_memory_test_1")
    for name in names:
        report["names"][name] = outcome(lambda: guard.refuse_production_database(name))

    # The lazy name guards, before any connection: an allowed name gets as
    # far as connecting, which the stub refuses.
    psycopg.connect = no_connection
    os.environ.pop("PYTEST_XDIST_WORKER", None)
    for name in ("pseudolife_memory", "live_bank_name", "pseudolife_memory_test_1"):
        os.environ["PSEUDOLIFE_TEST_DATABASE_URL"] = f"postgresql://u:p@127.0.0.1:1/{name}"
        report["override"][name] = [outcome(pg_fixtures._target_db_name),
                                    outcome(pg_fixtures.ensure_test_db)]
    # A lane that requires the test database stops the session on a refusal
    # instead of erroring every test (conftest.pytest_sessionstart).
    import pytest as child_pytest

    os.environ["PSEUDOLIFE_TEST_DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/pseudolife_memory"
    os.environ["PSEUDOLIFE_REQUIRE_TEST_POSTGRES"] = "1"
    try:
        conftest.pytest_sessionstart(None)
        report["sessionstart"] = "started"
    except child_pytest.UsageError as exc:
        report["sessionstart"] = "UsageError: " + str(exc).split(":", 1)[0]
    except Exception as exc:  # noqa: BLE001 - an uncaught refusal is the failure shown
        report["sessionstart"] = name_of(exc)
    os.environ.pop("PSEUDOLIFE_REQUIRE_TEST_POSTGRES")
    os.environ.pop("PSEUDOLIFE_TEST_DATABASE_URL")

    # The server-side check on pg_conn's reset: the server names the bank.
    class _Reached(Exception):
        pass

    for server_db in ("pseudolife_memory", "Live_Bank_Name", "pseudolife_memory_test_1"):
        sent = []

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *args):
                sent.append(sql)
                if sql != "SELECT current_database()":
                    raise _Reached(sql)
                return type("_Row", (), {"fetchone": lambda _s: (server_db,)})()

        psycopg.connect = lambda *a, **k: _Conn()
        session = pg_fixtures._pg_conn_session("postgresql://u:p@127.0.0.1:1/stub")
        try:
            got = outcome(lambda: next(session))
        finally:
            session.close()
        report["server"][server_db] = {"outcome": got, "statements": sent}

    # A dispatched run asks each test server which databases it holds.
    for held in (["postgres", "pseudolife_memory"], ["postgres", "Live_Bank_Name"],
                 ["postgres", "pseudolife_memory_test_1"]):
        class _Catalog:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *args):
                assert "pg_database" in sql
                return type("_Cur", (), {"fetchall": lambda _s: [(n,) for n in held]})()

        refusal = outcome(lambda: pg_defaults.dispatched_live_bank_refusal(
            {"PSEUDOLIFE_SUITE_DISPATCHED": "1"}, Path("absent"), connect=lambda *a, **k: _Catalog()))
        if refusal == "allowed":
            refusal = None
        elif "production bank" in refusal:
            refusal = "refused"
        report["dispatched"][held[1]] = refusal

    if report["recorded"] == guard.UNRESOLVED_PRODUCTION_DATABASE:
        try:
            conftest.pytest_configure(None)
        except child_pytest.UsageError as exc:
            report["configure"] = "UsageError: " + str(exc).split(" (", 1)[0]
        except Exception as exc:  # noqa: BLE001 - anything but the stop is the failure shown
            report["configure"] = name_of(exc)
    report["package"] = repr(sys.modules.get("pseudolife_memory")) if state == "absent" else "present"
    print(json.dumps(report))


def _run(state: str, **env_changes) -> dict:
    env = dict(os.environ)
    for name in ("PSEUDOLIFE_MCP_DATABASE_URL", "_PSEUDOLIFE_PRODUCTION_DB", "PSEUDOLIFE_TEST_DATABASE_URL",
                 "PSEUDOLIFE_SUITE_DISPATCHED", "PGDATABASE", "PGSERVICE"):
        env.pop(name, None)
    # Hermetic: an operator bench name disables conftest's per-run bench
    # database and its exit-time DROP; the admin URL points nowhere.
    env["PSEUDOLIFE_BENCH_DB"] = "pl_guard_child_unused"
    env["PSEUDOLIFE_BENCH_ADMIN_URL"] = "postgresql://u:p@127.0.0.1:1/postgres"
    env.update(env_changes)
    code = f"from tests.test_fixture_guards_package_absent import _child; _child({state!r})"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


PROBE = ["SELECT current_database()"]
# An allowed name reaches ensure_test_db's admin connection, which the stub
# refuses; ensure_test_db reports that as a setup failure, not a refusal.
PAST_NAMES = "tests.pg_defaults.PostgresSetupError"


def _past_the_guard(state):
    """A reset the server check allows goes on to the package's schema: with
    the package the next statement is sent (and the stub stops it there);
    without it the import fails before a second statement is sent."""
    if state == "present":
        return {"outcome": "tests.test_fixture_guards_package_absent._child.<locals>._Reached",
                "statements": PROBE + ["SET search_path TO public"]}
    return {"outcome": "builtins.ModuleNotFoundError", "statements": PROBE}


def _expected(state, live_refused):
    """Every outcome written out; only the cell past the guard depends on
    whether the package is there."""
    live = REFUSED if live_refused else "allowed"
    return {
        "recorded": "Live_Bank_Name" if live_refused else "pseudolife_memory",
        "dsn_left": None,
        "names": {"pseudolife_memory": REFUSED, "PSEUDOLIFE_MEMORY/": REFUSED,
                  "live_bank_name": live, "pseudolife_memory_test_1": "allowed"},
        "override": {
            "pseudolife_memory": [REFUSED, REFUSED],
            "live_bank_name": [REFUSED, REFUSED] if live_refused else ["live_bank_name", PAST_NAMES],
            "pseudolife_memory_test_1": ["pseudolife_memory_test_1", PAST_NAMES],
        },
        "sessionstart": "UsageError: refusing to reset database 'pseudolife_memory'",
        "server": {
            "pseudolife_memory": {"outcome": REFUSED, "statements": PROBE},
            "Live_Bank_Name": ({"outcome": REFUSED, "statements": PROBE} if live_refused
                               else _past_the_guard(state)),
            "pseudolife_memory_test_1": _past_the_guard(state),
        },
        "dispatched": {"pseudolife_memory": "refused",
                       "Live_Bank_Name": "refused" if live_refused else None,
                       "pseudolife_memory_test_1": None},
    }


@pytest.mark.parametrize("state", PACKAGE_STATES)
def test_with_an_exported_daemon_dsn_its_bank_and_the_default_are_refused(state):
    got = _run(state, PSEUDOLIFE_MCP_DATABASE_URL=LIVE_DSN)
    assert got.pop("package") == ("None" if state == "absent" else "present")
    assert got == _expected(state, live_refused=True)
    assert "secret" not in json.dumps(got)


@pytest.mark.parametrize("state", PACKAGE_STATES)
def test_without_a_daemon_dsn_the_default_bank_is_refused(state):
    got = _run(state)
    assert got.pop("package") == ("None" if state == "absent" else "present")
    assert got == _expected(state, live_refused=False)


@pytest.mark.parametrize("state", PACKAGE_STATES)
def test_an_implicit_daemon_dsn_stops_the_run_and_refuses_every_reset(state):
    got = _run(state, PSEUDOLIFE_MCP_DATABASE_URL=IMPLICIT_DSN)
    assert got.pop("package") == ("None" if state == "absent" else "present")
    assert got["recorded"] == "<unresolved>"
    assert got["dsn_left"] is None
    assert set(got["names"].values()) == {REFUSED}
    assert got["override"]["pseudolife_memory_test_1"] == [REFUSED, REFUSED]
    assert {cell["outcome"] for cell in got["server"].values()} == {REFUSED}
    assert got["sessionstart"] == "UsageError: refusing every test/bench reset"
    assert set(got["dispatched"].values()) == {REFUSED}
    assert got["configure"].startswith("UsageError: PSEUDOLIFE_MCP_DATABASE_URL is exported without")


def test_the_record_conftest_writes_is_the_one_the_frozen_package_reads():
    """Migration bridge only: DELETE this test when the package is retired.
    conftest writes the record through the package-free guard; the package's
    own resets (its tests' MemoryService) must read the same variable."""
    from pseudolife_memory.storage import schema
    from rust.harness_support import database_guard as guard

    for name in ("DEFAULT_PRODUCTION_DATABASE", "PRODUCTION_DATABASES",
                 "PRODUCTION_DATABASE_ENV", "UNRESOLVED_PRODUCTION_DATABASE"):
        assert getattr(guard, name) == getattr(schema, name), name
