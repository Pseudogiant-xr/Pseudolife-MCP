"""The local coordination instrument reports bounded, explicitly synthetic evidence."""
import importlib.metadata
import asyncio
import json
import platform
import subprocess
from contextlib import contextmanager

import pytest


def test_percentile_uses_nearest_rank_and_empty_is_unknown():
    from evals.coordination_bench import percentile
    assert percentile([], 95) is None
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile([9, 1, 5], 50) == 5


def test_summary_keeps_control_comparison_and_host_uncertainty(tmp_path):
    from evals.coordination_bench import summarize, write_summary
    arms = {name: {"ordinary_ms": values, "enqueue_to_event_ms": [], "harness_ack_ms": [],
                   "requests": 3, "context_bytes": 10, "duplicate_ids": 0}
            for name, values in {"disabled": [1, 2, 3], "pull": [2, 3, 4], "channel": [3, 4, 5]}.items()}
    arms["pull"]["recovery"] = {"receipt_preserved": True}
    summary = summarize(arms)
    assert summary["arms"]["pull"]["ordinary_p95_delta_ms"] == 1
    assert summary["arms"]["channel"]["ordinary_p95_delta_ms"] == 2
    assert summary["host_wake_ms"] is None and summary["model_ack_ms"] is None
    assert summary["ack_actor"] == "test_harness"
    assert summary["arms"]["pull"]["recovery"] == {"receipt_preserved": True}
    assert summary["python_version"] == platform.python_version()
    assert summary["mcp_sdk_version"] == importlib.metadata.version("mcp")
    # No MCP host takes part in the local instrument, so it can report none.
    assert summary["host_version"] is None
    path = tmp_path / "summary.json"
    write_summary(path, summary)
    assert json.loads(path.read_text()) == summary


def test_trace_rejects_sensitive_fields_and_flushes_each_record(tmp_path):
    from evals.coordination_bench import Trace
    path = tmp_path / "trace.jsonl"
    with Trace(path) as trace:
        trace.write(arm="pull", kind="request", action="send", status=200, elapsed_ms=1.0)
        assert len(path.read_text().splitlines()) == 1
        with pytest.raises(ValueError, match="trace field"):
            trace.write(credential="must-not-be-written")
    assert "must-not-be-written" not in path.read_text()


def test_database_override_is_refused_before_connection(monkeypatch):
    from evals.coordination_bench import disposable_database
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", "postgresql://example.invalid/unrelated")
    with pytest.raises(ValueError, match="override"):
        with disposable_database():
            pytest.fail("database opened")


@pytest.mark.parametrize("damage", [None, "receipt", "fence", "mail"])
def test_recovery_cell_reopens_identity_and_checks_mail_lease_and_mixed_search(tmp_path, damage):
    """The scripted HTTP fixture checks the replay oracle, not PG durability."""
    import httpx
    from evals.coordination_bench import run_recovery_cell
    from tests.test_coordination_adapter import FakeDaemon

    daemon = FakeDaemon()
    state = {"acked": False, "sends": 0, "searches": 0, "leases": 0}

    def hook(action, body):
        if action == "lease":
            state["leases"] += 1
            return httpx.Response(200, json={"state": "held", "fence":
                8 if damage == "fence" and state["leases"] == 2 else 7})
        if action == "send":
            state["sends"] += 1
            return httpx.Response(200, json={"message_id":
                "changed" if damage == "receipt" and state["sends"] == 2 else "mail-1"})
        if action == "receive":
            messages = [] if state["acked"] else [{"message_id":
                "changed" if damage == "mail" else "mail-1", "text": "synthetic recovery note"}]
            return httpx.Response(200, json={"messages": messages, "after": "agent-a:1"})
        if action == "ack":
            state["acked"] = True
            return httpx.Response(200, json={"state": "acknowledged"})
        if action == "release":
            return httpx.Response(200, json={"released": True})
        if action == "search":
            state["searches"] += 1
            return httpx.Response(200, json={"entries": [], "count": 0})

    daemon.hook = hook

    async def transport(request):
        if request.url.path == "/api/search":
            await asyncio.sleep(0)
            return hook("search", {})
        return await daemon(request)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            if damage:
                with pytest.raises(RuntimeError, match="recovery"):
                    await run_recovery_cell(client, tmp_path, "fixture-cell", samples=2)
            else:
                result = await run_recovery_cell(client, tmp_path, "fixture-cell", samples=2)
                assert result == {"identity_reopened": True, "pending_mail_replayed": True,
                                  "receipt_preserved": True, "lease_preserved": True,
                                  "mixed_search_calls": 2, "ack_actor": "test_harness"}
                assert state["searches"] == 2 and state["acked"]
                assert daemon.actions().count("register") == 1
            assert "release" in daemon.actions()  # Failure also releases the owned fixture lease.

    asyncio.run(asyncio.wait_for(drive(), 15))


def test_disposable_database_uses_explicit_admin_and_removes_only_minted_database(monkeypatch):
    from evals import coordination_bench as harness
    from psycopg.conninfo import conninfo_to_dict
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    executed = []

    class Admin:
        def execute(self, statement):
            executed.append(statement.as_string())

    @contextmanager
    def connect(dsn, **kwargs):
        assert conninfo_to_dict(dsn)["port"] == "6543"
        yield Admin()

    monkeypatch.setattr(harness.psycopg, "connect", connect)
    with harness.disposable_database("host=127.0.0.1 port=6543 dbname=postgres") as dsn:
        params = conninfo_to_dict(dsn)
        assert params["port"] == "6543" and params["dbname"].startswith("coordination_bench_")
        name = params["dbname"]
    assert executed == [f'CREATE DATABASE "{name}"', f'DROP DATABASE "{name}" WITH (FORCE)']


def test_private_output_accepts_ignored_directory_and_refuses_tracked_paths(tmp_path, monkeypatch):
    from evals import coordination_bench as harness
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("private/\n", encoding="utf-8")

    @contextmanager
    def database(_admin):
        raise RuntimeError("fixture reached isolated setup")
        yield

    monkeypatch.setattr(harness, "disposable_database", database)
    with pytest.raises(SystemExit) as refused:
        harness.main(["--out", str(tmp_path / "public")])
    assert refused.value.code == 2 and not (tmp_path / "public").exists()
    with pytest.raises(RuntimeError, match="reached isolated setup"):
        harness.main(["--out", str(tmp_path / "private" / "run")])
    assert (tmp_path / "private" / "run" / "trace.jsonl").exists()
