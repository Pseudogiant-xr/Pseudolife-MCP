"""``pseudolife-mcp lease`` against the real daemon code, end to end.

tests/test_lease_cli.py drives the CLI against a scripted fake board; this
drives it against the real coordination app and store on the bench Postgres,
so the client and the daemon are held to one contract: the lease-run address
registers, holds, renews and releases, a queued run times out and leaves the
queue, and the audit log records each step.
"""
import json
import subprocess
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


class _Bridge(httpx.BaseTransport):
    """A synchronous httpx transport into the ASGI app. Each request goes
    through Starlette's test client by its public ``request()`` and comes
    back as a plain-bytes response, so any httpx/Starlette pairing works
    (the test client's own transport object is private, and on the Linux CI
    pairing its responses do not satisfy another client's sync-stream
    check). The client is never entered as a context manager, so the app's
    lifespan (the daemon's startup) never runs."""

    _HOP = {"host", "content-length", "transfer-encoding", "connection"}

    def __init__(self, app):
        from starlette.testclient import TestClient
        self.client = TestClient(app, base_url="http://fixture")

    def handle_request(self, request):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in self._HOP}
        response = self.client.request(request.method, str(request.url), headers=headers,
                                       content=request.read())
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.content, request=request)


def _bridge(app):
    bridge = _Bridge(app)
    return bridge.client, bridge


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


def test_a_hold_mirrors_the_lease_and_tells_the_peers_concerned(board, monkeypatch, capsys,
                                                                 tmp_path):
    """``lease hold`` against the real store: the lease is held under the
    hold's own address while the followed process lives, the peers whose
    status says they work around the suite or the GPU get the acquire and
    release notices as ordinary board mail (so the request ids and texts
    pass the daemon's checks), and a peer with another status gets none."""
    bridge, storage = board
    monkeypatch.delenv("PSEUDOLIFE_AGENT_PROJECT", raising=False)
    runner = _post(bridge, "register", {"label": "suite-runner", "status": "SUITE-START; suite=running"})
    quiet = _post(bridge, "register", {"label": "quiet", "status": "reviewing a PR"})
    as_ = lambda a: {"X-PL-Agent": a["agent_id"], "X-PL-Agent-Key": a["credential"]}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    threading.Thread(target=child.wait, daemon=True).start()
    seen = {}

    def watch():
        for _ in range(400):
            leases = _post(bridge, "leases", {"name": "gpu"})["leases"]
            if leases:
                seen["lease"] = leases[0]
                return
            threading.Event().wait(0.02)

    watcher = threading.Thread(target=watch)
    watcher.start()
    code = lease_cli.main(["hold", "gpu", "--while-pid", str(child.pid), "--expect", "10m",
                           "--purpose", "bench server", "--worktree", "wt-bench"],
                          transport=bridge)
    watcher.join(10)
    assert code == 0
    # Stamped with the lock directory's instance id, which the real board
    # accepts (it is no secret-shaped value).
    label = seen["lease"]["holder"]["label"]
    assert label == lease_cli.hold_label(tmp_path / "locks") and "@" in label
    assert seen["lease"]["holder"]["purpose"] == "bench server"
    assert seen["lease"]["expected_end"] is not None
    assert _post(bridge, "leases", {"name": "gpu"})["leases"] == []
    texts = [m["text"] for m in _post(bridge, "receive", {}, as_(runner))["messages"]]
    assert len(texts) == 2, texts
    assert texts[0].startswith("LEASE gpu acquired") and texts[1].startswith("LEASE gpu released")
    assert f"pid {child.pid}" in texts[0] and "wt-bench" in texts[0] and "expected end" in texts[0]
    assert _post(bridge, "receive", {}, as_(quiet))["messages"] == []
    kinds = [event for event, _ in _events(storage, "lease_acquire", "lease_release")]
    assert kinds == ["lease_acquire", "lease_release"]
    assert "board skipped" not in capsys.readouterr().err


def test_a_session_parked_on_the_lease_is_rung_by_both_notices(board, monkeypatch):
    """A session parked until the ``gpu`` lease clears (``park_clear_by:
    gpu``) is rung by the hold's acquire and release notices. They come from
    the hold's own address, and the release notice after the board lease is
    already free, so the daemon names the clearer from its lease table and
    audit log: withheld as chatter, the parked session slept through the
    notice the CLI sent it (PR #443 review, 2026-09-28)."""
    bridge, storage = board
    monkeypatch.delenv("PSEUDOLIFE_AGENT_PROJECT", raising=False)
    parked = _post(bridge, "register", {"label": "parked-on-gpu"})
    as_ = {"X-PL-Agent": parked["agent_id"], "X-PL-Agent-Key": parked["credential"]}
    _post(bridge, "update", {"status": "waiting for the bench server; suite=idle",
                             "park_reason": "needs_resource", "park_needs": "the bench GPU",
                             "park_clear_by": "gpu"}, as_)
    storage.conn.execute("UPDATE coordination_agents SET wake_enabled=true WHERE agent_id=%s",
                         (parked["agent_id"],))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"])
    threading.Thread(target=child.wait, daemon=True).start()
    code = lease_cli.main(["hold", "gpu", "--while-pid", str(child.pid), "--worktree", "wt"],
                          transport=bridge)
    assert code == 0
    rows = storage.conn.execute(
        "SELECT text, wake FROM coordination_messages WHERE recipient_agent_id=%s "
        "ORDER BY recipient_sequence", (parked["agent_id"],)).fetchall()
    assert [text.split(":")[0] for text, _ in rows] == ["LEASE gpu acquired", "LEASE gpu released"]
    assert [(wake["decision"], wake["reason"]) for _, wake in rows] == [("rung", "clearer")] * 2
