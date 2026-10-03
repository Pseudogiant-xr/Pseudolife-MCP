"""A shim whose board registration fails at startup keeps trying.

A daemon that is unreachable, overloaded or slow when the shim starts (a
stuck tailnet link, 2026-09-30) used to leave the process memory-only for
its whole life: every board write went out without instance headers and
the daemon refused it with ``instance_authentication_required``. These
tests drive the real adapter against a scripted daemon through the shim's
startup and its proxy, the way a Claude Code session reaches them.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from pseudolife_memory import shim

BANK = "00000000-0000-4000-8000-000000000001"


class Daemon:
    """The coordination REST routes the adapter calls. ``mode`` is how the
    daemon behaves while it is down; ``up`` brings it back."""

    def __init__(self, mode="refused"):
        self.mode = mode
        self.up = False
        self.calls = []
        self.mail = []
        self.busy = 0  # attaches refused as attachment_busy once it is up

    async def __call__(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(action)
        if self.mode == "slow_attach":
            # Answers everything but attach, which it commits too late for
            # the startup budget: the lease outlives the cancelled request.
            if action == "attach":
                while not self.up:
                    await asyncio.sleep(0.01)
                if self.busy:
                    self.busy -= 1
                    return httpx.Response(400, json={"error": "attachment_busy"})
        elif not self.up:
            if self.mode == "refused":
                raise httpx.ConnectError("connection refused", request=request)
            if self.mode == "overloaded":
                return httpx.Response(503, json={"error": "unavailable"})
            if self.mode == "hung":
                while not self.up:
                    await asyncio.sleep(0.01)
            if self.mode == "unauthorized":
                return httpx.Response(401, json={"error": "unauthorized"})
        result = {"context": {"bank_id": BANK, "principal": "claude-code"},
                  "register": {"agent_id": "agent-late", "credential": "late-key"},
                  "attach": {"generation": 1},
                  "heartbeat": {"generation": 1},
                  "attempt": {}, "detach": {}}.get(action)
        if action == "receive":
            result = {"messages": self.mail, "after": "c1" if self.mail else None}
            self.mail = []
        assert result is not None, action
        return httpx.Response(200, json=result)


def _board_session(monkeypatch, daemon, *, driver, channel=False, wake=False,
                   state_path=None, coordination="1", probe=None, codex_registry=None):
    """Run ``_run_session_proxy`` with the adapter on ``daemon`` and the
    upstream MCP faked; ``driver(handler, calls, inbox)`` plays the client.
    ``codex_registry`` makes it a Codex shim whose per-thread registry is
    that class. Returns the upstream calls, after checking no task outlived
    the session."""
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    from pseudolife_memory import (
        channel as channel_module, codex_coordination, coordination_adapter)

    calls = []
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(daemon), **kwargs)

    @asynccontextmanager
    async def http_client(**kwargs):
        calls.append({"headers": dict(kwargs["headers"] or {})})
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
            calls[-1]["tool"] = name
            return types.CallToolResult(content=[], is_error=False)

    async def serve(server, *args, **kwargs):
        await driver(server.get_request_handler("tools/call").handler, calls, None)

    async def serve_channel(server, read, write, inbox_factory, **kwargs):
        await driver(server.get_request_handler("tools/call").handler, calls,
                     inbox_factory)

    async def session_run():
        before = asyncio.all_tasks()
        await shim._run_session_proxy(
            "http://127.0.0.1:8099", "fixture-token", "s", channel=channel)
        return [task for task in asyncio.all_tasks() - before if not task.done()]

    monkeypatch.setattr(coordination_adapter.httpx, "AsyncClient", client)
    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)
    monkeypatch.setattr(channel_module, "serve_channel", serve_channel)
    monkeypatch.setattr(shim, "_ADAPTER_STARTUP_SECONDS", 0.2)
    monkeypatch.setattr(shim, "_ADAPTER_RETRY_DELAYS", (0.02,), raising=False)
    if coordination is None:
        monkeypatch.delenv("PSEUDOLIFE_AGENT_COORDINATION", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", coordination)
    if probe is not None:
        monkeypatch.setattr(shim, "_board_available", probe)
    for name in ("PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_AGENT_STATE",
                 "PSEUDOLIFE_AGENT_STATE_DIR", "PSEUDOLIFE_AGENT_WAKE"):
        monkeypatch.delenv(name, raising=False)
    if wake:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_WAKE", "1")
    if state_path is not None:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_STATE", str(state_path))
    if codex_registry is not None:
        monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
        monkeypatch.setenv("PSEUDOLIFE_CODEX_DOORBELL", "0")
        monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry",
                            codex_registry)
    leftover = asyncio.run(asyncio.wait_for(session_run(), 10))
    assert leftover == []
    return calls


def _update():
    from mcp import types
    return types.CallToolRequestParams(
        name="memory_agents", arguments={"action": "update", "status": "working"})


def _texts(result):
    return [block.text for block in result.content]


async def _until_registered(handler, calls, timeout=5.0):
    """Board writes until one goes out; returns its headers and result."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            result = await handler(None, _update())
        except Exception:  # noqa: BLE001 - still registering
            await asyncio.sleep(0.02)
            continue
        return calls[-1]["headers"], result
    raise AssertionError("board registration never completed")


@pytest.mark.parametrize("mode", ["refused", "overloaded", "hung"])
def test_board_registration_retries_after_a_transient_startup_failure(
        monkeypatch, capsys, mode):
    daemon = Daemon(mode)
    seen = {}

    async def driver(handler, calls, inbox):
        # Before the daemon answers: the refusal names the retry, and the
        # call never reaches the daemon without its instance headers.
        with pytest.raises(Exception) as caught:
            await handler(None, _update())
        seen["refusal"] = caught.value.message
        assert caught.value.data["classification"] == "coordination_unavailable"
        assert not any(call.get("tool") == "memory_agents" for call in calls)
        daemon.up = True
        seen["headers"], _ = await _until_registered(handler, calls)

    _board_session(monkeypatch, daemon, driver=driver)
    assert "retried" in seen["refusal"]
    assert seen["headers"]["X-PL-Agent"] == "agent-late"
    assert seen["headers"]["X-PL-Agent-Key"] == "late-key"
    # The late adapter is closed with the session like a startup one.
    assert daemon.calls[-1] == "detach"
    err = capsys.readouterr().err
    assert "registration is retried in the background" in err
    assert "coordination registered after" in err


def test_late_registration_announces_itself_once(monkeypatch):
    """A check-in refused while registration was pending is not retried by
    itself: the first result after registration says the board now works,
    and only that one."""
    from mcp import types
    daemon = Daemon()
    seen = {}

    async def driver(handler, calls, inbox):
        daemon.up = True
        _, seen["first"] = await _until_registered(handler, calls)
        seen["later"] = [_texts(await handler(None, types.CallToolRequestParams(
            name="memory_search", arguments={"query": "x"}))) for _ in range(2)]

    _board_session(monkeypatch, daemon, driver=driver)
    assert any("board registration completed" in text for text in _texts(seen["first"]))
    assert seen["later"] == [[], []]


def test_permanent_startup_refusal_keeps_todays_behaviour(monkeypatch, capsys):
    """An auth verdict is not an outage: no retry, the old message, and a
    board write goes to the daemon (whose refusal says why)."""
    daemon = Daemon("unauthorized")

    async def driver(handler, calls, inbox):
        await asyncio.sleep(0.2)  # several retry periods
        await handler(None, _update())
        assert "X-PL-Agent" not in calls[-1]["headers"]

    _board_session(monkeypatch, daemon, driver=driver)
    assert daemon.calls == ["context"]
    err = capsys.readouterr().err
    assert "coordination unavailable; memory proxy remains active" in err
    assert "retried" not in err


def test_retry_that_meets_a_permanent_refusal_stops(monkeypatch, capsys):
    daemon = Daemon("refused")

    async def driver(handler, calls, inbox):
        await asyncio.sleep(0.1)
        daemon.mode = "unauthorized"
        await asyncio.sleep(0.3)
        count = len(daemon.calls)
        await asyncio.sleep(0.2)
        assert len(daemon.calls) == count  # no more attempts
        # Today's behaviour from here: forwarded, and the daemon decides;
        # the first result says once why, since the instructions asked for
        # a check-in.
        texts = _texts(await handler(None, _update()))
        assert "X-PL-Agent" not in calls[-1]["headers"]
        assert any("stopped retrying" in text for text in texts)
        assert _texts(await handler(None, _update())) == []

    _board_session(monkeypatch, daemon, driver=driver)
    assert "coordination unavailable; memory proxy remains active" in capsys.readouterr().err


def test_session_end_stops_a_pending_retry(monkeypatch):
    """An attempt in flight when the session ends is cancelled with it
    (``_board_session`` checks no task outlives the session), and no address
    is registered for a process that is letting go."""
    daemon = Daemon("hung")

    async def driver(handler, calls, inbox):
        await asyncio.sleep(0.3)

    _board_session(monkeypatch, daemon, driver=driver)
    assert daemon.calls[-1] == "context"  # an attempt was in flight
    assert "register" not in daemon.calls


def test_session_end_stops_a_retry_against_a_daemon_that_stays_down(monkeypatch):
    """The session ends between attempts that each fail at once (the
    connection keeps being refused): shutdown still completes."""
    daemon = Daemon("refused")

    async def driver(handler, calls, inbox):
        with pytest.raises(Exception):
            await handler(None, _update())
        await asyncio.sleep(0.3)

    _board_session(monkeypatch, daemon, driver=driver)
    assert "register" not in daemon.calls


def test_close_ends_a_retry_whose_attempt_lost_the_cancellation(monkeypatch):
    """Python 3.11's wait_for can return an attempt's own failure instead of
    the cancellation that landed as it failed (the test above hung about one
    run in four before the fix). The stand-in makes that race certain once."""
    from pseudolife_memory.coordination_adapter import AdapterError

    attempts = []
    lost = []

    class Attempt:
        async def __aenter__(self):
            attempts.append(self)
            await asyncio.sleep(10)

    async def lossy_wait_for(awaitable, timeout):
        try:
            return await awaitable
        except asyncio.CancelledError:
            if lost:
                raise
            lost.append(True)
            raise AdapterError("coordination context is unavailable",
                               code="transport_unavailable") from None

    async def run():
        late = shim._LateBoardAdapter(Attempt)
        late.start()
        while not attempts:
            await asyncio.sleep(0.001)
        closing = asyncio.ensure_future(late.aclose())
        done, _ = await asyncio.wait({closing}, timeout=2)
        return closing in done

    monkeypatch.setattr(shim, "_ADAPTER_RETRY_DELAYS", (0.001,))
    monkeypatch.setattr(asyncio, "wait_for", lossy_wait_for)
    assert asyncio.run(run()) is True
    assert lost and len(attempts) == 1


def test_late_adapter_feeds_the_channel_inbox(monkeypatch):
    """The channel opens its inbox at startup, before registration lands;
    push delivery starts once it does."""
    daemon = Daemon()
    seen = {}

    async def driver(handler, calls, inbox):
        async with aclosing(inbox()) as events:
            first = asyncio.ensure_future(anext(events))
            await asyncio.sleep(0.05)
            assert not first.done()
            daemon.mail = [{"message_id": "m1", "sender_agent_id": "peer",
                            "recipient_agent_id": "agent-late", "text": "hello"}]
            daemon.up = True
            seen["event"] = await asyncio.wait_for(first, 5)
        seen["headers"], _ = await _until_registered(handler, calls)

    _board_session(monkeypatch, daemon, driver=driver, channel=True, wake=True)
    assert seen["headers"]["X-PL-Agent"] == "agent-late"
    assert seen["event"].meta["message_id"] == "m1"
    assert "hello" in seen["event"].content


def test_retry_waits_out_the_attachment_its_cancelled_startup_left(
        monkeypatch, tmp_path):
    """With a session state file the startup attempt registers, saves the
    address, and its attach is cancelled after the daemon committed it. The
    retry resumes that address and is refused ``attachment_busy`` until the
    orphaned lease lapses; that is a wait, not a verdict."""
    daemon = Daemon("slow_attach")
    daemon.busy = 2
    state = tmp_path / "agent-state.json"
    seen = {}

    async def driver(handler, calls, inbox):
        await asyncio.sleep(0.05)
        daemon.up = True
        seen["headers"], _ = await _until_registered(handler, calls)

    _board_session(monkeypatch, daemon, driver=driver, state_path=state)
    assert seen["headers"]["X-PL-Agent"] == "agent-late"
    assert daemon.calls.count("register") == 1  # the saved address, resumed
    assert daemon.calls.count("attach") >= 3
    assert json.loads(state.read_text())["agent_id"] == "agent-late"


def test_registered_note_waits_for_a_validated_call():
    """A board call the registered adapter refuses asks for the hint too; the
    one-time note must not be spent on that refusal."""
    class Registered:
        def deliver_hint(self): return None
        def note_turn(self): pass

    late = shim._LateBoardAdapter(lambda: None)
    late._adapter = Registered()
    late._note = shim._BOARD_REGISTERED_NOTE
    assert late.deliver_hint() is None  # the refusal path: no validated call
    late.note_turn()
    assert late.deliver_hint() == shim._BOARD_REGISTERED_NOTE
    assert late.deliver_hint() is None


def _scripted_probe(*answers):
    """The default-mode board check, answering in turn (the last repeats):
    True serves the board, False refuses it, None is no answer."""
    asked = []

    def probe(url, provider):
        asked.append(url)
        return answers[min(len(asked), len(answers)) - 1]

    probe.asked = asked
    return probe


def test_unanswered_default_probe_is_retried_then_registers(monkeypatch, capsys):
    """Coordination unset (the default): the startup board check got no
    answer, which is not a no. The shim keeps asking, then registers."""
    daemon = Daemon()
    daemon.up = True
    probe = _scripted_probe(None, None, True)
    seen = {}

    async def driver(handler, calls, inbox):
        with pytest.raises(Exception) as caught:
            await handler(None, _update())
        seen["refusal"] = caught.value.message
        seen["headers"], seen["first"] = await _until_registered(handler, calls)

    _board_session(monkeypatch, daemon, driver=driver, coordination=None, probe=probe)
    assert "retried" in seen["refusal"]
    assert seen["headers"]["X-PL-Agent"] == "agent-late"
    assert any("board registration completed" in t for t in _texts(seen["first"]))
    assert len(probe.asked) == 3
    err = capsys.readouterr().err
    assert "did not answer the board check" in err
    assert "coordination registered after" in err


def test_unanswered_probe_then_a_refusal_stops_quietly(monkeypatch, capsys):
    """A board the daemon, once it answers, does not serve this bearer is
    today's quiet default: no adapter, no warning, no note; board writes go
    to the daemon, whose refusal says why."""
    daemon = Daemon()
    daemon.up = True
    probe = _scripted_probe(None, False)

    async def driver(handler, calls, inbox):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while len(probe.asked) < 2 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        texts = _texts(await handler(None, _update()))
        assert "X-PL-Agent" not in calls[-1]["headers"]
        assert texts == []

    _board_session(monkeypatch, daemon, driver=driver, coordination=None, probe=probe)
    assert len(probe.asked) == 2
    assert daemon.calls == []  # no adapter was ever built
    err = capsys.readouterr().err
    assert "coordination unavailable" not in err and "registered" not in err


def test_a_board_check_answered_as_the_close_begins_registers_nothing(monkeypatch):
    """The check's wait_for can lose a cancellation the way an attempt's can;
    a yes that arrives after the close began must not start a registration."""
    built = []

    async def run():
        late = None

        async def ask_board():
            late._closing = True  # the close began while the check was out
            return True

        late = shim._LateBoardAdapter(lambda: built.append(1), ask_board=ask_board)
        late.start()
        await asyncio.wait({late._task}, timeout=2)
        return late._task.done()

    monkeypatch.setattr(shim, "_ADAPTER_RETRY_DELAYS", (0.001,))
    assert asyncio.run(run()) is True
    assert built == []


# A Codex thread id, as Codex sends it in each tools/call's metadata.
CODEX_THREAD = "01a0ec35-a4b3-7651-a945-81ed1f6cb638"
OTHER_THREAD = "01a0ec35-a19d-7043-9336-ac6b9863afd7"


def _codex_registry():
    """A per-thread registry class that records its instances and attaches
    every thread at once."""
    class Adapter:
        instance_headers = {"X-PL-Agent": "codex-late", "X-PL-Agent-Key": "codex-key"}
        def deliver_hint(self): return None
        def note_turn(self): pass

    class Registry:
        built = []

        def __init__(self, url, token, **kwargs):
            self.closed = False
            Registry.built.append(self)

        async def get(self, thread_id, *, snapshot=None, parent_thread=None):
            return Adapter()

        def unread_hint(self, thread_id, adapter):
            return adapter.deliver_hint() if adapter is not None else "fixture: no identity"

        def note_call(self, *args, **kwargs):
            pass

        async def aclose(self):
            self.closed = True

    return Registry


def _codex_call(name, arguments, thread=CODEX_THREAD):
    from mcp import types
    return types.CallToolRequestParams(
        name=name, arguments=arguments, _meta={"threadId": thread})


def _codex_update(thread=CODEX_THREAD):
    return _codex_call("memory_agents", {"action": "update", "status": "working"}, thread)


def _codex_search(thread=CODEX_THREAD):
    return _codex_call("memory_search", {"query": "x"}, thread)


def test_codex_unanswered_board_check_is_retried_then_builds_its_registry(
        monkeypatch, capsys):
    """A Codex shim whose startup board check got no answer (the daemon slow
    or unreachable at start) keeps asking, and builds its per-thread registry
    once the daemon says it serves the board, instead of staying off the
    board for the life of the process."""
    Registry = _codex_registry()
    probe = _scripted_probe(None, None, True)
    seen = {}

    async def driver(handler, calls, inbox):
        # While the check is unanswered: a board write is refused with the
        # retry message and never forwarded; memory calls go out as before,
        # with no identity and no hint.
        with pytest.raises(Exception) as caught:
            await handler(None, _codex_update())
        seen["refusal"] = caught.value.message
        assert caught.value.data["classification"] == "coordination_unavailable"
        assert not any(call.get("tool") == "memory_agents" for call in calls)
        seen["pending_search"] = _texts(await handler(None, _codex_search()))
        assert "X-PL-Agent" not in calls[-1]["headers"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            try:
                seen["first"] = _texts(await handler(None, _codex_update()))
            except Exception:  # noqa: BLE001 - still asking
                await asyncio.sleep(0.02)
                continue
            break
        seen["headers"] = calls[-1]["headers"]
        seen["later"] = _texts(await handler(None, _codex_search()))
        # Another thread hears once too: the instructions carried no check-in.
        seen["other"] = [_texts(await handler(None, _codex_search(OTHER_THREAD)))
                         for _ in range(2)]

    _board_session(monkeypatch, Daemon(), driver=driver, coordination=None,
                   probe=probe, codex_registry=Registry)
    assert "retried" in seen["refusal"]
    assert seen["pending_search"] == []
    assert seen["headers"]["X-PL-Agent"] == "codex-late"
    assert seen["headers"]["X-PL-Session"] == CODEX_THREAD
    assert any("board registration completed" in text for text in seen["first"])
    assert seen["later"] == []
    assert any("board registration completed" in text for text in seen["other"][0])
    assert seen["other"][1] == []
    assert len(probe.asked) == 3
    assert len(Registry.built) == 1 and Registry.built[0].closed
    err = capsys.readouterr().err
    assert "did not answer the board check" in err
    assert "serves the board after a startup delay" in err


def test_codex_unanswered_board_check_then_a_refusal_stops_quietly(monkeypatch, capsys):
    """Once the daemon answers no, the Codex shim is in today's quiet
    default: no registry, no warning, no note; a board write goes to the
    daemon, whose refusal says why."""
    Registry = _codex_registry()
    probe = _scripted_probe(None, False)

    async def driver(handler, calls, inbox):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            try:  # refused with the retry message until the no lands
                texts = _texts(await handler(None, _codex_update()))
            except Exception:  # noqa: BLE001 - still asking
                await asyncio.sleep(0.01)
                continue
            break
        assert calls[-1]["tool"] == "memory_agents"
        assert "X-PL-Agent" not in calls[-1]["headers"]
        assert texts == []

    _board_session(monkeypatch, Daemon(), driver=driver, coordination=None,
                   probe=probe, codex_registry=Registry)
    assert len(probe.asked) == 2
    assert Registry.built == []
    err = capsys.readouterr().err
    assert "coordination unavailable" not in err and "registered" not in err


def test_codex_session_end_stops_an_unanswered_board_check(monkeypatch):
    """A daemon that never answers: the session still ends cleanly
    (``_board_session`` checks no task outlives it) and builds no registry."""
    Registry = _codex_registry()
    probe = _scripted_probe(None)

    async def driver(handler, calls, inbox):
        await asyncio.sleep(0.2)

    _board_session(monkeypatch, Daemon(), driver=driver, coordination=None,
                   probe=probe, codex_registry=Registry)
    assert len(probe.asked) >= 2
    assert Registry.built == []


def test_codex_doorbell_hears_when_a_late_board_check_says_no(monkeypatch, capsys):
    """An explicit doorbell needs the board; the no that arrives after an
    unanswered startup check says it is off, as a startup no does."""
    probe = _scripted_probe(None, False)

    async def driver(handler, calls, inbox):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while len(probe.asked) < 2 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(shim, "_doorbell_setting", lambda environ=None: (True, True))
    _board_session(monkeypatch, Daemon(), driver=driver, coordination=None,
                   probe=probe, codex_registry=_codex_registry())
    assert "PSEUDOLIFE_CODEX_DOORBELL needs agent coordination" in capsys.readouterr().err


def test_a_codex_board_check_answered_as_the_close_begins_builds_nothing(monkeypatch):
    """A yes that arrives after the close began must not build a registry
    that nothing would close."""
    built = []

    async def run():
        late = None

        async def ask_board():
            late._closing = True  # the close began while the check was out
            return True

        late = shim._LateCodexRegistry(lambda: built.append(1), ask_board)
        late.start()
        await asyncio.wait({late._task}, timeout=2)
        return late._task.done()

    monkeypatch.setattr(shim, "_ADAPTER_RETRY_DELAYS", (0.001,))
    assert asyncio.run(run()) is True
    assert built == []
