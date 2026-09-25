"""Roster hygiene: lease renewal is not activity, the peer list shows
active peers by default, per-launch addresses are reaped quickly, and a
Claude Code session keys its adapter state by session.

Measured 2026-09-20 on the live bank: 90 registered addresses, 11 leased,
67 of them Codex threads whose shim had been killed with the row still
marked attached, 5 to 160 hours idle, no task, no mail. The 20 s heartbeat had
kept every parked shim looking as busy as a working one, and the list tool
returned all of them.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from pseudolife_memory.coordination import dispatch
from pseudolife_memory.storage.coordination import CoordinationError
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_adapter import FakeDaemon, adapter
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_storage import creds, store  # noqa: F401


def _last_activity(store, agent):
    return store.storage.conn.execute(
        "SELECT last_activity FROM coordination_agents WHERE agent_id=%s",
        (agent["agent_id"],)).fetchone()[0]


def _remaining(store):
    return {row[0] for row in store.storage.conn.execute(
        "SELECT agent_id FROM coordination_agents").fetchall()}


# --- storage -----------------------------------------------------------------

def test_heartbeat_renews_the_lease_but_is_activity_only_when_the_shim_saw_a_turn(store):
    """A parked shim heartbeats every 20 s; that must not read as work.
    The heartbeat carries ``active`` only when a tool call passed through
    the shim since the previous one, and only then does the row's
    last_activity move."""
    a = store.register("alice")
    attached = store.attach(*creds(a), attachment_id="one")
    generation = attached["generation"]
    store.test_time[0] += 30
    out = store.heartbeat(*creds(a), attachment_id="one", generation=generation)
    assert out["lease_until"] == store.test_time[0] + 60
    assert _last_activity(store, a) == 1000.0
    store.test_time[0] += 30
    store.heartbeat(*creds(a), attachment_id="one", generation=generation, active=True)
    assert _last_activity(store, a) == 1060.0
    with pytest.raises(CoordinationError, match="invalid_active"):
        store.heartbeat(*creds(a), attachment_id="one", generation=generation, active="yes")


def test_peer_list_shows_leased_or_recently_active_peers_and_counts_the_rest(store):
    """The default list is peers holding a lease or active within
    ACTIVE_WINDOW, leased first; every other peer matching the scope is
    only counted, under ``idle_omitted``."""
    from pseudolife_memory.storage.coordination import ACTIVE_WINDOW
    caller = store.register("alice")
    parked = store.register("alice", project="p")
    recent = store.register("alice", project="p")
    stale = store.register("alice", project="p")
    elsewhere = store.register("alice", project="q")
    generation = store.attach(*creds(parked), attachment_id="parked")["generation"]
    # Keep the parked shim's lease alive across the whole window with
    # heartbeats that saw no turn.
    while store.test_time[0] < 1000 + ACTIVE_WINDOW + 1:
        store.test_time[0] += 30
        store.heartbeat(*creds(parked), attachment_id="parked", generation=generation)
    store.update(*creds(recent), status="working")

    listed = store.list_agents(*creds(caller))
    assert [row["agent_id"] for row in listed["agents"]] == [parked["agent_id"], recent["agent_id"]]
    assert listed["idle_omitted"] == 2
    assert listed["agents"][0]["adapter_available"] is True
    assert listed["agents"][0]["last_activity"] == 1000.0
    assert listed["truncated"] is False
    scoped = store.list_agents(*creds(caller), project="p")
    assert [row["agent_id"] for row in scoped["agents"]] == [parked["agent_id"], recent["agent_id"]]
    assert scoped["idle_omitted"] == 1
    assert stale["agent_id"] not in {row["agent_id"] for row in scoped["agents"]}
    assert elsewhere["agent_id"] not in {row["agent_id"] for row in listed["agents"]}
    # A page that cuts active peers says so; the idle count is unaffected.
    page = store.list_agents(*creds(caller), limit=1)
    assert [row["agent_id"] for row in page["agents"]] == [parked["agent_id"]]
    assert page["truncated"] is True and page["idle_omitted"] == 2


def test_a_recovery_reattach_is_not_activity_but_a_new_attachment_is(store):
    """The 2026-09-25 bulk touch. The host slept from 12:22 to 12:32 AEST, every
    idle shim's 60 s lease lapsed, and on wake each adapter re-attached
    under its own attachment id. attach stamped last_activity, so eleven
    sessions idle for hours all read as active within 28 s of each other.
    A re-attach under the id the row already holds is the adapter
    recovering its lease; a new id is a process starting, which is the
    agent's own act."""
    a = store.register("alice")
    generation = store.attach(*creds(a), attachment_id="one")["generation"]
    store.test_time[0] += 5 * 3600          # asleep: the lease lapses
    again = store.attach(*creds(a), attachment_id="one")
    assert again["generation"] == generation + 1
    assert _last_activity(store, a) == 1000.0
    store.test_time[0] += 30                # lease still live: a plain renewal
    store.attach(*creds(a), attachment_id="one")
    assert _last_activity(store, a) == 1000.0
    store.test_time[0] += 3600
    store.attach(*creds(a), attachment_id="two")
    assert _last_activity(store, a) == store.test_time[0]


def test_the_peer_list_says_how_old_each_status_is_and_marks_old_ones_stale(store):
    """On 2026-09-25 a dozen listed peers still reported PRs "awaiting
    maintainer merge" a day after the merge, with nothing saying when the
    line was written. The age comes from the v42 audit log: the newest
    register, or update that carried a status, for that agent."""
    from pseudolife_memory.storage.coordination import STATUS_STALE_AFTER
    caller = store.register("alice")
    fresh = store.register("alice", status="reviewing")
    old = store.register("alice", status="PR #1 awaiting merge")
    blank = store.register("alice")
    store.test_time[0] += STATUS_STALE_AFTER
    store.update(*creds(fresh), status="reviewing")    # re-stated counts as set
    store.update(*creds(old), task="t")                # leaves the status alone
    store.update(*creds(blank), task="t")
    rows = {row["agent_id"]: row for row in store.list_agents(*creds(caller))["agents"]}
    assert rows[fresh["agent_id"]]["status_set_at"] == 1000.0 + STATUS_STALE_AFTER
    assert rows[fresh["agent_id"]]["status_age"] == "just now"
    assert rows[fresh["agent_id"]]["status_stale"] is False
    assert rows[old["agent_id"]]["status_set_at"] == 1000.0
    assert rows[old["agent_id"]]["status_age"] == "2 hours ago"
    assert rows[old["agent_id"]]["status_stale"] is True
    # Nothing to be stale: a blank status is never flagged.
    assert rows[blank["agent_id"]]["status_set_at"] == 1000.0
    assert rows[blank["agent_id"]]["status_stale"] is False


def test_a_status_older_than_the_audit_log_is_reported_as_older_than_the_log(store):
    """The fix-week statuses were written before the v42 log existed, and
    retention cuts old rows too, so the log may hold no status event. The
    status is then at least as old as the log's oldest row, and that bound
    is what the list reports; it is stale only once the bound is."""
    from pseudolife_memory.storage.coordination import STATUS_STALE_AFTER
    caller = store.register("alice")
    legacy = store.register("alice", status="PR #343 awaiting merge")
    conn = store.storage.conn
    conn.execute("DELETE FROM coordination_events WHERE agent_id=%s", (legacy["agent_id"],))
    store.test_time[0] += 600
    store.update(*creds(legacy), task="t")             # active, status untouched
    row = store.list_agents(*creds(caller))["agents"][0]
    assert row["status_set_at"] is None
    assert row["status_age"] == "more than 10 minutes ago"
    assert row["status_stale"] is False                # the bound is only 10 min
    store.test_time[0] += STATUS_STALE_AFTER
    store.update(*creds(legacy), task="t2")
    row = store.list_agents(*creds(caller))["agents"][0]
    assert row["status_age"] == "more than 2 hours ago"
    assert row["status_stale"] is True
    # An empty log bounds nothing.
    conn.execute("DELETE FROM coordination_events")
    row = store.list_agents(*creds(caller))["agents"][0]
    assert (row["status_set_at"], row["status_age"], row["status_stale"]) == (None, "unknown", False)


def test_an_attached_peer_that_stays_silent_leaves_the_default_list(store):
    """Attached is not working. On 2026-09-25 the default list carried 18
    attached peers, a dozen of them sessions silent for hours, each held
    there by its shim's heartbeat. A leased peer is listed for
    ATTACHED_IDLE_WINDOW after its own last action (long enough to show a
    waiting session beside its stale status), then only counted; one own
    action lists it again."""
    from pseudolife_memory.storage.coordination import ATTACHED_IDLE_WINDOW
    caller = store.register("alice")
    parked = store.register("alice", status="PR #343 awaiting merge")
    generation = store.attach(*creds(parked), attachment_id="parked")["generation"]

    def hold_lease_until(moment):
        while store.test_time[0] < moment:
            store.test_time[0] += 55
            store.heartbeat(*creds(parked), attachment_id="parked", generation=generation)

    hold_lease_until(1000 + ATTACHED_IDLE_WINDOW - 60)
    listed = store.list_agents(*creds(caller))
    assert [row["agent_id"] for row in listed["agents"]] == [parked["agent_id"]]
    assert listed["agents"][0]["adapter_available"] is True
    assert listed["agents"][0]["status_stale"] is True
    hold_lease_until(1000 + ATTACHED_IDLE_WINDOW + 60)
    listed = store.list_agents(*creds(caller))
    assert listed["agents"] == [] and listed["idle_omitted"] == 1
    store.heartbeat(*creds(parked), attachment_id="parked", generation=generation, active=True)
    listed = store.list_agents(*creds(caller))
    assert [row["agent_id"] for row in listed["agents"]] == [parked["agent_id"]]
    assert listed["idle_omitted"] == 0


def test_ephemeral_reap_waits_an_hour_after_the_lease_lapsed_not_after_the_last_turn(store):
    """The deploy case. A state-less shim parked for hours holds its lease
    by heartbeat alone; a daemon restart longer than the lease lapses it,
    and the first heartbeat served afterwards prunes first. The address
    must survive that: it goes only once the lease itself has been gone
    for the window, by which time the adapter would long have re-attached."""
    from pseudolife_memory.storage.coordination import EPHEMERAL_AGENT_RETENTION
    parked = store.register("alice", capabilities={"resumable": False})
    generation = store.attach(*creds(parked), attachment_id="one")["generation"]
    while store.test_time[0] < 1000 + 2 * EPHEMERAL_AGENT_RETENTION:
        store.test_time[0] += 30
        store.heartbeat(*creds(parked), attachment_id="one", generation=generation)
    store.test_time[0] += 61 + 60          # restart: lease lapsed, prune a minute later
    store.prune()
    assert parked["agent_id"] in _remaining(store)
    store.test_time[0] += EPHEMERAL_AGENT_RETENTION
    store.prune()
    assert parked["agent_id"] not in _remaining(store)


def test_a_live_shim_idle_past_retention_survives_a_lease_lapse(store):
    """Recovery re-attaches used to stamp last_activity, which kept a live
    idle shim young against AGENT_RETENTION; they no longer do, since they
    are not the agent's act. So the lease speaks for the process instead:
    a resumable address, like a state-less one, goes only once its lease
    has been gone for EPHEMERAL_AGENT_RETENTION, not the moment a daemon
    restart or a host sleep lets it lapse."""
    from pseudolife_memory.storage.coordination import (
        AGENT_RETENTION, EPHEMERAL_AGENT_RETENTION)
    parked = store.register("alice", capabilities={"resumable": True})
    store.attach(*creds(parked), attachment_id="one")
    store.test_time[0] += AGENT_RETENTION + 3600
    store.attach(*creds(parked), attachment_id="one")   # recovery: not activity
    store.test_time[0] += 61 + 60          # restart: lease lapsed, prune a minute later
    store.prune()
    assert parked["agent_id"] in _remaining(store)
    store.attach(*creds(parked), attachment_id="one")
    store.test_time[0] += 61 + EPHEMERAL_AGENT_RETENTION   # the process is gone
    store.prune()
    assert parked["agent_id"] not in _remaining(store)


def test_prune_reaps_per_launch_addresses_after_an_hour_and_keeps_resumable_ones(store):
    """An address whose adapter declared ``resumable: false`` has no state
    file behind it, so nothing will ever attach to it again: once it is
    unleased, idle for EPHEMERAL_AGENT_RETENTION and referenced by no
    retained message it goes. Addresses that declared themselves resumable,
    or that predate the flag, keep the seven-day rule."""
    from pseudolife_memory.storage.coordination import (
        AGENT_RETENTION, EPHEMERAL_AGENT_RETENTION)
    assert EPHEMERAL_AGENT_RETENTION < AGENT_RETENTION
    ephemeral = store.register("alice", capabilities={"pull": True, "resumable": False})
    resumable = store.register("alice", capabilities={"pull": True, "resumable": True})
    legacy = store.register("alice")
    mailed = store.register("alice", capabilities={"resumable": False})
    leased = store.register("alice", capabilities={"resumable": False})
    sender = store.register("alice")
    store.send(*creds(sender), to=mailed["agent_id"], text="keep", request_id="r")
    store.test_time[0] += EPHEMERAL_AGENT_RETENTION + 1
    store.attach(*creds(leased), attachment_id="live")
    store.prune()
    assert _remaining(store) == {resumable["agent_id"], legacy["agent_id"], mailed["agent_id"],
                                 leased["agent_id"], sender["agent_id"]}
    with pytest.raises(CoordinationError, match="instance_not_found"):
        store.authenticate(*creds(ephemeral))
    store.test_time[0] += AGENT_RETENTION
    store.prune()
    assert _remaining(store) == set()


def test_dispatch_accepts_the_heartbeat_turn_flag(coordinating):
    agent = dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL)
    headers = {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
    attached = dispatch(coordinating, "attach", {"attachment_id": "turns"},
                        headers=headers, principal=PRINCIPAL)
    beat = {"attachment_id": "turns", "generation": attached["generation"]}
    out = dispatch(coordinating, "heartbeat", {**beat, "active": True},
                   headers=headers, principal=PRINCIPAL)
    assert out["generation"] == attached["generation"]
    with pytest.raises(ValueError, match="invalid_active"):
        dispatch(coordinating, "heartbeat", {**beat, "active": "yes"},
                 headers=headers, principal=PRINCIPAL)


# --- adapter -----------------------------------------------------------------

async def _wait_for(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("fixture condition never held")
        await asyncio.sleep(0.005)


def _heartbeats(daemon):
    return [body for action, body, _ in daemon.calls if action == "heartbeat"]


def test_registration_declares_whether_the_address_can_be_resumed(tmp_path):
    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(daemon)
        async with client, instance:
            pass
        assert daemon.calls[0][1]["capabilities"]["resumable"] is False
        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=tmp_path / "state.json")
        async with client, instance:
            pass
        assert daemon.calls[0][1]["capabilities"]["resumable"] is True

    asyncio.run(drive())


def test_heartbeat_carries_the_turn_flag_once_after_a_tool_call(monkeypatch):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        daemon = FakeDaemon()
        client, instance = adapter(daemon)
        async with client, instance:
            await _wait_for(lambda: len(_heartbeats(daemon)) >= 2)
            instance.note_turn()
            await _wait_for(lambda: any(b.get("active") is True for b in _heartbeats(daemon)))
            flagged = next(i for i, b in enumerate(_heartbeats(daemon)) if b.get("active") is True)
            await _wait_for(lambda: len(_heartbeats(daemon)) >= flagged + 3)
        beats = _heartbeats(daemon)
        assert all("active" not in b for b in beats[:flagged])
        assert [b.get("active") for b in beats[flagged:flagged + 3]] == [True, None, None]

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_turn_flag_is_dropped_when_the_daemon_predates_it(monkeypatch):
    """A shim upgraded ahead of its daemon must keep its lease: one
    unexpected_parameter refusal of the flag retires it for the process."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", ())
        daemon = FakeDaemon()
        daemon.hook = lambda action, body: (
            httpx.Response(400, json={"error": "unexpected_parameter"})
            if action == "heartbeat" and "active" in body else None)
        client, instance = adapter(daemon)
        async with client, instance:
            await _wait_for(lambda: len(_heartbeats(daemon)) >= 1)
            instance.note_turn()
            await _wait_for(lambda: any("active" in b for b in _heartbeats(daemon)))
            refused = next(i for i, b in enumerate(_heartbeats(daemon)) if "active" in b)
            await _wait_for(lambda: len(_heartbeats(daemon)) >= refused + 3)
            instance.note_turn()
            await _wait_for(lambda: len(_heartbeats(daemon)) >= refused + 6)
            assert instance._failure is None
        beats = _heartbeats(daemon)
        assert [("active" in b) for b in beats[refused:]] == [True] + [False] * (len(beats) - refused - 1)

    asyncio.run(asyncio.wait_for(drive(), 5))


def _losing_daemon():
    """A daemon that retired the adapter's first address: the next heartbeat
    and every attach under that address answer instance_not_found; a fresh
    registration works."""
    daemon = FakeDaemon()
    state = {"registers": 0, "lost": False}

    def hook(action, body):
        if action == "register":
            state["registers"] += 1
            return httpx.Response(200, json={"agent_id": f"agent-{state['registers']}",
                                             "credential": "fixture-key"})
        if action in {"heartbeat", "attach"} and state["registers"] == 1 and (
                state["lost"] or action == "heartbeat"):
            state["lost"] = True
            return httpx.Response(404, json={"error": "instance_not_found"})
        return None

    daemon.hook = hook
    return daemon, state


def test_a_state_less_adapter_replaces_an_address_the_daemon_retired(monkeypatch):
    """Nothing backs the address, so nothing is lost: re-register and carry
    on, instead of stopping for good with advice to restore a saved
    identity that never existed."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", ())
        daemon, state = _losing_daemon()
        client, instance = adapter(daemon)
        async with client, instance:
            await _wait_for(lambda: state["registers"] == 2 and instance._failure is None)
            assert instance.instance_headers["X-PL-Agent"] == "agent-2"
            assert instance._permanent_failure is False
            await _wait_for(lambda: daemon.actions()[-1] == "heartbeat"
                            and daemon.calls[-1][2]["x-pl-agent"] == "agent-2")

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_state_backed_adapter_keeps_its_retired_address_for_deliberate_recovery(monkeypatch, tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", ())
        daemon, state = _losing_daemon()
        client, instance = adapter(daemon, state_path=tmp_path / "state.json")
        async with client, instance:
            await _wait_for(lambda: instance._permanent_failure)
            assert state["registers"] == 1
            assert instance.instance_headers["X-PL-Agent"] == "agent-1"

    asyncio.run(asyncio.wait_for(drive(), 5))


# --- shim --------------------------------------------------------------------

def _proxy_fixture(monkeypatch, seen):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    @asynccontextmanager
    async def http_client(**kwargs):
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args): self._tool_output_schemas = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): return SimpleNamespace(instructions="fixture")
        async def call_tool(self, name, arguments):
            seen["calls"].append(name)
            return types.CallToolResult(content=[], is_error=False)

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    return Server, types


def test_every_tool_call_notes_a_turn_on_the_adapter(monkeypatch):
    from pseudolife_memory import shim
    seen = {"calls": [], "turns": 0}
    Server, types = _proxy_fixture(monkeypatch, seen)

    class Adapter:
        instance_headers = {"X-PL-Agent": "fixture-agent", "X-PL-Agent-Key": "private-key"}
        async def validate_snapshot(self, snapshot): pass
        def note_turn(self): seen["turns"] += 1

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        for name in ("memory_stats", "memory_search"):
            result = await handler.handler(None, types.CallToolRequestParams(
                name=name, arguments={}))
            assert result.is_error is False

    monkeypatch.setattr(Server, "run", serve)
    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token", "process-session",
                            coordination_adapter=Adapter(), coordination_hint=lambda: None))
    assert seen["calls"] == ["memory_stats", "memory_search"]
    assert seen["turns"] == 2


def test_codex_tool_calls_note_a_turn_on_the_thread_adapter(monkeypatch):
    from pseudolife_memory import shim
    seen = {"calls": [], "turns": 0}
    Server, types = _proxy_fixture(monkeypatch, seen)
    thread = str(uuid.uuid4())

    class Adapter:
        instance_headers = {"X-PL-Agent": "fixture-agent", "X-PL-Agent-Key": "private-key"}
        def note_turn(self): seen["turns"] += 1

    class Registry:
        async def get(self, thread_id, snapshot=None):
            assert thread_id == thread
            return Adapter()
        def unread_hint(self, thread_id, adapter): return None
        def note_call(self, thread_id, name, arguments, **options): pass

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        result = await handler.handler(None, types.CallToolRequestParams(
            name="memory_stats", arguments={}, _meta={"threadId": thread}))
        assert result.is_error is False

    monkeypatch.setattr(Server, "run", serve)
    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token", "process-session",
                            codex_metadata=True, coordination_registry=Registry()))
    assert seen["calls"] == ["memory_stats"]
    assert seen["turns"] == 1


def _session_proxy(monkeypatch, env, tmp_path):
    from pseudolife_memory import coordination_adapter, shim
    seen = {}

    class Adapter:
        def __init__(self, *args, **kwargs):
            seen["options"] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        def deliver_hint(self): return None

    async def proxy(*args, **kwargs):
        seen["proxy"] = kwargs

    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", Adapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    for name in ("PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_AGENT_STATE", "PSEUDOLIFE_AGENT_STATE_DIR",
                 "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))
    return seen["options"]["state_path"]


def test_claude_session_keys_its_state_file_when_a_state_dir_is_configured(monkeypatch, tmp_path):
    """Claude Code launches the shim with CLAUDE_CODE_SESSION_ID in its
    environment and keeps that id across resume, so with a state directory
    configured the session gets a durable address instead of a new one
    per launch."""
    from pseudolife_memory.coordination_identity import bound_state_path
    session_id = str(uuid.uuid4())
    path = _session_proxy(monkeypatch, {"PSEUDOLIFE_AGENT_STATE_DIR": str(tmp_path / "agents"),
                                        "CLAUDE_CODE_SESSION_ID": session_id}, tmp_path)
    assert path == bound_state_path(tmp_path / "agents", "http://fixture", session_id)
    assert path.parent.is_dir()


@pytest.mark.parametrize("env", [
    {"CLAUDE_CODE_SESSION_ID": "11111111-2222-3333-4444-555555555555"},
    {"PSEUDOLIFE_AGENT_STATE_DIR": "{tmp}", "CLAUDE_CODE_SESSION_ID": "not-a-session"},
    {"PSEUDOLIFE_AGENT_STATE_DIR": "{tmp}",
     "CLAUDE_CODE_SESSION_ID": "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"},
    {"PSEUDOLIFE_AGENT_STATE_DIR": "{tmp}"},
])
def test_session_state_needs_both_a_state_dir_and_a_canonical_session_id(monkeypatch, tmp_path, env):
    env = {k: v.replace("{tmp}", str(tmp_path)) for k, v in env.items()}
    assert _session_proxy(monkeypatch, env, tmp_path) is None


def test_an_explicit_state_file_still_wins_over_the_session_directory(monkeypatch, tmp_path):
    env = {"PSEUDOLIFE_AGENT_STATE": str(tmp_path / "fixed.json"),
           "PSEUDOLIFE_AGENT_STATE_DIR": str(tmp_path / "agents"),
           "CLAUDE_CODE_SESSION_ID": str(uuid.uuid4())}
    assert _session_proxy(monkeypatch, env, tmp_path) == str(tmp_path / "fixed.json")


# --- one board identity per session ------------------------------------------
#
# 2026-09-25: a Claude Code session in the Desktop app sent a pre-notice as
# agent 6b52025b (principal claude-desktop) and its START/END notices as
# b7437e8b (claude-code), and overwrote another session's status line on
# 6b52025b. That session saw two servers. One was its own shim, launched by
# Claude Code with CLAUDE_CODE_SESSION_ID. The other was the Desktop app's
# entry (writer id claude-desktop): one process serving every conversation in
# the app, so the one address it registered was every conversation's.

class _RecordingAdapter:
    constructed = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    instance_headers = {"X-PL-Agent": "shared-agent", "X-PL-Agent-Key": "shared-key"}
    async def validate_snapshot(self, snapshot): pass
    def note_turn(self): pass
    def deliver_hint(self): return None


def _desktop_env(monkeypatch, extra=None):
    from pseudolife_memory import coordination_adapter
    _RecordingAdapter.constructed = []
    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", _RecordingAdapter)
    for name in ("PSEUDOLIFE_AGENT_STATE", "PSEUDOLIFE_AGENT_STATE_DIR", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "claude-desktop")
    for name, value in (extra or {}).items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("extra", [
    {},
    {"PSEUDOLIFE_WRITER_ID": " Claude-Desktop "},
    # A fixed state file names one address; it does not stop the process
    # serving many conversations through it.
    {"PSEUDOLIFE_AGENT_STATE": "{tmp}/desktop.json"},
])
def test_the_desktop_app_process_registers_no_board_identity(monkeypatch, tmp_path, extra):
    from pseudolife_memory import shim
    _desktop_env(monkeypatch, {k: v.replace("{tmp}", str(tmp_path)) for k, v in extra.items()})
    seen = {}

    async def proxy(*args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(shim, "_proxy", proxy)
    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))
    assert _RecordingAdapter.constructed == []
    assert "coordination_adapter" not in seen and "agent_headers" not in seen
    assert "own Pseudolife server" in seen["coordination_refusal"]
    assert not (tmp_path / "desktop.json").exists()


def test_the_shared_process_refuses_board_writes_and_forwards_everything_else(monkeypatch):
    """Board writes from the shared process are refused before any request,
    with the way out in the error text, which is what the model reads; list
    and ordinary memory calls go through unchanged."""
    from pseudolife_memory import shim
    seen = {"calls": []}
    Server, types = _proxy_fixture(monkeypatch, seen)
    _desktop_env(monkeypatch)
    refused = [("memory_agents", {"action": "update", "status": "START live maintenance"}),
               ("memory_message", {"action": "send", "to": "b7437e8b", "text": "hi",
                                   "request_id": "r1"}),
               ("memory_message", {"action": "receive"}),
               ("memory_message", {"action": "ack", "message_id": "m1"})]

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        for name, arguments in refused:
            with pytest.raises(Exception) as caught:
                await handler.handler(None, types.CallToolRequestParams(
                    name=name, arguments=arguments))
            assert caught.value.data["classification"] == "coordination_unavailable"
            assert caught.value.data["operation_outcome"] == "not_dispatched"
            assert "every conversation" in caught.value.message
            assert "own Pseudolife server" in caught.value.message
        for name, arguments in (("memory_agents", {"action": "list"}), ("memory_search", {})):
            result = await handler.handler(None, types.CallToolRequestParams(
                name=name, arguments=arguments))
            assert result.is_error is False

    monkeypatch.setattr(Server, "run", serve)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token",
                                        "process-session"))
    assert seen["calls"] == ["memory_agents", "memory_search"]


def test_a_claude_code_session_shim_still_binds_its_own_identity(monkeypatch, tmp_path):
    """The per-session shim is the one that should hold the address: the
    Desktop rule keys on the app entry's writer id, not on Claude clients."""
    from pseudolife_memory import shim
    _desktop_env(monkeypatch, {"PSEUDOLIFE_WRITER_ID": "claude-code",
                               "CLAUDE_CODE_SESSION_ID": str(uuid.uuid4())})
    seen = {}

    async def proxy(*args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(shim, "_proxy", proxy)
    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))
    assert len(_RecordingAdapter.constructed) == 1
    assert seen["agent_headers"]["X-PL-Agent"] == "shared-agent"
    assert "coordination_refusal" not in seen
