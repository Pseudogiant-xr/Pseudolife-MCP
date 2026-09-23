"""The board's audit log: durable, append-only, tamper-evident.

The live mailbox forgets on purpose. Bodies blank after a day, rows go after
seven, idle addresses go after an hour or a week, and a status update
overwrites the one before it. ``coordination_events`` keeps what happened:
one hash-chained row per board mutation, written in that mutation's own
transaction and pruned only by its own, separately configured window. The
pruning is logged too.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import time

import psycopg
from psycopg.rows import dict_row
import pytest

from pseudolife_memory.storage.coordination import CoordinationError, CoordinationStore
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_storage import Storage, creds, pair, store  # noqa: F401

DAY = 86400


def events(store, event=None):
    sql = ("SELECT * FROM coordination_events" + (" WHERE event=%s" if event else "")
           + " ORDER BY seq")
    with store.storage.conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (event,) if event else ())
        rows = cur.fetchall()
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


def chain(store):
    from pseudolife_memory.storage.coordination import audit_events
    return list(audit_events(store.storage.conn))


def verify(store, **kwargs):
    from pseudolife_memory.storage.coordination import verify_audit_chain
    return verify_audit_chain(chain(store), **kwargs)


def test_prune_blanks_the_live_body_but_the_audit_log_keeps_it(store):
    a, b = pair(store)
    msg = store.send(*creds(a), to=b["agent_id"], text="suite=running on relay #6", request_id="r")
    store.test_time[0] += DAY + 1
    store.prune()
    assert store.storage.conn.execute(
        "SELECT text FROM coordination_messages WHERE message_id=%s",
        (msg["message_id"],)).fetchone() == (None,)
    [sent] = events(store, "send")
    assert sent["message_id"] == msg["message_id"]
    assert sent["payload"]["text"] == "suite=running on relay #6"
    assert (sent["actor"], sent["principal"], sent["agent_id"], sent["recipient_agent_id"]) == (
        "agent", "alice", a["agent_id"], b["agent_id"])
    [expired] = events(store, "expire")
    assert (expired["actor"], expired["payload"]) == ("daemon", {"message_ids": [msg["message_id"]]})

    store.test_time[0] += 7 * DAY
    store.prune()
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_messages").fetchone() == (0,)
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_agents").fetchone() == (0,)
    assert events(store, "send")[0]["payload"]["text"] == "suite=running on relay #6"
    [pruned] = events(store, "prune")
    assert pruned["payload"] == {"message_ids": [msg["message_id"]],
                                 "agent_ids": sorted([a["agent_id"], b["agent_id"]])}
    assert verify(store)["ok"]


def test_every_status_update_is_kept_with_the_value_it_replaced(store):
    a = store.register("alice", project="p", task="t", status="starting")
    for status in ("suite=queued #3", "suite=running", "suite=idle"):
        store.test_time[0] += 60
        store.update(*creds(a), status=status)
    # The live row keeps only the last one; the log keeps them all, in order.
    assert store.authenticate(*creds(a))["status"] == "suite=idle"
    assert [(e["created_at"], e["payload"]) for e in events(store, "update")] == [
        (1060.0, {"fields": {"status": "suite=queued #3"}, "before": {"status": "starting"}}),
        (1120.0, {"fields": {"status": "suite=running"}, "before": {"status": "suite=queued #3"}}),
        (1180.0, {"fields": {"status": "suite=idle"}, "before": {"status": "suite=running"}}),
    ]
    [registered] = events(store, "register")
    assert registered["payload"]["status"] == "starting"
    assert all((e["agent_id"], e["principal"], e["project"], e["task"])
               == (a["agent_id"], "alice", "p", "t") for e in events(store))
    store.update(*creds(a), task="t2")
    moved = events(store, "update")[-1]
    assert (moved["task"], moved["payload"]["before"]) == ("t2", {"task": "t"})


def test_first_read_is_stamped_once_for_pull_and_for_delivery(store):
    a = store.register("alice")
    b = store.register("alice", wake_enabled=True)
    pulled = store.send(*creds(a), to=b["agent_id"], text="pull me", request_id="pull")
    store.test_time[0] += 5
    page = store.receive(*creds(b))
    assert [m["message_id"] for m in page["messages"]] == [pulled["message_id"]]
    store.test_time[0] += 5
    store.receive(*creds(b))  # a replay of unacknowledged mail is not a first read

    def first_read(message_id):
        return store.storage.conn.execute(
            "SELECT first_read_at FROM coordination_messages WHERE message_id=%s",
            (message_id,)).fetchone()[0]

    assert first_read(pulled["message_id"]) == 1005.0
    attached = store.attach(*creds(b), attachment_id="live", wake_enabled=True)
    pushed = store.send(*creds(a), to=b["agent_id"], text="push me", request_id="push")
    store.test_time[0] += 1
    delivered = store.receive(*creds(b), after=page["after"], for_delivery=True)
    assert [m["message_id"] for m in delivered["messages"]] == [pushed["message_id"]]
    store.mark_attempt(*creds(b), message_id=pushed["message_id"], attachment_id="live",
                       generation=attached["generation"])
    assert first_read(pushed["message_id"]) == 1011.0
    assert [(e["message_id"], e["payload"]["path"], e["created_at"], e["agent_id"])
            for e in events(store, "read")] == [
        (pulled["message_id"], "pull", 1005.0, b["agent_id"]),
        (pushed["message_id"], "delivery", 1011.0, b["agent_id"]),
    ]
    [attempt] = events(store, "attempt")
    assert (attempt["message_id"], attempt["payload"]) == (
        pushed["message_id"], {"generation": attached["generation"], "attempts": 1,
                               "sender_agent_id": a["agent_id"]})


def test_an_empty_poll_or_a_replay_opens_no_transaction(store, monkeypatch):
    """Receive is the hot path (long-poll loops, the delivery adapter): only a
    first read may write, so everything else stays one autocommit read."""
    a, b = pair(store)
    opened = []
    real = store.storage._txn

    def counting():
        opened.append(1)
        return real()

    store.receive(*creds(b))
    store.send(*creds(a), to=b["agent_id"], text="once", request_id="r")
    monkeypatch.setattr(store.storage, "_txn", counting)
    store.receive(*creds(b))
    store.receive(*creds(b))
    assert len(opened) == 1
    assert len(events(store, "read")) == 1


def test_each_newly_acknowledged_message_is_logged_once(store):
    a, b = pair(store)
    ids = [store.send(*creds(a), to=b["agent_id"], text=f"n{i}", request_id=f"r{i}")["message_id"]
           for i in range(3)]
    store.ack(*creds(b), message_id=",".join(ids[:2] + ["not-mine"]))
    store.ack(*creds(b), message_id=ids[0])  # already acknowledged: no mutation, no event
    assert [(e["message_id"], e["agent_id"], e["payload"]) for e in events(store, "ack")] == [
        (ids[0], b["agent_id"], {"sender_agent_id": a["agent_id"]}),
        (ids[1], b["agent_id"], {"sender_agent_id": a["agent_id"]}),
    ]


def test_lease_renewals_are_not_logged_but_attach_and_detach_are(store):
    a = store.register("alice")
    first = store.attach(*creds(a), attachment_id="one")
    for _ in range(5):
        store.test_time[0] += 20
        store.heartbeat(*creds(a), attachment_id="one", generation=first["generation"], active=True)
    store.detach(*creds(a), attachment_id="one", generation=first["generation"])
    assert [e["event"] for e in events(store)] == ["register", "attach", "detach"]
    attach, detach = events(store)[1:]
    assert attach["payload"]["generation"] == first["generation"]
    assert attach["payload"]["renewed"] is False
    assert detach["payload"] == {"generation": first["generation"]}


def test_refused_and_rolled_back_mutations_leave_no_event(store):
    a, b = pair(store)
    msg = store.send(*creds(a), to=b["agent_id"], text="once", request_id="r")
    logged = len(events(store))
    assert store.send(*creds(a), to=b["agent_id"], text="once", request_id="r")["message_id"] == msg["message_id"]
    with pytest.raises(CoordinationError, match="request_conflict"):
        store.send(*creds(a), to=b["agent_id"], text="changed", request_id="r")
    with pytest.raises(CoordinationError, match="recipient_not_found"):
        store.send(*creds(a), to="missing", text="lost", request_id="r2")
    with pytest.raises(CoordinationError, match="message_not_found"):
        store.ack(*creds(a), message_id=msg["message_id"])
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.update("alice", a["agent_id"], b["credential"], status="spoofed")
    with pytest.raises(RuntimeError, match="rollback"):
        with store.storage._txn():
            store.update(*creds(a), status="rolled back")
            raise RuntimeError("rollback")
    assert len(events(store)) == logged
    assert verify(store)["ok"]


def test_every_board_mutation_path_is_logged_and_no_credential_is(store):
    store.context("alice")
    store.context("alice")  # the bank identity is established once
    a = store.register("alice", label="writer", capabilities={"pull": True})
    b = store.register("alice", wake_enabled=True)
    store.update(*creds(a), status="working")
    attached = store.attach(*creds(b), attachment_id="live", wake_enabled=True)
    msg = store.send(*creds(a), to=b["agent_id"], text="hello", request_id="r")
    store.receive(*creds(b), for_delivery=True)
    store.mark_attempt(*creds(b), message_id=msg["message_id"], attachment_id="live",
                       generation=attached["generation"])
    store.ack(*creds(b), message_id=msg["message_id"])
    store.detach(*creds(b), attachment_id="live", generation=attached["generation"])
    recovered = store.recover()
    rebound = store.rebind(a["agent_id"], "alice")
    store.test_time[0] += 8 * DAY
    store.prune()
    assert [e["event"] for e in events(store)] == [
        "bank_identity", "register", "register", "update", "attach", "send", "read",
        "attempt", "ack", "detach", "recover", "rebind", "expire", "prune"]
    logged = json.dumps(events(store))
    for secret in (a["credential"], b["credential"], rebound["credential"]):
        assert secret not in logged
    assert recovered == {"revoked": 2, "audited": True}
    assert verify(store)["ok"]


def test_restore_recovery_and_rebind_are_operator_events(store):
    a, b = pair(store)
    assert store.recover() == {"revoked": 2, "audited": True}
    [recovered] = events(store, "recover")
    assert (recovered["actor"], recovered["principal"]) == ("operator", "")
    assert recovered["payload"] == {"agent_ids": sorted([a["agent_id"], b["agent_id"]])}
    rebound = store.rebind(b["agent_id"], "alice")
    assert rebound["audited"] is True
    [rebind] = events(store, "rebind")
    assert (rebind["actor"], rebind["agent_id"], rebind["payload"]) == (
        "operator", b["agent_id"], {"principal": "alice"})


def test_recovery_still_revokes_on_a_restore_that_predates_the_audit_log(store):
    """A backup taken before v42 restores without the table, and recovery
    runs with the daemon stopped, before any schema pass could create it.
    Revocation must not depend on a log that does not exist yet; the result
    says it went unrecorded."""
    a = store.register("alice")
    store.storage.conn.execute("DROP TABLE coordination_events")
    assert store.recover() == {"revoked": 1, "audited": False}
    assert store.rebind(a["agent_id"], "alice")["audited"] is False


def test_the_recovery_cli_says_when_the_restored_bank_cannot_record_it(
        store, pg_url, tmp_path, monkeypatch, capsys):
    from tests.test_coordination_recovery import invoke, settings
    store.register("alice")
    store.storage.conn.execute("DROP TABLE coordination_events")
    assert invoke(monkeypatch, pg_url, settings(tmp_path), "recover", "--confirm-restore") == 0
    assert "not recorded" in capsys.readouterr().out


def test_verify_accepts_the_intact_chain_and_names_the_first_tampered_row(store):
    a, b = pair(store)
    for n in range(3):
        store.send(*creds(a), to=b["agent_id"], text=f"note {n}", request_id=f"r{n}")
    report = verify(store)
    assert report["ok"] and (report["events"], report["first_seq"], report["head_seq"]) == (5, 1, 5)
    assert (report["head_created_at"], report["start_cut"]) == (1000.0, None)
    store.storage.conn.execute(
        "UPDATE coordination_events SET payload=replace(payload, 'note 1', 'note X') WHERE seq=4")
    assert verify(store) == {"ok": False, "seq": 4, "reason": "hash_mismatch"}


@pytest.mark.parametrize("damage,seq,reason", [
    ("DELETE FROM coordination_events WHERE seq=3", 4, "sequence_gap"),
    ("DELETE FROM coordination_events WHERE seq<=2", 3, "unanchored_start"),
    ("UPDATE coordination_events SET prev_hash=repeat('0', 64) WHERE seq=4", 4, "broken_link"),
    ("UPDATE coordination_events SET created_at=created_at+1 WHERE seq=2", 2, "hash_mismatch"),
    ("UPDATE coordination_events SET principal='mallory' WHERE seq=5", 5, "hash_mismatch"),
])
def test_verify_fails_on_removed_relinked_or_edited_rows(store, damage, seq, reason):
    a, b = pair(store)
    for n in range(3):
        store.send(*creds(a), to=b["agent_id"], text=f"note {n}", request_id=f"r{n}")
    store.storage.conn.execute(damage)
    assert verify(store) == {"ok": False, "seq": seq, "reason": reason}


def test_tail_truncation_and_a_recomputed_rewrite_need_an_external_head(store):
    """What the chain does not prove on its own: dropping the newest rows
    leaves a valid prefix, and anyone with write access to the table can
    rewrite it and recompute every hash, because there is no secret. A head
    recorded elsewhere (``verify`` prints it) catches both."""
    from pseudolife_memory.storage.coordination import GENESIS_HASH, audit_hash
    a, b = pair(store)
    for n in range(3):
        store.send(*creds(a), to=b["agent_id"], text=f"note {n}", request_id=f"r{n}")
    head = verify(store)
    recorded = (head["head_seq"], head["head_hash"])
    assert verify(store, expect_head=recorded)["ok"]

    store.storage.conn.execute("DELETE FROM coordination_events WHERE seq=5")
    assert verify(store)["ok"]
    assert verify(store, expect_head=recorded) == {"ok": False, "seq": 5, "reason": "head_missing"}

    fourth = (4, chain(store)[3]["hash"])
    prev = GENESIS_HASH
    for row in chain(store):
        if row["seq"] == 3:
            row["payload"] = row["payload"].replace("note 0", "rewritten")
        row["prev_hash"], row["hash"] = prev, audit_hash(prev, row)
        prev = row["hash"]
        store.storage.conn.execute(
            "UPDATE coordination_events SET payload=%s, prev_hash=%s, hash=%s WHERE seq=%s",
            (row["payload"], row["prev_hash"], row["hash"], row["seq"]))
    assert "rewritten" in chain(store)[2]["payload"]
    assert verify(store)["ok"]
    assert verify(store, expect_head=fourth) == {"ok": False, "seq": 4, "reason": "head_mismatch"}


def test_an_expected_head_that_retention_removed_cannot_be_confirmed(store):
    a, b = pair(store)
    early = (1, chain(store)[0]["hash"])
    store.test_time[0] += 10 * DAY
    store.update(*creds(a), status="later")
    store.prune(audit_retention_days=7)
    assert verify(store)["ok"]
    pruned = verify(store, expect_head=early)
    assert (pruned["ok"], pruned["seq"], pruned["reason"]) == (False, 1, "head_pruned")
    [cut] = events(store, "audit_prune")
    assert pruned["start_cut"] == {"seq": cut["seq"], "created_at": cut["created_at"],
                                   "cutoff": 3 * DAY, "retention_days": 7, "through_seq": 2}


def test_audit_retention_prunes_an_old_prefix_logs_the_cut_and_still_verifies(store):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="old", request_id="r")
    third = chain(store)[2]
    store.test_time[0] += 10 * DAY
    store.update(*creds(a), status="later")
    result = store.prune(audit_retention_days=7)
    assert result["audit_removed"] == 3
    assert [e["event"] for e in events(store)] == ["update", "expire", "prune", "audit_prune"]
    [cut] = events(store, "audit_prune")
    assert cut["actor"] == "daemon"
    # The cut falls on a UTC day boundary: at least 7 days kept, at most 8.
    assert cut["payload"] == {"through_seq": 3, "through_hash": third["hash"], "removed": 3,
                              "retention_days": 7, "cutoff": 3 * DAY}
    report = verify(store)
    assert report["ok"] and report["first_seq"] == 4

    store.test_time[0] += 30 * DAY
    store.prune(audit_retention_days=7)
    # Everything older than the window went, including the head: the chain
    # continues from the removed head instead of restarting at genesis.
    assert [e["event"] for e in events(store)] == ["prune", "audit_prune"]
    assert chain(store)[0]["seq"] == 8
    assert verify(store)["ok"]


def test_retention_cuts_at_most_once_a_day_so_its_own_records_cannot_feed_it(store):
    """Each cut appends an audit_prune, which ages out a window later. Cut
    whenever anything aged out and a steadily used board makes a new cut on
    almost every prune pass, forever, mostly of earlier cut records (review
    of 15f31aec: 30 cutting passes a day from 31 events). On a day boundary
    it is one cut a day."""
    a, b = pair(store)
    for hour in range(24):                      # a day of activity, hourly
        store.test_time[0] += 3600
        store.update(*creds(a), status=f"hour {hour}")
    cutting_days = {}
    for _ in range(4 * 48):                     # four days of half-hourly passes
        store.test_time[0] += 1800
        store.update(*creds(b), status="still here")
        if store.prune(audit_retention_days=1)["audit_removed"]:
            day = int(store.test_time[0] // DAY)
            cutting_days[day] = cutting_days.get(day, 0) + 1
    assert cutting_days and max(cutting_days.values()) == 1
    assert len(events(store, "audit_prune")) <= 2
    assert verify(store)["ok"]


def test_retention_refuses_a_negative_window(store):
    pair(store)
    with pytest.raises(ValueError, match="audit_retention_days"):
        store.prune(audit_retention_days=-1)
    assert len(events(store)) == 2


def _forged_prefix_cut(store, *, actor="daemon", created_at, cutoff, retention_days):
    """Remove seq 1-3 of a five-row chain and append a chained audit_prune
    naming the removed rows, as someone with write access to the table could.
    Returns the original rows."""
    from pseudolife_memory.storage.coordination import audit_hash
    a, b = pair(store)
    for n in range(3):
        store.send(*creds(a), to=b["agent_id"], text=f"note {n}", request_id=f"r{n}")
    rows = chain(store)
    store.storage.conn.execute("DELETE FROM coordination_events WHERE seq<=3")
    forged = {"seq": 6, "event": "audit_prune", "actor": actor, "principal": "",
              "agent_id": "", "recipient_agent_id": None, "project": "", "task": "",
              "message_id": None, "created_at": created_at, "hlc": "",
              "payload": json.dumps({"through_seq": 3, "through_hash": rows[2]["hash"],
                                     "removed": 3, "cutoff": cutoff,
                                     "retention_days": retention_days},
                                    sort_keys=True, separators=(",", ":")),
              "prev_hash": rows[-1]["hash"]}
    forged["hash"] = audit_hash(forged["prev_hash"], forged)
    store.storage.conn.execute(
        "INSERT INTO coordination_events (seq,event,actor,principal,agent_id,"
        "recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash) "
        "VALUES (%(seq)s,%(event)s,%(actor)s,%(principal)s,%(agent_id)s,"
        "%(recipient_agent_id)s,%(project)s,%(task)s,%(message_id)s,%(payload)s,"
        "%(created_at)s,%(hlc)s,%(prev_hash)s,%(hash)s)", forged)
    return rows


@pytest.mark.parametrize("forgery", [
    # A cutoff its own time and window cannot produce (they give 0).
    dict(created_at=1000.0 + 7 * DAY, cutoff=500, retention_days=7),
    # A window that never prunes (its cutoff arithmetic alone would pass),
    # or is not a whole number of days.
    dict(created_at=2000.0, cutoff=0, retention_days=0),
    dict(created_at=1000.0 + 7 * DAY, cutoff=0, retention_days=7.0),
    # Values no clock or config can produce must fail, not crash verify.
    dict(created_at=float("inf"), cutoff=0, retention_days=7),
    dict(created_at=float("nan"), cutoff=0, retention_days=7),
    dict(created_at=1000.0 + 7 * DAY, cutoff=0, retention_days=10 ** 400),
    # Not the daemon's own maintenance.
    dict(actor="agent", created_at=1000.0 + 7 * DAY, cutoff=0, retention_days=7),
    # A real cut at that cutoff would have removed the first surviving row too.
    dict(created_at=20 * DAY, cutoff=13 * DAY, retention_days=7),
])
def test_a_cut_record_that_does_not_add_up_does_not_anchor_the_chain(store, forgery):
    """A removed prefix is accepted only behind a daemon cut whose own fields
    agree with how prune makes one."""
    _forged_prefix_cut(store, **forgery)
    assert verify(store) == {"ok": False, "seq": 4, "reason": "unanchored_start"}


def test_a_careful_forged_cut_passes_verify_and_only_an_earlier_recorded_head_exposes_it(store):
    """The documented limit: with no secret, someone who can write the table
    can remove the oldest rows and append a consistent cut record, and verify
    passes, even against a head recorded after those rows. An earlier
    recorded head exposes it: it comes back ``head_pruned`` although it was
    created at or after the cut's cutoff, and retention never removes a row
    at or after its cutoff."""
    created = 1000.0 + 7 * DAY
    rows = _forged_prefix_cut(store, created_at=created, cutoff=0, retention_days=7)
    report = verify(store, expect_head=(rows[-1]["seq"], rows[-1]["hash"]))
    assert report["ok"] and report["first_seq"] == 4
    assert report["start_cut"] == {"seq": 6, "created_at": created, "cutoff": 0,
                                   "retention_days": 7, "through_seq": 3}
    earlier = rows[1]
    pruned = verify(store, expect_head=(earlier["seq"], earlier["hash"]))
    assert (pruned["ok"], pruned["reason"]) == (False, "head_pruned")
    assert earlier["created_at"] >= pruned["start_cut"]["cutoff"]


def test_a_malformed_cut_record_fails_verification_instead_of_crashing(store):
    from pseudolife_memory.storage.coordination import audit_hash, verify_audit_chain
    pair(store)
    second = dict(chain(store)[1])
    second["event"] = "audit_prune"
    second["payload"] = json.dumps({"through_seq": [1], "through_hash": {"x": 1}})
    second["hash"] = audit_hash(second["prev_hash"], second)
    assert verify_audit_chain([second]) == {"ok": False, "seq": 2, "reason": "unanchored_start"}


def test_zero_audit_retention_keeps_the_log_forever(store):
    pair(store)
    store.test_time[0] += 4000 * DAY
    assert store.prune(audit_retention_days=0)["audit_removed"] == 0
    assert store.prune()["audit_removed"] == 0
    assert [e["event"] for e in events(store)][:2] == ["register", "register"]
    assert events(store, "audit_prune") == []


def test_concurrent_writers_on_separate_connections_keep_one_contiguous_chain(store, pg_url):
    """Writers on different mailboxes share no agent row lock, so only the
    chain's own lock orders their appends. Without it two writers read the
    same head and collide on ``seq``."""
    pairs = [pair(store) for _ in range(4)]

    def worker(n):
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("SET search_path TO public")
            own = CoordinationStore(Storage(conn), clock=lambda: 2000.0)
            a, b = pairs[n]
            for i in range(10):
                own.send(*creds(a), to=b["agent_id"], text=f"w{n} m{i}", request_id=f"w{n}-{i}")

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(worker, range(4)))
    report = verify(store)
    assert report["ok"] and report["events"] == 8 + 40


def test_two_receives_racing_on_one_page_log_a_single_first_read(store, pg_url):
    """The second receive reads the page before the first commits, then waits
    on the row lock; once the first commits, its stamp must not overwrite
    the first read or log a second one."""
    import threading
    a, b = pair(store)
    msg = store.send(*creds(a), to=b["agent_id"], text="once", request_id="r")
    other = psycopg.connect(pg_url, autocommit=True)
    other.execute("SET search_path TO public")
    racer_pid = other.info.backend_pid
    racer = CoordinationStore(Storage(other), clock=lambda: 3000.0)
    done = threading.Event()
    try:
        with store.storage._txn():
            store.test_time[0] = 2000.0
            store.receive(*creds(b))           # stamped, not yet committed
            thread = threading.Thread(target=lambda: (racer.receive(*creds(b)), done.set()))
            thread.start()
            deadline = time.monotonic() + 5
            # pg_locks is read live, not from the transaction's snapshot.
            while not store.storage.conn.execute(
                    "SELECT count(*) FROM pg_locks WHERE pid=%s AND NOT granted",
                    (racer_pid,)).fetchone()[0]:
                if done.is_set() or time.monotonic() > deadline:
                    break
                time.sleep(0.01)
            assert not done.is_set(), "the racing receive did not wait on the row lock"
        thread.join(5)
        assert done.is_set()
    finally:
        other.close()
    assert store.storage.conn.execute(
        "SELECT first_read_at FROM coordination_messages WHERE message_id=%s",
        (msg["message_id"],)).fetchone() == (2000.0,)
    assert [e["created_at"] for e in events(store, "read")] == [2000.0]


def test_appending_outside_a_transaction_is_refused(store):
    """A transaction-scoped chain lock taken in autocommit would end with its
    own statement, letting two writers read the same head."""
    with pytest.raises(RuntimeError, match="inside the mutation's transaction"):
        store._append([store._event("register", {})], 1000.0)
    assert events(store) == []


def test_dispatch_prunes_the_log_with_the_configured_window(coordinating, monkeypatch):
    seen = []

    def spy(self, **kwargs):
        seen.append(kwargs)
        return {}

    monkeypatch.setattr(CoordinationStore, "prune", spy)
    coordinating.config.coordination.audit_retention_days = 3
    coordinating._coordination_pruned_at = float("-inf")
    from pseudolife_memory.coordination import dispatch
    dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL)
    assert seen == [{"audit_retention_days": 3}]


def test_the_volume_harness_counts_one_row_per_logged_mutation(pg_conn, pg_url):
    """evals/coordination_audit_volume.py backs the retention default and the
    published cost; keep it runnable and its control arm row-free."""
    from evals.coordination_audit_volume import measure
    workload = dict(agents=3, messages=5, updates_per_agent=2, attaches_per_agent=1, seed=1)
    logged = measure(pg_url, **workload)
    assert logged["events"] == {"ack": 5, "attach": 3, "detach": 3, "expire": 1, "prune": 1,
                                "read": 5, "register": 3, "send": 5, "update": 6}
    assert set(logged["latency"]) == {"send", "receive", "ack", "update"}
    assert measure(pg_url, audit=False, **workload)["events_total"] == 0


def test_the_volume_harness_refuses_a_database_that_is_not_scratch(pg_conn, pg_url, monkeypatch):
    """The bench server also holds the production bank, and the harness
    truncates the board it replays into."""
    import evals.coordination_audit_volume as harness
    pg_conn.autocommit = True
    store = CoordinationStore(Storage(pg_conn))
    store.register("alice")
    monkeypatch.setattr(harness, "SCRATCH_PREFIXES", ("pseudolife_memory_bench_",))
    with pytest.raises(SystemExit, match="refusing"):
        harness.measure(pg_url, agents=2, messages=1, updates_per_agent=0,
                        attaches_per_agent=0, seed=1)
    assert pg_conn.execute("SELECT count(*) FROM coordination_agents").fetchone() == (1,)


@pytest.mark.parametrize("bad", [-1, 1.5, True, "90", None])
def test_audit_retention_must_be_a_non_negative_whole_number_of_days(bad):
    from pseudolife_memory.utils.config import CoordinationConfig
    with pytest.raises(ValueError, match="audit_retention_days"):
        CoordinationConfig(audit_retention_days=bad)


def test_audit_retention_defaults_to_ninety_days_and_loads_from_yaml(tmp_path):
    from pseudolife_memory.utils.config import CoordinationConfig, load_config
    assert CoordinationConfig().audit_retention_days == 90
    path = tmp_path / "config.yaml"
    path.write_text("coordination:\n  audit_retention_days: 0\n")
    assert load_config(path).coordination.audit_retention_days == 0
