"""Resource leases through the authenticated REST API and the model tool.

The daemon half of ``pseudolife-mcp lease run`` (process-held leases) and of
``memory_agents`` claim/release (session-held ones): the same board queue,
reached with the caller's instance credential; listing needs only the bearer,
like the awareness roster.
"""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.asgi_helpers import stub_mcp
from pseudolife_memory.memory.hlc import HybridLogicalClock
from pseudolife_memory.storage.postgres import PostgresStorage
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.api import build_console_app

BEARER = {"Authorization": "Bearer fixture-bearer"}


def _app(pg_url, *, token_map=None):
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
    app = build_console_app(stub_mcp, "fixture-bearer", lambda: {}, service,
                            token_map=token_map or {})
    return storage, app


def _run(storage, drive):
    try:
        asyncio.run(asyncio.wait_for(drive(), 20))
    finally:
        storage.close()


async def _register(client, label):
    response = await client.post("http://fixture/api/coordination/register", headers=BEARER,
                                 json={"label": label, "capabilities": {"resumable": False}})
    assert response.status_code == 200, response.text
    body = response.json()
    return {**BEARER, "X-PL-Agent": body["agent_id"], "X-PL-Agent-Key": body["credential"]}


async def _post(client, headers, action, body):
    return await client.post(f"http://fixture/api/coordination/{action}", headers=headers,
                             json=body)


def test_rest_lease_queue_release_and_grant(pg_conn, pg_url):
    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            a, b = await _register(client, "runner"), await _register(client, "next")
            held = (await _post(client, a, "lease", {
                "name": "full-suite", "ttl": 120, "expect": 1200, "purpose": "pytest"})).json()
            assert held["state"] == "held" and held["fence"] >= 1
            queued = (await _post(client, b, "lease", {"name": "full-suite", "ttl": 120})).json()
            assert queued["state"] == "queued" and queued["position"] == 1
            assert queued["holder"]["label"] == "runner"
            # Listing needs only the bearer, no instance credential.
            listed = await _post(client, BEARER, "leases", {})
            assert listed.status_code == 200, listed.text
            [lease] = listed.json()["leases"]
            assert lease["name"] == "full-suite" and lease["queued"] == 1
            assert lease["holder"]["purpose"] == "pytest"
            released = (await _post(client, a, "release", {"name": "full-suite"})).json()
            assert released == {"name": "full-suite", "released": True, "dequeued": False}
            taken = (await _post(client, b, "lease", {"name": "full-suite", "ttl": 120})).json()
            assert taken["state"] == "held" and taken["fence"] == held["fence"] + 1
            # An agent's roster carries the leases too.
            roster = (await _post(client, a, "agents", {})).json()
            assert [lease["name"] for lease in roster["leases"]] == ["full-suite"]

    _run(storage, drive)


def test_rest_lease_errors_map_to_stable_codes(pg_conn, pg_url):
    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            a = await _register(client, "a")
            bad = await _post(client, a, "lease", {"name": "gpu", "ttl": 5})
            assert (bad.status_code, bad.json()) == (400, {"error": "invalid_ttl"})
            missing = await _post(client, a, "lease", {"ttl": 120})
            assert (missing.status_code, missing.json()) == (400, {"error": "missing_parameter"})
            stranger = await _post(client, a, "release", {"name": "gpu"})
            assert (stranger.status_code, stranger.json()) == (400, {"error": "lease_not_held"})
            anonymous = await _post(client, BEARER, "lease", {"name": "gpu", "ttl": 120})
            assert anonymous.status_code == 401
            assert anonymous.json() == {"error": "instance_authentication_required"}
            extra = await _post(client, BEARER, "leases", {"name": "gpu", "owner": "x"})
            assert (extra.status_code, extra.json()) == (400, {"error": "unexpected_parameter"})

    _run(storage, drive)


def test_rest_full_queue_is_a_transient_429(pg_conn, pg_url, monkeypatch):
    from pseudolife_memory.storage import coordination as store_module
    monkeypatch.setattr(store_module, "LEASE_QUEUE_MAX", 1)
    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            a, b, c = [await _register(client, n) for n in "abc"]
            await _post(client, a, "lease", {"name": "gpu", "ttl": 120})
            await _post(client, b, "lease", {"name": "gpu", "ttl": 120})
            full = await _post(client, c, "lease", {"name": "gpu", "ttl": 120})
            assert (full.status_code, full.json()) == (429, {"error": "lease_queue_full"})

    _run(storage, drive)


def test_leases_listing_still_requires_an_admitted_principal(pg_conn, pg_url):
    storage, app = _app(pg_url, token_map={"map-bearer": "editor"})

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            denied = await _post(client, {"Authorization": "Bearer map-bearer"}, "leases", {})
            assert (denied.status_code, denied.json()) == (403, {"error": "principal_not_allowed"})
            nobody = await client.post("http://fixture/api/coordination/leases", json={})
            assert nobody.status_code == 401

    _run(storage, drive)


def test_rest_update_expect_marks_status_overdue_later(pg_conn, pg_url):
    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            a, b = await _register(client, "a"), await _register(client, "b")
            updated = await _post(client, a, "update", {"status": "suite=running", "expect": 1200})
            assert updated.status_code == 200, updated.text
            roster = (await _post(client, b, "agents", {})).json()
            [row] = [r for r in roster["agents"] if r["agent_id"] == a["X-PL-Agent"]]
            assert row["status_expires_at"] is not None and row["status_overdue"] is False
            bad = await _post(client, a, "update", {"status": "x", "expect": 0})
            assert (bad.status_code, bad.json()) == (400, {"error": "invalid_expect"})

    _run(storage, drive)


# ── the model surface ────────────────────────────────────────────────────


def _capture(monkeypatch):
    from pseudolife_memory import coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch",
                        lambda svc, action, args: seen.append((action, args)) or {"ok": True})
    return seen


def test_claim_maps_to_a_session_held_lease(monkeypatch):
    from pseudolife_memory import mcp_server as mod
    seen = _capture(monkeypatch)
    mod.memory_agents(action="claim", lease="coordinator:pseudolife-mcp",
                      status="running the overnight queue", expect=3600)
    mod.memory_agents(action="claim", lease="claim:pseudolife_memory/shim.py")
    mod.memory_agents(action="release", lease="claim:pseudolife_memory/shim.py")
    assert seen == [
        ("lease", {"name": "coordinator:pseudolife-mcp", "ttl": 3600, "expect": 3600,
                   "purpose": "running the overnight queue"}),
        ("lease", {"name": "claim:pseudolife_memory/shim.py", "ttl": 86400}),
        ("release", {"name": "claim:pseudolife_memory/shim.py"}),
    ]


def test_update_passes_expect_and_list_refuses_claim_fields(monkeypatch):
    from pseudolife_memory import mcp_server as mod
    from pseudolife_memory.writer_context import bind_request_headers, unbind_request_headers
    seen = _capture(monkeypatch)
    mod.memory_agents(action="update", status="suite=running", expect=1200)
    assert seen == [("update", {"status": "suite=running", "expect": 1200})]
    monkeypatch.setattr(mod, "service", SimpleNamespace(coordination_awareness=lambda: {}))
    token = bind_request_headers({})
    try:
        for kwargs in ({"lease": "gpu"}, {"expect": 5}):
            with pytest.raises(ValueError, match="unexpected_parameter"):
                mod.memory_agents(action="list", **kwargs)
    finally:
        unbind_request_headers(token)
    with pytest.raises(ValueError, match="missing_parameter"):
        mod.memory_agents(action="claim")
    with pytest.raises(ValueError, match="unexpected_parameter"):
        mod.memory_agents(action="release", lease="gpu", expect=5)
    with pytest.raises(ValueError, match="unexpected_parameter"):
        mod.memory_agents(action="update", lease="gpu")


def test_claim_and_release_need_the_sessions_board_identity():
    from pseudolife_memory.shim import _requires_coordination_identity
    for action in ("update", "claim", "release"):
        assert _requires_coordination_identity("memory_agents", {"action": action})
    assert not _requires_coordination_identity("memory_agents", {"action": "list"})
    assert not _requires_coordination_identity("memory_agents", None)
