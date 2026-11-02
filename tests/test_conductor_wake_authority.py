"""Operator-granted conductor rings use their own budget, never peer gates."""
import pytest

from pseudolife_memory.storage.coordination import WakePolicy
from tests.test_coordination_storage import (
    _delegate, _done, _wake, creds, pg_conn, pg_url, store,  # noqa: F401
)


@pytest.mark.parametrize("park", ["needs_approval", "done", ""])
@pytest.mark.parametrize("codex", [False, True])
def test_conductor_plain_mail_rings_and_is_served(store, park, codex):
    sender = _delegate(store)
    recipient = store.register("alice", project="proj", capabilities={
        "ring": True, **({"codex": False} if codex else {})})
    attached = store.attach(*creds(recipient), attachment_id="listener", ring=True,
                           ring_armed_until=1060.0)
    if park:
        store.update(*creds(recipient), park_reason=park, park_needs="maintainer approval",
                     park_clear_by="maintainer")
    wake = _wake(store, sender, recipient)
    assert (wake["decision"], wake["reason"], wake["ring_at"]) == (
        "rung", "conductor", 1000.0)
    offered = store.heartbeat(*creds(recipient), attachment_id="listener",
                              generation=attached["generation"], ring_armed_until=1060.0)
    assert offered["wake"]["reason"] == "conductor"
    # Ringing supplies no approval and does not clear the recipient's park.
    assert store.storage.conn.execute(
        "SELECT park_reason FROM coordination_agents WHERE agent_id=%s",
        (recipient["agent_id"],)).fetchone()[0] == (park or None)


def test_conductor_bypasses_peer_caps_but_spends_its_own_budget(store):
    store.wake = WakePolicy(nightly_total=0, per_recipient_per_hour=0,
                            urgent_per_sender_per_hour=0, authority_per_sender_per_hour=2)
    sender, recipient = _delegate(store), _done(store)
    for urgent in (False, True):
        wake = _wake(store, sender, recipient, urgent=urgent)
        assert (wake["decision"], wake["reason"], wake["ring_at"]) == (
            "rung", "conductor", 1000.0)
    assert _wake(store, sender, recipient) == {
        "decision": "capped", "reason": "authority_sender_hour"}


@pytest.mark.parametrize("codex", [False, True])
def test_pending_peer_stagger_cannot_delay_conductor_offer(store, codex):
    sender, peer = _delegate(store), store.register("alice")
    recipient = store.register("alice", project="proj", capabilities={
        "ring": True, **({"codex": False} if codex else {})})
    attached = store.attach(*creds(recipient), attachment_id="listener", ring=True,
                           ring_armed_until=1060.0)
    store.update(*creds(recipient), park_reason="needs_approval",
                 park_needs="maintainer approval", park_clear_by="maintainer")
    _wake(store, peer, recipient, urgent=True)
    assert _wake(store, peer, recipient, urgent=True)["ring_at"] == 1030.0
    store.test_time[0] = 1001.0
    assert _wake(store, sender, recipient)["ring_at"] == 1001.0
    offer = store.heartbeat(*creds(recipient), attachment_id="listener",
                            generation=attached["generation"], ring_armed_until=1061.0)["wake"]
    assert (offer["reason"], offer["ring_at"]) == ("conductor", 1001.0)

    if not codex:
        # Ordinary non-Codex offers lapse after one heartbeat interval;
        # that old authority offer must not hide a subsequent peer ring.
        store.test_time[0] = 1031.0
        _wake(store, peer, recipient, urgent=True)
        offer = store.heartbeat(*creds(recipient), attachment_id="listener",
                                generation=attached["generation"], ring_armed_until=1091.0)["wake"]
        assert offer["reason"] == "urgent"


@pytest.mark.parametrize("invalid", ["expired", "stolen", "fence", "record", "project", "label"])
def test_only_current_matching_grant_bypasses_need(store, invalid):
    sender, recipient = _delegate(store), _done(store)
    store.update(*creds(recipient), park_reason="needs_approval",
                 park_needs="maintainer approval", park_clear_by="maintainer")
    if invalid == "expired":
        store.storage.conn.execute("UPDATE coordination_leases SET expires_at=1000")
    elif invalid == "stolen":
        other = store.register("alice")
        store.grant_conductor("proj", other["agent_id"], hold=3600)
    elif invalid == "fence":
        store.storage.conn.execute("UPDATE coordination_leases SET fence=fence+1")
    elif invalid == "record":
        store.storage.conn.execute("DELETE FROM coordination_events WHERE event='lease_conductor'")
    elif invalid == "project":
        store.update(*creds(recipient), project="another")
    else:
        sender = store.register("alice", label="conductor", project="proj")
    assert _wake(store, sender, recipient)["reason"] == "need_not_cleared"


def test_conductor_without_listener_reports_no_path(store):
    sender = _delegate(store)
    recipient = store.register("alice", project="proj")
    store.update(*creds(recipient), park_reason="done", park_clear_by="maintainer")
    wake = _wake(store, sender, recipient)
    assert wake["decision"] == "no_path" and wake["queued"] is True


@pytest.mark.parametrize("actor", ["operator", "maintainer", "agent"])
def test_legacy_delegate_grant_has_the_same_plain_mail_authority(store, actor):
    sender, recipient = _delegate(store), _done(store)
    store.storage.conn.execute("DELETE FROM coordination_leases WHERE name='delegate:proj'")
    fence = store.storage.conn.execute(
        "UPDATE coordination_leases SET name='delegate:proj' WHERE name='conductor:proj' "
        "RETURNING fence").fetchone()[0]
    with store.storage._txn():
        store._append([store._event("lease_delegate", {"name": "delegate:proj", "fence": fence},
                                    actor=actor, agent_id=sender["agent_id"])], store.clock())
    assert _wake(store, sender, recipient)["reason"] == (
        "parked_done" if actor == "agent" else "conductor")


def test_default_authority_budget_covers_five_eleven_session_broadcasts():
    from pseudolife_memory.utils.config import WakeConfig

    assert WakePolicy().authority_per_sender_per_hour == 60
    assert WakeConfig().authority_per_sender_per_hour == 60


@pytest.mark.parametrize("codex", [False, True])
@pytest.mark.parametrize("authority", ["conductor", "maintainer"])
@pytest.mark.parametrize("repeat", [False, True])
def test_each_new_authority_and_later_peer_offer_changes_identity(store, codex, authority, repeat):
    from pseudolife_memory.storage.coordination import MAINTAINER_PRINCIPAL

    sender = (_delegate(store) if authority == "conductor"
              else store.register(MAINTAINER_PRINCIPAL))
    peer = store.register("alice")
    recipient = store.register("alice", project="proj", capabilities={
        "ring": True, **({"codex": False} if codex else {})})
    attached = store.attach(*creds(recipient), attachment_id="listener", ring=True,
                           ring_armed_until=1060.0)
    store.update(*creds(recipient), park_reason="needs_info", park_needs="result",
                 park_clear_by="anyone")
    def send_authority():
        if authority == "conductor":
            return _wake(store, sender, recipient)
        return store.send(MAINTAINER_PRINCIPAL, sender["agent_id"], sender["credential"],
                          to=recipient["agent_id"], text="follow up", request_id=str(store.clock()),
                          maintainer_proof={"label": "test"})["wake"]
    def offer():
        return store.heartbeat(*creds(recipient), attachment_id="listener",
                               generation=attached["generation"], ring_armed_until=1060.0)["wake"]
    send_authority()
    first = offer()
    store.test_time[0] += 1
    if repeat:
        send_authority()
        second = offer()
        assert second["ring_at"] > first["ring_at"]
    store.test_time[0] += 1
    _wake(store, peer, recipient)
    third = offer()
    assert (third["reason"], third["ring_at"]) == ("anyone", 1002.0)


@pytest.mark.parametrize("authority", ["conductor", "maintainer"])
def test_authority_wakes_do_not_consume_peer_budgets(store, authority):
    from pseudolife_memory.storage.coordination import MAINTAINER_WAKE_REASON

    sender, recipient = _delegate(store), _done(store)
    _wake(store, sender, recipient)
    if authority == "maintainer":
        store.storage.conn.execute("UPDATE coordination_wakes SET reason=%s", (MAINTAINER_WAKE_REASON,))
    store.wake = WakePolicy(nightly_total=1, per_recipient_per_hour=1)
    store.update(*creds(recipient), park_reason="needs_info", park_needs="result",
                 park_clear_by="anyone")
    peer = store.register("alice")
    assert _wake(store, peer, recipient)["decision"] == "rung"
    assert _wake(store, peer, recipient)["decision"] == "capped"
