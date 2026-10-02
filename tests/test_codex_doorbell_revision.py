"""Offline queue-policy and recovery regressions; no native Codex home is used."""
import asyncio
from contextlib import contextmanager, suppress
import json
import os
import threading
import time

import pytest

from pseudolife_memory.channel import ChannelEvent
from pseudolife_memory.codex_coordination import CodexCoordinationRegistry
from pseudolife_memory.codex_doorbell import CodexDoorbell, doorbell_text
from pseudolife_memory.codex_doorbell_state import PendingNotice
from pseudolife_memory.coordination_adapter import CoordinationAdapter
from tests.test_codex_doorbell import THREAD, _stub, _calls, _settle, Mailbox
from tests.test_coordination_storage import store, creds
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_queue_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_DIGEST_DIR", str(tmp_path))


def mailbox(*ids, offer=0):
    result = {"generation": 1, "pending_count": len(ids), "pending_preview": [
        {"message_id": item, "sender_agent_id": "f" * 32,
         "sender_label": "fixture peer", "excerpt": "synthetic mail",
         "created_at": 1000.0 + int(item[1:])} for item in ids]}
    if offer:
        # A fresh authoritative rung answer models a current eligible park
        # cleared by this arrival, not urgency to an unparked/unknown thread.
        result["wake"] = {"decision": "rung", "reason": "clears",
                          "ring_at": time.time() - 10 + offer / 100}
    return result


class OfflineAdapter(CoordinationAdapter):
    async def __aenter__(self):
        self._entered = True
        self._generation = 1
        self._identity = {"agent_id": "e" * 32}
        self._update_pending_count(mailbox("m1", offer=1))
        return self

    async def __aexit__(self, *exc):
        self._closing = True
        if self._ring_timer is not None:
            self._ring_timer.cancel()
        task = self._heartbeat_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _post_attach(self, wake_enabled):
        assert wake_enabled is False
        return mailbox("m1", offer=2)

    async def _renew(self):
        await asyncio.Event().wait()

    async def inbox(self):
        yield ChannelEvent("fixture agent-origin delivery", {"message_id": "m1"})
        await asyncio.Event().wait()


class AmbiguousBridge:
    calls = []

    def __init__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def verify(self):
        pass

    async def deliver(self, event):
        self.calls.append(event.meta["message_id"])
        # Owner acceptance is possible; its reply was lost.
        raise RuntimeError("synthetic ambiguous bridge disconnect")

    async def __aexit__(self, *exc):
        pass


class RecordingDoorbell(CodexDoorbell):
    def __init__(self, now):
        super().__init__(["never-launched-fixture-cli"], clock=lambda: now[0])
        self.queued = []

    async def _ring(self, thread_id, count, adapter, decision=("rung", ""), *, notice=None):
        self.queued.append(notice)
        seen = self._bells[thread_id].pending_notice.accept(notice)
        adapter.note_delivery("bell", len(notice["text"]) + 1,
                              f"{decision[0]} {decision[1]} accepted "
                              f"{'prompt_seen' if seen else 'pending'} recipient_state_unknown")


async def settle(bell):
    await asyncio.gather(*list(bell._tasks))


def test_later_eligible_arrival_rings_after_ambiguous_bridge_drop(tmp_path):
    async def drive():
        now = [1000.0]
        bell = RecordingDoorbell(now)
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:1", "fixture-bank-token", state_dir=tmp_path / "states",
            digest_dir=tmp_path / "digests", adapter_factory=OfflineAdapter,
            doorbell=bell, delivery_url="ws://127.0.0.1:2",
            delivery_token="fixture-host-token", delivery_factory=AmbiguousBridge)
        try:
            adapter = await registry.get(THREAD)
            await asyncio.wait_for(registry._delivery_tasks[THREAD], 2)
            assert adapter.wake_enabled is False
            assert await registry.get(THREAD) is adapter  # ordinary call never reattaches registry
            now[0] += 60
            adapter._update_pending_count(mailbox("m1", offer=3))
            await settle(bell)
            assert bell.queued == []  # same ambiguous arrival cannot get an alternate
            old_watermark = adapter.digest_watermark
            adapter._update_pending_count(mailbox("m1", "m2", offer=4))
            assert adapter.digest_watermark > old_watermark
            await settle(bell)
            assert len(bell.queued) == 1, "later eligible arrival lost: no watch installed after bridge drop"
            # The installed hook's receipt is exact generated text + nonce,
            # owning session id; mailbox receives/acks are unrelated to it.
            first = bell.queued[0]
            pending = PendingNotice(tmp_path / "digests", THREAD)
            assert not pending.note_prompt({"session_id": THREAD, "prompt": "ordinary user turn"})
            now[0] += 60
            adapter._update_pending_count(mailbox("m1", "m2", "m3", offer=5))
            await settle(bell)
            assert len(bell.queued) == 1
            assert pending.note_prompt({"session_id": THREAD, "prompt": first["text"]})
            now[0] += 60
            adapter._update_pending_count(mailbox("m1", "m2", "m3", "m4", offer=6))
            await settle(bell)
            assert len(bell.queued) == 2
        finally:
            await registry.aclose()
    asyncio.run(drive())


def test_restart_with_unavailable_bridge_installs_pull_watch(tmp_path):
    class Unavailable(AmbiguousBridge):
        async def verify(self):
            raise RuntimeError("synthetic bridge unavailable during fresh attachment")

    async def drive():
        now = [1000.0]
        bell = RecordingDoorbell(now)
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:1", "fixture-bank-token", state_dir=tmp_path / "states",
            digest_dir=tmp_path / "digests", adapter_factory=OfflineAdapter,
            doorbell=bell, delivery_url="ws://127.0.0.1:2",
            delivery_token="fixture-host-token", delivery_factory=Unavailable)
        try:
            adapter = await registry.get(THREAD)
            assert THREAD in bell._bells
            assert adapter._ring_armed_until() > time.time()
            now[0] += 60
            adapter._update_pending_count(mailbox("m1", "m2", offer=7))
            await settle(bell)
            assert len(bell.queued) == 1
        finally:
            await registry.aclose()
    asyncio.run(drive())


@pytest.mark.parametrize("delay", [45, 65])
def test_current_codex_park_grant_survives_quiet_and_stagger(store, delay):
    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"codex": False, "ring": True})
    store.update(*creds(recipient), park_reason="blocked", park_needs="review", park_clear_by=sender["agent_id"])
    attached = store.attach(*creds(recipient), attachment_id="fixture", ring=True, ring_armed_until=1060)
    store.test_time[0] = 1002
    sent = store.send(*creds(sender), to=recipient["agent_id"], text="review ready", request_id="quiet-grant")
    if delay == 65:
        store.storage.conn.execute("UPDATE coordination_wakes SET ring_at=1060")
    for offset in (5, 25, delay):
        store.test_time[0] = 1000 + offset
        answer = store.heartbeat(*creds(recipient), attachment_id="fixture", generation=attached["generation"], ring_armed_until=1060+offset)
        assert answer["wake"] and answer["wake"]["decision"] == "rung", "a current grant must be explicitly renewed, not silently omitted"
    assert answer["wake"]["message_expires_at"] == sent["expires_at"]
    store.ack(*creds(recipient), message_id=sent["message_id"])
    assert store.heartbeat(*creds(recipient), attachment_id="fixture", generation=attached["generation"], ring_armed_until=1120)["wake"] is None


def test_urgent_unknown_offer_explicitly_authorizes_one_queue(store):
    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"codex": False, "ring": True})
    attached = store.attach(*creds(recipient), attachment_id="fixture", ring=True, ring_armed_until=1060)
    sent = store.send(*creds(sender), to=recipient["agent_id"], text="urgent peer text", request_id="unknown-urgent", urgent=True)
    assert sent["wake"]["recipient_state"] == "unknown"
    assert sent["wake"]["delivery"] == "queue_pending"
    answer = store.heartbeat(*creds(recipient), attachment_id="fixture", generation=attached["generation"], ring_armed_until=1060)
    assert answer["wake"]["queue_allowed"] is True
    assert answer["wake"]["message_expires_at"] == sent["expires_at"]


def test_unknown_queue_notice_resumes_original_task_and_suppresses_duplicates(tmp_path):
    command, log = _stub(tmp_path)
    async def drive():
        now = [0.0]
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        bell = CodexDoorbell(command, clock=lambda: now[0])
        bell.watch(THREAD, adapter)
        try:
            now[0] = 60
            result = mailbox("m1")
            result["wake"] = {"decision": "attention", "reason": "urgent", "ring_at": 0, "queue_allowed": True, "recipient_state": "unknown", "message_expires_at": time.time()+86400}
            adapter._update_pending_count(result)
            await _settle(bell)
            assert len(_calls(log)) == 1
            text = _calls(log)[0]["argv"][-1]
            assert "turn state unknown" in text and "Continue the original task" in text
            assert "urgent peer text" not in text
            assert "queue_accepted" in (adapter.digest_path.parent / "ledger.log").read_text()
            now[0] = 120
            result["pending_preview"] = mailbox("m1", "m2")["pending_preview"]
            result["pending_count"] = 2
            result["wake"]["ring_at"] = 1
            adapter._update_pending_count(result)
            await _settle(bell)
            assert len(_calls(log)) == 1
        finally:
            await bell.aclose()
            if adapter._ring_timer:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


def test_origin_expiry_is_durable_and_is_an_explicit_unresolved_fallback(tmp_path):
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1, expires_at=2000.0, now=1000.0)
    pending.accept(first)
    restarted = PendingNotice(tmp_path, THREAD)
    assert restarted.reserve(2, expires_at=90000, now=1999) is None
    assert restarted.resolution(now=2000) == "unresolved_expired"
    second = restarted.reserve(2, expires_at=90000, now=2000)
    assert second and second["nonce"] != first["nonce"]
    assert second["expires_at"] == 90000
    assert restarted.resolution(now=2001) is None


def test_valid_legacy_notice_gets_one_durable_conservative_expiry(tmp_path):
    pending = PendingNotice(tmp_path, THREAD)
    nonce = "a" * 32
    legacy = {"thread_id": THREAD, "nonce": nonce, "count": 1, "text": doorbell_text(1, nonce)}
    assert pending.reserve(1)
    pending._replace(legacy)
    assert pending.resolution(now=1000) is None
    migrated = json.loads(pending.path.read_text())
    assert migrated["expiry_basis"] == "legacy_upper_bound"
    assert migrated["expires_at"] == 1000+86400
    restarted = PendingNotice(tmp_path, THREAD)
    assert restarted.resolution(now=2000) is None
    assert json.loads(pending.path.read_text())["expires_at"] == 87400
    assert restarted.resolution(now=87400) == "unresolved_expired"


def test_stored_versioned_notice_survives_current_template_wording_change(tmp_path, monkeypatch):
    import pseudolife_memory.codex_doorbell as module
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1)
    monkeypatch.setattr(module, "doorbell_text", lambda *a, **k: "future notice")
    assert pending.note_prompt({"session_id": THREAD, "prompt": first["text"]})
    assert pending.resolved()


def test_empty_lock_contention_retains_offer_without_phantom_outstanding(tmp_path):
    command, log = _stub(tmp_path)
    async def drive():
        now = [0.0]
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        bell = CodexDoorbell(command, clock=lambda: now[0])
        bell.watch(THREAD, adapter)
        pending = bell._bells[THREAD].pending_notice
        try:
            now[0] = 60
            answer = mailbox("m1", offer=1)
            with pending._locked() as taken:
                assert taken
                adapter._update_pending_count(answer)
                await _settle(bell)
            assert not pending.path.exists()
            assert not bell._bells[THREAD].outstanding
            assert adapter._ring_offer is not None
            assert not bell._tasks and not _calls(log)
            ledger = (tmp_path / "ledger.log").read_text(encoding="utf-8")
            assert "\tbell_deferred\t" in ledger
            assert "reservation_unavailable no_queue_attempt" in ledger
            assert "\tbell_pending\t" not in ledger
            adapter._update_pending_count(answer)
            await _settle(bell)
            assert len(_calls(log)) == 1
            adapter._update_pending_count(mailbox("m1", "m2", offer=2))
            await _settle(bell)
            assert len(_calls(log)) == 1
        finally:
            await bell.aclose()
            if adapter._ring_timer:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


def test_existing_reservation_reports_pending_without_another_queue_attempt(tmp_path):
    command, log = _stub(tmp_path)
    async def drive():
        now = [0.0]
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        bell = CodexDoorbell(command, clock=lambda: now[0])
        bell.watch(THREAD, adapter)
        pending = PendingNotice(tmp_path, THREAD)
        notice = pending.reserve(1)
        pending.accept(notice)
        try:
            now[0] = 60
            adapter._update_pending_count(mailbox("m1", offer=1))
            await _settle(bell)
            assert pending.path.exists() and bell._bells[THREAD].outstanding
            ledger = (tmp_path / "ledger.log").read_text(encoding="utf-8")
            assert "\tbell_pending\t" in ledger
            assert "unresolved queue transport" in ledger
            assert "\tbell_deferred\t" not in ledger
            adapter._update_pending_count(mailbox("m1", "m2", offer=2))
            await _settle(bell)
            assert not bell._tasks and not _calls(log)
        finally:
            await bell.aclose()
            if adapter._ring_timer:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


def test_definite_spawn_failure_rolls_back_only_its_private_reservation(tmp_path, monkeypatch):
    async def fail_spawn(*args, **kwargs):
        raise FileNotFoundError(2, "fixture missing CLI")
    async def drive():
        now = [0.0]
        bell = CodexDoorbell(["fixture-missing-cli"], clock=lambda: now[0])
        box = Mailbox()
        bell.watch(THREAD, box)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
        now[0] = 60
        box.set("m1")
        await _settle(bell)
        assert not bell._bells[THREAD].pending_notice.path.exists()
        assert not bell._bells[THREAD].outstanding
        await bell.aclose()
    asyncio.run(drive())


def test_single_exact_prompt_waits_briefly_for_real_lock_contention(tmp_path):
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1)
    locked = threading.Event()
    def hold():
        with pending._locked() as taken:
            assert taken
            locked.set()
            time.sleep(0.08)
    worker = threading.Thread(target=hold)
    worker.start()
    assert locked.wait(1)
    try:
        assert PendingNotice(tmp_path, THREAD).note_prompt({"session_id": THREAD, "prompt": first["text"]})
    finally:
        worker.join(1)
    assert pending.resolved()


def test_three_current_park_grants_reach_doorbell_after_quiet_and_real_stagger(store, tmp_path, monkeypatch):
    sender = store.register("alice")
    peers = [store.register("alice", capabilities={"codex": False, "ring": True}) for _ in range(3)]
    attachments = []
    for peer in peers:
        store.update(*creds(peer), park_reason="blocked", park_needs="review", park_clear_by=sender["agent_id"])
        attachments.append(store.attach(*creds(peer), attachment_id=peer["agent_id"][:8], ring=True, ring_armed_until=1060))
    monkeypatch.setattr(time, "time", lambda: store.test_time[0])
    async def drive():
        bell = RecordingDoorbell(store.test_time)
        adapters = []
        try:
            for index, attached in enumerate(attachments):
                adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / str(index) / "digest.txt")
                adapter._update_pending_count(attached)
                adapters.append(adapter)
                thread = f"aaaaaaaa-1111-4111-8111-{index:012d}"
                bell.watch(thread, adapter)
            store.test_time[0] = 1002
            sent = store.send(*creds(sender), to="all", text="review ready", request_id="real-three-stagger")
            assert sorted(r["wake"]["ring_at"] for r in sent["receipts"]) == [1002,1032,1062]
            for offset in (5,25,45,65):
                store.test_time[0] = 1000+offset
                for peer, attached, adapter in zip(peers, attachments, adapters):
                    answer = store.heartbeat(*creds(peer), attachment_id=peer["agent_id"][:8], generation=attached["generation"], ring_armed_until=1060+offset)
                    adapter._update_pending_count(answer)
                await settle(bell)
            assert len(bell.queued) == 3, "daemon omission must not silently lose quiet or staggered grants"
        finally:
            await bell.aclose()
            for adapter in adapters:
                if adapter._ring_timer:
                    adapter._ring_timer.cancel()
    asyncio.run(drive())


@pytest.mark.parametrize("state, allowed", [("busy", True), ("unknown", False)])
def test_attention_without_unknown_queue_permission_never_queues(tmp_path, state, allowed):
    async def drive():
        now = [0.0]
        bell = RecordingDoorbell(now)
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        bell.watch(THREAD, adapter)
        try:
            now[0] = 60
            answer = mailbox("m1")
            answer["wake"] = {"decision": "attention", "reason": "urgent", "ring_at": 0, "queue_allowed": allowed, "recipient_state": state}
            adapter._update_pending_count(answer)
            await settle(bell)
            assert bell.queued == []
            assert not (tmp_path / "digest.ring").exists()
            assert f"recipient_state_{state}" in (tmp_path / "ledger.log").read_text()
        finally:
            await bell.aclose()
    asyncio.run(drive())


def test_ambiguous_spawn_result_keeps_durable_reservation(tmp_path, monkeypatch):
    async def ambiguous(*a, **k):
        raise RuntimeError("fixture transport lost after possible launch")
    async def drive():
        now = [0.0]
        bell = CodexDoorbell(["fixture-cli"], clock=lambda: now[0])
        box = Mailbox()
        bell.watch(THREAD, box)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", ambiguous)
        now[0] = 60
        box.set("m1")
        await _settle(bell)
        assert bell._bells[THREAD].outstanding
        assert bell._bells[THREAD].pending_notice.path.exists()
        assert PendingNotice(tmp_path, THREAD).reserve(2) is None
        await bell.aclose()
    asyncio.run(drive())


def test_origin_expiry_rearms_after_restart_with_explicit_receipt(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    async def drive():
        first = RecordingDoorbell(now)
        box = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        first.watch(THREAD, box)
        now[0] = 1060
        answer = mailbox("m1", offer=1)
        answer["wake"]["message_expires_at"] = 87400
        box._update_pending_count(answer)
        await settle(first)
        assert len(first.queued) == 1
        original = first.queued[0]
        await first.aclose()
        restarted = RecordingDoorbell(now)
        restarted.watch(THREAD, box)
        now[0] = 2000
        answer = mailbox("m1", "m2", offer=2)
        answer["wake"]["message_expires_at"] = 88400
        box._update_pending_count(answer)
        await settle(restarted)
        assert restarted.queued == []
        assert json.loads(restarted._bells[THREAD].pending_notice.path.read_text())["expires_at"] == original["expires_at"]
        now[0] = 87400
        answer = mailbox("m2", "m3", offer=3)
        answer["wake"]["message_expires_at"] = 173800
        box._update_pending_count(answer)
        await settle(restarted)
        assert len(restarted.queued) == 1
        ledger = (tmp_path / "ledger.log").read_text()
        assert "unresolved_expired" in ledger and "native_cancellation_unknown origin_message_expiry" in ledger
        assert restarted.queued[0]["nonce"] != original["nonce"]
        await restarted.aclose()
        if box._ring_timer:
            box._ring_timer.cancel()
    asyncio.run(drive())


@pytest.mark.parametrize("invalid", ["version", "expiry", "text", "legacy_extra", "missing_expiry"])
def test_invalid_notice_version_or_expiry_never_authorizes_rearm(tmp_path, invalid):
    pending = PendingNotice(tmp_path, THREAD)
    record = pending.reserve(1, expires_at=time.time()+86400)
    if invalid == "version":
        record["version"] = 99
    elif invalid == "expiry":
        record["expires_at"] = float("nan")
    elif invalid == "missing_expiry":
        record.pop("expires_at")
    elif invalid == "text":
        record["text"] = "peer-provided arbitrary prompt"
    else:
        record = {k: record[k] for k in ("thread_id", "nonce", "count", "text")}
        record["expires_at"] = 1  # older valid records had exactly four fields
    pending._replace(record)
    assert pending.resolution(now=10**12) is None
    assert not pending.note_prompt({"session_id": THREAD, "prompt": record["text"]})
    assert pending.reserve(2) is None


def test_rollback_cannot_remove_an_accepted_or_newer_reservation(tmp_path):
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1)
    pending.accept(first)
    assert not pending.rollback_unstarted(first)
    assert pending.note_prompt({"session_id": THREAD, "prompt": first["text"]})
    second = pending.reserve(2)
    assert second
    assert not pending.rollback_unstarted(first)
    assert json.loads(pending.path.read_text())["nonce"] == second["nonce"]


def test_historical_hint_repeat_is_deduplicated_and_expires_without_new_cap_rows(store, tmp_path):
    from tests.test_coordination_storage import _legacy_codex_urgent_ring
    _, recipient = _legacy_codex_urgent_ring(store)
    store.test_time[0] = 2001
    attached = store.attach(*creds(recipient), attachment_id="fixture", ring=True, ring_armed_until=2061)
    adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
    for instant in (2002, 2022, 2042):
        store.test_time[0] = instant
        answer = store.heartbeat(*creds(recipient), attachment_id="fixture", generation=attached["generation"], ring_armed_until=instant+60)
        adapter._update_pending_count(answer)
        assert adapter.ring_due() is None
    assert (tmp_path / "ledger.log").read_text().count("\tattention\t") == 1
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone()[0] == 1
    store.storage.conn.execute("UPDATE coordination_messages SET expires_at=2043")
    store.test_time[0] = 2043
    assert store.heartbeat(*creds(recipient), attachment_id="fixture", generation=attached["generation"], ring_armed_until=2103)["wake"] is None


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process lifecycle")
def test_definite_suspended_adoption_failure_releases_its_reservation(tmp_path, monkeypatch):
    import pseudolife_memory.codex_doorbell as module
    def refuse(self, process):
        raise OSError("fixture before resume")
    monkeypatch.setattr(module._KillJob, "adopt", refuse)
    command, log = _stub(tmp_path)
    async def drive():
        now = [0.0]
        bell = CodexDoorbell(command, clock=lambda: now[0])
        box = Mailbox()
        bell.watch(THREAD, box)
        now[0] = 60
        box.set("m1")
        await _settle(bell)
        assert _calls(log) == []
        assert not bell._bells[THREAD].pending_notice.path.exists()
        assert not bell._bells[THREAD].outstanding
        await bell.aclose()
    asyncio.run(drive())


def test_late_accept_after_expiry_does_not_claim_a_prompt_receipt(tmp_path):
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1, expires_at=2000, now=1000)
    second = pending.reserve(2, expires_at=90000, now=2000)
    assert second
    receipt = json.loads(pending.expired_path.read_text())
    assert receipt["nonce"] == first["nonce"]
    assert receipt["outcome"] == "unresolved_expired"
    assert receipt["native_cancellation"] == "unknown"
    assert not pending.accept(first)
    assert not pending.prompt_seen_path.exists()
    assert json.loads(pending.path.read_text())["nonce"] == second["nonce"]


def test_reserve_expiry_receipt_failure_preserves_old_nonce(tmp_path, monkeypatch):
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1, expires_at=2000, now=1000)
    def refuse(path, text):
        raise OSError("fixture receipt persistence unavailable")
    monkeypatch.setattr(pending, "_write_atomic", refuse)
    assert pending.reserve(2, expires_at=90000, now=2000) is None
    assert json.loads(pending.path.read_text())["nonce"] == first["nonce"]
    assert not pending.expired_path.exists()


def test_external_expired_reservation_logs_once_before_new_queue(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    async def drive():
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        bell = RecordingDoorbell(now)
        bell.watch(THREAD, adapter)
        assert not bell._bells[THREAD].outstanding
        other = PendingNotice(tmp_path, THREAD)
        first = other.reserve(1, expires_at=1100, now=1000)
        other.accept(first)
        try:
            now[0] = 1200
            answer = mailbox("m2", offer=1)
            answer["wake"]["message_expires_at"] = 2000
            adapter._update_pending_count(answer)
            await settle(bell)
            assert len(bell.queued) == 1
            receipt = json.loads(other.expired_path.read_text())
            assert receipt["nonce"] == first["nonce"]
            assert receipt["expires_at"] == 1100
            assert receipt["native_cancellation"] == "unknown"
            ledger = (tmp_path / "ledger.log").read_text()
            assert ledger.count("\tunresolved_expired\t") == 1
            assert "native_cancellation_unknown origin_message_expiry" in ledger
            assert ledger.index("\tunresolved_expired\t") < ledger.index("\tbell\t")
            adapter._update_pending_count(answer)
            await settle(bell)
            assert (tmp_path / "ledger.log").read_text().count("\tunresolved_expired\t") == 1
            assert not other.note_prompt({"session_id": THREAD, "prompt": first["text"]})
            assert json.loads(other.path.read_text())["nonce"] == bell.queued[0]["nonce"]
        finally:
            await bell.aclose()
            if adapter._ring_timer:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


def test_reserve_exact_prompt_release_is_not_an_expiry_receipt(tmp_path):
    pending = PendingNotice(tmp_path, THREAD)
    first = pending.reserve(1, expires_at=2000, now=1000)
    assert pending.note_prompt({"session_id": THREAD, "prompt": first["text"]})
    second = pending.reserve(2, expires_at=90000, now=2000)
    assert second and second["nonce"] != first["nonce"]
    assert not pending.expired_path.exists()


def test_disabled_transport_still_observes_lazy_expiry_without_retry(tmp_path, monkeypatch):
    now = [1000.0]
    attempts = []
    monkeypatch.setattr(time, "time", lambda: now[0])
    async def ambiguous(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("fixture ambiguous launch result")
    async def drive():
        bell = CodexDoorbell(["fixture-cli"], clock=lambda: now[0])
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
        bell.watch(THREAD, adapter)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", ambiguous)
        try:
            now[0] = 1060
            answer = mailbox("m1", offer=1)
            answer["wake"]["message_expires_at"] = 1070
            adapter._update_pending_count(answer)
            await _settle(bell)
            assert bell._disabled and bell._bells[THREAD].outstanding
            now[0] = 1070
            adapter._update_pending_count(mailbox())
            assert bell._disabled and not bell._bells[THREAD].outstanding
            pending = bell._bells[THREAD].pending_notice
            assert not pending.path.exists()
            assert json.loads(pending.expired_path.read_text())["outcome"] == "unresolved_expired"
            ledger = (tmp_path / "ledger.log").read_text()
            assert ledger.count("\tunresolved_expired\t") == 1
            adapter._update_pending_count(mailbox("m2", offer=2))
            assert len(attempts) == 1
            assert (tmp_path / "ledger.log").read_text().count("\tunresolved_expired\t") == 1
        finally:
            await bell.aclose()
            if adapter._ring_timer:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


def test_busy_known_attention_receipt_replaces_unknown_receipt_without_queue(tmp_path):
    adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture", delivery_transport="codex", digest_path=tmp_path / "digest.txt")
    answer = mailbox("m1")
    answer["wake"] = {"decision": "attention", "reason": "urgent", "ring_at": 0}
    adapter._update_pending_count(answer)
    answer["wake"]["recipient_state"] = "busy"
    adapter._update_pending_count(answer)
    assert adapter.ring_due() is None
    ledger = (tmp_path / "ledger.log").read_text()
    assert "recipient_state_unknown" in ledger and "recipient_state_busy" in ledger
    assert "recipient turn state busy" in adapter.unread_hint
