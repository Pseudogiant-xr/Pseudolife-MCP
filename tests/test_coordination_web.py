"""Awareness request identity stays scoped to its authorized request."""
from types import SimpleNamespace
import asyncio
import json

import pytest

from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call, stub_mcp


def test_awareness_refresh_uses_request_headers_and_resets_context():
    from pseudolife_memory.writer_context import resolve_writer_detailed
    service = FixtureService()
    service.coordination_awareness = lambda **kw: {
        "caller": resolve_writer_detailed("fixture")[1]}
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, service)
    status, body = call(app, "GET", "/api/agents", headers=[
        (b"authorization", b"Bearer fixture-secret"),
        (b"x-pl-session", b"session-one")])
    assert status == 200 and b"session-one" in body
    status, body = call(app, "GET", "/api/agents", headers=[
        (b"authorization", b"Bearer fixture-secret")])
    assert status == 200 and b'"caller": null' in body
    assert resolve_writer_detailed("fixture")[1] is None
    status, _ = call(app, "GET", "/api/agents")
    assert status == 401


def test_memory_hook_skips_coordination_awareness():
    from pseudolife_memory.web.session_hook import hook_session_start
    service = FixtureService()
    service.config = SimpleNamespace(coordination=SimpleNamespace(enabled=True))
    service.episode_start_session = lambda *a, **_: {"id": "fixture-episode"}
    service.set_active_session = lambda *a: None
    service.session_briefing = lambda **kw: {"markdown": repr(kw)}
    text = hook_session_start(service, "own-session")
    assert "'include_coordination': False" in text
    assert "'session_id'" not in text


def test_hook_route_binds_request_headers_for_authorized_briefing():
    """The session-start hook route binds its request headers like
    /api/agents does, so authorized briefing calls see the request's
    identity without a previous caller's headers leaking across requests."""
    from pseudolife_memory.writer_context import _http_request_headers
    service = FixtureService()
    service.config = SimpleNamespace(coordination=SimpleNamespace(enabled=True))
    service.episode_start_session = lambda *a, **_: {"id": "fixture-episode"}
    service.set_active_session = lambda *a: None
    service.session_briefing = lambda **kw: {
        "markdown": "auth=" + str((_http_request_headers() or {}).get("authorization"))}
    app = build_console_app(stub_mcp, None, lambda: {"secret-a": "alpha", "secret-b": "beta"}, service)
    status, body = call(app, "GET", "/api/hook/session-start", query="session_id=own-session",
                        headers=[(b"authorization", b"Bearer secret-a")])
    assert status == 200 and b"auth=Bearer secret-a" in body
    # Bound per request: a second caller sees its own bearer, never the first.
    status, body = call(app, "GET", "/api/hook/session-start", query="session_id=other-session",
                        headers=[(b"authorization", b"Bearer secret-b")])
    assert status == 200 and b"auth=Bearer secret-b" in body


def test_mailbox_rest_receives_validated_principal(monkeypatch):
    import json
    from pseudolife_memory.web.coordination import CoordinationHub
    seen = []
    async def handle(self, action, body, headers, principal):
        seen.append((action, principal, headers.get("x-pl-agent")))
        return {"messages": []}
    monkeypatch.setattr(CoordinationHub, "handle", handle)
    svc = FixtureService()
    app = build_console_app(stub_mcp, None, lambda: {}, svc,
                            token_map={"fixture-secret": "agent-user"})
    status, body = call(app, "POST", "/api/coordination/receive", body=b"{}", headers=[
        (b"authorization", b"Bearer fixture-secret"),
        (b"content-type", b"application/json"), (b"x-pl-agent", b"a1")])
    assert status == 200 and json.loads(body) == {"messages": []}
    assert seen == [("receive", "agent-user", "a1")]


def test_mailbox_rest_requires_bearer_even_on_loopback():
    app = build_console_app(stub_mcp, None, lambda: {}, FixtureService())
    status, _ = call(app, "POST", "/api/coordination/register", body=b"{}",
                     headers=[(b"content-type", b"application/json")])
    assert status == 401


def test_disconnected_http_wait_cleans_hub_without_sending_response(monkeypatch):
    import threading
    from pseudolife_memory.web import coordination
    async def drive():
        svc = FixtureService()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def dispatch(*a, **kw):
            loop.call_soon_threadsafe(started.set)
            release.wait()
            return {"messages": [], "after": "a1:0"}
        monkeypatch.setattr(coordination, "dispatch", dispatch)
        app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, svc)
        hub = svc._coordination_notifier.__self__
        scope = {"type": "http", "method": "POST", "path": "/api/coordination/receive",
                 "query_string": b"", "headers": [
                     (b"authorization", b"Bearer fixture-secret"),
                     (b"content-type", b"application/json"), (b"x-pl-agent", b"a1")]}
        incoming = 0
        async def receive():
            nonlocal incoming
            incoming += 1
            if incoming == 1:
                return {"type": "http.request", "more_body": False,
                        "body": json.dumps({"wait_seconds": 30, "attachment_id": "one", "generation": 1}).encode()}
            await started.wait()
            return {"type": "http.disconnect"}
        responses = []
        async def send(message):
            responses.append(message)
        try:
            await asyncio.wait_for(app(scope, receive, send), 1)
            assert incoming == 2
            assert hub.waiters == {} and hub.attachments == set()
            # HTTP cancellation removes the subscription immediately; the held
            # synchronous worker still owns its reserved capacity.
            assert hub.pending_calls == 1
            assert responses == []
        finally:
            jobs = tuple(hub.jobs)
            release.set()
            await asyncio.wait_for(asyncio.gather(*jobs), 1)
        assert hub.pending_calls == 0
        assert not hub.jobs
    asyncio.run(drive())


@pytest.mark.parametrize("exception,status,code", [
    (ValueError("private-fixture-body"), 400, "invalid_request"),
    (TypeError("private-fixture-body"), 400, "invalid_request"),
    (RuntimeError("private-fixture-body"), 500, "coordination_unavailable"),
    (ValueError("stale_attachment"), 400, "stale_attachment"),
    (ValueError("instance_not_found"), 404, "instance_not_found"),
    (ValueError("invalid_credential"), 403, "invalid_credential"),
])
def test_mailbox_errors_do_not_expose_data_in_response_or_logs(monkeypatch, caplog, exception, status, code):
    from pseudolife_memory.web.coordination import CoordinationHub
    async def handle(*args):
        raise exception
    monkeypatch.setattr(CoordinationHub, "handle", handle)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, FixtureService())
    actual, body = call(app, "POST", "/api/coordination/receive", body=b"{}", headers=[
        (b"authorization", b"Bearer fixture-secret"), (b"content-type", b"application/json")])
    assert actual == status and json.loads(body) == {"error": code}
    assert "private-fixture-body" not in body.decode() + caplog.text


def test_mailbox_rejects_oversized_body_before_dispatch(monkeypatch):
    from pseudolife_memory.web.coordination import CoordinationHub
    async def handle(*args):
        pytest.fail("oversized body reached mailbox dispatch")
    monkeypatch.setattr(CoordinationHub, "handle", handle)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, FixtureService())
    status, body = call(app, "POST", "/api/coordination/send",
        body=json.dumps({"text": "x" * 32768}).encode(), headers=[
            (b"authorization", b"Bearer fixture-secret"), (b"content-type", b"application/json")])
    assert status == 413 and json.loads(body) == {"error": "request_too_large"}


def test_mailbox_malformed_utf8_is_a_bad_request():
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, FixtureService())
    status, body = call(app, "POST", "/api/coordination/receive", body=b"\xff", headers=[
        (b"authorization", b"Bearer fixture-secret"), (b"content-type", b"application/json")])
    assert status == 400 and json.loads(body) == {"error": "invalid_json"}


def _board_service(enabled=True, allowed=("default",), db_url="postgresql://fixture"):
    service = FixtureService()
    service.config = SimpleNamespace(coordination=SimpleNamespace(
        enabled=enabled, allowed_principals=list(allowed)))
    service._db_url = db_url
    return service


@pytest.mark.parametrize("token,token_map,bearer,settings,served", [
    ("fixture-secret", None, b"Bearer fixture-secret", {}, True),
    ("fixture-secret", None, b"Bearer fixture-secret", {"enabled": False}, False),
    (None, None, None, {}, False),
    ("fixture-secret", None, None, {}, False),
    ("fixture-secret", None, b"Bearer wrong", {}, False),
    (None, {"map-secret": "editor"}, b"Bearer map-secret", {}, False),
    (None, {"map-secret": "editor"}, b"Bearer map-secret", {"allowed": ["editor"]}, True),
    ("fixture-secret", None, b"Bearer fixture-secret", {"db_url": None}, False),
], ids=["available", "disabled", "open-install", "no-bearer", "wrong-bearer",
        "unlisted-principal", "listed-principal", "file-mode"])
def test_startup_checkin_is_served_only_where_the_board_works(token, token_map, bearer,
                                                              settings, served):
    """A check-in the caller cannot complete is a guaranteed tool failure on
    every session start, so the hook text is served only when this bearer
    could register, update and receive right now."""
    from pseudolife_memory.coordination import CHECKIN_TEXT
    app = build_console_app(stub_mcp, token, lambda: {}, _board_service(**settings),
                            token_map=token_map)
    headers = [(b"authorization", bearer)] if bearer else []
    status, body = call(app, "GET", "/api/hook/coordination-start", headers=headers)
    assert status == 200
    assert body == ((CHECKIN_TEXT + "\n").encode() if served else b"")


@pytest.mark.parametrize("token,token_map,bearer,settings,state", [
    ("fixture-secret", None, b"Bearer fixture-secret", {}, "on"),
    ("fixture-secret", None, b"Bearer fixture-secret", {"enabled": False}, "off; reason=disabled"),
    (None, None, None, {}, "off; reason=authentication_required"),
    ("fixture-secret", None, b"Bearer wrong", {}, "off; reason=unauthorized"),
    (None, {"map-secret": "editor"}, b"Bearer map-secret", {},
     "off; reason=principal_not_allowed"),
    ("fixture-secret", None, b"Bearer fixture-secret", {"db_url": None},
     "off; reason=coordination_requires_postgres"),
], ids=["available", "disabled", "open-install", "wrong-bearer", "unlisted-principal",
        "file-mode"])
def test_startup_checkin_names_why_the_board_is_off(token, token_map, bearer, settings, state):
    """The installer's and doctor's one-line board status read the reason
    from the daemon instead of guessing it client-side."""
    from tests.asgi_helpers import call_with_headers
    app = build_console_app(stub_mcp, token, lambda: {}, _board_service(**settings),
                            token_map=token_map)
    headers = [(b"authorization", bearer)] if bearer else []
    status, response, _ = call_with_headers(app, "GET", "/api/hook/coordination-start",
                                            headers=headers)
    assert status == 200
    assert response[b"x-pl-board"] == state.encode()


def test_startup_checkin_route_is_get_only_and_browser_gated():
    app = build_console_app(stub_mcp, None, lambda: {}, _board_service())
    status, _ = call(app, "POST", "/api/hook/coordination-start")
    assert status == 405
    status, _ = call(app, "GET", "/api/hook/coordination-start",
                     headers=[(b"origin", b"http://evil.example")])
    assert status == 403


class _CheckinResponse:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _checkin_cli(monkeypatch, body, argv, setting=None):
    import sys
    from pseudolife_memory.shim import _NoRedirectHandler
    seen = []

    class Opener:
        def open(self, req, timeout):
            # Inside a 5 s hook budget, after interpreter start and /health.
            assert timeout == 2
            seen.append((req.full_url, req.get_header("Authorization")))
            return _CheckinResponse(body)

    def build_opener(*handlers):
        # A redirect must never carry the bearer to another host.
        assert handlers == (_NoRedirectHandler,)
        return Opener()

    monkeypatch.setattr("pseudolife_memory.shim.probe_health",
                        lambda url, timeout=0.25: {"status": "ok"})
    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-token")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://fixture.invalid")
    if setting is None:
        monkeypatch.delenv("PSEUDOLIFE_AGENT_COORDINATION", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", setting)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing", *argv])
    from pseudolife_memory import briefing_cli as bc
    bc.run_briefing()
    return seen


@pytest.mark.parametrize("hook_json", [False, True])
def test_briefing_coordination_prints_the_daemon_served_checkin(monkeypatch, capsys, hook_json):
    """The lightweight installers' check-in hook: whatever the daemon serves
    for this bearer, which is nothing where the board is off."""
    import json
    argv = ["--coordination", *(["--hook-json"] if hook_json else [])]
    seen = _checkin_cli(monkeypatch, b"Pseudolife coordination: fixture.\n", argv)
    assert seen == [("http://fixture.invalid/api/hook/coordination-start", "Bearer fixture-token")]
    out = capsys.readouterr().out
    if hook_json:
        context = json.loads(out)["hookSpecificOutput"]
        assert context == {"hookEventName": "SessionStart",
                           "additionalContext": "Pseudolife coordination: fixture."}
    else:
        assert out == "Pseudolife coordination: fixture.\n"


def test_briefing_coordination_prints_nothing_where_the_board_is_off(monkeypatch, capsys):
    _checkin_cli(monkeypatch, b"", ["--coordination", "--hook-json"])
    assert capsys.readouterr().out == ""


def test_briefing_coordination_honours_an_explicit_client_opt_out(monkeypatch, capsys):
    seen = _checkin_cli(monkeypatch, b"Pseudolife coordination: fixture.\n",
                        ["--coordination", "--hook-json"], setting="0")
    assert seen == [] and capsys.readouterr().out == ""


def test_open_install_mcp_binding_never_reaches_the_board(monkeypatch):
    """Bearer auth stays required (maintainer decision, 2026-09-25): on an
    open install the identity binding a shim adapter sends fails closed
    before the store, though "default" is on the default allowed list."""
    from pseudolife_memory import coordination
    calls = []
    monkeypatch.setattr(coordination, "dispatch", lambda *a, **kw: calls.append(kw) or {})
    app = build_console_app(stub_mcp, None, lambda: {}, FixtureService())
    status, body = call(app, "POST", "/mcp", headers=[
        (b"x-pl-bank", b"fixture-bank"), (b"x-pl-principal", b"default")])
    assert status == 401 and json.loads(body) == {"error": "authentication_required"}
    assert calls == []


def test_authenticated_mcp_binding_still_checks_the_bank(monkeypatch):
    from pseudolife_memory import coordination
    calls = []

    def dispatch(service, action, parameters, **kw):
        calls.append((action, kw["principal"]))
        return {"bank_id": "fixture-bank", "principal": "default"}

    monkeypatch.setattr(coordination, "dispatch", dispatch)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, FixtureService())
    status, _ = call(app, "POST", "/mcp", headers=[
        (b"authorization", b"Bearer fixture-secret"),
        (b"x-pl-bank", b"fixture-bank"), (b"x-pl-principal", b"default")])
    assert calls == [("context", "default")]
    assert status == 501  # through to the stub MCP app



# --- the Stop-hook park gate route (v49) -----------------------------------

def test_park_gate_route_serves_the_daemons_answer(monkeypatch):
    """``GET /api/hook/park-gate?agent=<id>&since=<epoch>`` answers the
    Stop hook in two lines: the gate, then the message to show. Only a
    bearer that can use the board gets an answer; anyone else gets an
    empty body, which the hook reads as allow."""
    from pseudolife_memory import coordination
    seen = []

    def park_gate(service, headers, *, agent, since, token_map=None, token=None):
        seen.append((agent, since, headers.get("authorization")))
        return "block\nBefore ending: update your board status\n"
    monkeypatch.setattr(coordination, "park_gate", park_gate)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, _board_service())
    bearer = [(b"authorization", b"Bearer fixture-secret")]
    status, body = call(app, "GET", "/api/hook/park-gate", headers=bearer,
                        query="agent=" + "a" * 32 + "&since=1700000000")
    assert (status, body) == (200, b"block\nBefore ending: update your board status\n")
    assert seen == [("a" * 32, "1700000000", "Bearer fixture-secret")]
    status, body = call(app, "GET", "/api/hook/park-gate", query="agent=" + "a" * 32)
    assert (status, body) == (200, b"")
    assert len(seen) == 1
    status, _ = call(app, "POST", "/api/hook/park-gate", headers=bearer)
    assert status == 405
    # GET only and browser-gated, like the check-in route.
    open_app = build_console_app(stub_mcp, None, lambda: {}, _board_service())
    status, _ = call(open_app, "GET", "/api/hook/park-gate",
                     headers=[(b"origin", b"http://evil.example")])
    assert status == 403


def test_park_gate_is_open_where_the_board_is_not(monkeypatch):
    """The same gates as the check-in: no board, no question."""
    from pseudolife_memory.coordination import park_gate
    headers = {"authorization": "Bearer fixture-secret"}
    assert park_gate(_board_service(enabled=False), headers, agent="a" * 32, since=None,
                     token="fixture-secret") == "allow\n"
    assert park_gate(_board_service(), {}, agent="a" * 32, since=None,
                     token="fixture-secret") == "allow\n"
    # A malformed address is not looked up.
    assert park_gate(_board_service(), headers, agent="not-an-id", since=None,
                     token="fixture-secret") == "allow\n"


# --- the Stop-hook woke marker -----------------------------------------------

def test_woke_route_records_the_marker_for_a_board_bearer_only(monkeypatch):
    """``POST /api/hook/woke?agent=<id>``: the Stop hook's one call after it
    fires. ``ok`` for a recorded marker; an empty body for a bearer that
    cannot use the board, which the hook ignores either way."""
    from pseudolife_memory import coordination
    seen = []

    def woke(service, headers, *, agent, token_map=None, token=None):
        seen.append((agent, headers.get("authorization")))
        return "ok\n"
    monkeypatch.setattr(coordination, "woke", woke)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, _board_service())
    bearer = [(b"authorization", b"Bearer fixture-secret")]
    status, body = call(app, "POST", "/api/hook/woke", headers=bearer, query="agent=" + "a" * 32)
    assert (status, body) == (200, b"ok\n")
    assert seen == [("a" * 32, "Bearer fixture-secret")]
    status, body = call(app, "POST", "/api/hook/woke", query="agent=" + "a" * 32)
    assert (status, body) == (200, b"")
    assert len(seen) == 1
    # A marker is a write: POST only, and browser-gated like the gate.
    status, _ = call(app, "GET", "/api/hook/woke", headers=bearer, query="agent=" + "a" * 32)
    assert status == 405
    open_app = build_console_app(stub_mcp, None, lambda: {}, _board_service())
    status, _ = call(open_app, "POST", "/api/hook/woke",
                     headers=[(b"origin", b"http://evil.example")])
    assert status == 403


# --- the SubagentStart / SubagentStop liveness route (v50) -------------------

def test_subagent_route_passes_the_hook_payload_for_a_board_bearer_only(monkeypatch):
    """``POST /api/hook/subagent?agent=<id>&event=start|stop&child=<agent_id>
    &type=<agent_type>``: the plugin's subagent hooks' one call. ``ok`` when
    recorded; an empty body for a bearer that cannot use the board."""
    from pseudolife_memory import coordination
    seen = []

    def subagent(service, headers, *, agent, event, child, kind, token_map=None, token=None):
        seen.append((agent, event, child, kind, headers.get("authorization")))
        return "ok\n"
    monkeypatch.setattr(coordination, "subagent", subagent)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, _board_service())
    bearer = [(b"authorization", b"Bearer fixture-secret")]
    query = "agent=" + "a" * 32 + "&event=start&child=a698026ca4ba524e9&type=general-purpose"
    status, body = call(app, "POST", "/api/hook/subagent", headers=bearer, query=query)
    assert (status, body) == (200, b"ok\n")
    assert seen == [("a" * 32, "start", "a698026ca4ba524e9", "general-purpose",
                     "Bearer fixture-secret")]
    status, body = call(app, "POST", "/api/hook/subagent", query=query)
    assert (status, body) == (200, b"") and len(seen) == 1
    # A write: POST only, and browser-gated like the other hook routes.
    status, _ = call(app, "GET", "/api/hook/subagent", headers=bearer, query=query)
    assert status == 405
    open_app = build_console_app(stub_mcp, None, lambda: {}, _board_service())
    status, _ = call(open_app, "POST", "/api/hook/subagent",
                     headers=[(b"origin", b"http://evil.example")])
    assert status == 403


def test_subagent_records_nothing_where_the_board_is_not(monkeypatch):
    from pseudolife_memory import coordination
    from pseudolife_memory.coordination import subagent
    monkeypatch.setattr(coordination, "_store",
                        lambda service: pytest.fail("no store for a refused call"))
    headers = {"authorization": "Bearer fixture-secret"}
    args = {"child": "c1", "kind": "t", "token": "fixture-secret"}
    assert subagent(_board_service(enabled=False), headers, agent="a" * 32, event="start",
                    **args) == ""
    assert subagent(_board_service(), {}, agent="a" * 32, event="start", **args) == ""
    assert subagent(_board_service(), headers, agent="not-an-id", event="start", **args) == ""
    assert subagent(_board_service(), headers, agent="a" * 32, event="restart", **args) == ""


def test_subagent_reaches_the_store_and_fails_open(monkeypatch):
    """Start and stop reach the store as the bearer's principal; any error
    (a malformed child, a down database) is an empty answer, never raised."""
    import threading
    from pseudolife_memory import coordination
    from pseudolife_memory.coordination import subagent
    from pseudolife_memory.storage.coordination import CoordinationError
    calls = []

    class Store:
        def subagent_started(self, agent, principal, *, child, kind):
            calls.append(("start", agent, principal, child, kind))
            if child == "bad":
                raise CoordinationError("invalid_children")
            return {"recorded": True}

        def subagent_stopped(self, agent, principal, *, child):
            calls.append(("stop", agent, principal, child))
            return {"recorded": False, "reason": "not_listed"}

    service = _board_service()
    service._storage = object()
    service._coordination_lock = threading.Lock()
    monkeypatch.setattr(coordination, "_store", lambda s: Store())
    headers = {"authorization": "Bearer fixture-secret"}
    agent = "a" * 32
    assert subagent(service, headers, agent=agent, event="start", child="c1", kind="Explore",
                    token_map={}, token="fixture-secret") == "ok\n"
    assert subagent(service, headers, agent=agent, event="start", child="c2", kind=None,
                    token_map={}, token="fixture-secret") == "ok\n"
    assert subagent(service, headers, agent=agent, event="stop", child="c1", kind=None,
                    token_map={}, token="fixture-secret") == ""
    assert subagent(service, headers, agent=agent, event="start", child="bad", kind="t",
                    token_map={}, token="fixture-secret") == ""
    assert calls == [("start", agent, "default", "c1", "Explore"),
                     ("start", agent, "default", "c2", ""),
                     ("stop", agent, "default", "c1"),
                     ("start", agent, "default", "bad", "t")]


def test_woke_records_nothing_where_the_board_is_not(monkeypatch):
    from pseudolife_memory.coordination import woke
    headers = {"authorization": "Bearer fixture-secret"}
    assert woke(_board_service(enabled=False), headers, agent="a" * 32,
                token="fixture-secret") == ""
    assert woke(_board_service(), {}, agent="a" * 32, token="fixture-secret") == ""
    assert woke(_board_service(), headers, agent="not-an-id", token="fixture-secret") == ""


# ── the delegate's brief and per-turn reminder (spec 2026-10-14) ───────────

class _DelegateStore:
    def __init__(self, calls, answers):
        self.calls, self.answers = calls, answers

    def delegate_of(self, agent, principal):
        self.calls.append((agent, principal))
        answer = self.answers.get(agent, [])
        if isinstance(answer, dict):
            return answer
        return {"projects": list(answer), "remaining": 3600.0 if answer else None}


def _delegate_service(monkeypatch, answers):
    import threading
    from pseudolife_memory import coordination
    calls = []
    service = _board_service()
    service._storage = object()
    service._coordination_lock = threading.Lock()
    monkeypatch.setattr(coordination, "_store", lambda s: _DelegateStore(calls, answers))
    return service, calls


def test_the_delegate_answer_is_cached_per_agent_for_a_minute(monkeypatch):
    """The per-prompt path stays one cheap lookup: a second ask within 60 s
    is served from the process cache, a later one asks the store again, so a
    grant or a revoke shows within a minute."""
    from pseudolife_memory.coordination import DELEGATE_CACHE_SECONDS, delegate_projects
    assert DELEGATE_CACHE_SECONDS == 60
    answers = {"a" * 32: ["p"]}
    service, calls = _delegate_service(monkeypatch, answers)
    now = [1000.0]
    headers = {"authorization": "Bearer fixture-secret"}

    def ask(agent="a" * 32):
        return delegate_projects(service, headers, agent=agent, token_map={},
                                 token="fixture-secret", clock=lambda: now[0])

    assert ask() == ["p"] and calls == [("a" * 32, "default")]
    answers["a" * 32] = []                       # revoked meanwhile
    now[0] += 59
    assert ask() == ["p"] and len(calls) == 1    # cached
    assert ask("b" * 32) == [] and len(calls) == 2   # another agent asks for itself
    now[0] += 2
    assert ask() == [] and len(calls) == 3       # expired: asked again


def test_the_delegate_lookup_fails_closed_and_never_raises(monkeypatch):
    from pseudolife_memory import coordination
    service, calls = _delegate_service(monkeypatch, {})

    def boom(s):
        raise RuntimeError("database down")

    monkeypatch.setattr(coordination, "_store", boom)
    headers = {"authorization": "Bearer fixture-secret"}
    args = {"agent": "a" * 32, "token_map": {}, "token": "fixture-secret"}
    assert coordination.delegate_projects(service, headers, **args) == []
    assert getattr(service, "_delegate_cache", {}) == {}   # a failure is not cached
    assert coordination.delegate_projects(_board_service(enabled=False), headers, **args) == []
    assert coordination.delegate_projects(service, {}, **args) == []


def test_a_cold_daemon_skips_the_delegate_lookup(monkeypatch):
    """Opening the board's connection waits on the service lock; the hook
    requests carrying the check-in or the memory note must not wait on it
    for a brief."""
    from pseudolife_memory.coordination import delegate_projects
    service, calls = _delegate_service(monkeypatch, {"a" * 32: ["p"]})
    service._storage = None
    taken = []

    class ServiceLock:
        def __enter__(self):
            taken.append(True)
            return self

        def __exit__(self, *exc):
            return False

    service._lock = ServiceLock()
    assert delegate_projects(service, {"authorization": "Bearer fixture-secret"},
                             agent="a" * 32, token_map={}, token="fixture-secret") == []
    assert calls == [] and taken == []


def test_the_brief_and_the_reminder_texts():
    """Pinned so a reword is a deliberate change; the 2026-10-14 transcript
    measurement is what they answer (spec docs/specs/2026-10-14-delegate-
    mode-design.md, section 2)."""
    from pseudolife_memory.coordination import DELEGATE_REMINDER, render_delegate_brief
    assert DELEGATE_REMINDER == ("Delegate: raw output goes to a subagent; a quiet wake gets "
                                 "one line.")
    brief = render_delegate_brief(["p"])
    assert brief == (
        "You are the maintainer's delegate for p. Keep this session's context for decisions:\n"
        "- Start a watcher once: `pseudolife-mcp delegate-watch --detach`; it prints its board "
        "id. Park with park_needs=\"decision events\" and park_clear_by=<that id> before ending "
        "a turn that waits. On a wake, read with memory_message(action=\"receive\", ack=true), "
        "act, and park again. If it has stopped, run it again; it answers \"already running\" "
        "when it is live.\n"
        "- Hand raw output (CI logs, test runs, long diffs) to a subagent that returns at most "
        "ten lines. Put what you already know in its brief (paths, symbols, what is settled) "
        "so it reads little. Run it in the foreground while you are working; a background one "
        "wakes you when it finishes.\n"
        "- Ask workers for decision-ready mail and to keep progress in their board status.\n"
        "- Answer a quiet wake in one line, with no tool calls.")
    assert render_delegate_brief(["p", "q"]).startswith(
        "You are the maintainer's delegate for p and q. ")
    assert render_delegate_brief(["p", "q", "r"]).startswith(
        "You are the maintainer's delegate for p, q and r. ")


def test_coordination_start_appends_the_brief_after_the_unchanged_checkin(monkeypatch):
    from pseudolife_memory import coordination
    from pseudolife_memory.coordination import CHECKIN_TEXT, render_delegate_brief
    seen = []

    async def projects(service, headers, *, agent, token_map=None, token=None):
        seen.append((agent, headers.get("authorization")))
        return ["p"] if agent == "a" * 32 else []

    monkeypatch.setattr(coordination, "delegate_projects_soon", projects)
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, _board_service())
    bearer = [(b"authorization", b"Bearer fixture-secret")]
    status, body = call(app, "GET", "/api/hook/coordination-start", headers=bearer,
                        query="agent=" + "a" * 32)
    assert status == 200
    assert body.decode() == CHECKIN_TEXT + "\n\n" + render_delegate_brief(["p"]) + "\n"
    for query in ("", "agent=" + "b" * 32):
        status, body = call(app, "GET", "/api/hook/coordination-start", headers=bearer,
                            query=query)
        assert body.decode() == CHECKIN_TEXT + "\n"
    assert seen == [("a" * 32, "Bearer fixture-secret"), ("b" * 32, "Bearer fixture-secret")]
    # Where the board is off there is no check-in and no brief.
    off = build_console_app(stub_mcp, "fixture-secret", lambda: {},
                            _board_service(enabled=False))
    status, body = call(off, "GET", "/api/hook/coordination-start", headers=bearer,
                        query="agent=" + "a" * 32)
    assert body == b""


def test_memory_changes_appends_the_reminder_for_the_delegate(monkeypatch):
    from pseudolife_memory import coordination
    from pseudolife_memory.coordination import DELEGATE_REMINDER
    from pseudolife_memory.web import api
    async def projects(service, headers, *, agent, token_map=None, token=None):
        return ["p"] if agent == "a" * 32 else []

    monkeypatch.setattr(coordination, "delegate_projects_soon", projects)
    answers = iter(["100.000000\n", "100.000000\nMemory changed since your last turn:\n- x\n",
                    "", "100.000000\n"])
    monkeypatch.setattr(api, "hook_memory_changes", lambda *a: next(answers))
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, _board_service())
    bearer = [(b"authorization", b"Bearer fixture-secret")]

    def ask(query):
        status, body = call(app, "GET", "/api/hook/memory-changes", headers=bearer, query=query)
        assert status == 200
        return body.decode()

    delegate = "session_id=s1&agent=" + "a" * 32
    assert ask(delegate) == "100.000000\n" + DELEGATE_REMINDER + "\n"
    assert ask(delegate) == ("100.000000\nMemory changed since your last turn:\n- x\n"
                             + DELEGATE_REMINDER + "\n")
    # No cursor (a failed note) gets no reminder: the hook would read it as one.
    assert ask(delegate) == ""
    assert ask("session_id=s1&agent=" + "b" * 32) == "100.000000\n"


def test_a_cached_yes_ends_with_the_delegate_lease(monkeypatch):
    """The 60 s lag applies to a revoke or a new grant, but a cached yes is
    never served past the delegate lease's own expiry."""
    from pseudolife_memory.coordination import delegate_projects
    answers = {"a" * 32: {"projects": ["p"], "remaining": 10.0}}
    service, calls = _delegate_service(monkeypatch, answers)
    now = [1000.0]
    headers = {"authorization": "Bearer fixture-secret"}

    def ask():
        return delegate_projects(service, headers, agent="a" * 32, token_map={},
                                 token="fixture-secret", clock=lambda: now[0])

    assert ask() == ["p"] and len(calls) == 1
    answers["a" * 32] = []                       # the lease has lapsed
    now[0] += 9.5
    assert ask() == ["p"] and len(calls) == 1    # still inside the lease
    now[0] += 1
    assert ask() == [] and len(calls) == 2       # 10.5 s: asked again, not at 60 s


class _SlowStore:
    def __init__(self, calls, release):
        self.calls, self.release = calls, release

    def delegate_of(self, agent, principal):
        self.calls.append(agent)
        self.release.wait(10)
        return {"projects": ["p"], "remaining": 3600.0}


def _slow_service(monkeypatch, budget=0.2):
    import threading
    from pseudolife_memory import coordination
    calls, release = [], threading.Event()
    service = _board_service()
    service._storage = object()
    service._coordination_lock = threading.Lock()
    monkeypatch.setattr(coordination, "_store", lambda s: _SlowStore(calls, release))
    monkeypatch.setattr(coordination, "DELEGATE_LOOKUP_BUDGET", budget)
    return service, calls, release


def _wait_for_cache(service, agent, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(key[1] == agent for key in getattr(service, "_delegate_cache", {})):
            return
        time.sleep(0.02)
    raise AssertionError("the background lookup never filled the cache")


def test_a_slow_delegate_lookup_never_holds_the_hook_answer(monkeypatch):
    """A stalled coordination lock or query must not cost a session its
    memory note or check-in: past the budget the route sends the text it
    already has, the lookup finishes in the background and fills the cache,
    and the next call carries the reminder from it."""
    import time
    from pseudolife_memory.coordination import (CHECKIN_TEXT, DELEGATE_LOOKUP_BUDGET,
                                                DELEGATE_REMINDER)
    from pseudolife_memory.web import api
    assert DELEGATE_LOOKUP_BUDGET == 0.5
    service, calls, release = _slow_service(monkeypatch)
    monkeypatch.setattr(api, "hook_memory_changes", lambda *a: "100.000000\n")
    app = build_console_app(stub_mcp, "fixture-secret", lambda: {}, service)
    bearer = [(b"authorization", b"Bearer fixture-secret")]
    agent = "a" * 32
    try:
        for path, query, base in (
                ("/api/hook/memory-changes", f"session_id=s1&agent={agent}", "100.000000\n"),
                ("/api/hook/coordination-start", f"agent={agent}", CHECKIN_TEXT + "\n")):
            started = time.monotonic()
            status, body = call(app, "GET", path, headers=bearer, query=query)
            assert status == 200 and body.decode() == base
            assert time.monotonic() - started < 1.5
        assert calls == [agent]                  # the second miss joined the first
    finally:
        release.set()
    _wait_for_cache(service, agent)
    status, body = call(app, "GET", "/api/hook/memory-changes", headers=bearer,
                        query=f"session_id=s1&agent={agent}")
    assert body.decode() == "100.000000\n" + DELEGATE_REMINDER + "\n"
    assert calls == [agent]


def test_concurrent_misses_for_one_agent_start_one_lookup(monkeypatch):
    import asyncio
    from pseudolife_memory.coordination import delegate_projects_soon
    service, calls, release = _slow_service(monkeypatch, budget=0.1)
    headers = {"authorization": "Bearer fixture-secret"}

    async def both():
        ask = [delegate_projects_soon(service, headers, agent=agent, token_map={},
                                      token="fixture-secret")
               for agent in ("a" * 32, "a" * 32, "b" * 32)]
        return await asyncio.gather(*ask)

    try:
        assert asyncio.run(both()) == [[], [], []]
        # One lookup per key in flight (b's waits for the coordination lock).
        assert len(service._delegate_inflight) == 2
    finally:
        release.set()
    _wait_for_cache(service, "a" * 32)
    _wait_for_cache(service, "b" * 32)
    assert asyncio.run(both()) == [["p"], ["p"], ["p"]]
    assert sorted(calls) == ["a" * 32, "b" * 32]


def test_a_cached_yes_counts_the_lease_from_before_a_slow_query(monkeypatch):
    """The lease's remaining time is read inside the query, so the cache
    counts it from the query's start: a query that stalls past the lease's
    end answers no, to its caller and to every hook waiting on it, and
    leaves no yes behind (Codex reviews of the 2026-10-16 batch)."""
    from pseudolife_memory import coordination
    from pseudolife_memory.coordination import delegate_projects
    now = [1000.0]
    calls = []

    class StallingStore:
        def delegate_of(self, agent, principal):
            calls.append(agent)
            now[0] += 5.0                        # the query stalls 5 s after the read
            return {"projects": ["p"], "remaining": 2.0}

    service, _ = _delegate_service(monkeypatch, {})
    monkeypatch.setattr(coordination, "_store", lambda s: StallingStore())
    headers = {"authorization": "Bearer fixture-secret"}

    def ask():
        return delegate_projects(service, headers, agent="a" * 32, token_map={},
                                 token="fixture-secret", clock=lambda: now[0])

    assert ask() == [] and len(calls) == 1       # the lease ended during the query
    assert ask() == [] and len(calls) == 2       # nothing cached: asked again


def test_delegate_lookups_in_flight_are_bounded(monkeypatch):
    """Each distinct address starts its own lookup thread. While the
    coordination lock stalls, past the bound a miss answers [] at once and
    starts nothing, and a thread that cannot start costs only the brief,
    never the hook's own text (Codex review of the 2026-10-16 batch)."""
    import asyncio
    import threading
    import time
    from pseudolife_memory import coordination
    from pseudolife_memory.coordination import delegate_projects_soon
    service, calls, release = _slow_service(monkeypatch, budget=0.05)
    monkeypatch.setattr(coordination, "_DELEGATE_INFLIGHT_MAX", 2, raising=False)
    headers = {"authorization": "Bearer fixture-secret"}

    def ask(agent):
        return asyncio.run(delegate_projects_soon(service, headers, agent=agent, token_map={},
                                                  token="fixture-secret"))

    try:
        assert [ask(c * 32) for c in "abc"] == [[], [], []]
        assert sorted(service._delegate_inflight) == [("default", "a" * 32),
                                                      ("default", "b" * 32)]
    finally:
        release.set()
    _wait_for_cache(service, "a" * 32)
    _wait_for_cache(service, "b" * 32)
    assert "c" * 32 not in calls
    # A lookup leaves the in-flight map just after it fills the cache.
    deadline = time.monotonic() + 5.0
    while service._delegate_inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service._delegate_inflight == {}

    start = threading.Thread.start
    refused = []

    def refuse(thread):
        if thread.name == "pl-delegate-lookup":
            refused.append(thread.name)
            raise RuntimeError("can't start new thread")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert ask("d" * 32) == []
    assert refused and service._delegate_inflight == {}
