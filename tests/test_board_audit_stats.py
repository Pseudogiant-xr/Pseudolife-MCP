"""``pseudolife-mcp board-audit stats``: coordination telemetry from the log,
the v49 wakes table and the suite lock's durations file.

The report shape is pinned on a synthetic export and a fake wakes table (no
database), and the subcommand is exercised against the bench Postgres (those
tests skip without it). Whatever leaves the tool is counts, seconds and
names: the privacy test checks the whole report text.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

import pytest

from pseudolife_memory.board_audit_stats import REPORT_SHAPE, compute_stats
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_coordination_storage import creds, store  # noqa: F401

A, B, C = "a" * 32, "b" * 32, "c" * 32
SINCE, UNTIL = 1000.0, 5000.0


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _event(seq, event, agent, created_at, *, principal="claude-code", recipient=None,
           message_id=None, payload=None, body=None, actor="agent"):
    """One line as ``board-audit export`` writes it, payload parsed."""
    return {"seq": seq, "event": event, "actor": actor, "principal": principal, "agent_id": agent,
            "recipient_agent_id": recipient, "project": "p", "task": "t",
            "message_id": message_id, "payload": payload or {}, "created_at": created_at,
            "hlc": "", "prev_hash": "0" * 64, "hash": "1" * 64, "body": body, "body_salt": None}


def _park(agent, created_at, reason, *, expires=None, before=None, before_expires=None):
    """A new park's update payload as the daemon logs it: the daemon names
    an expiry for every new park (the caller's, or 12 h from now), and
    ``before`` holds the row's old value of every key in ``fields``."""
    fields = {"status": "parked", "park_reason": reason, "park_needs": "the thing",
              "park_clear_by": "anyone", "park_resume": "carry on", "park_set_at": created_at,
              "park_expires": created_at + 43200 if expires is None else expires}
    return {"fields": fields, "before": {"park_reason": before, "status": "working",
                                         "park_expires": before_expires}}


def _refine(created_at, *, expires=None, before_expires=None, **fields):
    """A park-field update without a reason, as the daemon logs it."""
    fields = {**fields, "park_set_at": created_at}
    before = {key: "old" for key in fields if key != "park_set_at"}
    if expires is not None:
        fields["park_expires"] = expires
        before["park_expires"] = before_expires
    return {"fields": fields, "before": before}


def _clear(created_at, before):
    return {"fields": {"status": "working", "park_reason": None, "park_needs": "",
                       "park_clear_by": "", "park_resume": "", "park_expires": None,
                       "park_set_at": None},
            "before": {"park_reason": before, "status": "parked"}}


def _send(seq, sender, recipient, created_at, message_id, decision, *, principal):
    return _event(seq, "send", sender, created_at, principal=principal, recipient=recipient,
                  message_id=message_id, body="the body: C:\\Users\\<user>\\secret",
                  payload={"text_commitment": "f" * 64, "request_id": "r" + message_id,
                           "recipient_sequence": 1, "expires_at": created_at + 86400,
                           "wake": decision})


def synthetic_events():
    """Three sessions over one window: A and C are claude-code, B is codex.
    B parks, is rung by A, wakes (woke marker), reads, clears, acks. A parks
    with a short expiry that lapses before it clears. C parks and stays
    parked. Six sends with six decisions; three are read."""
    return [
        _event(1, "register", A, 900.0),                                 # before the window
        _event(2, "attach", A, 1000.0, payload={"generation": 1, "renewed": False}),
        _event(3, "attach", B, 1200.0, principal="codex",
               payload={"generation": 1, "renewed": False}),
        _event(4, "update", B, 1300.0, principal="codex",
               payload=_park(B, 1300.0, "blocked", expires=1300.0 + 43200)),
        _send(5, A, B, 1400.0, "m1", "rung", principal="claude-code"),
        _event(6, "woke", B, 1425.0, principal="codex", recipient=B, payload={"rings": 1}),
        _event(7, "read", B, 1430.0, principal="codex", recipient=B, message_id="m1",
               payload={"path": "pull", "sender_agent_id": A}),
        _event(8, "update", B, 1440.0, principal="codex", payload=_clear(1440.0, "blocked")),
        _event(9, "ack", B, 1450.0, principal="codex", recipient=B, message_id="m1",
               payload={"sender_agent_id": A}),
        _send(10, B, A, 2000.0, "m2", "hinted", principal="codex"),
        _event(11, "read", A, 2060.0, recipient=A, message_id="m2",
               payload={"path": "pull", "sender_agent_id": B}),
        _send(12, A, B, 2500.0, "m3", "withheld", principal="claude-code"),
        _event(13, "update", A, 2600.0, payload=_park(A, 2600.0, "needs_info", expires=2700.0)),
        _event(14, "update", C, 3000.0, payload=_park(C, 3000.0, "waiting_peer")),
        _send(15, A, C, 3100.0, "m4", "nudged", principal="claude-code"),
        _send(16, C, A, 3200.0, "m5", "capped", principal="claude-code"),
        _event(17, "read", A, 3205.0, recipient=A, message_id="m5",
               payload={"path": "pull", "sender_agent_id": C}),
        _event(18, "update", A, 3300.0, payload=_clear(3300.0, "needs_info")),   # after its expiry
        _event(19, "detach", B, 4000.0, principal="codex", payload={"generation": 1}),
        _send(20, C, A, 4500.0, "m6", "rung", principal="claude-code"),
        _event(21, "ack", A, 4520.0, recipient=A, message_id="m5",
               payload={"sender_agent_id": C}),
        _send(22, A, B, 6000.0, "m7", "rung", principal="claude-code"),         # after the window
    ]


def _wake(recipient, sender, message_id, decision, created_at, served_at=None):
    return {"recipient_agent_id": recipient, "sender_agent_id": sender, "message_id": message_id,
            "decision": decision, "reason": "anyone", "urgent": False, "ring_at": created_at,
            "created_at": created_at, "served_at": served_at}


def synthetic_wakes():
    return [
        _wake(B, A, "m0", "rung", 500.0, 510.0),          # before the window
        _wake(B, A, "m1", "rung", 1400.0, 1420.0),        # served, woke at 1425, read at 1430
        _wake(C, A, "m4", "nudged", 3100.0),              # never served
        _wake(A, C, "m6", "rung", 4500.0, 4510.0),        # served, no woke, ack at 4520
    ]


def synthetic_durations():
    return [
        {"seconds": 1000.0, "worktree": "C:\\Users\\<user>\\wt-a", "ended": _iso(4000),
         "queued_at": _iso(2900), "started_at": _iso(3000)},
        {"seconds": 1200.0, "worktree": "/home/someone/wt-b", "ended": _iso(4500)},
        {"seconds": 900.0, "worktree": "wt-c", "ended": _iso(6000)},                # after
        "not a record",
    ]


def _report():
    return compute_stats(synthetic_events(), synthetic_wakes(), synthetic_durations(),
                         since=SINCE, until=UNTIL, now=5000.5, version="0.0.0+test",
                         sources={"events": "export", "wakes": "fake", "durations": "file"})


def _pct(n, p50, p95):
    return {"n": n, "p50": p50, "p95": p95}


def _latency(sends, read, p50=None, p95=None):
    return {"sends": sends, "read": read, "unread": sends - read, "seconds": _pct(read, p50, p95)}


def test_the_report_shape_is_pinned_on_a_synthetic_export_and_a_fake_wakes_table():
    report = _report()
    assert report["report"] == "board-audit stats" and report["shape"] == REPORT_SHAPE == 1
    assert report["version"] == "0.0.0+test" and report["generated_at"] == 5000.5
    assert report["window"] == {"since": 1000.0, "until": 5000.0, "hours": 1.111}
    assert report["sources"] == {"events": "export", "wakes": "fake", "durations": "file"}
    assert report["events"] == {"total": 20, "by_kind": {
        "ack": 2, "attach": 2, "detach": 1, "read": 3, "send": 6, "update": 5, "woke": 1}}

    # 1. Send to first read, by the send's wake decision and the recipient's
    # principal. m1 30 s, m2 60 s, m5 5 s; m3, m4 and m6 unread at `until`.
    assert report["mail_latency"] == {
        **_latency(6, 3, 30.0, 60.0),
        "by_decision": {
            "rung": _latency(2, 1, 30.0, 30.0), "nudged": _latency(1, 0),
            "hinted": _latency(1, 1, 60.0, 60.0), "withheld": _latency(1, 0),
            "no_path": _latency(0, 0), "capped": _latency(1, 1, 5.0, 5.0),
            "not_needed": _latency(0, 0), "unknown": _latency(0, 0)},
        "by_principal": {"claude-code": _latency(4, 2, 5.0, 60.0),
                         "codex": _latency(2, 1, 30.0, 30.0)}}

    # 2. Wake precision: three rings in the window (one before it is not
    # counted); m1 served 20 s after, woke 5 s later, read 5 s after that;
    # m6 served, no woke, acked within 120 s; m4 never served.
    rung = {"rings": 2, "served": 2, "never_served": 0, "share_never_served": 0.0,
            "seconds_to_served": _pct(2, 10.0, 20.0),
            "acted_after_served": 2, "share_acted_after_served": 1.0,
            "woke": 1, "share_woke": 0.5, "seconds_served_to_woke": _pct(1, 5.0, 5.0),
            "acted_after_woke": 1, "share_acted_after_woke": 1.0}
    nudged = {"rings": 1, "served": 0, "never_served": 1, "share_never_served": 1.0,
              "seconds_to_served": _pct(0, None, None),
              "acted_after_served": 0, "share_acted_after_served": None,
              "woke": 0, "share_woke": None, "seconds_served_to_woke": _pct(0, None, None),
              "acted_after_woke": 0, "share_acted_after_woke": None}
    assert report["wake_precision"] == {
        **rung, "rings": 3, "never_served": 1, "share_never_served": 0.333,
        "by_decision": {"rung": rung, "nudged": nudged}}

    # 3. Parks: B's cleared 140 s after a ring reached it (a clearing send);
    # A's expiry passed before its clearing update; C's still stands.
    def outcome(parks, send=0, owner_update=0, expiry=0, open=0, seconds=_pct(0, None, None)):
        return {"parks": parks, "cleared": {"send": send, "owner_update": owner_update,
                                            "expiry": expiry, "open": open},
                "seconds_to_clear": seconds}
    assert report["park_outcomes"] == {
        **outcome(3, send=1, expiry=1, open=1, seconds=_pct(1, 140.0, 140.0)),
        "by_reason": {"blocked": outcome(1, send=1, seconds=_pct(1, 140.0, 140.0)),
                      "needs_info": outcome(1, expiry=1),
                      "waiting_peer": outcome(1, open=1)}}

    # 4. Sends per attached session-hour: A attached for the whole window
    # (4000 s), B for 2800 s; C never attached, so its sends count and its
    # hours do not.
    assert report["sends_per_session_hour"] == {
        "sends": 6, "session_hours": 1.89, "rate": 3.18,
        "by_principal": {"claude-code": {"sends": 5, "session_hours": 1.11, "rate": 4.5},
                         "codex": {"sends": 1, "session_hours": 0.78, "rate": 1.29}}}

    # 5. Suite lock: two runs ended in the window; one carries the queue
    # stamps (100 s wait), the older one only its hold.
    assert report["suite_lock"] == {"runs": 2, "hold": _pct(2, 1000.0, 1200.0),
                                    "queue_wait": _pct(1, 100.0, 100.0)}
    assert set(report) == {"report", "shape", "version", "generated_at", "window", "sources",
                           "events", "mail_latency", "wake_precision", "park_outcomes",
                           "sends_per_session_hour", "suite_lock"}


def test_the_report_carries_no_ids_bodies_paths_or_user_names():
    text = json.dumps(_report())
    assert not re.search(r"[0-9a-f]{8}", text.replace("0.0.0+test", ""))   # no id, not even a prefix
    for private in ("the body", "someone", "<user>", "Users","/home", "wt-a", "wt-b", "worktree",
                    "the thing", "carry on", "\\\\"):
        assert private not in text, private
    # What does leave: counts, seconds, decision, reason and principal names.
    assert {"claude-code", "codex", "blocked", "rung", "nudged"} <= set(re.findall(r"[a-z_-]+", text))


def test_a_park_refined_or_set_before_the_window_is_not_a_new_park():
    events = [
        # Set before the window, refined and cleared inside it: not counted.
        _event(1, "update", A, 900.0, payload=_park(A, 900.0, "blocked")),
        _event(2, "update", A, 1100.0, payload=_refine(1100.0, park_needs="more")),
        _event(3, "update", A, 1200.0, payload=_clear(1200.0, "blocked")),
        # Set inside it, then given another reason while it stands: one park
        # (the daemon overwrites the reason, so the park counts under the
        # newer one), which the refinement's expiry ends.
        _event(4, "update", B, 2000.0, payload=_park(B, 2000.0, "needs_info")),
        _event(5, "update", B, 2100.0, payload=_park(B, 2100.0, "blocked", expires=2400.0,
                                                    before="needs_info",
                                                    before_expires=2000.0 + 43200)),
    ]
    report = compute_stats(events, [], [], since=SINCE, until=UNTIL)
    assert report["park_outcomes"] == {
        "parks": 1, "cleared": {"send": 0, "owner_update": 0, "expiry": 1, "open": 0},
        "seconds_to_clear": {"n": 0, "p50": None, "p95": None},
        "by_reason": {"blocked": {
            "parks": 1, "cleared": {"send": 0, "owner_update": 0, "expiry": 1, "open": 0},
            "seconds_to_clear": {"n": 0, "p50": None, "p95": None}}}}


def test_a_park_that_lapsed_is_ended_by_its_expiry_and_the_next_park_counts():
    """A lapsed park's fields stay on the row, so the next park's ``before``
    still names a reason. Under the daemon's live-park rule (#442) that park
    is new, starting empty with its own expiry, and the lapsed one ended at
    its expiry. A park lapsed before the window, seen through the lookback,
    does not hide one set inside it."""
    events = [
        # Before the window: A parks, and the park lapses at 950.
        _event(1, "update", A, 800.0, payload=_park(A, 800.0, "blocked", expires=950.0)),
        # Inside it: A parks again (the row still says "blocked"), then is
        # cleared by its own status 100 s later.
        _event(2, "update", A, 1100.0, payload=_park(A, 1100.0, "needs_info", expires=9000.0,
                                                    before="blocked", before_expires=950.0)),
        _event(3, "update", A, 1200.0, payload=_clear(1200.0, "needs_info")),
        # B parks, lapses at 2100, and parks again at 2500 with a new reason
        # and the daemon's default expiry.
        _event(4, "update", B, 2000.0, payload=_park(B, 2000.0, "blocked", expires=2100.0)),
        _event(5, "update", B, 2500.0, payload=_park(B, 2500.0, "waiting_peer",
                                                    before="blocked", before_expires=2100.0)),
        # C parks with an expiry, refines it with a park field alone (no
        # reason), and the refinement's later expiry is the one that counts.
        _event(6, "update", C, 3000.0, payload=_park(C, 3000.0, "needs_resource", expires=3100.0)),
        _event(7, "update", C, 3050.0, payload=_refine(3050.0, expires=9000.0,
                                                       before_expires=3100.0)),
    ]
    report = compute_stats(events, [], [], since=SINCE, until=UNTIL)["park_outcomes"]
    assert (report["parks"], report["cleared"]) == (
        4, {"send": 0, "owner_update": 1, "expiry": 1, "open": 2})
    assert {reason: summary["cleared"] for reason, summary in report["by_reason"].items()} == {
        "blocked": {"send": 0, "owner_update": 0, "expiry": 1, "open": 0},
        "needs_info": {"send": 0, "owner_update": 1, "expiry": 0, "open": 0},
        "needs_resource": {"send": 0, "owner_update": 0, "expiry": 0, "open": 1},
        "waiting_peer": {"send": 0, "owner_update": 0, "expiry": 0, "open": 1}}
    assert report["seconds_to_clear"] == _pct(1, 100.0, 100.0)


def test_logs_written_before_the_live_park_rule_still_count_every_park():
    """Before #442 the daemon accepted a park field alone on a lapsed park,
    and a new reason over one carried its dead expiry; the audit log keeps
    those rows. The review's sequences (orchestrator, 2026-09-28): a lapsed
    park touched by a field-only update, then parked again, is two parks;
    a lapsed park revived by a field-only future expiry is a new park from
    then."""
    events = [
        # A: blocked lapses at 1500, a field-only update at 2000 leaves it
        # lapsed, and needs_info at 3000 is a new park.
        _event(1, "update", A, 1000.0, payload=_park(A, 1000.0, "blocked", expires=1500.0)),
        _event(2, "update", A, 2000.0, payload=_refine(2000.0, park_needs="still the thing")),
        _event(3, "update", A, 3000.0, payload=_park(A, 3000.0, "needs_info", expires=9000.0,
                                                    before="blocked", before_expires=1500.0)),
        # B: blocked lapses at 1500; a field-only update at 2000 names a
        # future expiry, which the old daemon took, so a park stands again.
        _event(4, "update", B, 1000.0, payload=_park(B, 1000.0, "blocked", expires=1500.0)),
        _event(5, "update", B, 2000.0, payload=_refine(2000.0, expires=9000.0,
                                                       before_expires=1500.0)),
        # C: blocked lapses at 1500; a new reason at 2000 with no expiry of
        # its own carried the dead one, so it stood for no time at all.
        _event(6, "update", C, 1000.0, payload=_park(C, 1000.0, "blocked", expires=1500.0)),
        _event(7, "update", C, 2000.0, payload={
            "fields": {"park_reason": "needs_info", "park_set_at": 2000.0},
            "before": {"park_reason": "blocked"}}),
    ]
    report = compute_stats(events, [], [], since=SINCE, until=UNTIL)["park_outcomes"]
    assert {reason: summary["cleared"] for reason, summary in report["by_reason"].items()} == {
        "blocked": {"send": 0, "owner_update": 0, "expiry": 3, "open": 1},
        "needs_info": {"send": 0, "owner_update": 0, "expiry": 1, "open": 1}}
    assert report["parks"] == 6


def test_a_park_set_before_the_lookback_is_never_counted_or_mistaken():
    """An address whose park the events never showed: a field-only update
    shows the daemon still honoured one (it refuses them otherwise), so a
    later reason without an expiry of its own refines that park; a reason
    over a row whose old expiry had passed starts a new one."""
    events = [
        _event(1, "update", A, 2000.0, payload=_refine(2000.0, park_needs="more")),
        _event(2, "update", A, 2100.0, payload={
            "fields": {"park_reason": "needs_info", "park_set_at": 2100.0},
            "before": {"park_reason": "blocked"}}),
        _event(3, "update", B, 2000.0, payload=_park(B, 2000.0, "needs_info",
                                                    before="blocked", before_expires=1000.0)),
        _event(4, "update", C, 2000.0, payload=_park(C, 2000.0, "needs_info", expires=9000.0,
                                                    before="blocked", before_expires=8000.0)),
    ]
    report = compute_stats(events, [], [], since=SINCE, until=UNTIL)["park_outcomes"]
    assert (report["parks"], report["cleared"]["open"]) == (1, 1)


def test_a_ring_served_after_the_window_counts_as_never_served():
    wakes = [_wake(B, A, "m1", "rung", 1000.0, 5500.0)]
    report = compute_stats([], wakes, [], since=SINCE, until=UNTIL)["wake_precision"]
    assert (report["rings"], report["served"], report["never_served"]) == (1, 0, 1)


def test_the_adapters_own_delivery_read_is_not_the_recipient_acting():
    """A channel session's adapter logs ``read`` with ``path: delivery`` on
    its own when it pushes mail in; only the model's board actions show a
    ring landed."""
    events = [_event(1, "read", B, 1030.0, recipient=B, message_id="m9",
                     payload={"path": "delivery", "sender_agent_id": A})]
    wakes = [_wake(B, A, "m1", "rung", 1000.0, 1010.0)]
    report = compute_stats(events, wakes, [], since=SINCE, until=UNTIL)["wake_precision"]
    assert (report["served"], report["acted_after_served"]) == (1, 0)


def test_a_hook_or_daemon_update_is_not_the_recipient_acting():
    """v50: the subagent hooks update a session's children (actor ``hook``)
    and the daemon links a subagent to its parent (actor ``daemon``); neither
    is the model acting on the board."""
    events = [_event(1, "update", B, 1030.0, actor="hook",
                     payload={"fields": {"children": []}, "before": {"children": []}}),
              _event(2, "update", B, 1031.0, actor="daemon",
                     payload={"fields": {"parent_agent_id": A}, "before": {"parent_agent_id": None}})]
    wakes = [_wake(B, A, "m1", "rung", 1000.0, 1010.0)]
    report = compute_stats(events, wakes, [], since=SINCE, until=UNTIL)["wake_precision"]
    assert (report["served"], report["acted_after_served"]) == (1, 0)


def test_an_empty_window_reports_zeros_not_errors():
    report = compute_stats([], [], [], since=SINCE, until=UNTIL)
    assert report["events"] == {"total": 0, "by_kind": {}}
    assert report["mail_latency"]["sends"] == 0 and report["mail_latency"]["by_principal"] == {}
    assert report["wake_precision"]["rings"] == 0
    assert report["park_outcomes"]["parks"] == 0 and report["park_outcomes"]["by_reason"] == {}
    assert report["sends_per_session_hour"] == {"sends": 0, "session_hours": 0.0, "rate": None,
                                                "by_principal": {}}
    assert report["suite_lock"] == {"runs": 0, "hold": _pct(0, None, None),
                                    "queue_wait": _pct(0, None, None)}


# ── the subcommand, against the bench Postgres ────────────────────────────


@pytest.fixture
def cli(pg_url, monkeypatch, capsys):
    from pseudolife_memory.board_audit_cli import main
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)

    def run(*args):
        code = main(list(args))
        return code, capsys.readouterr()
    return run


def _board_day(store):
    """A parked session rung, served, woken and answering; then chatter to
    it while active. Clock-driven, so every interval is exact."""
    a = store.register("alice", label="sender")
    b = store.register("alice", capabilities={"ring": True})
    store.test_time[0] = 1000.0
    attached = store.attach(*creds(b), attachment_id="one", ring=True)
    store.update(*creds(b), status="parked", park_reason="blocked", park_needs="the review",
                 park_clear_by="anyone")
    store.test_time[0] = 1020.0
    first = store.send(*creds(a), to=b["agent_id"], text="review is in", request_id="r1")
    assert first["wake"]["decision"] == "rung"
    store.test_time[0] = 1030.0  # within the 60 s attachment lease
    store.heartbeat(*creds(b), attachment_id="one", generation=attached["generation"])  # serves it
    store.test_time[0] = 1032.0
    assert store.woke(b["agent_id"], "alice")["rings"] == 1
    store.test_time[0] = 1040.0
    store.receive(*creds(b))
    store.update(*creds(b), status="applying the review")
    store.test_time[0] = 1050.0
    store.ack(*creds(b), message_id=first["message_id"])
    store.test_time[0] = 1100.0
    assert store.send(*creds(a), to=b["agent_id"], text="thanks",
                      request_id="r2")["wake"]["decision"] == "hinted"
    # The lease lapsed long ago: the adapter re-attaches (a fresh attach on
    # an address already counted as a session), then detaches for good.
    store.test_time[0] = 4590.0
    attached = store.attach(*creds(b), attachment_id="one", ring=True)
    store.test_time[0] = 4600.0
    store.detach(*creds(b), attachment_id="one", generation=attached["generation"])
    return a, b


def test_stats_reads_the_bank_the_wakes_table_and_the_durations_file(store, cli, tmp_path):
    _board_day(store)
    durations = tmp_path / "full-suite.durations.jsonl"
    durations.write_text(json.dumps({"seconds": 950.0, "worktree": str(tmp_path), "ended": _iso(4000),
                                     "queued_at": _iso(2000), "started_at": _iso(2300)}) + "\n"
                         + "not json\n", encoding="utf-8")
    out = tmp_path / "stats.json"
    code, output = cli("stats", "--since", "0", "--until", "5000", "--durations", str(durations),
                       "--out", str(out))
    assert (code, output.out, output.err) == (0, "", "")
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["sources"] == {"durations": "file", "events": "bank", "wakes": "bank"}
    assert report["window"] == {"since": 0.0, "until": 5000.0, "hours": 1.389}
    latency = report["mail_latency"]
    assert (latency["sends"], latency["read"]) == (2, 1)
    assert latency["by_decision"]["rung"] == _latency(1, 1, 20.0, 20.0)
    assert latency["by_decision"]["hinted"] == _latency(1, 0)
    assert latency["by_principal"] == {"alice": _latency(2, 1, 20.0, 20.0)}
    precision = report["wake_precision"]
    assert (precision["rings"], precision["served"], precision["woke"]) == (1, 1, 1)
    assert precision["seconds_to_served"] == _pct(1, 10.0, 10.0)
    assert precision["seconds_served_to_woke"] == _pct(1, 2.0, 2.0)
    assert (precision["acted_after_served"], precision["acted_after_woke"]) == (1, 1)
    parks = report["park_outcomes"]
    assert (parks["parks"], parks["cleared"]["send"]) == (1, 1)
    assert parks["seconds_to_clear"] == _pct(1, 40.0, 40.0)
    # One attached session-hour (1000 to 4600) against two sends.
    assert report["sends_per_session_hour"] == {
        "sends": 2, "session_hours": 1.0, "rate": 2.0,
        "by_principal": {"alice": {"sends": 2, "session_hours": 1.0, "rate": 2.0}}}
    assert report["suite_lock"] == {"runs": 1, "hold": _pct(1, 950.0, 950.0),
                                    "queue_wait": _pct(1, 300.0, 300.0)}
    text = out.read_text(encoding="utf-8")
    assert str(tmp_path) not in text and "review" not in text and "alice" in text
    assert not re.search(r"[0-9a-f]{32}", text)

    # A new file only; --append keeps a running log.
    code, output = cli("stats", "--since", "0", "--until", "5000", "--out", str(out))
    assert code == 2 and "exists" in output.err
    log = tmp_path / "stats.jsonl"
    for _ in range(2):
        code, output = cli("stats", "--since", "0", "--until", "5000", "--durations",
                           str(durations), "--append", str(log))
        assert (code, output.out) == (0, "")
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and lines[0]["mail_latency"] == lines[1]["mail_latency"] == latency


def test_stats_prints_to_stdout_by_default_and_the_window_defaults_to_a_day(store, cli, monkeypatch):
    from pseudolife_memory import board_audit_cli
    _board_day(store)
    monkeypatch.setattr(board_audit_cli.time, "time", lambda: 5000.0)
    code, output = cli("stats")
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["window"] == {"since": 5000.0 - 86400, "until": 5000.0, "hours": 24.0}
    assert report["generated_at"] == 5000.0 and report["mail_latency"]["sends"] == 2
    assert report["version"]
    code, output = cli("stats", "--since", "5000", "--until", "4000")
    assert code == 2 and "before" in output.err


def test_stats_from_an_export_file_has_no_wake_rows(store, cli, tmp_path):
    _board_day(store)
    archive = tmp_path / "board.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    code, output = cli("stats", "--input", str(archive), "--since", "0", "--until", "5000",
                       "--durations", str(tmp_path / "absent.jsonl"))
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["sources"] == {"durations": "absent", "events": "export", "wakes": "none"}
    assert report["mail_latency"]["sends"] == 2 and report["wake_precision"]["rings"] == 0
    assert report["park_outcomes"]["parks"] == 1
    assert report["suite_lock"]["runs"] == 0
    # An export is read as the bank is, up to `until`: the first message's
    # read at 1040 is after a window that ends at 1030, so it is unread,
    # as the bank path reports it.
    by_file = cli("stats", "--input", str(archive), "--since", "0", "--until", "1030",
                  "--durations", str(tmp_path / "absent.jsonl"))
    by_bank = cli("stats", "--since", "0", "--until", "1030",
                  "--durations", str(tmp_path / "absent.jsonl"))
    file_report, bank_report = (json.loads(result[1].out) for result in (by_file, by_bank))
    assert file_report["mail_latency"] == bank_report["mail_latency"]
    assert file_report["mail_latency"]["unread"] == 1
    assert file_report["events"] == bank_report["events"]


def test_a_bank_before_v49_reports_no_wake_rows(store, cli):
    _board_day(store)
    from pseudolife_memory.storage.schema import COORDINATION_SCHEMA_SQL
    store.storage.conn.execute("DROP TABLE coordination_wakes")
    try:
        code, output = cli("stats", "--since", "0", "--until", "5000")
        assert code == 0, output.err
        report = json.loads(output.out)
        assert report["sources"]["wakes"] == "absent" and report["wake_precision"]["rings"] == 0
        assert report["mail_latency"]["sends"] == 2
    finally:
        store.storage.conn.execute(COORDINATION_SCHEMA_SQL)


def test_the_usage_names_stats():
    from pseudolife_memory import cli as console
    line = next(line for line in console._USAGE.splitlines() if "board-audit" in line)
    assert "stats" in line
