"""Board names (v55): each address shows the name its harness already shows.

Before v55 almost every row on the board read ``claude-code`` or ``codex``:
the label is set once at registration, from the environment, and nothing
could change it. A row now carries ``name`` and ``name_source``. The harness
(the shim, reading the session title Claude Code or Codex already keeps)
sends it with a heartbeat; an agent can name itself with
``memory_agents(update, name=...)``; and a ``memory_session_title`` rename
names rows of that session when nothing better is set. Precedence is
harness > agent > title.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from pseudolife_memory.coordination import dispatch
from pseudolife_memory.storage.coordination import CoordinationError, CoordinationStore
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_adapter import FakeDaemon, adapter
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_storage import creds, store  # noqa: F401


def _row(store, agent):
    return store.storage.conn.execute(
        "SELECT name,name_source,name_set_at FROM coordination_agents WHERE agent_id=%s",
        (agent["agent_id"],)).fetchone()


def _name_events(store, agent):
    rows = store.storage.conn.execute(
        "SELECT payload FROM coordination_events WHERE event='update' AND agent_id=%s "
        "ORDER BY seq", (agent["agent_id"],)).fetchall()
    return [json.loads(r[0]) for r in rows if "name" in json.loads(r[0])["fields"]]


def _attached(store, agent):
    attached = store.attach(*creds(agent), attachment_id="names")
    return {"attachment_id": "names", "generation": attached["generation"]}


# --- storage -------------------------------------------------------------------

def test_register_takes_a_name_and_the_public_row_shows_it(store):
    # An unnamed row reads as its label and the first 8 characters of its
    # id (name_source ""), so rows that all say "claude-code" still differ.
    plain = store.register("alice", label="claude-code")
    assert (plain["name"], plain["name_source"]) == (
        f"claude-code {plain['agent_id'][:8]}", "")
    bare = store.register("alice")
    assert bare["name"] == bare["agent_id"][:8]
    named = store.register("alice", label="codex", name="Fix the flaky suite")
    assert (named["name"], named["name_source"]) == ("Fix the flaky suite", "agent")
    harness = store.register("alice", name="Desktop title", name_source="harness")
    assert harness["name_source"] == "harness"
    assert _row(store, harness)[2] == 1000.0
    for bad in ({"name": "x", "name_source": "title"}, {"name": "x", "name_source": "other"},
                {"name_source": "agent"}):
        with pytest.raises(CoordinationError, match="invalid_name"):
            store.register("alice", **bad)


def test_a_heartbeat_name_is_stored_once_and_logged_only_when_it_changes(store):
    agent = store.register("alice")
    beat = _attached(store, agent)
    store.heartbeat(*creds(agent), **beat, name="Session one")
    assert _row(store, agent)[:2] == ("Session one", "harness")
    store.test_time[0] += 20
    store.heartbeat(*creds(agent), **beat, name="Session one")
    store.heartbeat(*creds(agent), **beat)
    assert _row(store, agent)[2] == 1000.0           # unchanged: not rewritten
    store.heartbeat(*creds(agent), **beat, name="Renamed")
    events = _name_events(store, agent)
    assert [e["fields"]["name"] for e in events] == ["Session one", "Renamed"]
    assert events[1]["before"] == {"name": "Session one", "name_source": "harness"}


def test_precedence_is_harness_then_agent_then_title(store):
    agent = store.register("alice", episode="sess-1")
    beat = _attached(store, agent)
    assert store.title_names(["sess-1"], "From the title", principal="alice") == 1
    assert _row(store, agent)[:2] == ("From the title", "title")
    store.update(*creds(agent), name="Chosen by the agent")
    assert _row(store, agent)[:2] == ("Chosen by the agent", "agent")
    assert store.title_names(["sess-1"], "A later title", principal="alice") == 0
    assert _row(store, agent)[:2] == ("Chosen by the agent", "agent")
    store.heartbeat(*creds(agent), **beat, name="Harness title")
    assert _row(store, agent)[:2] == ("Harness title", "harness")
    out = store.update(*creds(agent), name="Agent again")
    assert (out["name"], out["name_source"]) == ("Harness title", "harness")
    store.heartbeat(*creds(agent), **beat, name="Harness renamed")
    assert _row(store, agent)[:2] == ("Harness renamed", "harness")


def test_an_agent_clears_its_own_name_but_not_the_harness_one(store):
    agent = store.register("alice")
    store.update(*creds(agent), name="Mine")
    store.update(*creds(agent), name="")
    assert _row(store, agent)[:2] == ("", "")
    beat = _attached(store, agent)
    store.heartbeat(*creds(agent), **beat, name="Harness")
    store.update(*creds(agent), name="")
    assert _row(store, agent)[:2] == ("Harness", "harness")


@pytest.mark.parametrize("bad,code", [
    ("x" * 121, "invalid_name"),
    ("tab\there", "invalid_name"),
    (7, "invalid_name"),
    ("daemon", "invalid_name"),
    ("Maintainer", "invalid_name"),
    ("ＭＡＩＮＴＡＩＮＥＲ", "invalid_name"),  # fullwidth
    ("Main tainer review", "invalid_name"),
    ("d-a-e-m-o-n", "invalid_name"),
    ("maіntainer notes", "invalid_name"),  # Cyrillic i
    ("Passkey", "invalid_name"),
    ("VERIFIED", "invalid_name"),
    ("token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "secret_like_body"),
])
def test_a_bad_name_is_refused_on_every_path(store, bad, code):
    agent = store.register("alice", episode="sess-bad")
    beat = _attached(store, agent)
    with pytest.raises(CoordinationError, match=code):
        store.register("alice", name=bad)
    with pytest.raises(CoordinationError, match=code):
        store.update(*creds(agent), name=bad)
    with pytest.raises(CoordinationError, match=code):
        store.heartbeat(*creds(agent), **beat, name=bad)
    # A title is the daemon's own source: it is cleaned to one bounded line
    # (a long or multi-line title still names the row), and one naming a
    # reserved sender or shaped like a credential names nothing.
    if isinstance(bad, str) and len(bad) <= 120 and "	" not in bad:
        assert store.title_names(["sess-bad"], bad, principal="alice") == 0
        assert _row(store, agent)[:2] == ("", "")


@pytest.mark.parametrize("bad", [
    "‮reniatniam",          # right-to-left override: renders as "maintainer"
    "board​names",          # zero-width space
    "name⁦isolate⁩",   # bidi isolates
    "line\x85break",             # C1 next-line
])
def test_a_client_name_with_control_or_format_characters_is_refused(store, bad):
    # Review of 2026-10-05: the reserved-word check reads a name with its
    # format characters removed, so a bidi override could show "maintainer"
    # in the Console. A client name may carry none (the harness and title
    # readers already turn them into spaces).
    agent = store.register("alice", episode="sess-fmt")
    beat = _attached(store, agent)
    with pytest.raises(CoordinationError, match="invalid_name"):
        store.register("alice", name=bad)
    with pytest.raises(CoordinationError, match="invalid_name"):
        store.update(*creds(agent), name=bad)
    with pytest.raises(CoordinationError, match="invalid_name"):
        store.heartbeat(*creds(agent), **beat, name=bad)


@pytest.mark.parametrize("label", ["daemon", " Daemon ", "Maintainer", "Main tainer",
                                   "ＭＡＩＮＴＡＩＮＥＲ",
                                   "verified", "passkey"])
def test_a_label_naming_a_reserved_sender_is_refused(store, label):
    with pytest.raises(CoordinationError, match="invalid_label"):
        store.register("alice", label=label)


def test_ordinary_labels_and_names_still_register(store):
    for label in ("claude-code", "codex", "agent", "demo-agent"):
        store.register("alice", label=label, name=f"{label} session")
    # Names follow the label rule (``reserved_name``): only "maintainer" is
    # refused inside a longer name, so ordinary session titles that mention
    # the daemon or a passkey still name a row.
    for name in ("Fix daemon restart", "Passkey enrolment review", "verified build"):
        store.register("alice", name=name)


def test_title_names_only_the_matching_session_rows_of_the_principal(store):
    mine = store.register("alice", episode="sess-a")
    other_session = store.register("alice", episode="sess-b")
    other_principal = store.register("bob", episode="sess-a")
    blank = store.register("alice")
    assert store.title_names(["sess-a", ""], "Titled", principal="alice") == 1
    assert _row(store, mine)[:2] == ("Titled", "title")
    for row in (other_session, other_principal, blank):
        assert _row(store, row)[:2] == ("", "")
    assert store.title_names(["sess-a"], "Titled", principal="alice") == 0   # unchanged
    assert store.title_names(["sess-a"], "  Re\ntitled  ", principal=None) == 2
    assert _row(store, other_principal)[:2] == ("Re titled", "title")
    events = _name_events(store, mine)
    assert [e["fields"]["name"] for e in events] == ["Titled", "Re titled"]


def test_an_old_bank_row_reads_without_a_name(store):
    agent = store.register("alice")
    row = dict(store._one("SELECT * FROM coordination_agents WHERE agent_id=%s",
                          (agent["agent_id"],)))
    for column in ("name", "name_source", "name_set_at"):
        row.pop(column)
    public = store._public(row)
    assert (public["name"], public["name_source"]) == (agent["agent_id"][:8], "")


def test_the_console_snapshot_and_peer_list_carry_the_name(store):
    named = store.register("alice", name="Board names")
    viewer = store.register("alice")
    snapshot = store.console_snapshot("alice")
    row = next(a for a in snapshot["agents"] if a["agent_id"] == named["agent_id"])
    assert (row["name"], row["name_source"]) == ("Board names", "agent")
    listed = store.list_agents(*creds(viewer))["agents"]
    assert next(a for a in listed if a["agent_id"] == named["agent_id"])["name"] == "Board names"


# --- dispatch and service ---------------------------------------------------------

def _headers(agent):
    return {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}


def test_dispatch_carries_the_name_on_register_update_and_heartbeat(coordinating):
    agent = dispatch(coordinating, "register", {"name": "Registered", "name_source": "harness"},
                     headers={}, principal=PRINCIPAL)
    assert agent["name"] == "Registered"
    headers = _headers(agent)
    attached = dispatch(coordinating, "attach", {"attachment_id": "n"}, headers=headers,
                        principal=PRINCIPAL)
    beat = {"attachment_id": "n", "generation": attached["generation"]}
    dispatch(coordinating, "heartbeat", {**beat, "name": "Beat name"}, headers=headers,
             principal=PRINCIPAL)
    out = dispatch(coordinating, "update", {"name": "Agent name"}, headers=headers,
                   principal=PRINCIPAL)
    assert (out["name"], out["name_source"]) == ("Beat name", "harness")
    with pytest.raises(ValueError, match="invalid_name"):
        dispatch(coordinating, "heartbeat", {**beat, "name": "maintainer"}, headers=headers,
                 principal=PRINCIPAL)
    with pytest.raises(ValueError, match="unexpected_parameter"):
        dispatch(coordinating, "update", {"name_source": "harness"}, headers=headers,
                 principal=PRINCIPAL)


def test_memory_agents_update_forwards_the_name(monkeypatch):
    from pseudolife_memory import coordination
    seen = {}
    monkeypatch.setattr(coordination, "dispatch",
                        lambda service, action, params: seen.update(action=action, **params))
    coordination.agents(object(), action="update", name="Named")
    assert seen == {"action": "update", "name": "Named"}
    with pytest.raises(ValueError, match="unexpected_parameter"):
        coordination.agents(object(), action="list", name="Named")


def test_a_session_title_names_its_board_rows(coordinating):
    from pseudolife_memory.writer_context import (
        bind_request_headers, reset_writer_context, set_writer_context,
        unbind_request_headers)
    mine = dispatch(coordinating, "register", {"episode": "SESS-T"}, headers={},
                    principal=PRINCIPAL)
    agent_named = dispatch(coordinating, "register", {"episode": "SESS-T", "name": "Own"},
                           headers={}, principal=PRINCIPAL)
    other = dispatch(coordinating, "register", {"episode": "SESS-U"}, headers={},
                     principal=PRINCIPAL)
    writer = set_writer_context("w", "SESS-T")
    request = bind_request_headers({}, principal=PRINCIPAL)
    try:
        assert coordinating.set_session_title("Board names work")["ok"] is True
    finally:
        unbind_request_headers(request)
        reset_writer_context(writer)
    store = CoordinationStore(coordinating._coordination_storage)
    names = {row["agent_id"]: (row["name"], row["name_source"]) for row in store._all(
        "SELECT agent_id,name,name_source FROM coordination_agents")}
    assert names[mine["agent_id"]] == ("Board names work", "title")
    assert names[agent_named["agent_id"]] == ("Own", "agent")
    assert names[other["agent_id"]] == ("", "")


def test_a_session_title_survives_a_board_failure(coordinating, monkeypatch):
    from pseudolife_memory import coordination
    from pseudolife_memory.writer_context import reset_writer_context, set_writer_context

    def broken(*args, **kwargs):
        raise RuntimeError("board down")

    monkeypatch.setattr(coordination, "title_board_names", broken)
    writer = set_writer_context("w", "SESS-X")
    try:
        assert coordinating.set_session_title("Still titled")["ok"] is True
    finally:
        reset_writer_context(writer)


# --- adapter ------------------------------------------------------------------------

async def _wait_for(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("fixture condition never held")
        await asyncio.sleep(0.005)


def _beats(daemon):
    return [body for action, body, _ in daemon.calls if action == "heartbeat"]


def _named(daemon):
    return [b["name"] for b in _beats(daemon) if "name" in b]


def test_a_changed_name_rides_the_next_heartbeat_and_an_unchanged_one_does_not(monkeypatch):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        title = ["First title"]
        daemon = FakeDaemon()
        client, instance = adapter(daemon, harness_name=lambda: title[0])
        async with client, instance:
            await _wait_for(lambda: len(_beats(daemon)) >= 4)
            title[0] = None                       # unreadable for a while: nothing sent
            await _wait_for(lambda: len(_beats(daemon)) >= 7)
            title[0] = "Renamed"
            await _wait_for(lambda: "Renamed" in _named(daemon))
            count = len(_beats(daemon))
            await _wait_for(lambda: len(_beats(daemon)) >= count + 3)
        assert _named(daemon) == ["First title", "Renamed"]
        assert daemon.calls[0][0] == "register" and "name" not in daemon.calls[0][1]

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_daemon_before_v55_drops_the_name_and_keeps_the_lease(monkeypatch):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", ())
        daemon = FakeDaemon()
        daemon.hook = lambda action, body: (
            httpx.Response(400, json={"error": "unexpected_parameter"})
            if action == "heartbeat" and "name" in body else None)
        client, instance = adapter(daemon, harness_name=lambda: "A title", ring_path=True)
        async with client, instance:
            await _wait_for(lambda: len(_beats(daemon)) >= 6)
            assert instance._failure is None
        beats = _beats(daemon)
        assert sum("name" in b for b in beats) == 1
        # Only the name went: the ring liveness field is still sent.
        assert all("ring_armed_until" in b for b in beats)

    asyncio.run(asyncio.wait_for(drive(), 5))


@pytest.mark.parametrize("code", ["invalid_name", "secret_like_body"])
def test_a_refused_name_is_not_sent_again_until_it_changes(monkeypatch, code):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", ())
        title = ["Maintainer"]
        daemon = FakeDaemon()
        daemon.hook = lambda action, body: (
            httpx.Response(400, json={"error": code})
            if action == "heartbeat" and body.get("name") == "Maintainer" else None)
        client, instance = adapter(daemon, harness_name=lambda: title[0])
        async with client, instance:
            await _wait_for(lambda: len(_beats(daemon)) >= 5)
            assert instance._failure is None
            title[0] = "Usable"
            await _wait_for(lambda: "Usable" in _named(daemon))
        assert _named(daemon) == ["Maintainer", "Usable"]

    asyncio.run(asyncio.wait_for(drive(), 5))


def test_a_reader_that_raises_sends_no_name(monkeypatch):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)

        def broken():
            raise OSError("gone")

        daemon = FakeDaemon()
        client, instance = adapter(daemon, harness_name=broken)
        async with client, instance:
            await _wait_for(lambda: len(_beats(daemon)) >= 3)
            assert instance._failure is None
        assert _named(daemon) == []

    asyncio.run(asyncio.wait_for(drive(), 5))


# --- shim wiring -------------------------------------------------------------------

def _claude_adapter_options(monkeypatch, env):
    from pseudolife_memory import coordination_adapter, shim
    seen = {}

    class Adapter:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        def deliver_hint(self): return None

    async def proxy(*args, **kwargs):
        pass

    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", Adapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    for name in ("PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_AGENT_STATE", "PSEUDOLIFE_AGENT_STATE_DIR",
                 "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))
    return seen


def test_a_claude_code_shim_names_its_row_from_the_session_transcript(monkeypatch, tmp_path):
    session = str(uuid.uuid4())
    transcript = tmp_path / "projects" / "C--repo" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "custom-title", "customTitle": "Desktop title",
                                      "sessionId": session}) + "\n", encoding="utf-8")
    options = _claude_adapter_options(monkeypatch, {"CLAUDE_CODE_SESSION_ID": session,
                                                    "CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert options["harness_name"]() == "Desktop title"
    options = _claude_adapter_options(monkeypatch, {})
    assert options.get("harness_name") is None


def test_each_codex_thread_adapter_reads_its_own_thread_name(tmp_path, monkeypatch):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": first, "thread_name": "Thread one"}) + "\n"
        + json.dumps({"id": second, "thread_name": "Thread two"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    built = {}

    class Adapter:
        def __init__(self, *args, **kwargs):
            built[kwargs["episode"]] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8099", "token", state_dir=tmp_path / "state",
            digest_dir=tmp_path / "digests", adapter_factory=Adapter, startup_seconds=1)
        await registry.get(first)
        await registry.get(second)
        await registry.aclose()

    asyncio.run(drive())
    assert built[first]["harness_name"]() == "Thread one"
    assert built[second]["harness_name"]() == "Thread two"


def test_lease_holders_and_waiters_carry_the_board_name(store):
    # The Roles band and the Leases panel name a holder that may have left
    # the recent roster: the lease listing carries the row's name, with the
    # same label-and-short-id fallback as the roster.
    holder = store.register("alice", label="claude-code", name="Holder session")
    waiter = store.register("alice", label="codex")
    store.acquire_lease(*creds(holder), name="full-suite", ttl=120)
    store.acquire_lease(*creds(waiter), name="full-suite", ttl=120)
    lease = next(l for l in store.list_leases()["leases"] if l["name"] == "full-suite")
    assert lease["holder"]["name"] == "Holder session"
    assert [w["name"] for w in lease["queue"]] == [f"codex {waiter['agent_id'][:8]}"]
