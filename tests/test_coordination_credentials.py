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
    from pseudolife_memory import credentials, coordination_adapter, private_state

    path = tmp_path / "identity.json"
    fd = coordination_adapter._open_state(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    monkeypatch.setattr(credentials, "_windows_owner_only", lambda fd: False)
    mutations = []
    # open_private calls private_state's _private_fd, not the adapter's wrapper.
    monkeypatch.setattr(private_state, "_private_fd", lambda *args: mutations.append(True))
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
            if "agent_id" in body:
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


@pytest.mark.parametrize("read_only", [False, True])
def test_context_mode_preserves_mailbox_proof_body(read_only):
    from pseudolife_memory.coordination_identity import fetch_context

    async def run():
        provider, bank = Provider(), Bank()
        bank.agent = "a" * 32
        identity = {"agent_id": bank.agent, "credential": bank.credential}
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            context = await fetch_context(client, URL, provider.snapshot(),
                                          identity=identity, read_only=read_only)
        assert context == {"bank_id": bank.bank_id, "principal": bank.principal}
        action, body, headers = bank.calls[0]
        assert action == "context" and body["agent_id"] == bank.agent
        assert len(body["nonce"]) == 32 and "x-pl-agent-key" not in headers
        assert set(body) == ({"agent_id", "nonce", "read_only"} if read_only
                             else {"agent_id", "nonce"})
        if read_only:
            assert body["read_only"] is True
    asyncio.run(run())


_UNREADABLE_CONTEXT_ANSWERS = [
    b"<html>proxy</html>",
    b"[]",
    b'"fixture"',
    b'{"principal": "fixture-user"}',
    b'{"bank_id": "11111111-1111-4111-8111-111111111111"}',
    b'{"bank_id": 7, "principal": "fixture-user"}',
    b'{"bank_id": "11111111-1111-4111-8111-111111111111", "principal": 7}',
    b'{"bank_id": "11111111111141118111111111111111", "principal": "fixture-user"}',
    b'{"bank_id": "11111111-1111-4111-8111-111111111111", "principal": ""}',
    b'{"bank_id": "11111111-1111-4111-8111-111111111111", "principal": "a\\nb"}',
]


@pytest.mark.parametrize("answer", _UNREADABLE_CONTEXT_ANSWERS)
@pytest.mark.parametrize("proved", [False, True])
def test_unreadable_context_answer_is_not_an_identity_verdict(answer, proved):
    """An answer that cannot be read as a bank identity (a proxy's error
    page, a cut or reshaped body) says nothing about which bank answered,
    so it is ``context_unreadable``, which callers retry, never
    ``bank_identity_mismatch``, which stops background delivery for good."""
    from pseudolife_memory.coordination_identity import fetch_context

    async def run():
        provider = Provider()
        identity = {"agent_id": "a" * 32, "credential": "fixture-instance-key"}
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=answer))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(AdapterError) as caught:
                await fetch_context(client, URL, provider.snapshot(),
                                    identity=identity if proved else None, read_only=True)
        assert caught.value.code == "context_unreadable"
        assert caught.value.status is None
        assert "fixture" not in str(caught.value)
    asyncio.run(run())


# A non-ASCII proof made hmac.compare_digest raise TypeError on str
# arguments, and a lone surrogate cannot be encoded; each must still be a
# refusal, not an exception that ends the proxy (Codex review of d7c8ecad).
@pytest.mark.parametrize("proof", [None, 7, "f" * 64, "é" * 64, "\ud800"])
def test_failed_mailbox_proof_on_a_readable_answer_stays_an_identity_verdict(proof):
    """The near miss: a well-formed answer whose proof is missing or wrong
    was read fine and did not prove this mailbox, so it is still
    ``bank_identity_mismatch``."""
    from pseudolife_memory.coordination_identity import fetch_context

    async def run():
        provider, bank = Provider(), Bank()
        value = {"bank_id": bank.bank_id, "principal": bank.principal}
        if proof is not None:
            value["proof"] = proof
        # ASCII JSON, escapes included, as a daemon puts it on the wire: a
        # lone surrogate arrives as "\ud800" and decodes to an unencodable str.
        body = json.dumps(value).encode("ascii")
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(AdapterError) as caught:
                await fetch_context(client, URL, provider.snapshot(),
                    identity={"agent_id": "a" * 32, "credential": bank.credential})
        assert caught.value.code == "bank_identity_mismatch"
    asyncio.run(run())


@pytest.mark.parametrize("answer", ["garbled", "other_bank"])
def test_one_unreadable_context_answer_does_not_stop_background_delivery(
        tmp_path, monkeypatch, capsys, answer):
    """2026-10-23: one unparseable 200 to the heartbeat's read-only identity
    check was a permanent identity verdict, so background board delivery
    stopped until the credential rotated. It is now an outage: reported
    once, retried, and restored. The near miss, a readable answer naming
    another bank, still stops delivery."""
    async def run():
        provider, bank = Provider(), Bank()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0,))
        armed, served = [], asyncio.Event()

        async def daemon(request):
            if (armed and request.url.path.endswith("/context")
                    and json.loads(request.content).get("read_only")):
                armed.clear()
                served.set()
                if answer == "garbled":
                    return httpx.Response(200, content=b"<html>proxy</html>")
                bank.bank_id = "22222222-2222-4222-8222-222222222222"
            return await bank(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(daemon))
        adapter = CoordinationAdapter(URL, provider.token, client=client,
            provider=provider, state_path=tmp_path / "state.json")
        async with client, adapter:
            attaches = [action for action, _, _ in bank.calls].count("attach")
            armed.append(True)
            await asyncio.wait_for(served.wait(), 5)

            async def settled():
                while True:
                    attached = [action for action, _, _ in bank.calls].count("attach")
                    if answer == "other_bank" and adapter._permanent_failure:
                        return
                    if (answer == "garbled" and adapter._failure is None
                            and attached > attaches):
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(settled(), 5)
        return bank.registered

    assert asyncio.run(run()) == 1
    stderr = capsys.readouterr().err
    if answer == "garbled":
        assert stderr.count("live coordination delivery unavailable; retrying") == 1
        assert stderr.count("live coordination delivery restored") == 1
        assert "background delivery stopped" not in stderr
    else:
        assert stderr.count("background delivery stopped") == 1


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


def test_bank_mismatch_never_transmits_key_or_replaces_state(tmp_path, capsys):
    """A saved address bound to another bank is never used or replaced: the
    endpoint now serves a different bank, which a fresh address would not
    fix. The near miss of the principal-change case below."""
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            pass
        original = path.read_bytes()
        bank.bank_id = "22222222-2222-4222-8222-222222222222"
        bank.calls.clear()
        client, resumed = instance(bank, provider, path)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await resumed.__aenter__()
        assert caught.value.code == "bank_identity_mismatch"
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".stale").exists()
        assert bank.registered == 1
        assert all("x-pl-agent-key" not in headers for _, _, headers in bank.calls)
    asyncio.run(run())
    assert ".stale" not in capsys.readouterr().err


def test_principal_change_on_the_same_bank_retires_and_registers_fresh(tmp_path, capsys):
    """2026-10-30: a client's login moved to a per-machine token (another
    principal on the same bank). The saved address, bound to the old
    principal, was refused as ``bank_identity_mismatch`` on every start, so
    the session never reached the board. The same bank under a new login
    now gets a fresh address, as a new session would, and the old state is
    kept beside it; the old address's key is never sent."""
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            old = first.instance_headers["X-PL-Agent"]
        original = path.read_bytes()
        # An older retired record is replaced by this one (documented).
        stale = path.with_name(path.name + ".stale")
        stale.write_text("older retired record")
        bank.principal = "machine-login"
        bank.token = "fixture-machine"
        provider.rotate(bank.token)
        bank.calls.clear()
        client, resumed = instance(bank, provider, path)
        async with client, resumed:
            headers = resumed.instance_headers
            assert headers["X-PL-Agent"] == bank.agent != old
            assert headers["X-PL-Principal"] == "machine-login"
            # A board call on the new address goes through.
            await resumed._heartbeat()
        assert bank.registered == 2
        assert all(h.get("x-pl-agent") != old for _, _, h in bank.calls)
        register = next(i for i, (a, _, _) in enumerate(bank.calls) if a == "register")
        assert all("x-pl-agent-key" not in h for _, _, h in bank.calls[:register])
        state = json.loads(path.read_text())
        assert state["agent_id"] == bank.agent and state["principal"] == "machine-login"
        assert state["bank_id"] == bank.bank_id
        assert stale.read_bytes() == original
    asyncio.run(run())
    err = capsys.readouterr().err
    assert err.count("the daemon now maps this login to a different principal than the "
                     "saved coordination address was registered under (same bank)") == 1
    assert "mail sent to the old address is not carried over" in err
    assert "no longer valid on this bank" not in err


def test_bank_and_principal_both_changed_still_refuses(tmp_path, capsys):
    """The near miss for the principal change: a record bound to another
    bank is refused and kept whatever its principal, so a principal that
    also differs must not make it look like a login change."""
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            pass
        original = path.read_bytes()
        bank.bank_id = "22222222-2222-4222-8222-222222222222"
        bank.principal = "machine-login"
        client, resumed = instance(bank, provider, path)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await resumed.__aenter__()
        assert caught.value.code == "bank_identity_mismatch"
        assert caught.value.binding == "bank"
        assert caught.value.state_file == path
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".stale").exists()
        assert bank.registered == 1
    asyncio.run(run())
    assert ".stale" not in capsys.readouterr().err


def test_a_record_replaced_after_it_was_read_is_reloaded_not_retired(tmp_path, monkeypatch):
    """Two shims on one state path: after this one read the old record,
    another retired it and saved a new address under the current principal.
    That new record is resumed, not moved into .stale, and nothing more
    registers."""
    from pseudolife_memory import coordination_identity

    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            pass
        bank.principal = "machine-login"
        peer = dict(json.loads(path.read_text()), agent_id="b" * 32,
                    principal="machine-login")
        real, swapped = coordination_identity.principal_changed, []

        def racing(state, context):
            changed = real(state, context)
            if changed and not swapped:
                # The peer's atomic save: a new file over the old one.
                fresh = tmp_path / "peer.json"
                fresh.write_text(json.dumps(peer))
                _secure_state(fresh)
                os.replace(fresh, path)
                swapped.append(True)
            return changed

        monkeypatch.setattr(coordination_identity, "principal_changed", racing)
        bank.agent = peer["agent_id"]
        client, resumed = instance(bank, provider, path)
        async with client, resumed:
            assert resumed.instance_headers["X-PL-Agent"] == peer["agent_id"]
        assert swapped
        assert bank.registered == 1
        assert json.loads(path.read_text())["agent_id"] == peer["agent_id"]
        assert not path.with_name(path.name + ".stale").exists()
    asyncio.run(run())


def test_a_legacy_record_naming_another_principal_is_left_and_replaced(tmp_path, capsys):
    """Round-2 review of #1066: a record read from the token-derived legacy
    path that names this bank under another principal was refused on every
    start, and the hint's "restart" re-read it. It is now left as it is (never
    retired or rewritten) and a new address registers on the bound path, so
    later starts resume that one."""
    async def run():
        provider, bank = Provider(), Bank()
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({
            "bank_url": URL, "agent_id": "a" * 32, "credential": bank.credential,
            "version": 2, "bank_id": bank.bank_id, "principal": "previous-login"}))
        _secure_state(legacy)
        original = legacy.read_bytes()
        path = tmp_path / "bound.json"
        client, resumed = instance(bank, provider, path, legacy_state_path=legacy)
        async with client, resumed:
            assert resumed.instance_headers["X-PL-Agent"] == bank.agent
        assert bank.registered == 1
        assert all(h.get("x-pl-agent") != "a" * 32 for _, _, h in bank.calls)
        assert json.loads(path.read_text())["principal"] == bank.principal
        assert legacy.read_bytes() == original
        assert not legacy.with_name(legacy.name + ".stale").exists()
    asyncio.run(run())
    assert ("than the saved legacy coordination address was registered under (same bank); "
            "registering a new address (the legacy record is left as it is") in capsys.readouterr().err


def test_a_legacy_record_naming_another_bank_is_refused_and_named(tmp_path):
    """The near miss: a legacy record bound to another bank is refused, kept
    as it is, and named as the file the refusal is about."""
    async def run():
        provider, bank = Provider(), Bank()
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({
            "bank_url": URL, "agent_id": "a" * 32, "credential": bank.credential,
            "version": 2, "bank_id": "22222222-2222-4222-8222-222222222222",
            "principal": bank.principal}))
        _secure_state(legacy)
        original = legacy.read_bytes()
        path = tmp_path / "bound.json"
        client, resumed = instance(bank, provider, path, legacy_state_path=legacy)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await resumed.__aenter__()
        assert caught.value.code == "bank_identity_mismatch"
        assert caught.value.binding == "bank"
        assert caught.value.state_file == legacy
        assert legacy.read_bytes() == original
        assert not path.exists() and bank.registered == 0
    asyncio.run(run())


def test_a_record_that_keeps_being_replaced_is_refused_after_three_retires(tmp_path, monkeypatch):
    """Round-2 review of #1066: the retire budget ends as in the Rust shim.
    After three retires that each lost the race to another process, a
    fourth read that still needs one is refused as ``reservation_changed``;
    nothing registers and nothing is moved into .stale."""
    from pseudolife_memory import coordination_identity

    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            pass
        bank.principal = "machine-login"
        stale_record = json.loads(path.read_text())
        real, swaps = coordination_identity.principal_changed, []

        def racing(state, context):
            changed = real(state, context)
            if changed:
                # Another process saves a record that still names the old
                # principal: a new file over the one just read.
                fresh = tmp_path / f"peer-{len(swaps)}.json"
                fresh.write_text(json.dumps(dict(stale_record, agent_id=f"{len(swaps):032x}")))
                _secure_state(fresh)
                os.replace(fresh, path)
                swaps.append(True)
            return changed

        monkeypatch.setattr(coordination_identity, "principal_changed", racing)
        client, resumed = instance(bank, provider, path)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await resumed.__aenter__()
        assert caught.value.code == "reservation_changed"
        assert len(swaps) == 4
        assert bank.registered == 1
        assert not path.with_name(path.name + ".stale").exists()
    asyncio.run(run())


def test_a_failed_registration_after_a_retire_does_not_import_the_legacy_record(tmp_path):
    """Round-2 Codex review of #1066: after a principal-change retire, a
    registration the daemon could not take (a 503) left only the .stale; the
    next attempt imported the legacy record bound under the previous
    principal and was refused for good, even after the daemon recovered. The
    retired .stale beside the bound path keeps the legacy record out, so the
    retry registers a new address."""
    async def run():
        provider, bank = Provider(), Bank()
        owners, refuse = {}, []

        async def daemon(request):
            body = json.loads(request.content) if request.content else {}
            path = request.url.path
            if (path.endswith("/context") and "agent_id" in body
                    and owners.get(body["agent_id"], bank.principal) != bank.principal):
                return httpx.Response(403, json={"error": "invalid_credential"})
            if path.endswith("/register") and refuse:
                refuse.pop()
                return httpx.Response(503, json={"error": "principals_unavailable"})
            response = await bank(request)
            if path.endswith("/register"):
                owners[bank.agent] = bank.principal
            return response

        def adapter(**kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(daemon))
            return client, CoordinationAdapter(URL, provider.token, client=client,
                                               provider=provider, state_path=path, **kwargs)

        legacy = tmp_path / "legacy.json"
        path = tmp_path / "bound.json"
        client, first = adapter()
        async with client, first:
            old = first.instance_headers["X-PL-Agent"]
        legacy.write_text(json.dumps({"bank_url": URL, "agent_id": old,
                                      "credential": bank.credential}))
        _secure_state(legacy)
        original = legacy.read_bytes()
        bank.principal = "machine-login"
        refuse.append(True)
        client, failed = adapter(legacy_state_path=legacy)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await failed.__aenter__()
        assert caught.value.status == 503
        assert not path.exists() and path.with_name(path.name + ".stale").exists()
        client, retried = adapter(legacy_state_path=legacy)
        async with client, retried:
            assert retried.instance_headers["X-PL-Agent"] == bank.agent != old
        assert json.loads(path.read_text())["principal"] == "machine-login"
        assert legacy.read_bytes() == original
    asyncio.run(run())


def test_a_retired_record_does_not_fall_back_to_the_legacy_record(tmp_path):
    """Codex review of 22f969dc: a migrated session keeps its token-derived
    legacy record beside the bound one. After the bound record is retired
    for a principal change, the legacy record (bound under the previous
    principal too) must not be imported: its proof is refused under the new
    principal, as the daemon refuses another principal's mailbox, and the
    session would be off the board on every start."""
    async def run():
        provider, bank = Provider(), Bank()
        owners = {}

        async def daemon(request):
            body = json.loads(request.content) if request.content else {}
            if (request.url.path.endswith("/context") and "agent_id" in body
                    and owners.get(body["agent_id"], bank.principal) != bank.principal):
                return httpx.Response(403, json={"error": "invalid_credential"})
            response = await bank(request)
            if request.url.path.endswith("/register"):
                owners[bank.agent] = bank.principal
            return response

        legacy = tmp_path / "legacy.json"
        path = tmp_path / "bound.json"
        client = httpx.AsyncClient(transport=httpx.MockTransport(daemon))
        first = CoordinationAdapter(URL, provider.token, client=client, provider=provider,
                                    state_path=path)
        async with client, first:
            old = first.instance_headers["X-PL-Agent"]
        legacy.write_text(json.dumps({"bank_url": URL, "agent_id": old,
                                      "credential": bank.credential}))
        _secure_state(legacy)
        original = legacy.read_bytes()
        bank.principal = "machine-login"
        client = httpx.AsyncClient(transport=httpx.MockTransport(daemon))
        resumed = CoordinationAdapter(URL, provider.token, client=client, provider=provider,
                                      state_path=path, legacy_state_path=legacy)
        async with client, resumed:
            assert resumed.instance_headers["X-PL-Agent"] == bank.agent != old
        assert bank.registered == 2
        assert json.loads(path.read_text())["principal"] == "machine-login"
        assert legacy.read_bytes() == original
    asyncio.run(run())


def test_same_bank_and_principal_resume_the_saved_address_unchanged(tmp_path, capsys):
    """The near miss: nothing changed, so the saved address is resumed as
    it is, with no new registration and nothing retired."""
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            old = first.instance_headers["X-PL-Agent"]
        original = path.read_bytes()
        client, resumed = instance(bank, provider, path)
        async with client, resumed:
            assert resumed.instance_headers["X-PL-Agent"] == old
        assert bank.registered == 1
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".stale").exists()
    asyncio.run(run())
    assert ".stale" not in capsys.readouterr().err


def test_principal_change_during_legacy_migration_still_refuses(tmp_path):
    """The near miss for unversioned state: it names no bank or principal,
    so a context that changes between the startup check and the mailbox
    proof is refused as before and the state is kept."""
    async def run():
        provider, bank = Provider(), Bank()
        bank.agent = "a" * 32
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"bank_url": URL, "agent_id": bank.agent,
                                    "credential": bank.credential}))
        _secure_state(path)
        original = path.read_bytes()

        def flip(action):
            if action == "context":
                bank.principal = "machine-login"
                bank.response_hook = None
        bank.response_hook = flip
        client, resumed = instance(bank, provider, path)
        async with client:
            with pytest.raises(AdapterError, match="changed during migration") as caught:
                await resumed.__aenter__()
        assert caught.value.code == "bank_identity_mismatch"
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".stale").exists()
        assert bank.registered == 0
    asyncio.run(run())


def test_bound_address_the_bank_no_longer_has_is_retired_and_replaced(tmp_path):
    """A resumed session whose address the bank pruned (idle past
    AGENT_RETENTION, so no retained mail named it) gets a fresh address. The
    bank and principal were verified first, and the old file stays beside
    the new one."""
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            old = first.instance_headers["X-PL-Agent"]
        bank.agent = None
        bank.calls.clear()
        client, resumed = instance(bank, provider, path)
        async with client, resumed:
            assert resumed.instance_headers["X-PL-Agent"] == bank.agent != old
        assert [a for a, _, _ in bank.calls if a != "context"][:3] == ["attach", "register", "attach"]
        assert bank.registered == 2
        state = json.loads(path.read_text())
        assert state["agent_id"] == bank.agent and state["bank_id"] == bank.bank_id
        assert json.loads(path.with_name(path.name + ".stale").read_text())["agent_id"] == old
    asyncio.run(run())


def test_bound_address_with_a_rejected_key_is_preserved(tmp_path):
    async def run():
        provider, bank = Provider(), Bank()
        path = tmp_path / "state.json"
        client, first = instance(bank, provider, path)
        async with client, first:
            pass
        original = path.read_bytes()

        async def rejecting(request):
            if request.url.path.endswith("/attach"):
                return httpx.Response(401, json={"error": "invalid_credential"})
            return await bank(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(rejecting))
        resumed = CoordinationAdapter(URL, provider.token, client=client,
                                      provider=provider, state_path=path)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await resumed.__aenter__()
        assert caught.value.code == "invalid_credential"
        assert path.read_bytes() == original and bank.registered == 1
        assert not path.with_name(path.name + ".stale").exists()
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
                          if action == "context" and "agent_id" in body)
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
