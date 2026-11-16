"""Production-bank refusal and nearby disposable inputs, without a server."""
from types import SimpleNamespace

import pytest

from rust.harness_support import database_guard as guard

ENV = ("_PSEUDOLIFE_PRODUCTION_DB", "PSEUDOLIFE_MCP_DATABASE_URL", "PGDATABASE", "PGSERVICE")
DSNS = [
    ("postgresql://u:p@localhost/pseudolife%5Fmemory", {}, "pseudolife_memory"),
    ("postgresql://u:p@localhost/other?dbname=pseudolife_memory", {}, "pseudolife_memory"),
    ("host=localhost dbname='disposable bank'", {}, "disposable bank"),
    ("host=localhost", {"PGDATABASE": "pl_cf_pgs_test"}, "pl_cf_pgs_test"),
    ("service=fixture dbname=pl_cf_pgs_test", {}, "pl_cf_pgs_test"),
    ("host=localhost", {}, None),
    ("not a dsn ===", {}, None),
    ("dbname=''", {"PGDATABASE": "pl_cf_pgs_test"}, None),
    ("postgresql://u@localhost/?dbname=", {"PGDATABASE": "pl_cf_pgs_test"}, None),
    ("service=fixture", {"PGDATABASE": "pl_cf_pgs_test"}, None),
    ("host=localhost", {"PGDATABASE": "pl_cf_pgs_test", "PGSERVICE": "fixture"}, None),
]
# (environment, target, extra refusals, decision). Each protected name has
# a nearby legitimate disposable name; uncertainty refuses either one.
DECISIONS = [
    ({}, "pseudolife_memory", (), True),
    ({}, "PSEUDOLIFE_MEMORY/", (), True),
    ({}, "pseudolife_memory_bench", (), False),
    ({}, "pseudolife_memory_test_1", (), False),
    ({}, "pl_cf_pgs_test", ("PL_CF_PGS_TEST/",), True),
    ({}, "pl_cf_pgs_test_1", ("pl_cf_pgs_test",), False),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "dbname=pl_cf_pgs_live"}, "pl_cf_pgs_live", (), True),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "dbname=pl_cf_pgs_live"}, "pl_cf_pgs_live_1", (), False),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://u:p@localhost/other?dbname=pl_cf_pgs_live"},
     "pl_cf_pgs_live", (), True),
    ({"_PSEUDOLIFE_PRODUCTION_DB": "Recorded_Bank"}, "recorded_bank/", (), True),
    ({"_PSEUDOLIFE_PRODUCTION_DB": "Recorded_Bank"}, "recorded_bank_test", (), False),
    ({"_PSEUDOLIFE_PRODUCTION_DB": "recorded_bank", "PSEUDOLIFE_MCP_DATABASE_URL": "dbname=pl_cf_pgs_test"},
     "pl_cf_pgs_test", (), False),
    ({"_PSEUDOLIFE_PRODUCTION_DB": "<unresolved>"}, "pl_cf_pgs_test", (), "unresolved"),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "host=localhost"}, "pl_cf_pgs_test", (), "unresolved"),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "not a dsn ==="}, "pl_cf_pgs_test", (), "unresolved"),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "dbname=''", "PGDATABASE": "pl_cf_pgs_live"},
     "pl_cf_pgs_test", (), "unresolved"),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "service=fixture", "PGDATABASE": "pl_cf_pgs_live"},
     "pl_cf_pgs_test", (), "unresolved"),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "host=localhost", "PGDATABASE": "pl_cf_pgs_live"},
     "pl_cf_pgs_test", (), False),
    ({"PSEUDOLIFE_MCP_DATABASE_URL": "host=localhost", "PGDATABASE": "pl_cf_pgs_live"},
     "pl_cf_pgs_live", (), True),
]


def environment(monkeypatch, values):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def connection(name, statements):
    def execute(sql):
        statements.append(sql)
        assert sql == "SELECT current_database()"
        return SimpleNamespace(fetchone=lambda: (name,))
    return SimpleNamespace(execute=execute)


@pytest.mark.parametrize("dsn,env,want", DSNS)
def test_dsn_identity(dsn, env, want, monkeypatch):
    environment(monkeypatch, env)
    assert guard.dsn_database_name(dsn) == want


@pytest.mark.parametrize("env,name,extra,want", DECISIONS)
def test_production_name_decision(env, name, extra, want, monkeypatch):
    environment(monkeypatch, env)
    if want == "unresolved":
        with pytest.raises(guard.ProductionDatabaseError, match="cannot be identified"):
            guard.is_production_database(name, extra=extra)
    else:
        assert guard.is_production_database(name, extra=extra) is want


@pytest.mark.parametrize("env,name,extra,want", [row for row in DECISIONS if not row[2]])
def test_name_and_server_guards(env, name, extra, want, monkeypatch):
    environment(monkeypatch, env)
    statements = []
    for check in (lambda: guard.refuse_production_database(name),
                  lambda: guard.assert_disposable_database(connection(name, statements))):
        if want:
            with pytest.raises(guard.ProductionDatabaseError):
                check()
        else:
            check()
    assert statements == ["SELECT current_database()"]


def test_server_identity_overrules_a_disposable_looking_dsn(monkeypatch):
    environment(monkeypatch, {})
    assert guard.dsn_database_name("dbname=pl_cf_pgs_test") == "pl_cf_pgs_test"
    with pytest.raises(guard.ProductionDatabaseError):
        guard.assert_disposable_database(connection("pseudolife_memory", []))
    assert guard.assert_disposable_database(connection("pl_cf_pgs_test", [])) == "pl_cf_pgs_test"


def test_server_probe_failure_is_not_permission_to_reset(monkeypatch):
    environment(monkeypatch, {})

    def broken(sql):
        raise OSError("probe unavailable")

    with pytest.raises(OSError, match="probe unavailable"):
        guard.assert_disposable_database(SimpleNamespace(execute=broken))


def test_temporary_frozen_package_decision_comparison(monkeypatch):
    """Migration bridge only: DELETE this test when the package is retired."""
    from pseudolife_memory.storage import schema

    def result(module, action):
        try:
            return ("allowed", action(module))
        except module.ProductionDatabaseError as exc:
            return ("refused", str(exc))

    for dsn, env, _ in DSNS:
        environment(monkeypatch, env)
        assert guard.dsn_database_name(dsn) == schema.dsn_database_name(dsn)
    for env, name, extra, _ in DECISIONS:
        environment(monkeypatch, env)
        actions = [lambda m: m.is_production_database(name, extra=extra),
                   lambda m: m.refuse_production_database(name),
                   lambda m: m.assert_disposable_database(connection(name, []))]
        for action in actions:
            assert result(guard, action) == result(schema, action)
