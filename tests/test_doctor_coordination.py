"""Coordination diagnostics keep health, authentication and delivery separate."""
import asyncio
import json

import httpx
import pytest

from pseudolife_memory import doctor_cli


@pytest.mark.parametrize("health,board,state", [
    (None, {"state": "unreachable"}, "offline"),
    ({"status": "ok"}, {"state": "unauthorized"}, "unauthorized"),
    ({"status": "ok"}, {"state": "on"}, "admitted"),
    ({"status": "ok"}, {"state": "principal_not_allowed"}, "principal_not_allowed"),
])
def test_snapshot_separates_health_from_authentication(health, board, state):
    result = doctor_cli.coordination_snapshot(health, board, {}, "generic", None)
    assert result["authentication"] == state
    assert result["registration"] == "not_checked"
    assert result["wake"]["state"] == "unsupported"
    assert result["delivery"] == "unverified"
    assert result["next"]


def test_supported_host_configuration_is_not_a_wake_receipt():
    result = doctor_cli.coordination_snapshot(
        {"status": "ok"}, {"state": "on"},
        {"codex": {"registered": True, "doorbell": "on"}}, "codex", None)
    assert result["wake"] == {"state": "configured", "evidence": "configuration_only"}
    assert result["registration"] == "not_checked"
    assert result["delivery"] == "unverified"


def test_shared_conversation_host_has_no_session_mailbox():
    result = doctor_cli.coordination_snapshot(
        {"status": "ok"}, {"state": "on"}, {}, "claude-desktop", None)
    assert result["registration"] == "unsupported_host"
    assert result["wake"]["state"] == "unsupported"


@pytest.mark.parametrize("status,body,expected", [
    (200, {"agents": [], "credential": "fixture-secret"}, "authenticated"),
    (401, {"error": "unauthorized", "hint": "fixture-secret"}, "unauthorized"),
    (400, {"error": "instance_not_found"}, "missing_registration"),
    (403, {"error": "invalid_credential"}, "invalid_credential"),
    (404, {"error": "fixture-secret"}, "unsupported_capability"),
    (503, {"error": "fixture-secret"}, "unavailable"),
])
def test_instance_probe_is_read_only_and_redacted(tmp_path, monkeypatch, status, body, expected):
    from pseudolife_memory import coordination_identity
    monkeypatch.setattr(coordination_identity, "read_legacy", lambda path, url: {
        "version": 2, "bank_id": "00000000-0000-4000-8000-000000000001",
        "principal": "default", "agent_id": "a" * 32, "credential": "fixture-key"})
    requests = []
    state = tmp_path / "state.json"
    state.write_text("fixture", encoding="utf-8")

    def respond(request):
        requests.append(request)
        value = body
        if status == 200:
            import hashlib
            import hmac
            nonce = json.loads(request.content)["nonce"]
            message = json.dumps(["pseudolife-context-v1", "00000000-0000-4000-8000-000000000001",
                "default", "a" * 32, nonce], separators=(",", ":")).encode("ascii")
            value = {"bank_id": "00000000-0000-4000-8000-000000000001", "principal": "default",
                     "proof": hmac.new(hashlib.sha256(b"fixture-key").digest(), message, hashlib.sha256).hexdigest()}
        return httpx.Response(status, json=value)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await doctor_cli.probe_registration(
                "http://fixture", "fixture-bearer", state, 1, client=client)

    result = asyncio.run(drive())
    assert result == expected
    assert len(requests) == 1
    assert requests[0].url.path == "/api/coordination/context"
    assert json.loads(requests[0].content)["read_only"] is True
    assert "x-pl-agent-key" not in requests[0].headers
    assert "fixture-secret" not in result and "fixture-key" not in result


def test_missing_state_does_not_contact_daemon(tmp_path):
    assert asyncio.run(doctor_cli.probe_registration(
        "http://fixture", "fixture", tmp_path / "absent", 1)) == "missing_registration"


def test_doctor_handshake_disables_coordination_registration(monkeypatch):
    from contextlib import asynccontextmanager
    from mcp.client import stdio
    seen = {}

    @asynccontextmanager
    async def stopped(params):
        seen.update(params.env)
        raise RuntimeError("fixture stop before handshake")
        yield

    monkeypatch.setattr(stdio, "stdio_client", stopped)
    with pytest.raises(RuntimeError):
        asyncio.run(doctor_cli._handshake())
    assert seen["PSEUDOLIFE_AGENT_COORDINATION"] == "0"
    assert seen["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"


def test_disposable_proof_refuses_implicit_database_and_never_reads_client_settings(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor", "--disposable-proof"])
    monkeypatch.delenv("PSEUDOLIFE_TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(doctor_cli, "registration_credentials", lambda env: pytest.fail("client settings read"))
    with pytest.raises(SystemExit) as stop:
        doctor_cli.run_doctor()
    assert stop.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "ExplicitDisposableDatabaseRequired"


def test_doctor_reports_only_exact_owned_fixture_when_cleanup_fails(monkeypatch, capsys):
    import sys
    from pseudolife_memory import coordination_proof as proof
    name = "pseudolife_memory_test_proof_" + "a" * 32
    def failed(*args, **kwargs):
        raise proof.FixtureCleanupError(name)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor", "--disposable-proof"])
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", "host=fixture.invalid dbname=pseudolife_memory_test_fixture password=fixture-secret")
    monkeypatch.setattr(proof, "run_disposable_proof", failed)
    with pytest.raises(SystemExit) as stop:
        doctor_cli.run_doctor()
    assert stop.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["fixture_removed"] is False and report["fixture_bank"] == name
    assert name in report["recovery"] and "DROP DATABASE" in report["recovery"]
    assert "fixture-secret" not in json.dumps(report) and "fixture.invalid" not in json.dumps(report)


def test_wake_report_does_not_echo_arbitrary_environment_values(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "fixture-secret")
    result = doctor_cli._wake_state(True, lambda key: doctor_cli.os.environ.get(key, ""), "PSEUDOLIFE_CODEX_DOORBELL")
    assert "fixture-secret" not in result


def test_wake_caps_only_report_known_numeric_fields(monkeypatch):
    monkeypatch.setattr(doctor_cli, "_claude_code_wake", lambda enabled: {})
    monkeypatch.setattr(doctor_cli, "_codex_wake", lambda enabled: {})
    # nudge_interval_seconds is a daemon from before 2026-10-02 reporting a
    # retired cap: it no longer bounds anything, so doctor does not list it.
    result = doctor_cli._wake_report({"coordination": {"wake": {
        "nightly_total": 200, "token": "fixture-secret", "urgent_per_sender_per_hour": "fixture-secret",
        "nudge_interval_seconds": 3600, "authority_per_sender_per_hour": 12}}})
    assert result["caps"] == {"nightly_total": 200, "authority_per_sender_per_hour": 12}


@pytest.mark.parametrize("status,body", [
    (200, {"bank_id": "other-bank", "principal": "default", "proof": "f" * 64}),
    (200, {"bank_id": "fixture-bank", "principal": "default", "proof": "f" * 64}),
    (302, {"error": "fixture-secret"}),
])
def test_registration_never_discloses_instance_credential_to_changed_or_redirecting_bank(tmp_path, monkeypatch, status, body):
    from pseudolife_memory import coordination_identity
    state = tmp_path / "state.json"
    state.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(coordination_identity, "read_legacy", lambda path, url: {
        "version": 2, "bank_id": "fixture-bank", "principal": "default",
        "agent_id": "a" * 32, "credential": "fixture-key"})
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body, headers={"Location": "http://other-fixture"})

    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
            return await doctor_cli.probe_registration("http://fixture", "fixture-bearer", state, 1, client=client)

    assert asyncio.run(drive()) in ("bank_identity_mismatch", "invalid_credential", "refused")
    assert len(requests) == 1
    assert "fixture-key" not in requests[0].content.decode()
    assert "fixture-key" not in str(dict(requests[0].headers))


@pytest.mark.parametrize("status,state", [(401, "unauthorized"), (404, "unsupported_capability"), (503, "transport_refused")])
def test_board_probe_preserves_transport_refusal_status(monkeypatch, status, state):
    import urllib.error
    from pseudolife_memory import board_status

    class RefusingOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "fixture-secret", {}, None)

    monkeypatch.setattr(board_status.urllib.request, "build_opener", lambda *args: RefusingOpener())
    result = board_status.board_probe("http://fixture", "fixture-bearer")
    assert result["state"] == state
    assert "fixture-secret" not in json.dumps(result)


def test_board_probe_does_not_echo_an_unknown_header_reason(monkeypatch):
    import io
    from pseudolife_memory import board_status

    class Opener:
        def open(self, request, timeout):
            response = io.BytesIO(b"")
            response.headers = {"X-PL-Board": "off; reason=fixture_secret"}
            return response

    monkeypatch.setattr(board_status.urllib.request, "build_opener", lambda *args: Opener())
    assert "fixture_secret" not in json.dumps(board_status.board_probe("http://fixture", "fixture-bearer"))


def test_default_doctor_snapshot_does_not_probe_or_register_an_instance(tmp_path, monkeypatch, capsys):
    import sys
    from unittest.mock import AsyncMock
    from pseudolife_memory import shim
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for key in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_DAEMON_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor", "--host", "codex"])
    monkeypatch.setattr(doctor_cli, "_windows", lambda: False)
    monkeypatch.setattr(doctor_cli, "_board_probe", lambda timeout: {"state": "on", "line": "on"})
    monkeypatch.setattr(doctor_cli, "_wake_report", lambda health: {"codex": {"registered": True, "doorbell": "on"}})
    monkeypatch.setattr(doctor_cli, "_codex_hooks_line", lambda health: "current")
    monkeypatch.setattr(doctor_cli, "path_resolution", lambda: {})
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(doctor_cli, "_handshake", AsyncMock(return_value={
        "instructions_present": True, "tool_count": 2, "tools_missing_annotations": [],
        "coordination_tools_present": True}))
    probe = AsyncMock()
    monkeypatch.setattr(doctor_cli, "probe_registration", probe)
    with pytest.raises(SystemExit) as stop:
        doctor_cli.run_doctor()
    assert stop.value.code == 0
    result = json.loads(capsys.readouterr().out)["coordination"]
    assert result["daemon"] == "reachable" and result["authentication"] == "admitted"
    assert result["registration"] == "not_checked" and result["delivery"] == "unverified"
    probe.assert_not_called()


def test_bad_sdk_does_not_misreport_a_reachable_daemon(tmp_path, monkeypatch, capsys):
    import sys
    from pseudolife_memory import shim
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(doctor_cli, "registration_credentials", lambda env: ({}, None))
    monkeypatch.setattr(doctor_cli, "_board_probe", lambda timeout: {"state": "on", "line": "on"})
    monkeypatch.setattr(doctor_cli, "_windows", lambda: False)
    monkeypatch.setattr(doctor_cli, "_wake_report", lambda health: {})
    monkeypatch.setattr(doctor_cli, "path_resolution", lambda: {})
    monkeypatch.setattr(shim, "probe_health", lambda *args, **kwargs: {"status": "ok"})

    def incompatible_sdk():
        raise RuntimeError("fixture-secret")

    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", incompatible_sdk)
    with pytest.raises(SystemExit) as stop:
        doctor_cli.run_doctor()
    assert stop.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["coordination"]["daemon"] == "reachable"


def test_http_authentication_refusal_is_reachable_even_when_health_is_blocked():
    result = doctor_cli.coordination_snapshot(None, {"state": "unauthorized"}, {}, "generic", None)
    assert result["daemon"] == "reachable"
    assert result["authentication"] == "unauthorized"
