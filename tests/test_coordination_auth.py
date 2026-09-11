"""Mailbox authentication is separate from fail-open memory attribution."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


def service(enabled=True, allowed=None):
    return SimpleNamespace(config=SimpleNamespace(coordination=SimpleNamespace(
        enabled=enabled, allowed_principals=allowed or [])),
        _lock=nullcontext(), _storage=object(), _ensure_init=lambda: None,
        _hlc=SimpleNamespace(tick=lambda: (100, 1)))


def test_no_bearer_configuration_cannot_authenticate_mail(monkeypatch):
    from pseudolife_memory.coordination import dispatch
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    with pytest.raises(ValueError, match="authentication_required"):
        dispatch(service(allowed=["default"]), "register", {}, headers={})


@pytest.mark.parametrize("headers,allowed", [
    ({"authorization": "Bearer wrong"}, ["default"]),
    ({"authorization": "Bearer fixture-secret"}, []),
    ({"authorization": "Bearer fixture-secret", "x-pl-writer": "allowed"}, ["allowed"]),
])
def test_unmatched_bearer_or_principal_cannot_register(monkeypatch, headers, allowed):
    from pseudolife_memory.coordination import dispatch
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    with pytest.raises(ValueError, match="unauthorized|principal_not_allowed"):
        dispatch(service(allowed=allowed), "register", {}, headers=headers)


def test_model_send_never_accepts_sender_override(monkeypatch):
    from pseudolife_memory.coordination import dispatch
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    headers = {"authorization": "Bearer fixture-secret", "x-pl-agent": "own",
               "x-pl-agent-key": "private-fixture"}
    with pytest.raises(ValueError, match="unexpected_parameter"):
        dispatch(service(allowed=["default"]), "send", {
            "sender_id": "peer", "to": "target", "text": "hello", "request_id": "r1",
        }, headers=headers)


def test_disabled_feature_does_not_initialize_storage():
    from pseudolife_memory.coordination import dispatch
    svc = service(enabled=False)
    svc._ensure_init = lambda: pytest.fail("disabled coordination initialized storage")
    assert dispatch(svc, "receive", {}, headers={}) == {"enabled": False}


def test_identity_comes_from_instance_headers(monkeypatch):
    from pseudolife_memory import coordination
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    seen = []
    class Store:
        def __init__(self, storage):
            pass
        def prune(self):
            return {}
        def send(self, principal, agent_id, credential, **kwargs):
            seen.append((principal, agent_id, credential, kwargs))
            return {"message_id": "m1", "status": "queued"}
    monkeypatch.setattr(coordination, "_store", lambda svc: Store(None))
    headers = {"authorization": "Bearer fixture-secret", "x-pl-agent": "own",
               "x-pl-agent-key": "private-fixture"}
    out = coordination.dispatch(service(allowed=["default"]), "send", {
        "to": "peer", "text": "hello", "request_id": "r1"}, headers=headers)
    assert out == {"message_id": "m1", "status": "queued"}
    assert seen == [("default", "own", "private-fixture",
                     {"to": "peer", "text": "hello", "request_id": "r1", "hlc": "100:1"})]


def test_missing_send_arguments_are_a_validation_error(monkeypatch):
    from pseudolife_memory.coordination import dispatch
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    with pytest.raises(ValueError, match="missing_parameter"):
        dispatch(service(allowed=["default"]), "send", {}, headers={
            "authorization": "Bearer fixture-secret", "x-pl-agent": "a",
            "x-pl-agent-key": "private"})
