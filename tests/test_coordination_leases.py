"""Board leases (schema v45): a named, expiring hold on a shared resource.

A process-held lease's truth is an OS file lock on the host (``pseudolife-mcp
lease run``); the board row mirrors it so peers see the holder, queue in
arrival order and see the expected end. A session-held lease (``coordinator``,
``claim:<path>``) lives only here. Either way the board grants a freed lease
to the head of its queue, which must renew within a short window or lose the
grant to the next in line: a sleeping session cannot stall the queue the way
the 2026-09-24 02:39 relay stall did.
"""
from contextlib import contextmanager
import json

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_repository_claims import repositories  # noqa: F401
from pseudolife_memory.storage.coordination import (
    COORDINATION_SCHEMA_SQL, LEASE_GRANT_WINDOW, LEASE_LIST_QUEUE, LEASE_QUEUE_MAX, CoordinationError,
    CoordinationStore, audit_events, verify_audit_chain,
)


class Storage:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


@pytest.fixture
def store(pg_conn):
    pg_conn.autocommit = True
    pg_conn.execute(COORDINATION_SCHEMA_SQL)
    from pseudolife_memory.storage.schema import assert_disposable_database
    assert_disposable_database(pg_conn)
    pg_conn.execute("TRUNCATE coordination_messages, coordination_agents, coordination_events, "
                    "coordination_leases, coordination_lease_waiters, coordination_wakes")
    # A fresh bank's fences start at 1; the sequence outlives TRUNCATE.
    pg_conn.execute("ALTER SEQUENCE coordination_lease_fence RESTART WITH 1")
    now = [1000.0]
    out = CoordinationStore(Storage(pg_conn), clock=lambda: now[0])
    out.test_time = now
    return out


def creds(agent, principal="alice"):
    return principal, agent["agent_id"], agent["credential"]


def events(store, kind=None):
    rows = list(audit_events(store.storage.conn))
    return [r for r in rows if kind is None or r["event"] == kind]


def payload(row):
    return json.loads(row["payload"])


def test_free_lease_is_granted_with_fence_and_expiry(store):
    a = store.register("alice", label="suite-runner")
    out = store.acquire_lease(*creds(a), name="full-suite", ttl=120, expect=1200,
                              purpose="pytest tests/")
    assert out["state"] == "held"
    base = out["fence"]
    assert base >= 1
    assert out["expires_at"] == 1120.0
    assert out["expected_end"] == 2200.0
    assert out["position"] is None and out["queued"] == 0
    assert out["holder"]["agent_id"] == a["agent_id"]
    assert out["holder"]["label"] == "suite-runner"
    assert out["holder"]["purpose"] == "pytest tests/"
    [row] = events(store, "lease_acquire")
    assert row["agent_id"] == a["agent_id"]
    assert payload(row) == {"name": "full-suite", "fence": base, "ttl": 120, "expect": 1200,
                            "purpose": "pytest tests/"}


def test_exact_file_claims_share_fifo_expiry_and_fencing(store, repositories):
    from pseudolife_memory.repository_claims import prepare_file_claim, file_claim_name
    main, linked, other = repositories
    name = file_claim_name(**prepare_file_claim(str(main), "src/file.py"))
    same = file_claim_name(**prepare_file_claim(str(linked), "src\\file.py"))
    unrelated = file_claim_name(**prepare_file_claim(str(other), "src/file.py"))
    a, b, c = [store.register("alice", label=label) for label in ("editor", "next", "last")]
    held = store.acquire_lease(*creds(a), name=name, ttl=120)
    first = store.acquire_lease(*creds(b), name=same, ttl=120)
    second = store.acquire_lease(*creds(c), name=same, ttl=120)
    assert first["position"] == 1 and second["position"] == 2
    assert first["holder"]["agent_id"] == a["agent_id"]
    assert first["holder"]["fence"] == held["fence"]
    assert first["holder"]["expires_at"] == held["expires_at"]
    assert store.acquire_lease(*creds(b), name=unrelated, ttl=120)["state"] == "held"
    store.test_time[0] = held["expires_at"]
    taken = store.acquire_lease(*creds(b), name=same, ttl=120)
    assert taken["state"] == "held" and taken["fence"] > held["fence"]
    # The former holder cannot release another session's new generation.
    with pytest.raises(CoordinationError, match="lease_not_held"):
        store.release_lease(*creds(a), name=name)
    released = store.release_lease(*creds(b), name=same)
    assert released["released"]
    last = store.acquire_lease(*creds(c), name=name, ttl=120)
    assert last["state"] == "held" and last["fence"] > taken["fence"]


def test_second_agent_queues_once_and_sees_the_holder(store):
    a, b = store.register("alice", label="a"), store.register("alice", label="b")
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    first = store.acquire_lease(*creds(b), name="gpu", ttl=120, purpose="bench")
    assert first["state"] == "queued"
    assert first["position"] == 1 and first["queued"] == 1
    assert first["fence"] is None
    assert first["holder"]["agent_id"] == a["agent_id"]
    store.test_time[0] += 5
    again = store.acquire_lease(*creds(b), name="gpu", ttl=120)
    assert again["position"] == 1 and again["queued"] == 1
    # A repeat poll keeps its place and writes nothing.
    assert len(events(store, "lease_queue")) == 1


def test_holder_renewal_moves_expiry_without_an_audit_row(store):
    a = store.register("alice")
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    before = len(events(store))
    store.test_time[0] += 60
    out = store.acquire_lease(*creds(a), name="gpu", ttl=120)
    assert out["state"] == "held" and out["fence"] == 1
    assert out["expires_at"] == 1180.0
    assert out["expected_end"] is None
    assert len(events(store)) == before


def test_release_grants_the_queue_head_with_an_acceptance_window(store):
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="full-suite", ttl=120)
    store.acquire_lease(*creds(b), name="full-suite", ttl=3600, expect=900)
    store.test_time[0] += 1
    store.acquire_lease(*creds(c), name="full-suite", ttl=120)
    store.test_time[0] += 10
    out = store.release_lease(*creds(a), name="full-suite")
    assert out == {"name": "full-suite", "released": True, "dequeued": False}
    view = store.acquire_lease(*creds(c), name="full-suite", ttl=120)
    assert view["state"] == "queued" and view["position"] == 1
    holder = view["holder"]
    assert holder["agent_id"] == b["agent_id"]
    # The grantee has the shorter of its ttl and the window to take it up.
    assert holder["expires_at"] == store.test_time[0] + LEASE_GRANT_WINDOW
    assert holder["expected_end"] == store.test_time[0] + 900
    [grant] = events(store, "lease_grant")
    assert grant["agent_id"] == b["agent_id"]
    assert payload(grant)["fence"] == 2
    assert [e["event"] for e in events(store)][-2:] == ["lease_release", "lease_grant"]
    taken = store.acquire_lease(*creds(b), name="full-suite", ttl=3600)
    assert taken["state"] == "held" and taken["fence"] == 2
    assert taken["expires_at"] == store.test_time[0] + 3600


def test_grant_lapses_to_the_next_waiter_when_not_taken(store):
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.acquire_lease(*creds(b), name="gpu", ttl=120)
    store.test_time[0] += 1
    store.acquire_lease(*creds(c), name="gpu", ttl=120)
    store.release_lease(*creds(a), name="gpu")
    store.test_time[0] += 121  # b's grant (ttl 120 < window) lapses untaken
    out = store.acquire_lease(*creds(c), name="gpu", ttl=120)
    assert out["state"] == "held" and out["fence"] == 3
    expired = events(store, "lease_expire")
    assert [e["agent_id"] for e in expired] == [b["agent_id"]]
    # b lost it: asking again queues b behind nobody but the holder.
    again = store.acquire_lease(*creds(b), name="gpu", ttl=120)
    assert again["state"] == "queued" and again["position"] == 1


def test_expired_holder_loses_the_lease_to_the_queue(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="live-daemon", ttl=60)
    store.acquire_lease(*creds(b), name="live-daemon", ttl=60)
    store.test_time[0] += 61
    out = store.acquire_lease(*creds(b), name="live-daemon", ttl=60)
    assert out["state"] == "held" and out["fence"] == 2
    lost = store.acquire_lease(*creds(a), name="live-daemon", ttl=60)
    assert lost["state"] == "queued" and lost["position"] == 1


def test_fence_rises_on_every_grant(store):
    a = store.register("alice")
    fences = []
    for _ in range(3):
        fences.append(store.acquire_lease(*creds(a), name="coordinator:p", ttl=3600)["fence"])
        store.release_lease(*creds(a), name="coordinator:p")
    assert fences == [1, 2, 3]


def test_release_by_a_waiter_dequeues_and_by_a_stranger_refuses(store):
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.acquire_lease(*creds(b), name="gpu", ttl=120)
    assert store.release_lease(*creds(b), name="gpu") == {
        "name": "gpu", "released": False, "dequeued": True}
    assert len(events(store, "lease_dequeue")) == 1
    with pytest.raises(CoordinationError, match="lease_not_held"):
        store.release_lease(*creds(c), name="gpu")
    with pytest.raises(CoordinationError, match="lease_not_held"):
        store.release_lease(*creds(c), name="never-existed")


@pytest.mark.parametrize("kwargs,code", [
    ({"name": ""}, "invalid_lease"),
    ({"name": "x" * 121}, "invalid_lease"),
    ({"name": "bad\nname"}, "invalid_lease"),
    ({"name": 7}, "invalid_lease"),
    ({"name": "gpu", "ttl": 29}, "invalid_ttl"),
    ({"name": "gpu", "ttl": 86401}, "invalid_ttl"),
    ({"name": "gpu", "ttl": True}, "invalid_ttl"),
    ({"name": "gpu", "ttl": 60.5}, "invalid_ttl"),
    ({"name": "gpu", "expect": 0}, "invalid_expect"),
    ({"name": "gpu", "expect": 7 * 86400 + 1}, "invalid_expect"),
    ({"name": "gpu", "purpose": "p" * 241}, "invalid_purpose"),
])
def test_lease_arguments_are_validated(store, kwargs, code):
    a = store.register("alice")
    with pytest.raises(CoordinationError, match=code):
        store.acquire_lease(*creds(a), **{"ttl": 120, **kwargs})
    assert events(store, "lease_acquire") == []


def test_queue_is_bounded(store):
    holder = store.register("alice")
    store.acquire_lease(*creds(holder), name="gpu", ttl=120)
    for _ in range(LEASE_QUEUE_MAX):
        store.acquire_lease(*creds(store.register("alice")), name="gpu", ttl=120)
    with pytest.raises(CoordinationError, match="lease_queue_full"):
        store.acquire_lease(*creds(store.register("alice")), name="gpu", ttl=120)


def test_wrong_credential_cannot_acquire_or_release(store):
    a, b = store.register("alice"), store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.acquire_lease("alice", a["agent_id"], b["credential"], name="gpu", ttl=120)
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.release_lease("mallory", a["agent_id"], a["credential"], name="gpu")


def test_list_shows_held_and_queued_leases_with_staleness(store):
    a, b = store.register("alice", label="runner"), store.register("alice", label="next")
    store.acquire_lease(*creds(a), name="full-suite", ttl=3600, expect=600, purpose="suite")
    store.acquire_lease(*creds(b), name="full-suite", ttl=120, purpose="my turn")
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.release_lease(*creds(a), name="gpu")  # free rows are not listed
    listed = store.list_leases()
    assert [lease["name"] for lease in listed["leases"]] == ["full-suite"]
    [lease] = listed["leases"]
    assert lease["holder"]["label"] == "runner" and lease["fence"] == 1
    assert lease["stale"] is False
    assert lease["queued"] == 1
    assert [w["label"] for w in lease["queue"]] == ["next"]
    assert lease["queue"][0]["purpose"] == "my turn"
    store.test_time[0] += 601
    assert store.list_leases(name="full-suite")["leases"][0]["stale"] is True
    assert store.list_leases(name="gpu")["leases"] == []


def test_list_settles_an_expired_hold_before_reporting(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="gpu", ttl=60)
    store.acquire_lease(*creds(b), name="gpu", ttl=60)
    store.test_time[0] += 61
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == b["agent_id"] and lease["queued"] == 0
    assert [e["event"] for e in events(store)][-2:] == ["lease_expire", "lease_grant"]


def test_list_queue_pages_preserve_exact_payload_and_bounds(store):
    holder = store.register("alice", label="holder")
    # Labels and enqueue times deliberately tie: ticket order is authoritative.
    waiters = sorted([store.register("alice", label="waiter")
                      for _ in range(LEASE_LIST_QUEUE + 3)],
                     key=lambda agent: agent["agent_id"], reverse=True)
    names = ["claim:file:example", "gpu", "full-suite", "coordinator:example"]
    held = {name: store.acquire_lease(*creds(holder), name=name, ttl=120,
                                     expect=10, purpose=name) for name in names}
    queued = {"full-suite": waiters, "gpu": waiters[:1],
              "coordinator:example": [], "claim:file:example": waiters[:2]}
    for name, agents in queued.items():
        for index, agent in enumerate(agents):
            store.acquire_lease(*creds(agent), name=name, ttl=120, purpose=f"turn {index}")
    # A left join must keep both a departed waiter and a missing holder.
    store.storage.conn.execute("DELETE FROM coordination_agents WHERE agent_id=%s",
                               (waiters[0]["agent_id"],))
    store.storage.conn.execute("DELETE FROM coordination_agents WHERE agent_id=%s",
                               (holder["agent_id"],))
    store.test_time[0] = 1121.0  # read-only listing keeps the expired hold

    def expected(name):
        owner = dict(held[name]["holder"], label="")
        return {"name": name, "holder": owner, "fence": held[name]["fence"],
                "expires_at": 1120.0, "expected_end": 1010.0, "stale": True,
                "queued": len(queued[name]),
                "queue": [{"agent_id": agent["agent_id"],
                           "label": "" if index == 0 else "waiter",
                           "enqueued_at": 1000.0, "purpose": f"turn {index}"}
                          for index, agent in enumerate(queued[name][:LEASE_LIST_QUEUE])]}

    before = events(store)
    assert store.list_leases(limit=2, settle=False) == {
        "leases": [expected("coordinator:example"), expected("full-suite")], "truncated": True}
    assert store.list_leases(settle=False) == {
        "leases": [expected(name) for name in
                   ("coordinator:example", "full-suite", "gpu", "claim:file:example")],
        "truncated": False}
    assert store.list_leases(name="gpu", settle=False) == {
        "leases": [expected("gpu")], "truncated": False}
    assert store.list_leases(name="missing", settle=False) == {"leases": [], "truncated": False}
    assert events(store) == before


@pytest.mark.parametrize("lease_count", [1, 3, 50])
def test_list_queue_reads_do_not_grow_with_the_lease_page(store, monkeypatch, lease_count):
    holder, waiter = store.register("alice"), store.register("alice")
    for index in range(lease_count):
        name = f"resource:{index:02d}"
        store.acquire_lease(*creds(holder), name=name, ttl=120)
        store.acquire_lease(*creds(waiter), name=name, ttl=120)
    reads = []
    for method in ("_one", "_all"):
        original = getattr(store, method)

        def counted(*args, _original=original, **kwargs):
            reads.append(1)
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, method, counted)
    result = store.list_leases(settle=False)
    assert len(result["leases"]) == lease_count
    assert all(lease["queued"] == 1 and lease["queue"][0]["agent_id"] == waiter["agent_id"]
               for lease in result["leases"])
    # One lease page plus two batched queue reads, independent of page size.
    assert len(reads) <= 3


def test_list_agents_carries_the_leases(store):
    a, b = store.register("alice", label="runner"), store.register("alice")
    store.acquire_lease(*creds(a), name="coordinator:pseudolife-mcp", ttl=3600)
    listed = store.list_agents(*creds(b))
    assert [lease["name"] for lease in listed["leases"]] == ["coordinator:pseudolife-mcp"]


def test_prune_keeps_a_live_holder_and_drops_a_departed_waiter(store):
    holder = store.register("alice", capabilities={"resumable": False})
    waiter = store.register("alice", capabilities={"resumable": False})
    store.acquire_lease(*creds(holder), name="coordinator:p", ttl=86400)
    store.acquire_lease(*creds(waiter), name="coordinator:p", ttl=3600)
    store.test_time[0] += 7200  # both idle past the ephemeral window
    store.prune()
    rows = store.storage.conn.execute(
        "SELECT agent_id FROM coordination_agents").fetchall()
    assert [r[0] for r in rows] == [holder["agent_id"]]
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_lease_waiters").fetchone()[0] == 0
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == holder["agent_id"] and lease["queued"] == 0


def test_prune_settles_expired_holds_before_removing_their_holder(store):
    gone = store.register("alice", capabilities={"resumable": False})
    store.acquire_lease(*creds(gone), name="gpu", ttl=60)
    store.test_time[0] += 7200
    store.prune()
    # Checked before any listing, which would settle the hold itself.
    [expired] = events(store, "lease_expire")
    assert expired["agent_id"] == gone["agent_id"] and expired["actor"] == "daemon"
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_agents").fetchone()[0] == 0
    assert store.list_leases()["leases"] == []
    row = store.storage.conn.execute(
        "SELECT holder_agent_id, fence FROM coordination_leases WHERE name='gpu'").fetchone()
    assert row == (None, 1)


def test_operator_break_frees_the_lease_and_grants_the_next(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="live-daemon", ttl=3600)
    store.acquire_lease(*creds(b), name="live-daemon", ttl=3600)
    out = store.break_lease("live-daemon")
    assert out == {"name": "live-daemon", "broken": True, "was_held_by": a["agent_id"]}
    [row] = events(store, "lease_break")
    assert row["actor"] == "operator" and row["principal"] == ""
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == b["agent_id"]
    assert store.break_lease("never-existed") == {
        "name": "never-existed", "broken": False, "was_held_by": None}


# --- operator-designated roles (2026-10-03) ------------------------------------
# Any session can claim ``coordinator:<project>``, so the lease cannot carry
# wake power (review of #549). The operator designates the coordinator
# instead, as a ``designated:`` lease no agent can take.

def test_an_agent_cannot_take_a_designated_lease(store):
    a = store.register("alice", project="proj")
    for name in ("designated:coordinator:proj", "designated:anything"):
        with pytest.raises(CoordinationError, match="^reserved_lease$"):
            store.acquire_lease(*creds(a), name=name, ttl=3600)
    assert events(store, "lease_acquire") == [] and events(store, "lease_queue") == []
    assert store.list_leases()["leases"] == []


def test_the_operator_designates_a_coordinator_until_it_expires(store):
    a = store.register("alice", project="proj")
    b = store.register("alice", project="proj")
    out = store.designate_coordinator("proj", a["agent_id"][:8], hold=3600)
    assert out == {"name": "designated:coordinator:proj", "agent_id": a["agent_id"],
                   "fence": 1, "expires_at": 4600.0, "replaced": None}
    [row] = events(store, "lease_designate")
    assert (row["actor"], row["principal"], row["agent_id"]) == ("operator", "", a["agent_id"])
    assert payload(row) == {"name": "designated:coordinator:proj", "fence": 1, "hold": 3600}
    [lease] = store.list_leases()["leases"]
    assert (lease["name"], lease["holder"]["agent_id"]) == ("designated:coordinator:proj",
                                                           a["agent_id"])
    # The designee cannot renew it through the board, only the operator can.
    with pytest.raises(CoordinationError, match="^reserved_lease$"):
        store.acquire_lease(*creds(a), name="designated:coordinator:proj", ttl=3600)
    # A new designation replaces the old one and says whom it replaced.
    out = store.designate_coordinator("proj", b["agent_id"], hold=60)
    assert (out["replaced"], out["fence"], out["expires_at"]) == (a["agent_id"], 2, 1060.0)
    store.test_time[0] = 1061.0
    assert store.list_leases()["leases"] == []
    [expired] = events(store, "lease_expire")
    assert (expired["agent_id"], payload(expired)["name"]) == (b["agent_id"],
                                                             "designated:coordinator:proj")


def test_a_designated_coordinator_may_resign_and_the_operator_may_revoke(store):
    a = store.register("alice", project="proj")
    store.designate_coordinator("proj", a["agent_id"], hold=3600)
    assert store.release_lease(*creds(a), name="designated:coordinator:proj")["released"]
    store.designate_coordinator("proj", a["agent_id"], hold=3600)
    assert store.break_lease("designated:coordinator:proj")["was_held_by"] == a["agent_id"]
    assert store.list_leases()["leases"] == []


@pytest.mark.parametrize("project, agent, hold, code", [
    ("", "self", 3600, "invalid_project"),
    ("  ", "self", 3600, "invalid_project"),
    ("proj", "nobody", 3600, "instance_not_found"),
    ("proj", "revoked", 3600, "agent_revoked"),
    ("proj", "self", 59, "invalid_ttl"),
    ("proj", "self", 7 * 86400 + 1, "invalid_ttl"),
])
def test_a_designation_needs_a_project_a_live_agent_and_a_bounded_hold(store, project, agent,
                                                                       hold, code):
    a = store.register("alice", project="proj")
    if agent == "revoked":
        store.storage.conn.execute("UPDATE coordination_agents SET credential_hash=NULL "
                                   "WHERE agent_id=%s", (a["agent_id"],))
    agent_id = "f" * 32 if agent == "nobody" else a["agent_id"]
    with pytest.raises(CoordinationError, match=f"^{code}$"):
        store.designate_coordinator(project, agent_id, hold=hold)
    assert events(store, "lease_designate") == []


def test_status_expectation_marks_a_status_overdue(store):
    a, b = store.register("alice"), store.register("alice")
    store.update(*creds(a), status="suite=running", expect=1200)
    row = store.list_agents(*creds(b))["agents"][0]
    assert row["status_expires_at"] == 2200.0 and row["status_overdue"] is False
    store.test_time[0] += 1201
    assert store.list_agents(*creds(b))["agents"][0]["status_overdue"] is True
    # A new status without an expectation clears the old one.
    store.update(*creds(a), status="idle")
    row = store.list_agents(*creds(b))["agents"][0]
    assert row["status_expires_at"] is None and row["status_overdue"] is False
    [*_, last] = events(store, "update")
    assert payload(last)["fields"] == {"status": "idle", "status_expires_at": None}
    with pytest.raises(CoordinationError, match="invalid_expect"):
        store.update(*creds(a), status="x", expect=0)


def test_expect_alone_retimes_the_current_status(store):
    a, b = store.register("alice"), store.register("alice")
    store.update(*creds(a), status="deploying")
    store.update(*creds(a), expect=300)
    row = store.list_agents(*creds(b))["agents"][0]
    assert row["status"] == "deploying" and row["status_expires_at"] == 1300.0


def test_lease_history_keeps_the_audit_chain_valid(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="gpu", ttl=60)
    store.acquire_lease(*creds(b), name="gpu", ttl=60)
    store.test_time[0] += 61
    store.list_leases()
    store.release_lease(*creds(b), name="gpu")
    store.break_lease("gpu")
    report = verify_audit_chain(list(audit_events(store.storage.conn)))
    assert report["ok"] is True


# ── review fixes (2026-09-26) ────────────────────────────────────────────


def test_renewal_keeps_the_expected_end_unless_the_estimate_changes(store):
    a = store.register("alice")
    store.acquire_lease(*creds(a), name="full-suite", ttl=120, expect=1200, purpose="suite")
    before = len(events(store))
    store.test_time[0] += 40
    # A wrapper renewing with the same estimate must not push the end out,
    # or a stalled hold could never read stale.
    same = store.acquire_lease(*creds(a), name="full-suite", ttl=120, expect=1200,
                               purpose="suite")
    assert same["expected_end"] == 2200.0 and same["expires_at"] == 1160.0
    assert len(events(store)) == before
    store.test_time[0] += 10
    moved = store.acquire_lease(*creds(a), name="full-suite", ttl=120, expect=600,
                                purpose="suite, second half")
    assert moved["expected_end"] == 1650.0
    [update] = events(store, "lease_update")
    assert payload(update) == {"name": "full-suite", "fence": 1, "expect": 600,
                               "purpose": "suite, second half"}


@pytest.mark.parametrize("name", ["gpu\u200b", "\u202egpu", "gpu\u0085", "\ufeffgpu"])
def test_invisible_and_bidi_characters_are_refused_in_names(store, name):
    a = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_lease"):
        store.acquire_lease(*creds(a), name=name, ttl=120)


def test_a_full_queue_after_a_settle_logs_nothing(store, monkeypatch):
    from pseudolife_memory.storage import coordination as module
    a, b, c, d = (store.register("alice") for _ in range(4))
    store.acquire_lease(*creds(a), name="gpu", ttl=60)
    store.acquire_lease(*creds(b), name="gpu", ttl=60)
    store.test_time[0] += 1
    store.acquire_lease(*creds(c), name="gpu", ttl=60)  # queue: b, c
    # A settle only ever shortens a queue, so shrink the bound to make the
    # refusal follow a settle that expires a and grants b (queue: c, full).
    monkeypatch.setattr(module, "LEASE_QUEUE_MAX", 1)
    store.test_time[0] += 60
    before = len(events(store))
    with pytest.raises(CoordinationError, match="lease_queue_full"):
        store.acquire_lease(*creds(d), name="gpu", ttl=60)
    assert len(events(store)) == before  # the settle rolled back with the refusal


def test_roster_lists_resource_leases_before_claims_and_says_when_cut(store, monkeypatch):
    from pseudolife_memory.storage import coordination as module
    monkeypatch.setattr(module, "MAX_PAGE", 3)
    a, b = store.register("alice"), store.register("alice")
    for path in ("a.py", "b.py", "c.py"):
        store.acquire_lease(*creds(a), name=f"claim:{path}", ttl=3600)
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    listed = store.list_agents(*creds(b), limit=3)
    assert [lease["name"] for lease in listed["leases"]] == ["gpu", "claim:a.py", "claim:b.py"]
    assert listed["leases_truncated"] is True


def test_prune_drops_a_departed_waiter_before_granting(store):
    holder = store.register("alice")
    departed = store.register("alice", capabilities={"resumable": False})
    live = store.register("alice")
    store.acquire_lease(*creds(holder), name="gpu", ttl=60)
    store.acquire_lease(*creds(departed), name="gpu", ttl=60)
    store.test_time[0] += 1
    store.acquire_lease(*creds(live), name="gpu", ttl=60)
    store.test_time[0] += 3700  # holder lapsed; the ephemeral waiter is past its window
    store.update(*creds(live), status="still here")
    store.prune()
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == live["agent_id"]
    [left] = events(store, "lease_dequeue")
    assert (left["agent_id"], left["actor"], payload(left)["reason"]) == (
        departed["agent_id"], "daemon", "departed")
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_agents WHERE agent_id=%s",
        (departed["agent_id"],)).fetchone()[0] == 0


def test_prune_forgets_a_lease_left_free_for_a_week(store):
    a = store.register("alice")
    store.acquire_lease(*creds(a), name="claim:old.py", ttl=3600)
    store.release_lease(*creds(a), name="claim:old.py")
    store.acquire_lease(*creds(a), name="claim:new.py", ttl=3600)
    store.test_time[0] += 7 * 86400 + 1
    store.acquire_lease(*creds(a), name="claim:new.py", ttl=86400)  # renew, keeps it held
    store.prune()
    names = [r[0] for r in store.storage.conn.execute(
        "SELECT name FROM coordination_leases ORDER BY name").fetchall()]
    assert names == ["claim:new.py"]
    [pruned] = [e for e in events(store, "prune") if "leases" in payload(e)]
    assert payload(pruned)["leases"] == ["claim:old.py"]


def test_restore_recovery_frees_every_lease_and_queue(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="coordinator:p", ttl=3600)
    store.acquire_lease(*creds(b), name="coordinator:p", ttl=3600)
    out = store.recover()
    assert out["revoked"] == 2
    assert store.list_leases()["leases"] == []
    [row] = events(store, "recover")
    assert payload(row)["leases_freed"] == ["coordinator:p"]
    assert payload(row)["waiters_removed"] == 1
    # A revoked waiter can never take a turn up, so none is granted one.
    assert events(store, "lease_grant") == []


def test_a_revoked_waiter_is_passed_over(store):
    """A restore recovered by an older build (before v45) revokes every
    credential but leaves the queues; a revoked waiter can never take a turn
    up, so the next grant skips it rather than idling for the window."""
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.acquire_lease(*creds(b), name="gpu", ttl=120)
    store.test_time[0] += 1
    store.acquire_lease(*creds(c), name="gpu", ttl=120)
    store.storage.conn.execute(
        "UPDATE coordination_agents SET credential_hash=NULL WHERE agent_id=%s",
        (b["agent_id"],))
    store.release_lease(*creds(a), name="gpu")
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == c["agent_id"]


# ── second review (Codex, 2026-09-26): fences and lock order ─────────────


def test_a_fence_never_repeats_after_the_row_is_forgotten(store):
    """Prune forgets a lease row left free for a week; the next grant of the
    same name must still carry a fence higher than any before it."""
    a = store.register("alice")
    first = store.acquire_lease(*creds(a), name="claim:old.py", ttl=3600)["fence"]
    store.release_lease(*creds(a), name="claim:old.py")
    store.test_time[0] += 7 * 86400 + 1
    store.update(*creds(a), status="back")  # still active, so prune keeps the agent
    store.prune()
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_leases WHERE name='claim:old.py'").fetchone()[0] == 0
    again = store.acquire_lease(*creds(a), name="claim:old.py", ttl=3600)["fence"]
    assert again > first


def test_prune_locks_a_lease_before_its_queue(store, pg_url):
    """Every lease path locks the lease row before its waiter rows; prune
    locking a departed waiter first could deadlock against an operator
    break or an acquire holding the lease and reaching for its queue."""
    import threading
    import psycopg
    holder = store.register("alice")
    departed = store.register("alice", capabilities={"resumable": False})
    store.acquire_lease(*creds(holder), name="gpu", ttl=60)
    store.acquire_lease(*creds(departed), name="gpu", ttl=60)
    store.test_time[0] += 3700  # the hold lapsed; the waiter is past its window
    other = psycopg.connect(pg_url, autocommit=False)
    try:
        # Hold the lease row the way break or acquire would, first.
        other.execute("SELECT 1 FROM coordination_leases WHERE name='gpu' FOR UPDATE")
        store.storage.conn.execute("SET lock_timeout = '2s'")
        done = threading.Event()
        failures = []

        def run_prune():
            # Waiting out the lock is fine; a deadlock (Postgres aborting
            # prune because it held the queue while waiting for the lease)
            # is the bug.
            try:
                store.prune()
            except psycopg.errors.LockNotAvailable:
                pass
            except Exception as exc:  # noqa: BLE001
                failures.append(type(exc).__name__)
            finally:
                done.set()

        worker = threading.Thread(target=run_prune)
        worker.start()
        try:
            # While prune waits on the lease row, the queue must still be
            # free for the lease holder to lock next.
            threading.Event().wait(0.5)
            other.execute("SET lock_timeout = '500ms'")
            other.execute("SELECT 1 FROM coordination_lease_waiters WHERE name='gpu' "
                          "FOR UPDATE")
        finally:
            other.rollback()
            worker.join(10)
        assert done.is_set()
        assert failures == []
    finally:
        other.close()
        store.storage.conn.execute("SET lock_timeout = '5s'")


# --- a park cleared by a lease (v49 wake decision) ---------------------------------

def _parked_on(store, lease):
    """A ring-capable session parked until ``lease`` clears, idle since."""
    agent = store.register("alice", wake_enabled=True)
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], wake_enabled=True)
    store.update(*creds(agent), park_reason="needs_resource",
                 park_needs="the suite lock, then my suite result", park_clear_by=lease)
    store.storage.conn.execute("UPDATE coordination_agents SET last_activity=0 WHERE agent_id=%s",
                               (agent["agent_id"],))
    return agent


def _notice(store, sender, recipient, tag):
    return store.send(*creds(sender), to=recipient["agent_id"], text="LEASE notice",
                      request_id=f"notice-{tag}")["wake"]


def test_a_release_notice_clears_a_park_on_that_lease(store):
    """``park_clear_by`` may name a lease (full-suite, gpu). The lease CLI's
    notices come from the mirror's own address, and its release notice after
    the board lease is already free (a successor told "released" must find
    it free), so the daemon's own record of the release, at most
    LEASE_CLEAR_GRACE earlier, makes the sender the clearer. Taking the
    lease clears nothing: the acquire notice is chatter, as is mail from a
    peer that never held it or that let go of another lease."""
    holder, successor, bystander, other = (store.register("alice") for _ in range(4))
    parked = _parked_on(store, "full-suite")
    store.acquire_lease(*creds(holder), name="full-suite", ttl=600)
    store.acquire_lease(*creds(other), name="gpu", ttl=600)
    assert _notice(store, holder, parked, "acquired")["decision"] == "withheld"
    store.acquire_lease(*creds(successor), name="full-suite", ttl=600)   # queued
    store.release_lease(*creds(holder), name="full-suite")               # granted on
    store.release_lease(*creds(other), name="gpu")
    store.test_time[0] = 1059.0
    assert _notice(store, holder, parked, "released") == {
        "decision": "rung", "reason": "clearer", "ring_at": 1059.0}
    assert _notice(store, successor, parked, "successor")["decision"] == "withheld"
    assert _notice(store, bystander, parked, "chatter")["decision"] == "withheld"
    assert _notice(store, other, parked, "other-lease")["decision"] == "withheld"
    store.test_time[0] = 1061.0
    assert _notice(store, holder, parked, "late")["decision"] == "withheld"


def test_no_lease_stands_in_for_the_maintainer_or_a_peer(store):
    """Any string is a lease name, so a lease called ``maintainer``, or
    after a peer's agent id or the 8-hex prefix every surface shows of it,
    taken and released, clears no park that names the human or that peer."""
    sender, peer = store.register("alice"), store.register("alice")
    on_human = _parked_on(store, "maintainer")
    on_peer = _parked_on(store, peer["agent_id"])
    on_prefix = _parked_on(store, peer["agent_id"][:8])
    for name in ("maintainer", peer["agent_id"], peer["agent_id"][:8]):
        store.acquire_lease(*creds(sender), name=name, ttl=600)
        store.release_lease(*creds(sender), name=name)
    store.test_time[0] = 1010.0
    assert _notice(store, sender, on_human, "human")["decision"] == "withheld"
    assert _notice(store, sender, on_peer, "peer")["decision"] == "withheld"
    assert _notice(store, sender, on_prefix, "prefix")["decision"] == "withheld"


def test_a_hold_that_lapsed_moments_ago_still_clears(store):
    """A mirror whose renewals stopped loses the board lease to expiry, then
    announces its release: the expiry the daemon logged against it counts
    like a release."""
    holder, lister = store.register("alice"), store.register("alice")
    parked = _parked_on(store, "full-suite")
    store.acquire_lease(*creds(holder), name="full-suite", ttl=60)
    store.test_time[0] = 1100.0
    store.list_leases()                     # settles the lapsed hold
    store.test_time[0] = 1105.0
    store.attach(*creds(parked), attachment_id=parked["agent_id"][:8], wake_enabled=True)
    assert _notice(store, holder, parked, "expired")["reason"] == "clearer"
    assert _notice(store, lister, parked, "lister")["decision"] == "withheld"
