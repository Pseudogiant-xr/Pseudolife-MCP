"""``pseudolife-mcp lease`` against the real daemon code, end to end.

tests/test_lease_cli.py drives the CLI against a scripted fake board; this
drives it against the real coordination app and store on the bench Postgres,
so the client and the daemon are held to one contract: the lease-run address
registers, holds, renews and releases, a queued run times out and leaves the
queue, and the audit log records each step.
"""
import json
import sys
import threading

import httpx
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.asgi_helpers import stub_mcp
from pseudolife_memory import lease_cli
from pseudolife_memory.memory.hlc import HybridLogicalClock
from pseudolife_memory.storage.postgres import PostgresStorage
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.api import build_console_app

BEARER = "fixture-bearer"


def _bridge(app):
    """A synchronous httpx transport into the ASGI app: Starlette's test
    client serves each request on a blocking portal, so the CLI's blocking
    client reaches the real app in process. Not entered as a context
    manager, so the app's lifespan (the daemon's startup) never runs."""
    from starlette.testclient import TestClient
    client = TestClient(app, base_url="http://fixture")
    return client, client._transport


@pytest.fixture
def board(pg_conn, pg_url, tmp_path, monkeypatch):
    storage = PostgresStorage(pg_url)
    from pseudolife_memory.storage.schema import assert_disposable_database
    assert_disposable_database(storage.conn)
    storage.conn.execute("TRUNCATE coordination_leases, coordination_lease_waiters")
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._db_url = pg_url
    service._storage = storage
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    client, bridge = _bridge(build_console_app(stub_mcp, BEARER, lambda: {}, service))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", BEARER)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    monkeypatch.delenv(lease_cli.HELD_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_LEASE_LOCK_DIR", str(tmp_path / "locks"))
    for name, value in (("BOARD_POLL", 0.05), ("LOCK_POLL", 0.05), ("NOTICE_EVERY", 0.0),
                        ("CHILD_POLL", 0.02), ("TRANSIENT_RETRY", 0.05)):
        monkeypatch.setattr(lease_cli, name, value)
    monkeypatch.setattr(lease_cli, "_renew_interval", lambda ttl: 0.1)
    try:
        yield bridge, storage
    finally:
        client.close()
        storage.close()


def _post(bridge, action, body, headers=None):
    with httpx.Client(transport=bridge, base_url="http://fixture") as client:
        response = client.post(f"/api/coordination/{action}", json=body,
                               headers={"Authorization": f"Bearer {BEARER}", **(headers or {})})
    assert response.status_code == 200, response.text
    return response.json()


def _events(storage, *kinds):
    rows = storage.conn.execute(
        "SELECT event, payload FROM coordination_events WHERE event = ANY(%s) ORDER BY seq",
        (list(kinds),)).fetchall()
    return [(event, json.loads(payload)) for event, payload in rows]


def test_a_run_holds_the_lease_on_the_board_and_releases_it(board, tmp_path):
    bridge, storage = board
    seen = {}
    marker = tmp_path / "running"
    child = ("import pathlib, time; pathlib.Path(r'%s').write_text('x'); time.sleep(1.5)"
             % marker)

    def watch():
        for _ in range(200):
            if marker.exists():
                seen["leases"] = _post(bridge, "leases", {})["leases"]
                return
            threading.Event().wait(0.02)

    watcher = threading.Thread(target=watch)
    watcher.start()
    code = lease_cli.main(["run", "gpu", "--expect", "60", "--purpose", "bench smoke",
                           "--", sys.executable, "-c", child], transport=bridge)
    watcher.join(10)
    assert code == 0
    [lease] = seen["leases"]
    assert lease["name"] == "gpu"
    assert lease["holder"]["label"] == lease_cli.LABEL
    assert lease["holder"]["purpose"] == "bench smoke"
    assert lease["expected_end"] is not None and lease["stale"] is False
    assert _post(bridge, "leases", {})["leases"] == []
    kinds = [event for event, _ in _events(storage, "lease_acquire", "lease_release",
                                           "lease_update")]
    # Renewals repeat the same estimate, so none of them is logged.
    assert kinds == ["lease_acquire", "lease_release"]


def test_a_queued_run_times_out_and_leaves_the_queue(board, capsys):
    bridge, storage = board
    holder = _post(bridge, "register", {"label": "holder"})
    headers = {"X-PL-Agent": holder["agent_id"], "X-PL-Agent-Key": holder["credential"]}
    assert _post(bridge, "lease", {"name": "full-suite", "ttl": 300}, headers)["state"] == "held"
    code = lease_cli.main(["run", "full-suite", "--timeout", "1", "--",
                           sys.executable, "-c", "raise SystemExit(3)"], transport=bridge)
    assert code == lease_cli.EXIT_TEMPFAIL
    err = capsys.readouterr().err
    assert "holder" in err and holder["credential"] not in err
    [lease] = _post(bridge, "leases", {"name": "full-suite"})["leases"]
    assert lease["holder"]["agent_id"] == holder["agent_id"] and lease["queued"] == 0
    assert [e for e, _ in _events(storage, "lease_queue", "lease_dequeue")] == [
        "lease_queue", "lease_dequeue"]
    # Freed, the next run gets it at once and the command's exit code comes back.
    _post(bridge, "release", {"name": "full-suite"}, headers)
    code = lease_cli.main(["run", "full-suite", "--timeout", "5", "--",
                           sys.executable, "-c", "raise SystemExit(3)"], transport=bridge)
    assert code == 3


def test_lease_list_shows_the_board_beside_local_locks(board, capsys):
    bridge, _ = board
    holder = _post(bridge, "register", {"label": "coordinator-session"})
    headers = {"X-PL-Agent": holder["agent_id"], "X-PL-Agent-Key": holder["credential"]}
    _post(bridge, "lease", {"name": "coordinator:pseudolife-mcp", "ttl": 3600,
                            "purpose": "overnight queue"}, headers)
    assert lease_cli.main(["list", "--json"], transport=bridge) == 0
    listed = json.loads(capsys.readouterr().out)
    board_names = [lease["name"] for lease in listed["board"]["leases"]]
    assert board_names == ["coordinator:pseudolife-mcp"]


def test_the_operator_breaks_a_lease_and_the_next_waiter_gets_it(board, pg_url, monkeypatch,
                                                                    capsys, tmp_path):
    """``lease break`` is the maintainer's way out of a hold nobody will
    release (a dead session's day-long claim): it opens the bank directly,
    like board-audit, never through an agent's credential."""
    bridge, storage = board
    stuck = _post(bridge, "register", {"label": "stuck"})
    nxt = _post(bridge, "register", {"label": "next"})
    as_ = lambda a: {"X-PL-Agent": a["agent_id"], "X-PL-Agent-Key": a["credential"]}
    _post(bridge, "lease", {"name": "claim:shim.py", "ttl": 86400}, as_(stuck))
    _post(bridge, "lease", {"name": "claim:shim.py", "ttl": 3600}, as_(nxt))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    assert lease_cli.main(["break", "claim:shim.py"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"name": "claim:shim.py", "broken": True, "was_held_by": stuck["agent_id"]}
    [lease] = _post(bridge, "leases", {"name": "claim:shim.py"})["leases"]
    assert lease["holder"]["agent_id"] == nxt["agent_id"]
    [(event, body)] = _events(storage, "lease_break")
    assert body["name"] == "claim:shim.py"
    assert storage.conn.execute(
        "SELECT actor FROM coordination_events WHERE event='lease_break'").fetchone()[0] == "operator"
    # Nothing held: says so, still exit 0; an unknown bank is a usage error.
    assert lease_cli.main(["break", "gpu"]) == 0
    assert json.loads(capsys.readouterr().out)["broken"] is False
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "no-bank-here"))
    assert lease_cli.main(["break", "gpu"]) == 1
    assert "no bank found" in capsys.readouterr().err
