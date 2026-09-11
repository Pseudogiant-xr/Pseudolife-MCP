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


def test_hook_passes_its_own_session_for_awareness():
    from pseudolife_memory.web.session_hook import hook_session_start
    service = FixtureService()
    service.config = SimpleNamespace(coordination=SimpleNamespace(enabled=True))
    service.episode_start_session = lambda *a: {"id": "fixture-episode"}
    service.set_active_session = lambda *a: None
    service.session_briefing = lambda **kw: {"markdown": repr(kw)}
    text = hook_session_start(service, "own-session")
    assert "'session_id': 'own-session'" in text


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
