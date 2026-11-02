"""A queue survives idle sessions and one missed acceptance window."""
import pytest
from psycopg.types.json import Jsonb

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_coordination_leases import creds, events, payload, store  # noqa: F401


def queue(store, count=4, ttl=120):
    agents = [store.register("alice", label=f"waiter-{n}") for n in range(count)]
    for agent in agents:
        store.acquire_lease(*creds(agent), name="resource", ttl=ttl, purpose="queued work")
    return agents


def touch(store, *agents):
    for agent in agents:
        store.update(*creds(agent), status="ready")


def view(store):
    return store.list_leases(name="resource")["leases"][0]


def test_dead_head_is_skipped_without_losing_its_place(store):
    a, b, c, d = queue(store)
    store.test_time[0] += 121
    touch(store, c, d)
    result = view(store)
    assert result["holder"]["agent_id"] == c["agent_id"]
    assert [w["agent_id"] for w in result["queue"]] == [b["agent_id"], d["agent_id"]]
    assert not events(store, "lease_dequeue")
    touch(store, b)
    store.release_lease(*creds(c), name="resource")
    assert view(store)["holder"]["agent_id"] == b["agent_id"]


@pytest.mark.parametrize("signal", ["attachment", "poll"])
def test_live_idle_waiter_keeps_its_turn(store, signal):
    a, b, c = queue(store, 3)
    store.test_time[0] += 121
    if signal == "attachment":
        store.storage.conn.execute("UPDATE coordination_agents SET lease_until=%s WHERE agent_id=%s",
                                   (store.test_time[0] + 60, b["agent_id"]))
    else:
        touch(store, b)
    touch(store, c)
    assert view(store)["holder"]["agent_id"] == b["agent_id"]


def test_returning_waiter_is_alive_in_its_own_acquire(store):
    a, b, c = queue(store, 3)
    store.test_time[0] += 121
    touch(store, c)
    result = store.acquire_lease(*creds(b), name="resource")
    assert result["state"] == "held"
    assert result["queued"] == 1
    assert not store._one("SELECT 1 FROM coordination_lease_waiters WHERE agent_id=%s", (b["agent_id"],))


def test_missed_grant_requeues_once_before_remaining_waiters(store):
    a, b, c, d = queue(store)
    store.release_lease(*creds(a), name="resource")
    store.test_time[0] += 121
    touch(store, c, d)
    result = view(store)
    assert result["holder"]["agent_id"] == c["agent_id"]
    assert [w["agent_id"] for w in result["queue"]] == [b["agent_id"], d["agent_id"]]
    retry = events(store, "lease_queue")[-1]
    assert retry["actor"] == "daemon"
    assert payload(retry)["reason"] == "missed_grant"
    touch(store, b)
    store.release_lease(*creds(c), name="resource")
    assert payload(events(store, "lease_grant")[-1])["retry"] is True
    store.test_time[0] += 121
    touch(store, d)
    result = view(store)
    assert result["holder"]["agent_id"] == d["agent_id"]
    assert result["queued"] == 0


@pytest.mark.parametrize("mode", ["renewed", "direct", "no_next"])
def test_only_a_missed_grant_with_a_successor_requeues(store, mode):
    a, b, c = queue(store, 3)
    if mode != "direct":
        store.release_lease(*creds(a), name="resource")
    if mode == "renewed":
        store.test_time[0] += 1
        store.acquire_lease(*creds(b), name="resource", ttl=3600)
        store.test_time[0] += 3601
    else:
        store.test_time[0] += 121
    if mode != "no_next":
        touch(store, c)
    store.list_leases(name="resource")
    assert not [e for e in events(store, "lease_queue") if e["actor"] == "daemon"]


def test_a_new_queue_stint_is_not_marked_as_the_old_retry(store):
    a, b, c, d = queue(store)
    store.release_lease(*creds(a), name="resource")
    store.test_time[0] += 121
    touch(store, c, d)
    view(store)
    store.release_lease(*creds(b), name="resource")
    store.test_time[0] += 1
    store.acquire_lease(*creds(b), name="resource")
    store.release_lease(*creds(c), name="resource")
    store.release_lease(*creds(d), name="resource")
    assert "retry" not in payload(events(store, "lease_grant")[-1])


def daemon(store):
    from pseudolife_memory.storage.coordination import DAEMON_PRINCIPAL
    return store.register(DAEMON_PRINCIPAL, label="daemon")


@pytest.mark.parametrize("codex", [False, True])
def test_grant_notice_rings_and_survives_sender_restart_without_duplicates(store, codex):
    a, b = queue(store, 2)
    store.update(*creds(b), park_reason="needs_resource", park_needs="resource",
                 park_clear_by="maintainer")
    store.storage.conn.execute("UPDATE coordination_agents SET capabilities=%s WHERE agent_id=%s",
                               (Jsonb({"ring": True, **({"codex": False} if codex else {})}), b["agent_id"]))
    attached = store.attach(*creds(b), attachment_id="listener", ring=True, ring_armed_until=1060)
    store.release_lease(*creds(a), name="resource")
    [grant] = store.pending_lease_grants()
    first = daemon(store)
    result = store.send_lease_grant(first["agent_id"], first["credential"], **grant)
    assert (result["wake"]["decision"], result["wake"]["reason"]) == ("rung", "lease_grant")
    restarted = daemon(store)
    assert store.send_lease_grant(restarted["agent_id"], restarted["credential"], **grant) is None
    assert not store.pending_lease_grants()
    offer = store.heartbeat(*creds(b), attachment_id="listener", generation=attached["generation"],
                            ring_armed_until=1060)["wake"]
    assert (offer["decision"], offer["reason"]) == ("rung", "lease_grant")
    messages = store.receive(*creds(b), for_delivery=True)["messages"]
    assert len(messages) == 1 and messages[0]["origin"] == "agent"
    assert "resource" in messages[0]["text"] and "approval" in messages[0]["text"]


@pytest.mark.parametrize("stale", ["expired", "replaced", "released"])
def test_stale_grant_never_sends_a_notice(store, stale):
    a, b, c = queue(store, 3)
    store.release_lease(*creds(a), name="resource")
    [grant] = store.pending_lease_grants()
    sender = daemon(store)
    if stale == "expired":
        store.test_time[0] += 121
    elif stale == "replaced":
        store.break_lease("resource")
    else:
        store.release_lease(*creds(b), name="resource")
    assert store.send_lease_grant(sender["agent_id"], sender["credential"], **grant) is None
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_messages").fetchone()[0] == 0


def test_notice_failure_after_send_rolls_back_with_its_outbox_entry(store, monkeypatch):
    a, b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    [grant] = store.pending_lease_grants()
    sender = daemon(store)
    original = store.send
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before outer commit")
    monkeypatch.setattr(store, "send", interrupted)
    with pytest.raises(RuntimeError, match="outer commit"):
        store.send_lease_grant(sender["agent_id"], sender["credential"], **grant)
    assert not store.receive(*creds(b))["messages"]
    assert len(store.pending_lease_grants()) == 1
    monkeypatch.setattr(store, "send", original)
    store.send_lease_grant(sender["agent_id"], sender["credential"], **grant)
    assert len(store.receive(*creds(b))["messages"]) == 1


@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("codex", [False, True])
def test_unserved_grant_bell_precedes_later_peer_offer(store, parked, codex):
    a, b = queue(store, 2)
    store.storage.conn.execute("UPDATE coordination_agents SET capabilities=%s WHERE agent_id=%s",
                               (Jsonb({"ring": True, **({"codex": False} if codex else {})}), b["agent_id"]))
    attached = store.attach(*creds(b), attachment_id="listener", ring=True, ring_armed_until=1060)
    if parked:
        store.update(*creds(b), park_reason="needs_resource", park_needs="resource",
                     park_clear_by="maintainer")
    store.send(*creds(a), to=b["agent_id"], text="peer", request_id="before", urgent=True)
    store.test_time[0] += 1
    store.release_lease(*creds(a), name="resource")
    sender = daemon(store)
    store.send_lease_grant(sender["agent_id"], sender["credential"], **store.pending_lease_grants()[0])
    store.test_time[0] += 1
    store.send(*creds(a), to=b["agent_id"], text="peer", request_id="after", urgent=True)
    def offer():
        return store.heartbeat(*creds(b), attachment_id="listener", generation=attached["generation"],
                               ring_armed_until=1060)["wake"]
    first = offer()
    assert (first["decision"], first["reason"], first["ring_at"]) == ("rung", "lease_grant", 1001)
    assert offer()["reason"] == ("urgent" if codex or parked else "lease_grant")


def test_notice_dedupe_survives_live_message_pruning(store):
    a, b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    sender = daemon(store)
    pending = store.pending_lease_grants()[0]
    store.send_lease_grant(sender["agent_id"], sender["credential"], **pending)
    # Long-lived renewed hold outlives the mail retention window.
    store.acquire_lease(*creds(b), name="resource", ttl=86400)
    store.storage.conn.execute("DELETE FROM coordination_messages")
    assert store.pending_lease_grants() == []
    replacement = daemon(store)
    assert store.send_lease_grant(replacement["agent_id"], replacement["credential"], **pending) is None


def test_short_renewal_accepts_grant_without_priority_retry(store):
    a, b, c = queue(store, 3, ttl=600)
    store.release_lease(*creds(a), name="resource")
    store.test_time[0] += 10
    store.acquire_lease(*creds(b), name="resource", ttl=120)
    store.test_time[0] += 121
    touch(store, c)
    result = view(store)
    assert result["holder"]["agent_id"] == c["agent_id"]
    assert result["queued"] == 0


@pytest.mark.parametrize("end", ["expired", "released"])
def test_obsolete_grant_never_rings_or_delivers_live(store, end):
    a, b, c = queue(store, 3)
    store.storage.conn.execute("UPDATE coordination_agents SET capabilities=%s WHERE agent_id=%s",
                               (Jsonb({"ring": True, "codex": False}), b["agent_id"]))
    store.release_lease(*creds(a), name="resource")
    sender = daemon(store)
    store.send_lease_grant(sender["agent_id"], sender["credential"], **store.pending_lease_grants()[0])
    if end == "expired":
        store.test_time[0] += 121
    else:
        store.release_lease(*creds(b), name="resource")
    attached = store.attach(*creds(b), attachment_id="listener", ring=True,
                            ring_armed_until=store.test_time[0] + 60)
    assert attached["wake"] is None
    assert store.receive(*creds(b), for_delivery=True)["messages"] == []
