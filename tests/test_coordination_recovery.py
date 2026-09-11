"""Offline restore recovery never exposes or silently replaces credentials."""
import json
from contextlib import contextmanager

import pytest

from tests.test_coordination_storage import store, creds, pair  # noqa: F401
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


def settings(tmp_path, enabled=False):
    path = tmp_path / "config.yaml"
    path.write_text(f"coordination:\n  enabled: {str(enabled).lower()}\n  allowed_principals: [alice]\n")
    return path


def invoke(monkeypatch, pg_url, config, action, *options):
    from pseudolife_memory.coordination_recovery import main
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    return main([action, "--config", str(config), "--confirm-daemon-stopped", *options])


def test_rebind_persists_identity_with_adapter_reservation(tmp_path, monkeypatch):
    from pseudolife_memory import coordination_recovery as recovery

    @contextmanager
    def connect():
        yield object()

    class Storage:
        def __init__(self, conn):
            pass

        @contextmanager
        def _txn(self):
            yield

    class Store:
        def __init__(self, storage):
            pass

        def rebind(self, agent, principal):
            return {"agent_id": agent, "credential": "fixture-key"}

    monkeypatch.setattr(recovery, "_connect", connect)
    monkeypatch.setattr(recovery, "_RecoveryStorage", Storage)
    monkeypatch.setattr(recovery, "CoordinationStore", Store)
    state = tmp_path / "agent.json"
    assert recovery.main(["rebind", "--config", str(settings(tmp_path)),
                          "--confirm-daemon-stopped", "--agent", "agent-a",
                          "--principal", "alice", "--bank-url", "http://127.0.0.1:8099",
                          "--state", str(state)]) == 0
    assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == "agent-a"


def test_recovery_revokes_and_rebinds_private_state_without_exposing_key(store, pg_url, tmp_path, monkeypatch, capsys):
    from pseudolife_memory.storage.coordination import CoordinationError
    a, b = pair(store)
    store.attach(*creds(b), attachment_id="old", wake_enabled=True)
    message = store.send(*creds(a), to=b["agent_id"], text="pending", request_id="r")
    config = settings(tmp_path)
    assert invoke(monkeypatch, pg_url, config, "recover", "--confirm-restore") == 0
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.authenticate(*creds(b))
    path = tmp_path / "new-agent.json"
    assert invoke(monkeypatch, pg_url, config, "rebind", "--agent", b["agent_id"], "--principal", "alice",
                  "--bank-url", "http://127.0.0.1:8099", "--state", str(path)) == 0
    identity = json.loads(path.read_text())
    assert set(identity) == {"bank_url", "agent_id", "credential"}
    assert identity["credential"] != b["credential"]
    assert store.authenticate(*creds(identity))["wake_enabled"] is False
    assert store.receive(*creds(identity))["messages"][0]["message_id"] == message["message_id"]
    output = capsys.readouterr()
    assert identity["credential"] not in output.out + output.err
    assert b["credential"] not in output.out + output.err


@pytest.mark.parametrize("enabled,confirmation", [(True, True), (False, False)])
def test_recovery_refuses_unsafe_preconditions_before_connect(tmp_path, monkeypatch, enabled, confirmation):
    from pseudolife_memory import coordination_recovery as recovery
    monkeypatch.setattr(recovery, "_connect", lambda: pytest.fail("database connected"))
    args = ["recover", "--config", str(settings(tmp_path, enabled)), "--confirm-daemon-stopped"]
    if confirmation:
        args.append("--confirm-restore")
    assert recovery.main(args) == 1


def test_recovery_requires_stopped_daemon_confirmation(tmp_path, monkeypatch):
    from pseudolife_memory import coordination_recovery as recovery
    monkeypatch.setattr(recovery, "_connect", lambda: pytest.fail("database connected"))
    assert recovery.main(["recover", "--config", str(settings(tmp_path)), "--confirm-restore"]) == 1


def test_rebind_refuses_repository_state_before_reserving_or_connecting(tmp_path, monkeypatch):
    from pseudolife_memory import coordination_recovery as recovery
    repository = tmp_path / "checkout"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: fixture")
    state = repository / "agent.json"
    calls = []
    monkeypatch.setattr(recovery, "_connect", lambda: calls.append("connect"))
    assert recovery.main(["rebind", "--config", str(settings(tmp_path)), "--confirm-daemon-stopped",
                          "--agent", "agent-a", "--principal", "alice", "--bank-url",
                          "http://127.0.0.1:8099", "--state", str(state)]) == 1
    assert not state.exists()
    assert calls == []


def test_rebind_existing_path_and_wrong_owner_do_not_issue_credentials(store, pg_url, tmp_path, monkeypatch):
    a, _ = pair(store)
    store.recover()
    config = settings(tmp_path)
    path = tmp_path / "existing.json"
    path.write_text("unchanged")
    options = ["--agent", a["agent_id"], "--principal", "alice", "--bank-url", "http://127.0.0.1:8099"]
    assert invoke(monkeypatch, pg_url, config, "rebind", *options, "--state", str(path)) == 1
    assert path.read_text() == "unchanged"
    config.write_text("coordination:\n  enabled: false\n  allowed_principals: [bob]\n")
    options[3] = "bob"
    assert invoke(monkeypatch, pg_url, config, "rebind", *options, "--state", str(tmp_path / "wrong.json")) == 1
    assert store.storage.conn.execute("SELECT credential_hash FROM coordination_agents WHERE agent_id=%s",
                                      (a["agent_id"],)).fetchone() == (None,)


def test_rebind_state_failure_rolls_back_credential_issuance(store, pg_url, tmp_path, monkeypatch, capsys):
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    a, _ = pair(store)
    store.recover()
    def fail(self, reservation):
        raise OSError("fixture-secret-must-not-appear")
    monkeypatch.setattr(CoordinationAdapter, "_save_new_identity", fail)
    assert invoke(monkeypatch, pg_url, settings(tmp_path), "rebind", "--agent", a["agent_id"],
                  "--principal", "alice", "--bank-url", "http://127.0.0.1:8099",
                  "--state", str(tmp_path / "failed.json")) == 1
    assert store.storage.conn.execute("SELECT credential_hash FROM coordination_agents WHERE agent_id=%s",
                                      (a["agent_id"],)).fetchone() == (None,)
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out + output.err
