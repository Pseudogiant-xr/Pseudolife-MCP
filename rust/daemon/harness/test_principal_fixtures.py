"""In-process controls for package-independent principal/Console fixtures."""
import builtins
import copy
import importlib
import sys
from unittest.mock import MagicMock, Mock

import pytest

import run
from principal_fixture_vectors import AUTH, HEADERS, SIGNATURES


@pytest.fixture
def blocked(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "pseudolife_memory" or name.startswith("pseudolife_memory."):
            raise ImportError("product import blocked: " + name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def test_runner_guard_keeps_slice_restriction_without_package(blocked):
    from rust.harness_support import database_guard
    from unittest.mock import patch
    for name in ("pl_cf_w1a_fixture", "pl_cf_w2g_fixture"):
        with patch.object(database_guard, "assert_disposable_database", return_value=name):
            if run.pg.DISPOSABLE_NAME.fullmatch(name):
                run.assert_disposable(Mock())
            else:
                with pytest.raises(RuntimeError, match="non-disposable"):
                    run.assert_disposable(Mock())


def test_readiness_classification_imports_without_package(blocked, monkeypatch):
    monkeypatch.delitem(sys.modules, "test_principals_settle", raising=False)
    checks = importlib.import_module("test_principals_settle")
    checks.test_scenarios_reaching_the_store_start_ready()
    checks.test_only_never_ready_scenarios_wait_for_less_than_ready()


def test_static_contract_is_package_independent_and_detects_wrong_headers(blocked):
    assert run.static_contract_type("/ui/fixture.css")[0] == "text/css; charset=utf-8"
    c = {"name": "refusal", "method": "GET", "path": "/ui/../outside",
         "refusal_policy": "lexical-outside-root"}
    headers = {"content-security-policy": "default-src 'self'; script-src 'self'; "
               "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
               "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
               "frame-ancestors 'none'; form-action 'self'",
               "x-frame-options": "DENY", "referrer-policy": "no-referrer",
               "x-content-type-options": "nosniff", "content-type": "text/plain",
               "cache-control": "no-store", "content-length": "9"}
    answer = {"status": 403, "headers": headers, "bytes": "forbidden"}
    assert not run.compare_case(c, None, answer)["diffs"]
    for key in headers:
        wrong = copy.deepcopy(answer)
        wrong["headers"][key] = "wrong"
        assert run.compare_case(c, None, wrong)["diffs"]


def test_health_golden_host_adjustment_without_package(blocked):
    run.load_golden("tokens")
    run.load_golden("tokenless")


def test_seed_paths_without_package_or_connections(blocked, monkeypatch):
    import mcp_run
    import principal_fixtures as fixtures
    from rust.harness_support import database_guard, schema
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.fetchone.return_value = ("uses",)
    monkeypatch.setattr(fixtures.psycopg, "connect", Mock(return_value=conn))
    monkeypatch.setattr(fixtures, "ensure_schema", Mock())
    monkeypatch.setattr(schema, "ensure_schema", Mock())
    monkeypatch.setattr(fixtures, "assert_disposable_database", Mock())
    monkeypatch.setattr(database_guard, "assert_disposable_database", lambda conn: "pl_cf_w1a_fixture")
    for name in ("tokens", "pairing", "stamped", "json-float-shortest-ties", "null-embedding",
                 "unconstrained-dims", "console-signin", "console-passkey-signin"):
        run.SCENARIOS[name]().prepare_template("dbname=pl_cf_w1a_fixture")
    mcp_run.seed_stored("dbname=pl_cf_w2g_fixture")
    assert conn.execute.called


def test_fixture_guards_precede_connections_and_writes(blocked, monkeypatch):
    import principal_fixtures as fixtures
    from rust.harness_support.database_guard import ProductionDatabaseError
    connect = Mock()
    monkeypatch.setattr(fixtures.psycopg, "connect", connect)
    for dsn in ("dbname=pseudolife_memory", "postgresql://localhost/pseudolife_memory"):
        with pytest.raises(ProductionDatabaseError):
            fixtures.prepare_bank(dsn)
    with pytest.raises(ValueError, match="explicit disposable database"):
        fixtures.prepare_bank("dbname=''")
    connect.assert_not_called()
    conn = Mock()
    monkeypatch.setattr(fixtures, "assert_disposable_database", Mock(side_effect=ProductionDatabaseError))
    with pytest.raises(ProductionDatabaseError):
        fixtures.seed_principal(conn, "fixture", token="synthetic")
    conn.execute.assert_not_called()


def test_environment_vectors_and_wrong_parser_control(blocked):
    from principal_fixtures import env_auth, environment_principal
    for row in AUTH:
        mapped, singular = env_auth(row["env"])
        assert (mapped, singular) == (row["mapped"], row["singular"])
        assert [environment_principal(h, mapped, singular) for h in HEADERS] == [
            value[0] for value in row["resolved"]]
    # A parser that makes every bearer an environment identity loses the
    # stored-principal readiness witness and must fail the generated table.
    with pytest.raises(AssertionError):
        assert all(value[0] == "default" for row in AUTH for value in row["resolved"])


def test_signer_vectors_and_bad_signature_control(blocked):
    from run_scenarios.console_passkey import signer
    for row in SIGNATURES:
        key = signer(row["name"])
        options = dict(row["options"])
        if "signature" in options:
            options["signature"] = bytes.fromhex(options["signature"])
        assert key.id == row["id"]
        assert key.cose_key().hex() == row["cose"]
        actual = key.assertion(b"fixture-challenge", **options)
        assert actual == row["assertion"]
        assert key.assertion(b"next-challenge") == row["next"]
        wrong = copy.deepcopy(actual)
        wrong["response"]["signature"] = "wrong"
        with pytest.raises(AssertionError):
            assert wrong == row["assertion"]


def test_host_shape_and_malformed_controls(blocked, tmp_path):
    from host_memory import memory_shape
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    assert memory_shape(cg, proc) == {"source": "unavailable"}
    (proc / "status").write_text("VmRSS: 12 kB\nVmHWM: 19 kB\n")
    assert memory_shape(cg, proc) == {"source": "process", "near_limit": None,
                                     "rss_bytes": "<free int>", "rss_peak_bytes": "<free int>"}
    (cg / "memory.current").write_text("100")
    (cg / "memory.max").write_text("1000")
    (cg / "memory.stat").write_text("anon 40\nfile 60\ninactive_file 50\n")
    (cg / "memory.events").write_text("max 2\noom 0\noom_kill 0\n")
    good = {"source": "cgroup", "current_bytes": 100, "working_set_bytes": 50,
            "limit_bytes": 1000, "used_fraction": 0.05, "near_limit": False,
            "anon_bytes": 40, "file_bytes": 60, "rss_bytes": 12000,
            "rss_peak_bytes": 19000, "events": {"max": 2, "oom": 0, "oom_kill": 0}}
    assert run.normalize_memory(good) == memory_shape(cg, proc)
    for key, value in (("source", "unavailable"), ("used_fraction", "<free float>"),
                       ("current_bytes", "<free int>"), ("near_limit", "<free bool>"),
                       ("events", {"oom": "<free int>"}), ("limit_bytes", True)):
        wrong = {**good, key: value}
        assert run.normalize_memory(wrong) != memory_shape(cg, proc)
