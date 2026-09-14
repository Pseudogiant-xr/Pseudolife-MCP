"""Credential rollover preserves a mailbox without crossing authority boundaries."""
import asyncio
import hashlib
import hmac
import json
import os
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
from tests.test_coordination_adapter import _secure_state

URL = "http://127.0.0.1:8099"
THREAD = "aaaaaaaa-1111-4111-8111-111111111111"


def test_existing_identity_with_unsafe_permissions_is_not_repaired_and_trusted(tmp_path):
    from pseudolife_memory.coordination_adapter import _open_state

    path = tmp_path / "identity.json"
    path.write_text("{}")
    if os.name != "nt":
        path.chmod(0o644)
    with pytest.raises(AdapterError, match="private"):
        fd = _open_state(path, os.O_RDONLY)
        os.close(fd)
    assert path.read_text() == "{}"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ACL validation")
def test_existing_windows_identity_checks_owner_before_mutating_acl(tmp_path, monkeypatch):
    from pseudolife_memory import credentials, coordination_adapter

    path = tmp_path / "identity.json"
    fd = coordination_adapter._open_state(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    monkeypatch.setattr(credentials, "_windows_owner_only", lambda fd: False)
    mutations = []
    monkeypatch.setattr(coordination_adapter, "_private_fd", lambda *args: mutations.append(True))
    with pytest.raises(AdapterError, match="private"):
        fd = coordination_adapter._open_state(path, os.O_RDONLY)
        os.close(fd)
    assert not mutations


class Provider:
    def __init__(self):
        self.token = "fixture-a"
        self.generation = 0

    def snapshot(self):
        return SimpleNamespace(token=self.token, generation=self.generation)

    def rotate(self, token):
        self.token = token
        self.generation += 1


class Bank:
    def __init__(self):
        self.bank_id = "11111111-1111-4111-8111-111111111111"
        self.principal = "fixture-user"
        self.token = "fixture-a"
        self.calls = []
        self.registered = 0
        self.agent = None
        self.credential = "fixture-instance-key"
        self.response_hook = None
        self.pages = []

    async def __call__(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((action, body, dict(request.headers)))
        if request.headers.get("authorization") != "Bearer " + self.token:
            return httpx.Response(401, json={"error": "unauthorized"})
        if action == "context":
            assert "x-pl-agent-key" not in request.headers
            result = {"bank_id": self.bank_id, "principal": self.principal}
            if body:
                if body.get("agent_id") != self.agent:
                    return httpx.Response(401, json={"error": "invalid_credential"})
                message = json.dumps(["pseudolife-context-v1", self.bank_id,
                    self.principal, self.agent, body["nonce"]], separators=(",", ":"),
                    ensure_ascii=True).encode("ascii")
                result["proof"] = hmac.new(hashlib.sha256(self.credential.encode()).digest(),
                                            message, hashlib.sha256).hexdigest()
        elif action == "register":
            self.registered += 1
            self.agent = f"{self.registered:032x}"
            result = {"agent_id": self.agent, "credential": self.credential}
        else:
            if request.headers.get("x-pl-agent") != self.agent:
                return httpx.Response(404, json={"error": "instance_not_found"})
            result = {"generation": 3, "pending_count": 1}
            if action == "receive":
                result = self.pages.pop(0) if self.pages else {"messages": [], "after": None}
        if self.response_hook:
            self.response_hook(action)
        return httpx.Response(200, json=result)


def instance(bank, provider, path, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(bank))
    return client, CoordinationAdapter(URL, provider.token, client=client,
        provider=provider, state_path=path, **kwargs)


def test_context_accepts_existing_maximum_length_principal(tmp_path):
    async def run():
        provider, bank = Provider(), Bank()
        bank.principal = "p" * 256
        client, adapter = instance(bank, provider, tmp_path / "state.json")
        async with client, adapter:
            assert adapter.instance_headers["X-PL-Principal"] == bank.principal
    asyncio.run(run())


@pytest.mark.parametrize("principal", ["r\u00e9viseur", "\u5ba1\u9605\u8005", "review%41"])
def test_principal_binding_uses_ascii_without_changing_identity(tmp_path, principal):
    async def run():
        provider, bank = Provider(), Bank()
        bank.principal = principal
        client, adapter = instance(bank, provider, tmp_path / "state.json")
        async with client, adapter:
            assert adapter.instance_headers["X-PL-Principal"] == quote(principal, safe="")
            assert json.loads((tmp_path / "state.json").read_text())["principal"] == principal
    asyncio.run(run())


def test_running_adapter_and_restart_keep_mailbox_after_rotation(tmp_path):
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        assert first._token is None
        async with client, first:
            original = first.instance_headers
            bank.token = "fixture-b"
            provider.rotate(bank.token)
            await first._heartbeat()
            assert first.instance_headers == original
        client, second = instance(bank, provider, path)
        async with client, second:
            assert second.instance_headers == original
        assert bank.registered == 1
        state = json.loads(path.read_text())
        assert state["version"] == 2 and state["bank_id"] == bank.bank_id
        assert state["principal"] == bank.principal
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("bank_id", "22222222-2222-4222-8222-222222222222"),
                                         ("principal", "other-user")])
def test_context_mismatch_never_transmits_key_or_replaces_state(tmp_path, field, value):
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            pass
        original = path.read_bytes()
        setattr(bank, field, value)
        bank.calls.clear()
        client, resumed = instance(bank, provider, path)
        async with client:
            with pytest.raises(AdapterError):
                await resumed.__aenter__()
        assert path.read_bytes() == original
        assert bank.registered == 1
        assert all("x-pl-agent-key" not in headers for _, _, headers in bank.calls)
    asyncio.run(run())


def test_legacy_migration_proves_server_ownership_without_sending_key(tmp_path):
    async def run():
        provider, bank = Provider(), Bank()
        bank.agent = "a" * 32
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({"bank_url": URL, "agent_id": bank.agent,
                                       "credential": bank.credential}))
        _secure_state(legacy)
        original = legacy.read_bytes()
        client, migrated = instance(bank, provider, tmp_path / "bound.json",
                                    legacy_state_path=legacy)
        async with client, migrated:
            assert migrated.instance_headers["X-PL-Agent"] == bank.agent
        proof_call = next(i for i, (action, body, _) in enumerate(bank.calls)
                          if action == "context" and body)
        first_key = next(i for i, (_, _, headers) in enumerate(bank.calls)
                         if "x-pl-agent-key" in headers)
        assert proof_call < first_key
        assert bank.registered == 0 and legacy.read_bytes() == original
    asyncio.run(run())


def test_wrong_bank_cannot_bind_legacy_state(tmp_path):
    async def run():
        provider, bank = Provider(), Bank()
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({"bank_url": URL, "agent_id": "a" * 32,
                                       "credential": "secret-fixture"}))
        _secure_state(legacy)
        original = legacy.read_bytes()
        path = tmp_path / "bound.json"
        client, migrated = instance(bank, provider, path, legacy_state_path=legacy)
        async with client:
            with pytest.raises(AdapterError):
                await migrated.__aenter__()
        assert not path.exists() and legacy.read_bytes() == original
        assert bank.registered == 0
        assert all("x-pl-agent-key" not in headers for _, _, headers in bank.calls)
    asyncio.run(run())


def test_changed_credential_during_response_is_not_retried(tmp_path):
    async def run():
        provider, bank = Provider(), Bank()
        client, adapter = instance(bank, provider, tmp_path / "state.json")
        async with client, adapter:
            bank.calls.clear()
            bank.response_hook = lambda action: provider.rotate("fixture-b") if action == "heartbeat" else None
            with pytest.raises(AdapterError):
                await adapter._heartbeat()
            assert sum(action == "heartbeat" for action, _, _ in bank.calls) == 1
    asyncio.run(run())


def test_background_renewal_recovers_after_revocation_and_file_refresh(tmp_path, monkeypatch):
    async def run():
        provider, bank = Provider(), Bank()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        client, adapter = instance(bank, provider, tmp_path / "state.json")
        async with client, adapter:
            original = adapter.instance_headers
            bank.token = "fixture-b"
            for _ in range(100):
                if adapter._failure is not None:
                    break
                await asyncio.sleep(0.01)
            assert adapter._failure is not None
            provider.rotate(bank.token)
            for _ in range(100):
                if adapter._failure is None:
                    break
                await asyncio.sleep(0.01)
            assert adapter._failure is None
            assert adapter.instance_headers == original and bank.registered == 1
    asyncio.run(run())


@pytest.mark.parametrize("explicit_operation", [False, True])
def test_wrong_principal_then_restored_credential_resumes_without_restart(tmp_path, monkeypatch, explicit_operation):
    async def run():
        provider, bank = Provider(), Bank()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        client, adapter = instance(bank, provider, tmp_path / "state.json")
        async with client, adapter:
            original = adapter.instance_headers
            principal = bank.principal
            boundary = len(bank.calls)
            bank.principal = "wrong-principal"
            bank.token = "fixture-wrong"
            provider.rotate(bank.token)
            for _ in range(100):
                if adapter._permanent_failure:
                    break
                await asyncio.sleep(0.01)
            assert adapter._permanent_failure
            assert all("x-pl-agent-key" not in headers for _, _, headers in bank.calls[boundary:])
            bank.principal = principal
            bank.token = "fixture-restored"
            provider.rotate(bank.token)
            if explicit_operation:
                await adapter.validate_snapshot(provider.snapshot())
            for _ in range(100):
                if adapter._failure is None:
                    break
                await asyncio.sleep(0.01)
            assert adapter._failure is None and not adapter._permanent_failure
            assert adapter._recovered.is_set()
            assert not adapter._heartbeat_task.done()
            assert adapter.instance_headers == original and bank.registered == 1
    asyncio.run(run())


def test_rotation_after_yield_does_not_commit_old_page_cursor(tmp_path):
    async def run():
        from contextlib import aclosing

        provider, bank = Provider(), Bank()
        client, adapter = instance(bank, provider, tmp_path / "state.json", wake_enabled=True)
        async with client, adapter:
            bank.pages.append({"messages": [{"message_id": "message-a", "sender_agent_id": "sender",
                "recipient_agent_id": bank.agent, "text": "fixture"}], "after": "old-page"})
            async with aclosing(adapter.inbox()) as inbox:
                await anext(inbox)
                provider.rotate("fixture-b")
                bank.token = provider.token
                def finish(action):
                    if action == "receive":
                        raise asyncio.CancelledError
                bank.response_hook = finish
                with pytest.raises(asyncio.CancelledError):
                    await anext(inbox)
            assert adapter._after is None
    asyncio.run(run())


def test_registry_rotation_and_restart_reuse_bound_path(tmp_path):
    async def run():
        from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

        provider, bank = Provider(), Bank()
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            def factory(*args, **kwargs):
                return CoordinationAdapter(*args, client=client, **kwargs)
            registry = CodexCoordinationRegistry(URL, provider.token, provider=provider,
                state_dir=tmp_path, adapter_factory=factory)
            first = await registry.get(THREAD)
            assert first is not None
            identity = first.instance_headers
            provider.rotate("fixture-b")
            bank.token = provider.token
            assert (await registry.get(THREAD)).instance_headers == identity
            await registry.aclose()
            resumed = CodexCoordinationRegistry(URL, provider.token, provider=provider,
                state_dir=tmp_path, adapter_factory=factory)
            assert (await resumed.get(THREAD)).instance_headers == identity
            await resumed.aclose()
        assert bank.registered == 1
    asyncio.run(run())
