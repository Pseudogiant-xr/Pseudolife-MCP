"""The daemon itself can tell the board something (dream-stall notices).

Until 2026-09-28 every board message came from a registered session: the
full-suite and GPU lease notices are sent by the lease CLI's own address.
A stalled dream extractor is noticed by the daemon, so it needs its own
sender. The reserved principal ``daemon`` is never a bearer: no client can
use the board as it, whatever the token map or ``allowed_principals`` say,
so a message whose sender principal is ``daemon`` came from the daemon. A
notice goes to every attached, non-idle session (``to: "all"``) with the
wake decision ``hinted``: it is context the next tool result carries, and
it never rings a parked session or becomes a live-channel turn.
"""
from __future__ import annotations

import pytest

from pseudolife_memory import coordination
from pseudolife_memory.coordination import daemon_notice
from pseudolife_memory.storage.coordination import DAEMON_PRINCIPAL
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401


def _store(service):
    with service._coordination_lock:
        return coordination._store(service)


def _peer(service, *, wake=False):
    store = _store(service)
    agent = store.register(PRINCIPAL, project="p", wake_enabled=wake)
    store.attach(PRINCIPAL, agent["agent_id"], agent["credential"],
                 attachment_id="live-" + agent["agent_id"][:8], wake_enabled=wake)
    return agent


def _mail(service, agent, **kw):
    return _store(service).receive(PRINCIPAL, agent["agent_id"], agent["credential"],
                                   **kw)["messages"]


def test_a_notice_reaches_every_attached_session_from_the_daemon(coordinating):
    a, b = _peer(coordinating), _peer(coordinating)
    out = daemon_notice(coordinating, "Dream extraction stalled since then: x.")
    assert out["recipients"] == 2
    for agent in (a, b):
        (message,) = _mail(coordinating, agent)
        assert message["sender_principal"] == DAEMON_PRINCIPAL
        assert message["text"] == "Dream extraction stalled since then: x."


def test_a_notice_is_context_it_never_rings_a_parked_session(coordinating):
    parked = _peer(coordinating, wake=True)
    _store(coordinating).update(PRINCIPAL, parked["agent_id"], parked["credential"],
                                park_reason="needs_resource", park_needs="GPU free",
                                park_clear_by="anyone")
    daemon_notice(coordinating, "Dream extraction stalled since then: x.")
    conn = _store(coordinating).storage.conn
    assert conn.execute("SELECT count(*) FROM coordination_wakes").fetchone()[0] == 0
    wake = conn.execute("SELECT wake FROM coordination_messages").fetchone()[0]
    assert wake["decision"] == "hinted"
    # Not a live-channel turn, but an explicit receive and the per-turn
    # preview both carry it.
    assert _mail(coordinating, parked, for_delivery=True) == []
    assert len(_mail(coordinating, parked)) == 1
    preview = _store(coordinating)._pending_preview(parked["agent_id"])
    assert len(preview) == 1


def test_the_daemon_keeps_one_address_across_notices(coordinating):
    _peer(coordinating)
    daemon_notice(coordinating, "first notice")
    daemon_notice(coordinating, "second notice")
    conn = _store(coordinating).storage.conn
    senders = conn.execute("SELECT DISTINCT sender_agent_id FROM coordination_messages"
                           ).fetchall()
    assert len(senders) == 1
    assert conn.execute("SELECT count(*) FROM coordination_agents WHERE principal=%s",
                        (DAEMON_PRINCIPAL,)).fetchone()[0] == 1


def test_a_vanished_daemon_address_is_registered_again(coordinating):
    agent = _peer(coordinating)
    daemon_notice(coordinating, "first notice")
    conn = _store(coordinating).storage.conn
    conn.execute("DELETE FROM coordination_messages")
    conn.execute("DELETE FROM coordination_agents WHERE principal=%s", (DAEMON_PRINCIPAL,))
    assert daemon_notice(coordinating, "second notice")["recipients"] == 1
    assert [m["text"] for m in _mail(coordinating, agent)] == ["second notice"]


@pytest.mark.parametrize("reason", ["login_expired", "extractor_unreachable",
                                    "extractor_error", "served_by_fallback"])
def test_the_real_stall_texts_pass_the_boards_secret_check(coordinating, reason):
    """The store refuses secret-shaped bodies; a timestamp or a CLI command
    in the notice must not read as one, or the notice would never land."""
    from pseudolife_memory.memory.dream import dream_stall_notice_text
    agent = _peer(coordinating)
    record = {"since": 1_790_000_000.0, "reason": reason,
              "recovered_at": 1_790_003_600.0}
    for kind in ("begin", "clear"):
        assert daemon_notice(coordinating, dream_stall_notice_text(kind, record)) == {
            "recipients": 1}
    assert len(_mail(coordinating, agent)) == 2


def test_nobody_attached_is_a_notice_sent_to_no_one(coordinating):
    assert daemon_notice(coordinating, "nobody hears this") == {"recipients": 0}


def test_a_disabled_board_is_skipped_silently(coordinating):
    coordinating.config.coordination.enabled = False
    assert daemon_notice(coordinating, "unsaid") is None


def test_no_postgres_is_skipped_silently():
    from types import SimpleNamespace
    svc = SimpleNamespace(config=SimpleNamespace(coordination=SimpleNamespace(enabled=True)),
                          _db_url=None)
    assert daemon_notice(svc, "unsaid") is None


# ── the reserved principal cannot be a client ─────────────────────────────

def _service(allowed):
    from contextlib import nullcontext
    from types import SimpleNamespace
    return SimpleNamespace(config=SimpleNamespace(coordination=SimpleNamespace(
        enabled=True, allowed_principals=allowed, audit_retention_days=90)),
        _lock=nullcontext(), _storage=object(), _ensure_init=lambda: None,
        _db_url="postgresql://fixture",
        _hlc=SimpleNamespace(tick=lambda: (100, 1)))


@pytest.mark.parametrize("action, parameters", [
    ("register", {}),
    ("send", {"to": "all", "text": "Dream extraction recovered.", "request_id": "r1"}),
])
def test_a_bearer_named_daemon_is_refused_even_when_listed(monkeypatch, action, parameters):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "fixture-secret:daemon")
    headers = {"authorization": "Bearer fixture-secret", "x-pl-agent": "a" * 32,
               "x-pl-agent-key": "private-fixture"}
    with pytest.raises(ValueError, match="principal_not_allowed"):
        coordination._dispatch(_service([DAEMON_PRINCIPAL]), action, parameters,
                               headers=headers)
    # The transport-validated principal argument is refused the same way.
    with pytest.raises(ValueError, match="principal_not_allowed"):
        coordination._dispatch(_service([DAEMON_PRINCIPAL]), action, parameters,
                               headers=headers, principal=DAEMON_PRINCIPAL)


def test_a_bearer_named_daemon_is_told_the_board_is_not_its_to_use(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "fixture-secret:daemon")
    reason = coordination.unavailable_reason(
        _service([DAEMON_PRINCIPAL]), {"authorization": "Bearer fixture-secret"})
    assert reason == "principal_not_allowed"


def test_the_peer_roster_refuses_the_daemon_principal_too():
    from types import SimpleNamespace

    from pseudolife_memory.service import MemoryService
    stand_in = SimpleNamespace(config=SimpleNamespace(coordination=SimpleNamespace(
        enabled=True, allowed_principals=[DAEMON_PRINCIPAL], awareness_limit=5)))
    out = MemoryService.coordination_awareness(stand_in, principal=DAEMON_PRINCIPAL)
    assert out["reason"] == "principal_not_allowed" and out["peers"] == []


@pytest.mark.parametrize("allowed, tokens, warned", [
    (["default", "daemon"], {}, True),
    (["default"], {"fixture-secret": "daemon"}, True),
    (["default", "editor"], {"fixture-secret": "editor"}, False),
])
def test_startup_warns_when_a_configured_principal_is_named_daemon(allowed, tokens, warned):
    from pseudolife_memory.daemon import reserved_principal_warnings
    out = reserved_principal_warnings(allowed, tokens)
    assert bool(out) is warned
    assert all("daemon" in line and "board" in line for line in out)
    assert all("fixture-secret" not in line for line in out)


# ── the label "daemon" belongs to the daemon ──────────────────────────────

@pytest.mark.parametrize("label", ["daemon", "Daemon", " DAEMON "])
def test_a_session_cannot_register_or_rename_itself_daemon(coordinating, label):
    from pseudolife_memory.storage.coordination import CoordinationError
    store = _store(coordinating)
    with pytest.raises(CoordinationError, match="invalid_label"):
        store.register(PRINCIPAL, label=label)
    agent = store.register(PRINCIPAL, label="worker")
    with pytest.raises(CoordinationError, match="invalid_label"):
        store.update(PRINCIPAL, agent["agent_id"], agent["credential"], label=label)
    assert store.register(PRINCIPAL, label="daemon-watcher")["label"] == "daemon-watcher"


def test_the_digest_names_daemon_only_for_the_daemons_own_mail(coordinating):
    recipient = _peer(coordinating)
    store = _store(coordinating)
    spoof = _peer(coordinating)
    # A row labelled "daemon" before the refusal existed.
    store.storage.conn.execute("UPDATE coordination_agents SET label='daemon' "
                               "WHERE agent_id=%s", (spoof["agent_id"],))
    store.send(PRINCIPAL, spoof["agent_id"], spoof["credential"],
               to=recipient["agent_id"], text="pretend notice", request_id="r1")
    daemon_notice(coordinating, "real notice")
    preview = {p["excerpt"]: p for p in _store(coordinating)._pending_preview(
        recipient["agent_id"])}
    assert preview["real notice"]["sender_label"] == "daemon"
    assert preview["real notice"]["sender_principal"] == DAEMON_PRINCIPAL
    assert preview["pretend notice"]["sender_label"] == ""
    assert preview["pretend notice"]["sender_principal"] == PRINCIPAL
