"""A disposable doctor proof uses real storage and separates harness and host evidence."""
import json

import psycopg
import pytest

from pseudolife_memory.coordination_proof import _disposable_bank, run_disposable_proof
from pseudolife_memory.storage.schema import ProductionDatabaseError
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.mark.parametrize("failures", [1, 2])
def test_owned_bank_cleanup_reconnects_or_reports_exact_name(monkeypatch, failures):
    from pseudolife_memory import coordination_proof as proof
    statements, connections = [], []
    class Admin:
        def __enter__(self):
            connections.append(self)
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            text = statement if isinstance(statement, str) else statement.as_string()
            if text.startswith("SET"): return
            statements.append(text)
            if text.startswith("DROP") and sum(s.startswith("DROP") for s in statements) <= failures:
                raise psycopg.OperationalError("fixture connection unavailable")
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Admin())
    name = None
    try:
        with proof._disposable_bank("host=127.0.0.1 port=6543 dbname=pseudolife_memory_test_fixture") as dsn:
            from psycopg.conninfo import conninfo_to_dict
            name = conninfo_to_dict(dsn)["dbname"]
    except Exception as exc:
        assert failures == 2 and type(exc).__name__ == "FixtureCleanupError"
        report = exc.report()
        assert report["fixture_removed"] is False and report["fixture_bank"] == name
        assert name in report["recovery"] and "DROP DATABASE" in report["recovery"]
    else:
        assert failures == 1
    assert len(connections) >= 2
    assert statements == [f'CREATE DATABASE "{name}"', *[f'DROP DATABASE "{name}" WITH (FORCE)'] * 2]


def test_create_collision_does_not_drop_any_database(monkeypatch):
    statements = []
    class Admin:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement):
            if isinstance(statement, str): return
            statements.append(statement.as_string())
            raise psycopg.errors.DuplicateDatabase("fixture collision")
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Admin())
    with pytest.raises(psycopg.errors.DuplicateDatabase):
        with _disposable_bank("host=127.0.0.1 port=6543 dbname=pseudolife_memory_test_fixture"):
            pytest.fail("collision admitted")
    assert len(statements) == 1 and statements[0].startswith("CREATE")


@pytest.mark.parametrize("failure", ["constructor", "midproof", "timeout"])
def test_proof_failure_removes_exact_owned_bank(pg_url, pg_conn, monkeypatch, failure):
    import asyncio
    from pseudolife_memory import coordination_proof as proof
    from pseudolife_memory.storage import postgres
    from psycopg.conninfo import conninfo_to_dict
    names = []
    original = postgres.PostgresStorage
    def storage(dsn):
        names.append(conninfo_to_dict(dsn)["dbname"])
        if failure == "constructor": raise RuntimeError("fixture constructor failure")
        return original(dsn)
    async def failing(*args):
        if failure == "timeout": await asyncio.sleep(1)
        raise RuntimeError("fixture proof failure")
    monkeypatch.setattr(postgres, "PostgresStorage", storage)
    monkeypatch.setattr(proof, "_proof", failing)
    expected = TimeoutError if failure == "timeout" else RuntimeError
    with pytest.raises(expected) as caught:
        proof.run_disposable_proof(pg_url, timeout=0.05)
    assert type(caught.value) is expected
    if failure != "timeout":
        assert str(caught.value) == ("fixture constructor failure" if failure == "constructor" else "fixture proof failure")
    assert names and pg_conn.execute("SELECT datname FROM pg_database WHERE datname=%s", (names[0],)).fetchone() is None


def test_proof_refuses_production_database_before_connecting(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: pytest.fail("production connection attempted"))
    with pytest.raises(ProductionDatabaseError, match="production bank"):
        with _disposable_bank("postgresql://fixture@127.0.0.1/pseudolife_memory"):
            pytest.fail("production bank admitted")


def test_tagged_proof_covers_hint_ring_receive_ack_and_drops_its_bank(pg_url, pg_conn, monkeypatch):
    from pathlib import Path
    from pseudolife_memory.web import fixtures
    package_data = Path(fixtures.__file__).parent / ".devdata"
    original_mkdir = Path.mkdir
    data_paths = []

    def mkdir(path, *args, **kwargs):
        if path == package_data:
            raise PermissionError("installed package is read-only")
        return original_mkdir(path, *args, **kwargs)

    original_service = fixtures.FixtureService

    def service(*args, **kwargs):
        result = original_service(*args, **kwargs)
        data_paths.append(result.data_dir)
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(fixtures, "FixtureService", service)
    before = pg_conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'pseudolife_memory_test_proof_%'").fetchall()
    result = run_disposable_proof(pg_url)
    repeated = run_disposable_proof(pg_url)
    assert repeated["ok"] and repeated["fixture_removed"] and repeated["tag"] != result["tag"]
    after = pg_conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'pseudolife_memory_test_proof_%'").fetchall()
    assert before == after
    assert data_paths and all(not path.exists() for path in data_paths)
    assert result["ok"] and result["fixture_removed"]
    assert result["tag"].startswith("coordination-proof-")
    assert [stage["path"] for stage in result["stages"]] == ["hint", "ring"]
    assert all(stage["enqueued"] and stage["signal_observed"] and stage["received"] and stage["harness_ack"]
               for stage in result["stages"])
    assert result["host_delivery"] == result["model_ack"] == "unverified"
    assert "synthetic-proof-bearer" not in json.dumps(result)
