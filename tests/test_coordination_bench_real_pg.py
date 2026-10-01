"""Bounded ASGI recovery uses a minted bank on the designated fixture server."""
from contextlib import contextmanager
import json

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


def test_actual_adapter_recovery_replays_mail_and_keeps_fence(pg_url, pg_conn, tmp_path, monkeypatch):
    from evals import coordination_bench as harness
    from pseudolife_memory.web import fixtures
    original_database = harness.disposable_database
    original_service = fixtures.FixtureService
    names = []
    @contextmanager
    def database(admin_url):
        with original_database(admin_url) as dsn:
            names.append(conninfo_to_dict(dsn)["dbname"])
            yield dsn
    monkeypatch.setattr(harness, "disposable_database", database)
    monkeypatch.setattr(fixtures, "FixtureService", lambda: original_service(data_dir=tmp_path / "console-state"))
    for key in ("PSEUDOLIFE_TEST_DATABASE_URL", "PSEUDOLIFE_MCP_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    out = tmp_path / "bench"
    assert harness.main(["--admin-url", make_conninfo(pg_url, dbname="postgres"),
                         "--out", str(out), "--samples", "2", "--recovery"]) == 0
    summary = json.loads((out / "summary.json").read_text())
    assert set(summary["arms"]) == {"disabled", "pull", "channel"}
    for arm in ("pull", "channel"):
        recovery = summary["arms"][arm]["recovery"]
        assert recovery["receipt_preserved"] and recovery["identity_reopened"] and recovery["lease_preserved"] and recovery["pending_mail_replayed"]
        assert recovery["mixed_search_calls"] == 2 and recovery["ack_actor"] == "test_harness"
    assert names and pg_conn.execute("SELECT datname FROM pg_database WHERE datname=%s", (names[0],)).fetchone() is None
    print("Exact minted database removed; recovery trace and summary persisted.")
