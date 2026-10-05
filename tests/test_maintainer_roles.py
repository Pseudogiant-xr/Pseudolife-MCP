"""Board roles from the Console, proven by a passkey (schema v54).

Spec: docs/superpowers/specs/2026-10-04-board-roles-passkey.md. Four signed
purposes complete at ``maintainer_role`` (``POST /api/maintainer/role``):
grant-delegate, revoke-delegate, assign-coordinator, revoke-coordinator. A
bearer alone cannot change a role; the delegate's authority counts with an
operator's or the maintainer's grant record, and nothing else.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pseudolife_memory import coordination
from pseudolife_memory.coordination import dispatch
from pseudolife_memory.storage import coordination as coordination_store
from pseudolife_memory.storage.maintainer import challenge_bytes
from tests.maintainer_authenticator import SoftAuthenticator
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_maintainer_messages import (  # noqa: F401
    board, bootstrap, call, headers_of, peer, refused, sql, svc,
)

HOUR = 3600


def role(service, auth, purpose, **fields):
    challenge = call(service, "challenge", {"purpose": purpose, **fields})
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    return call(service, "role", body), challenge


def holder(service, name):
    rows = sql(service, "SELECT holder_agent_id FROM coordination_leases WHERE name=%s", (name,))
    return rows[0][0] if rows else None


def events(service, kind):
    return [(r[0], json.loads(r[1]), r[2]) for r in sql(
        service, "SELECT actor,payload,agent_id FROM coordination_events WHERE event=%s "
                 "ORDER BY seq", (kind,))]


# ── each purpose ───────────────────────────────────────────────────────────

def test_grant_and_revoke_the_delegate(svc):
    auth = bootstrap(svc)
    agent, other = peer(svc), peer(svc, label="other")
    out, challenge = role(svc, auth, "grant-delegate", project="p", agent_id=agent["agent_id"],
                          hold=HOUR)
    assert set(out) == {"name", "agent_id", "fence", "expires_at", "replaced", "reachable",
                        "reason", "warning"}
    assert (out["name"], out["agent_id"], out["replaced"]) == ("delegate:p", agent["agent_id"],
                                                               None)
    preview = challenge["preview"]
    assert (preview["role"], preview["action"], preview["project"]) == ("delegate", "grant", "p")
    assert preview["agent_id_prefix"] == agent["agent_id"][:12] and preview["replaces"] is None
    [(actor, payload, agent_id)] = events(svc, "lease_delegate")
    assert (actor, agent_id, payload["fence"]) == ("maintainer", agent["agent_id"], out["fence"])
    # A new grant replaces the old one and says whom it replaced.
    out, challenge = role(svc, auth, "grant-delegate", project="p",
                          agent_id=other["agent_id"], hold=HOUR)
    assert challenge["preview"]["replaces"] == agent["agent_id"]
    assert out["replaced"] == agent["agent_id"] and holder(svc, "delegate:p") == other["agent_id"]
    out, challenge = role(svc, auth, "revoke-delegate", project="p")
    assert challenge["preview"]["current_holder"] == other["agent_id"]
    assert out == {"name": "delegate:p", "broken": True, "was_held_by": other["agent_id"]}
    assert events(svc, "lease_break")[-1][0] == "maintainer"
    assert holder(svc, "delegate:p") is None


def test_extending_the_delegate_is_a_new_hold_from_now(svc):
    auth = bootstrap(svc)
    agent = peer(svc)
    first, _ = role(svc, auth, "grant-delegate", project="p", agent_id=agent["agent_id"],
                    hold=HOUR)
    again, _ = role(svc, auth, "grant-delegate", project="p", agent_id=agent["agent_id"],
                    hold=8 * HOUR)
    assert again["replaced"] == agent["agent_id"] and again["fence"] > first["fence"]
    assert again["expires_at"] - first["expires_at"] >= 7 * HOUR - 60


def test_assign_and_revoke_the_coordinator(svc):
    auth = bootstrap(svc)
    agent, squatter, waiter = peer(svc), peer(svc, label="squatter"), peer(svc, label="waiter")
    # The open lease, claimed the ordinary way, with a queue behind it.
    store = board(svc)
    store.acquire_lease(PRINCIPAL, squatter["agent_id"], squatter["credential"],
                        name="coordinator:p", ttl=HOUR)
    store.acquire_lease(PRINCIPAL, waiter["agent_id"], waiter["credential"],
                        name="coordinator:p", ttl=HOUR)
    out, challenge = role(svc, auth, "assign-coordinator", project="p",
                          agent_id=agent["agent_id"], hold=HOUR)
    assert challenge["preview"]["replaces"] == squatter["agent_id"]
    assert (out["name"], out["agent_id"], out["replaced"]) == ("coordinator:p",
                                                               agent["agent_id"],
                                                               squatter["agent_id"])
    [(actor, payload, _)] = events(svc, "lease_assign")
    assert actor == "maintainer" and payload["replaced"] == squatter["agent_id"]
    # Afterwards an ordinary lease: the holder renews it, others stay queued.
    renewed = store.acquire_lease(PRINCIPAL, agent["agent_id"], agent["credential"],
                                  name="coordinator:p", ttl=HOUR)
    assert renewed["state"] == "held" and renewed["queued"] == 1
    out, _ = role(svc, auth, "revoke-coordinator", project="p")
    assert out == {"name": "coordinator:p", "broken": True, "was_held_by": agent["agent_id"]}
    # The next queued waiter takes it.
    assert holder(svc, "coordinator:p") == waiter["agent_id"]


def test_revoking_a_free_role_reports_nothing_broken(svc):
    auth = bootstrap(svc)
    out, _ = role(svc, auth, "revoke-coordinator", project="p")
    assert out == {"name": "coordinator:p", "broken": False, "was_held_by": None}


def test_status_lists_live_roles_per_project(svc):
    auth = bootstrap(svc)
    a, b = peer(svc), peer(svc, label="b")
    role(svc, auth, "grant-delegate", project="p", agent_id=a["agent_id"], hold=HOUR)
    role(svc, auth, "assign-coordinator", project="p", agent_id=b["agent_id"], hold=HOUR)
    roles = call(svc, "status")["roles"]
    assert set(roles) == {"p"}
    assert roles["p"]["delegate"]["agent_id"] == a["agent_id"]
    assert roles["p"]["delegate"]["granted_by"] == "maintainer"
    assert roles["p"]["coordinator"]["agent_id"] == b["agent_id"]
    assert set(roles["p"]["coordinator"]) == {"agent_id", "expires_at", "reachable", "reason"}


def test_status_names_an_operator_grant_and_skips_an_unrecorded_hold(svc):
    a, b = peer(svc, project="q"), peer(svc, project="r")
    board(svc).grant_delegate("q", a["agent_id"], hold=HOUR)
    # A hold on delegate:r with no grant record (an ordinary lease from
    # before the namespace was reserved) grants nothing and is not listed.
    sql(svc, "INSERT INTO coordination_leases (name,holder_agent_id,holder_principal,fence,"
             "acquired_at,expires_at) VALUES ('delegate:r',%s,%s,999,0,9e12)",
        (b["agent_id"], PRINCIPAL))
    roles = call(svc, "status")["roles"]
    assert roles == {"q": {"delegate": {"agent_id": a["agent_id"],
                                        "expires_at": roles["q"]["delegate"]["expires_at"],
                                        "granted_by": "operator", "reachable": False,
                                        "reason": "wake_disabled"},
                           "coordinator": None}}


# ── reachability: maintainer mail lands only through a live listener ──────

def test_a_grant_warns_when_the_grantee_has_no_live_listener(svc):
    """Maintainer requirement 2026-10-05: the delegate above all must be
    reachable. A grant (and an extend, which signs a new grant) says whether
    its grantee has a live wake path now, and warns when maintainer mail to
    it would wait for its next turn."""
    auth = bootstrap(svc)
    deaf, live = peer(svc), peer(svc, wake=True, label="live")
    out, _ = role(svc, auth, "grant-delegate", project="p", agent_id=deaf["agent_id"], hold=HOUR)
    assert (out["reachable"], out["reason"]) == (False, "wake_disabled")
    assert out["warning"] == coordination_store.UNREACHABLE_WARNING.format(
        role="delegate", reason=coordination_store.UNREACHABLE_REASONS["wake_disabled"])
    out, _ = role(svc, auth, "grant-delegate", project="p", agent_id=live["agent_id"], hold=HOUR)
    assert (out["reachable"], out["reason"]) == (True, None) and "warning" not in out
    out, _ = role(svc, auth, "assign-coordinator", project="p", agent_id=deaf["agent_id"],
                  hold=HOUR)
    assert out["reachable"] is False and "coordinator" in out["warning"]


def test_status_shows_whether_each_role_holder_is_reachable_now(svc):
    """The Roles band reads this: a Claude Code Stop-hook watcher or a Codex
    doorbell renews ``ring_armed_until`` about a minute ahead, so a holder
    whose listener stopped shows as unreachable within a minute."""
    store = board(svc)
    agent = store.register(PRINCIPAL, project="p", label="claude")
    with svc._coordination_lock:
        attached = store.attach(PRINCIPAL, agent["agent_id"], agent["credential"],
                                attachment_id="a1", ring=True,
                                ring_armed_until=store.clock() + 30)
    store.grant_delegate("p", agent["agent_id"], hold=HOUR)
    delegate = call(svc, "status")["roles"]["p"]["delegate"]
    assert (delegate["reachable"], delegate["reason"]) == (True, None)
    with svc._coordination_lock:
        store.heartbeat(PRINCIPAL, agent["agent_id"], agent["credential"], attachment_id="a1",
                        generation=attached["generation"], ring_armed_until=0)
    delegate = call(svc, "status")["roles"]["p"]["delegate"]
    assert (delegate["reachable"], delegate["reason"]) == (False, "listener_expired")


# ── one role per session ───────────────────────────────────────────────────

def test_granting_the_delegate_to_the_coordinator_breaks_its_coordinator_lease(svc):
    auth = bootstrap(svc)
    agent, waiter = peer(svc), peer(svc, label="waiter")
    role(svc, auth, "assign-coordinator", project="p", agent_id=agent["agent_id"], hold=HOUR)
    board(svc).acquire_lease(PRINCIPAL, waiter["agent_id"], waiter["credential"],
                             name="coordinator:p", ttl=HOUR)
    out, challenge = role(svc, auth, "grant-delegate", project="p",
                          agent_id=agent["agent_id"], hold=HOUR)
    assert challenge["preview"]["also_breaks"] == "coordinator:p"
    assert out["also_broken"] == "coordinator:p"
    assert holder(svc, "delegate:p") == agent["agent_id"]
    assert holder(svc, "coordinator:p") == waiter["agent_id"]
    (actor, payload, agent_id) = events(svc, "lease_break")[-1]
    assert (actor, agent_id, payload["reason"]) == ("maintainer", agent["agent_id"], "one_role")


def test_assigning_the_coordinator_to_the_delegate_breaks_its_delegate_lease(svc):
    auth = bootstrap(svc)
    agent = peer(svc)
    role(svc, auth, "grant-delegate", project="p", agent_id=agent["agent_id"], hold=HOUR)
    out, _ = role(svc, auth, "assign-coordinator", project="p", agent_id=agent["agent_id"],
                  hold=HOUR)
    assert out["also_broken"] == "delegate:p"
    assert holder(svc, "coordinator:p") == agent["agent_id"]
    assert holder(svc, "delegate:p") is None


def test_the_operator_cli_grant_keeps_the_one_role_rule(svc):
    agent = peer(svc)
    store = board(svc)
    store.acquire_lease(PRINCIPAL, agent["agent_id"], agent["credential"],
                        name="coordinator:p", ttl=HOUR)
    out = store.grant_delegate("p", agent["agent_id"], hold=HOUR)
    assert out["also_broken"] == "coordinator:p"
    assert events(svc, "lease_break")[-1][0] == "operator"


def test_the_delegate_cannot_claim_or_queue_for_the_coordinator_lease(svc):
    """Security review 2026-10-04: the one-role rule held only on the signed
    routes, so a delegate took ``coordinator:<project>`` with an ordinary
    claim and held both roles."""
    auth = bootstrap(svc)
    agent, other = peer(svc), peer(svc, label="other")
    role(svc, auth, "grant-delegate", project="p", agent_id=agent["agent_id"], hold=HOUR)
    with pytest.raises(coordination.CoordinationRefused, match="already_delegate"):
        dispatch(svc, "lease", {"name": "coordinator:p", "ttl": HOUR},
                 headers=headers_of(agent), principal=PRINCIPAL)
    assert holder(svc, "coordinator:p") is None
    # Held by another session, the delegate cannot queue for it either.
    board(svc).acquire_lease(PRINCIPAL, other["agent_id"], other["credential"],
                             name="coordinator:p", ttl=HOUR)
    with pytest.raises(coordination.CoordinationRefused, match="already_delegate"):
        dispatch(svc, "lease", {"name": "coordinator:p", "ttl": HOUR},
                 headers=headers_of(agent), principal=PRINCIPAL)
    assert sql(svc, "SELECT agent_id FROM coordination_lease_waiters") == []
    assert holder(svc, "delegate:p") == agent["agent_id"]
    # Another project's coordinator lease is not a second role here.
    dispatch(svc, "lease", {"name": "coordinator:q", "ttl": HOUR},
             headers=headers_of(agent), principal=PRINCIPAL)
    assert holder(svc, "coordinator:q") == agent["agent_id"]


def test_a_session_holding_both_roles_from_before_v54_still_renews_its_coordinator_lease(svc):
    """Review of #569, 2026-10-05: before v54 a session could hold both
    ``delegate:<p>`` and ``coordinator:<p>``. The one-role check ran before
    the renewal branch, so that coordinator's renewal was refused and the
    lease lapsed under it. Renewal by the current holder goes through; a
    new claim after the hold lapsed is still refused."""
    agent = peer(svc)
    store = board(svc)
    store.acquire_lease(PRINCIPAL, agent["agent_id"], agent["credential"],
                        name="coordinator:p", ttl=HOUR)
    now = store.clock()
    sql(svc, "INSERT INTO coordination_leases (name) VALUES ('delegate:p') "
             "ON CONFLICT (name) DO NOTHING")
    sql(svc, "UPDATE coordination_leases SET holder_agent_id=%s,holder_principal=%s,"
             "purpose='granted by the operator',fence=nextval('coordination_lease_fence'),"
             "acquired_at=%s,expires_at=%s WHERE name='delegate:p'",
        (agent["agent_id"], PRINCIPAL, now, now + HOUR))
    before = sql(svc, "SELECT expires_at,fence FROM coordination_leases "
                      "WHERE name='coordinator:p'")[0]
    out = dispatch(svc, "lease", {"name": "coordinator:p", "ttl": 2 * HOUR},
                   headers=headers_of(agent), principal=PRINCIPAL)
    assert out["holder"]["agent_id"] == agent["agent_id"]
    after = sql(svc, "SELECT expires_at,fence FROM coordination_leases "
                     "WHERE name='coordinator:p'")[0]
    assert after[1] == before[1] and after[0] > before[0]
    assert holder(svc, "delegate:p") == agent["agent_id"]
    # Once that hold lapses, claiming it again is a second role.
    sql(svc, "UPDATE coordination_leases SET expires_at=%s WHERE name='coordinator:p'",
        (now - 1,))
    with pytest.raises(coordination.CoordinationRefused, match="already_delegate"):
        dispatch(svc, "lease", {"name": "coordinator:p", "ttl": HOUR},
                 headers=headers_of(agent), principal=PRINCIPAL)


def test_a_coordinator_waiter_made_delegate_leaves_the_queue(svc):
    """A session queued for ``coordinator:<project>`` when it is made the
    delegate leaves that queue, so the lease never passes to the delegate."""
    auth = bootstrap(svc)
    agent, other = peer(svc), peer(svc, label="other")
    store = board(svc)
    store.acquire_lease(PRINCIPAL, other["agent_id"], other["credential"],
                        name="coordinator:p", ttl=HOUR)
    store.acquire_lease(PRINCIPAL, agent["agent_id"], agent["credential"],
                        name="coordinator:p", ttl=HOUR)
    role(svc, auth, "grant-delegate", project="p", agent_id=agent["agent_id"], hold=HOUR)
    (actor, payload, agent_id) = events(svc, "lease_dequeue")[-1]
    assert (actor, agent_id, payload) == ("daemon", agent["agent_id"],
                                          {"name": "coordinator:p", "reason": "one_role"})
    store.release_lease(PRINCIPAL, other["agent_id"], other["credential"], name="coordinator:p")
    assert holder(svc, "coordinator:p") is None
    assert holder(svc, "delegate:p") == agent["agent_id"]


# ── refusals ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("purpose", ["grant-delegate", "revoke-delegate", "assign-coordinator",
                                     "revoke-coordinator"])
@pytest.mark.parametrize("route", ["send", "cancel", "revoke", "repudiate", "enrol"])
def test_a_role_payload_is_refused_on_every_other_route(svc, purpose, route):
    auth = bootstrap(svc)
    agent = peer(svc)
    fields = {"project": "p"}
    if purpose in ("grant-delegate", "assign-coordinator"):
        fields.update(agent_id=agent["agent_id"], hold=HOUR)
    challenge = call(svc, "challenge", {"purpose": purpose, **fields})
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("assertion_invalid", call, svc, route, body)
    assert holder(svc, "delegate:p") is None and holder(svc, "coordinator:p") is None


@pytest.mark.parametrize("purpose,fields", [
    ("send", {"text": "hi"}), ("repudiate", None), ("cancel", {"credential_id": "x" * 22}),
])
def test_other_purposes_are_refused_at_the_role_route(svc, purpose, fields):
    auth = bootstrap(svc)
    agent = peer(svc)
    if purpose == "send":
        fields = {"to": agent["agent_id"], **fields}
    if purpose == "repudiate":
        out, _ = call_send(svc, auth, agent)
        fields = {"message_id": out["message_id"]}
    challenge = call(svc, "challenge", {"purpose": purpose, **fields})
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("assertion_invalid", call, svc, "role", body)


def call_send(svc, auth, agent):
    from tests.test_maintainer_messages import maintainer_send
    return maintainer_send(svc, auth, agent["agent_id"])


@pytest.mark.parametrize("edit", [("hold", 3600, 604800), ("agent_id", None, None),
                                  ("project", '"p"', '"q"')])
def test_a_role_payload_edited_under_a_kept_mac_is_refused(svc, edit):
    auth = bootstrap(svc)
    agent, other = peer(svc), peer(svc, label="other")
    challenge = call(svc, "challenge", {"purpose": "grant-delegate", "project": "p",
                                        "agent_id": agent["agent_id"], "hold": HOUR})
    field, before, after = edit
    if field == "agent_id":
        before, after = agent["agent_id"], other["agent_id"]
    edited = challenge["payload"].replace(str(before), str(after))
    assert edited != challenge["payload"]
    for signed_over in (challenge["payload"], edited):
        body = {"payload": edited, "mac": challenge["mac"],
                "assertion": auth.assertion(challenge_bytes(signed_over))}
        refused("assertion_invalid", call, svc, "role", body)
    assert holder(svc, "delegate:p") is None


@pytest.mark.parametrize("purpose", ["grant-delegate", "assign-coordinator"])
def test_a_recipient_on_another_project_is_refused(svc, purpose):
    bootstrap(svc)
    agent = peer(svc, project="other")
    refused("invalid_request", call, svc, "challenge",
            {"purpose": purpose, "project": "p", "agent_id": agent["agent_id"], "hold": HOUR})


def test_a_recipient_that_moved_project_before_the_tap_is_refused(svc):
    auth = bootstrap(svc)
    agent = peer(svc)
    challenge = call(svc, "challenge", {"purpose": "grant-delegate", "project": "p",
                                        "agent_id": agent["agent_id"], "hold": HOUR})
    sql(svc, "UPDATE coordination_agents SET project='elsewhere' WHERE agent_id=%s",
        (agent["agent_id"],))
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("invalid_request", call, svc, "role", body)
    assert holder(svc, "delegate:p") is None


@pytest.mark.parametrize("purpose", ["grant-delegate", "assign-coordinator"])
@pytest.mark.parametrize("kind", ["subagent", "reserved", "unknown", "prefix", "revoked"])
def test_subagent_reserved_and_unknown_recipients_are_refused(svc, purpose, kind):
    bootstrap(svc)
    agent = peer(svc, attach=False)
    code = "recipient_unknown"
    target = agent["agent_id"]
    if kind == "subagent":
        sql(svc, "UPDATE coordination_agents SET parent_thread=%s WHERE agent_id=%s",
            ("00000000-0000-0000-0000-000000000001", target))
        code = "recipient_reserved"
    elif kind == "reserved":
        coordination.daemon_notice(svc, "hello")
        target = svc._daemon_board_identity[0]
        code = "recipient_reserved"
    elif kind == "unknown":
        target = "f" * 32
    elif kind == "prefix":
        target = target[:12]
    else:
        sql(svc, "UPDATE coordination_agents SET credential_hash=NULL WHERE agent_id=%s",
            (target,))
    refused(code, call, svc, "challenge",
            {"purpose": purpose, "project": "p", "agent_id": target, "hold": HOUR})


@pytest.mark.parametrize("hold", [59, 604801, 3600.0, "3600", True, None])
def test_a_hold_out_of_range_is_refused(svc, hold):
    bootstrap(svc)
    agent = peer(svc)
    refused("invalid_request", call, svc, "challenge",
            {"purpose": "grant-delegate", "project": "p", "agent_id": agent["agent_id"],
             "hold": hold})


@pytest.mark.parametrize("project", ["", " p", "p ", None, 7, "p" * 109])
def test_a_bad_project_is_refused(svc, project):
    bootstrap(svc)
    refused("invalid_request", call, svc, "challenge",
            {"purpose": "revoke-delegate", "project": project})


def test_a_role_from_a_quarantined_key_is_refused(svc):
    from tests.test_maintainer_messages import _enrol_second
    first = bootstrap(svc)
    newcomer, _ = _enrol_second(svc, first)
    agent = peer(svc)
    refused("assertion_invalid", role, svc, newcomer, "grant-delegate", project="p",
            agent_id=agent["agent_id"], hold=HOUR)


def test_a_spent_role_payload_is_refused(svc):
    auth = bootstrap(svc)
    agent = peer(svc)
    _, challenge = role(svc, auth, "revoke-delegate", project="p")
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("challenge_spent", call, svc, "role", body)
    assert agent


# ── no bearer-only path changes a role ─────────────────────────────────────

def test_a_bearer_cannot_claim_the_delegate_lease(svc, monkeypatch):
    """MCP ``memory_agents claim`` on ``delegate:<project>`` stays
    ``reserved_lease`` (the tool's claim is dispatch's ``lease``)."""
    from pseudolife_memory.writer_context import bind_request_headers, unbind_request_headers
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", f"role-test-bearer:{PRINCIPAL}")
    agent = peer(svc)
    with pytest.raises(coordination.CoordinationRefused, match="reserved_lease"):
        dispatch(svc, "lease", {"name": "delegate:p", "ttl": HOUR},
                 headers=headers_of(agent), principal=PRINCIPAL)
    token = bind_request_headers({"authorization": "Bearer role-test-bearer",
                                  **headers_of(agent)}, principal=PRINCIPAL)
    try:
        with pytest.raises(coordination.CoordinationRefused, match="reserved_lease"):
            coordination.agents(svc, action="claim", lease="delegate:p")
    finally:
        unbind_request_headers(token)
    assert holder(svc, "delegate:p") is None


_ROLE_METHODS = {"grant_delegate", "assign_coordinator", "break_lease"}
# The only modules that may call a role change: the host CLI (the operator,
# who opens the bank directly) and the passkey route, which calls them only
# inside ``MaintainerStore.complete`` (the assertion is verified first).
_ROLE_CALLERS = {"lease_cli.py", "maintainer.py"}


def test_no_bearer_route_reaches_a_role_change():
    root = Path(__file__).resolve().parent.parent / "pseudolife_memory"
    callers = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # Any reference, called or not (a method picked by a conditional).
            if isinstance(node, ast.Attribute) and node.attr in _ROLE_METHODS:
                callers.setdefault(path.relative_to(root).as_posix(), set()).add(node.attr)
    # storage/coordination.py defines them; nothing in web/ or the MCP
    # server calls them.
    assert set(callers) <= _ROLE_CALLERS, callers
    assert callers.get("maintainer.py") == _ROLE_METHODS
    source = (root / "maintainer.py").read_text(encoding="utf-8")
    role_handler = source[source.index("def _role("):source.index("def _repudiate(")]
    assert "store.complete(" in role_handler
    for method in _ROLE_METHODS:
        assert role_handler.index(method) < role_handler.index("store.complete(")


def test_the_routes_reach_role_changes_only_through_the_signed_method():
    from pseudolife_memory.web.routes import ConsoleRoutes
    from pseudolife_memory.web.fixtures import FixtureService
    routes = ConsoleRoutes(FixtureService())
    maintainer_paths = {path for (_m, path) in routes.table if path.startswith("/api/maintainer")}
    assert maintainer_paths == {
        "/api/maintainer", "/api/maintainer/challenge", "/api/maintainer/enrol",
        "/api/maintainer/send", "/api/maintainer/role", "/api/maintainer/cancel",
        "/api/maintainer/revoke", "/api/maintainer/repudiate", "/api/maintainer/sent",
        "/api/maintainer/inbox"}


# ── the delegate's authority ───────────────────────────────────────────────

def _authority(svc, sender, recipient):
    with svc._coordination_lock:
        store = coordination._store(svc)
        rows = {r["agent_id"]: r for r in store._all(
            "SELECT * FROM coordination_agents WHERE agent_id=ANY(%s)",
            ([sender["agent_id"], recipient["agent_id"]],))}
        return store._authority(rows[sender["agent_id"]], rows[recipient["agent_id"]],
                                store.clock())


def test_authority_honours_a_maintainer_grant_and_an_operator_grant(svc):
    auth = bootstrap(svc)
    delegate, recipient = peer(svc), peer(svc, label="recipient")
    role(svc, auth, "grant-delegate", project="p", agent_id=delegate["agent_id"], hold=HOUR)
    assert _authority(svc, delegate, recipient) == "delegate"
    board(svc).grant_delegate("p", delegate["agent_id"], hold=HOUR)
    assert _authority(svc, delegate, recipient) == "delegate"


def test_authority_refuses_a_plain_hold_and_a_record_by_any_other_actor(svc):
    delegate, recipient = peer(svc), peer(svc, label="recipient")
    sql(svc, "INSERT INTO coordination_leases (name,holder_agent_id,holder_principal,fence,"
             "acquired_at,expires_at) VALUES ('delegate:p',%s,%s,4242,0,9e12)",
        (delegate["agent_id"], PRINCIPAL))
    assert _authority(svc, delegate, recipient) is None
    with svc._coordination_lock:
        store = coordination._store(svc)
        with store.storage._txn():
            store._append([store._event("lease_delegate", {"name": "delegate:p", "fence": 4242,
                                                           "hold": HOUR, "replaced": None},
                                        actor="agent", agent_id=delegate["agent_id"])],
                          store.clock())
    assert _authority(svc, delegate, recipient) is None


def test_only_the_role_actors_may_write_a_role_record(svc):
    agent = peer(svc)
    store = board(svc)
    for change in (lambda: store.grant_delegate("p", agent["agent_id"], hold=HOUR,
                                                actor="agent"),
                   lambda: store.assign_coordinator("p", agent["agent_id"], hold=HOUR,
                                                    actor="agent"),
                   lambda: store.break_lease("coordinator:p", actor="agent")):
        with pytest.raises(coordination.CoordinationError, match="invalid_request"):
            change()
    assert events(svc, "lease_delegate") == [] and events(svc, "lease_assign") == []


def test_a_role_route_without_an_assertion_changes_nothing(svc):
    bootstrap(svc)
    agent = peer(svc)
    challenge = call(svc, "challenge", {"purpose": "grant-delegate", "project": "p",
                                        "agent_id": agent["agent_id"], "hold": HOUR})
    for assertion in (None, {}, {"id": SoftAuthenticator().id}):
        refused("assertion_invalid", call, svc, "role",
                {"payload": challenge["payload"], "mac": challenge["mac"],
                 "assertion": assertion})
    assert holder(svc, "delegate:p") is None


def test_a_revoke_signed_for_one_holder_cannot_evict_another(svc):
    """Review 2026-10-04: the holder a revoke or grant replaces is decided
    by the signed payload too. A session that swaps in during the 120 s
    window is not evicted by a revoke the maintainer approved for another."""
    auth = bootstrap(svc)
    a, b = peer(svc), peer(svc, label="b")
    store = board(svc)
    store.acquire_lease(PRINCIPAL, a["agent_id"], a["credential"], name="coordinator:p", ttl=HOUR)
    challenge = call(svc, "challenge", {"purpose": "revoke-coordinator", "project": "p"})
    assert json.loads(challenge["payload"])["holder"] == a["agent_id"]
    store.release_lease(PRINCIPAL, a["agent_id"], a["credential"], name="coordinator:p")
    store.acquire_lease(PRINCIPAL, b["agent_id"], b["credential"], name="coordinator:p", ttl=HOUR)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("role_changed", call, svc, "role", body)
    assert holder(svc, "coordinator:p") == b["agent_id"]


def test_a_grant_signed_over_a_free_slot_does_not_replace_a_newcomer(svc):
    auth = bootstrap(svc)
    a, b = peer(svc), peer(svc, label="b")
    challenge = call(svc, "challenge", {"purpose": "grant-delegate", "project": "p",
                                        "agent_id": a["agent_id"], "hold": HOUR})
    assert json.loads(challenge["payload"])["holder"] is None
    board(svc).grant_delegate("p", b["agent_id"], hold=HOUR)     # the operator, meanwhile
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("role_changed", call, svc, "role", body)
    assert holder(svc, "delegate:p") == b["agent_id"]


def test_a_grant_whose_grantee_took_the_coordinator_lease_since_the_preview_is_refused(svc):
    """Review of #569, 2026-10-05: the preview said ``also_breaks: null``,
    then the grantee claimed ``coordinator:<p>`` before the tap, and the
    grant broke it anyway. The other role's holder is signed too."""
    auth = bootstrap(svc)
    agent = peer(svc)
    challenge = call(svc, "challenge", {"purpose": "grant-delegate", "project": "p",
                                        "agent_id": agent["agent_id"], "hold": HOUR})
    assert challenge["preview"]["also_breaks"] is None
    board(svc).acquire_lease(PRINCIPAL, agent["agent_id"], agent["credential"],
                             name="coordinator:p", ttl=HOUR)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("role_changed", call, svc, "role", body)
    assert holder(svc, "coordinator:p") == agent["agent_id"]
    assert holder(svc, "delegate:p") is None


def test_an_assign_whose_assignee_was_made_delegate_since_the_preview_is_refused(svc):
    auth = bootstrap(svc)
    agent = peer(svc)
    challenge = call(svc, "challenge", {"purpose": "assign-coordinator", "project": "p",
                                        "agent_id": agent["agent_id"], "hold": HOUR})
    assert challenge["preview"]["also_breaks"] is None
    board(svc).grant_delegate("p", agent["agent_id"], hold=HOUR)  # the operator, meanwhile
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    refused("role_changed", call, svc, "role", body)
    assert holder(svc, "delegate:p") == agent["agent_id"]
    assert holder(svc, "coordinator:p") is None
