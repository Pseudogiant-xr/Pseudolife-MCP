"""``evals/coordination_report.py``: the aggregate-only coordination report.

Every later coordination phase is judged against the trial baseline this
report produced, so its metrics are pinned here on small hand-computed
boards, in both input formats it reads (the audit log's JSON-lines export
and the older whole-board export the 2026-09-23/24 trial was recorded in),
and its privacy is pinned by construction: bodies, labels, statuses, paths
and agent ids never reach either output file.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid

import pytest

from evals import coordination_report as report_mod
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_coordination_storage import creds, store  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
# 2026-09-23T16:00:00+10:00, the trial export's own window_start.
T0 = 1_790_143_200.0


def aid(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_OID, "coordination-report-test-" + name).hex


class Board:
    """A tiny board described once and written in either input format."""

    def __init__(self, agents, *, statuses=None, scope="scope"):
        self.agents = dict(agents)          # name -> principal
        self.statuses = dict(statuses or {})
        self.scope = scope                  # every project and task field
        self.messages = []
        self.extra = []                     # (offset, event, name, payload, actor)

    def send(self, sender, recipient, sent, acked=None, text="status note", *,
             reply_to=None, kind=None):
        self.messages.append({"id": uuid.uuid5(uuid.NAMESPACE_OID, f"m{len(self.messages)}").hex,
                              "sender": sender, "recipient": recipient, "sent": sent,
                              "acked": acked, "text": text, "reply_to": reply_to, "kind": kind})
        return self.messages[-1]["id"]

    def event(self, offset, event, name, payload, actor="agent"):
        """``name`` None is the daemon's own row; ``actor="operator"`` is a
        restore-recovery row, which carries no principal."""
        self.extra.append((offset, event, name, payload, actor))

    # -- the older whole-board export (dict with "agents" and "messages") --
    def legacy(self, exported_at):
        agents = [{"agent_id": aid(n), "principal": p, "label": f"agent-label-{n}",
                   "status": self.statuses.get(n, ""), "project": self.scope, "task": self.scope,
                   "episode": "", "capabilities": {}, "created_at": T0 - 600,
                   "last_activity": T0, "lease_until": None, "lifecycle": "attached",
                   "next_sequence": 0, "generation": 1, "attachment_id": None,
                   "wake_enabled": False} for n, p in self.agents.items()]
        messages = [{"message_id": m["id"], "sender_agent_id": aid(m["sender"]),
                     "recipient_agent_id": aid(m["recipient"]),
                     "sender_principal": self.agents[m["sender"]],
                     "created_at": T0 + m["sent"],
                     "acknowledged_at": None if m["acked"] is None else T0 + m["acked"],
                     "text": m["text"], "reply_to": m["reply_to"], "project": self.scope,
                     "task": self.scope, "request_id": f"r-{m['id']}", "hlc": "1:0",
                     "attempts": 0, "attempt_at": None, "attempt_generation": None,
                     "expires_at": T0 + m["sent"] + 86400, "fingerprint": "f" * 64,
                     "recipient_sequence": i + 1}
                    for i, m in enumerate(self.messages)]
        for m, row in zip(self.messages, messages):
            if m["kind"] is not None:
                row["kind"] = m["kind"]
        return {"exported_at": T0 + exported_at, "window_start": T0,
                "agents": agents, "messages": messages}

    # -- the audit log's export: one event per line, chain order --
    def audit(self):
        pending = []
        for n, p in self.agents.items():
            pending.append((T0 - 600, "register", n, None, None, {
                "label": f"agent-label-{n}", "project": self.scope, "task": self.scope,
                "episode": "", "status": self.statuses.get(n, ""), "capabilities": {},
                "wake_enabled": False}, "agent"))
        for m in self.messages:
            payload = {"text": m["text"], "reply_to": m["reply_to"],
                       "request_id": f"r-{m['id']}", "recipient_sequence": 1,
                       "expires_at": T0 + m["sent"] + 86400}
            if m["kind"] is not None:
                payload["kind"] = m["kind"]
            pending.append((T0 + m["sent"], "send", m["sender"], m["recipient"], m["id"],
                            payload, "agent"))
            if m["acked"] is not None:
                sender = {"sender_agent_id": aid(m["sender"])}
                pending.append((T0 + m["acked"], "read", m["recipient"], m["recipient"], m["id"],
                                {"path": "pull", **sender}, "agent"))
                pending.append((T0 + m["acked"], "ack", m["recipient"], m["recipient"], m["id"],
                                sender, "agent"))
        for offset, event, name, payload, actor in self.extra:
            pending.append((T0 + offset, event, name, None, None, payload, actor))
        pending.sort(key=lambda e: e[0])
        rows = []
        for seq, (at, event, name, recipient, message_id, payload, actor) in enumerate(pending, 1):
            agent = name is not None and actor == "agent"
            rows.append({"seq": seq, "event": event,
                         "actor": "daemon" if name is None and actor == "agent" else actor,
                         "principal": self.agents[name] if agent else "",
                         "agent_id": "" if name is None else aid(name),
                         "recipient_agent_id": None if recipient is None else aid(recipient),
                         "project": self.scope if agent else "",
                         "task": self.scope if agent else "",
                         "message_id": message_id, "payload": payload, "created_at": at,
                         "hlc": "", "prev_hash": "0" * 64, "hash": "0" * 64})
        return rows


def write_legacy(tmp_path, board, exported_at=None, name="board.json"):
    """``exported_at`` defaults to a minute after the board's last send or ack."""
    if exported_at is None:
        exported_at = 60 + max(max(m["sent"], m["acked"] or 0) for m in board.messages)
    path = tmp_path / name
    path.write_text(json.dumps(board.legacy(exported_at)), encoding="utf-8")
    return path


def write_audit(tmp_path, board, name="board.jsonl"):
    return write_rows(tmp_path, board.audit(), name)


def write_rows(tmp_path, rows, name):
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def as_v46(rows, *, upgraded_at=None):
    """Audit rows as a schema v46 export writes them: every row carries a
    top-level ``body`` (null unless it is a send's); a send written from v46
    on keeps its text there, with only its sha256 and byte count in the hashed
    payload. Sends before ``upgraded_at`` predate v46: a v46 export of an
    upgraded bank shows them with a null body and the text still inside the
    payload."""
    for row in rows:
        row["body"] = None
        if row["event"] == "send" and (upgraded_at is None or row["created_at"] >= upgraded_at):
            text = row["payload"].pop("text")
            raw = text.encode("utf-8")
            row["payload"]["text_sha256"] = hashlib.sha256(raw).hexdigest()
            row["payload"]["text_bytes"] = len(raw)
            row["body"] = text
    return rows


def run(path, **kwargs):
    return report_mod.build_report(report_mod.load_board(path), **kwargs)


def both(tmp_path, board, **kwargs):
    return (run(write_legacy(tmp_path, board), **kwargs),
            run(write_audit(tmp_path, board), **kwargs))


# ---------------------------------------------------------------- quantiles

def test_quantile_interpolates_between_ranks_like_the_trial_analysis():
    """The trial's published 296.8 s / 5261 s were linear-interpolated
    percentiles; nearest-rank would have given other numbers."""
    q = report_mod.quantile
    assert q([10, 20, 30, 40], 0.5) == 25
    assert q([10, 20, 30, 40], 0.9) == pytest.approx(37)
    assert q([40, 10, 30, 20], 0.9) == pytest.approx(37)
    assert q([7], 0.9) == 7
    assert q([], 0.5) is None


def test_rolling_peak_counts_a_half_open_sixty_minute_window():
    peak = report_mod.rolling_peak
    assert peak([1000, 1600, 2200, 2800, 4600]) == 4   # 1000 falls out at exactly 3600 s
    assert peak([1000, 1600, 2200, 2800, 4599]) == 5
    assert peak([]) == 0


# ---------------------------------------------------------------- ack latency

def ack_board():
    b = Board({"c": "claude-code", "w1": "claude-code", "x1": "codex"})
    b.send("c", "w1", 0, 30)       # latency 30  } one acknowledgment call
    b.send("c", "w1", 10, 30)      # latency 20  } covering three messages
    b.send("c", "w1", 20, 30)      # latency 10  }
    b.send("c", "w1", 50, 90)      # latency 40
    b.send("c", "w1", 60)          # never acknowledged
    b.send("c", "x1", 100, 200)    # latency 100
    b.send("c", "x1", 110)         # never acknowledged
    return b


WINDOWS = (("early", T0, T0 + 55), ("late", T0 + 55, None))


def test_ack_latency_per_recipient_principal_overall_and_per_window(tmp_path):
    for report in both(tmp_path, ack_board(), windows=WINDOWS):
        lat = report["metrics"]["ack_latency"]
        assert lat["definition"]
        assert lat["overall"]["claude-code"] == {
            "messages": 5, "acked": 4, "never_acked": 1, "median_s": 25.0, "p90_s": 37.0}
        assert lat["overall"]["codex"] == {
            "messages": 2, "acked": 1, "never_acked": 1, "median_s": 100.0, "p90_s": 100.0}
        assert lat["overall"]["all"] == {
            "messages": 7, "acked": 5, "never_acked": 2, "median_s": 30.0, "p90_s": 76.0}
        early, late = lat["windows"]["early"], lat["windows"]["late"]
        assert early["by_principal"]["claude-code"] == {
            "messages": 4, "acked": 4, "never_acked": 0, "median_s": 25.0, "p90_s": 37.0}
        assert early["by_principal"]["codex"] == {
            "messages": 0, "acked": 0, "never_acked": 0, "median_s": None, "p90_s": None}
        assert late["by_principal"]["claude-code"] == {
            "messages": 1, "acked": 0, "never_acked": 1, "median_s": None, "p90_s": None}
        assert late["by_principal"]["codex"]["messages"] == 2
        assert late["end"] is None


def test_batch_ack_share_counts_one_instant_covering_three_or_more(tmp_path):
    for report in both(tmp_path, ack_board()):
        batch = report["metrics"]["batch_acks"]
        assert batch["definition"]
        assert (batch["acked_messages"], batch["batch_instants"],
                batch["messages_in_batches"]) == (5, 1, 3)
        assert batch["share"] == pytest.approx(0.6)


def test_a_batch_is_one_recipients_acknowledgment(tmp_path):
    b = Board({"c": "claude-code", "w1": "claude-code", "w2": "claude-code"})
    for t, w in ((0, "w1"), (1, "w1"), (2, "w2"), (3, "w2")):
        b.send("c", w, t, 10)          # four acks in one instant, two per recipient
    for report in both(tmp_path, b):
        assert report["metrics"]["batch_acks"]["batch_instants"] == 0


def test_windows_are_start_inclusive_and_end_exclusive(tmp_path):
    b = Board({"c": "claude-code", "w1": "claude-code"})
    b.send("c", "w1", 55, 60)
    for report in both(tmp_path, b, windows=WINDOWS):
        windows = report["metrics"]["ack_latency"]["windows"]
        assert windows["early"]["by_principal"]["all"]["messages"] == 0
        assert windows["late"]["by_principal"]["all"]["messages"] == 1


def test_an_ack_stamped_before_its_send_counts_as_zero_and_is_reported(tmp_path):
    """A clock step can stamp an acknowledgment before its send; the report
    neither drops it nor lets a negative latency pull the median down."""
    b = ack_board()
    b.send("c", "w1", 400, 399)
    b.event(5, "prune", None, {"message_ids": "not-a-list", "agent_ids": 7})  # malformed
    for report in both(tmp_path, b):
        assert report["input"]["acks_before_send"] == 1
        # claude-code latencies: 10, 20, 30, 40 and the clamped 0.
        assert report["metrics"]["ack_latency"]["overall"]["claude-code"] == {
            "messages": 6, "acked": 5, "never_acked": 1, "median_s": 20.0, "p90_s": 36.0}


# ---------------------------------------------------------------- pair peaks

def test_pair_peak_is_the_busiest_sixty_minutes_of_each_directed_pair(tmp_path):
    b = Board({"c": "claude-code", "w2": "claude-code", "w3": "claude-code"})
    for t in (1000, 1600, 2200, 2800, 4600):
        b.send("w2", "c", t, t + 1)
    b.send("w3", "c", 0, 1)
    b.send("w3", "c", 100, 101)
    b.send("c", "w2", 5000, 5001)
    for report in both(tmp_path, b):
        peak = report["metrics"]["pair_peak_60min"]
        assert peak["definition"]
        assert (peak["pairs"], peak["max"]) == (3, 4)
        assert peak["pairs_over"] == {"3": 1, "6": 0, "10": 0}
        # First seen: w3 and c (t=0), then w2 (t=1000).
        assert peak["top_pairs"][0] == {"pair": "claude-code-3 -> claude-code-2", "peak": 4}


# ---------------------------------------------------------------- fan-out

def test_fan_out_bursts_need_one_sender_three_recipients_one_minute_similar_text(tmp_path):
    b = Board({"c": "claude-code", "w1": "claude-code", "w2": "claude-code",
               "w3": "claude-code"})
    hold = "HOLD: nobody start a full suite until the daemon restart finishes, ETA 18:40"
    for i, w in enumerate(("w1", "w2", "w3")):
        b.send("c", w, i * 3, 500, hold)                          # a burst
    for i, (w, text) in enumerate((("w1", "review the retry change when free"),
                                   ("w2", "SUITE-END #12 green, 11000 passed"),
                                   ("w3", "the bench database password rotated at noon"))):
        b.send("c", w, 1000 + i * 10, 1500, text)                  # dissimilar: none
    for i, w in enumerate(("w2", "w3", "c")):
        b.send("w1", w, 2000 + (0, 30, 61)[i], 2500, hold)         # third is 61 s late
    for report in both(tmp_path, b):
        fan = report["metrics"]["fanout_bursts"]
        assert fan["definition"]
        assert (fan["bursts"], fan["messages"]) == (1, 3)
        assert fan["share"] == pytest.approx(3 / 9, abs=5e-5)   # shares carry 4 places


LONG = ("run relay database bench hold session failed the review green worktree suite env "
        "start slot database worktree run worktree mail bench wake green green database suite "
        "session database next worktree restart worktree failed the hold passed mail reply suite")


def test_fan_out_similarity_holds_on_long_messages(tmp_path):
    """difflib's autojunk heuristic stops treating common characters as match
    anchors once a text reaches 200 characters: copies of this 260-character
    text that differ only in their first word scored 0.012 with it and 0.983
    without (review of the first draft, 2026-09-26)."""
    assert len(LONG) > 200
    b = Board({"c": "claude-code", "w1": "claude-code", "w2": "claude-code",
               "w3": "claude-code"})
    for i, (w, first) in enumerate((("w1", "daemon"), ("w2", "zebra"), ("w3", "alpha"))):
        b.send("c", w, i * 30, 500, f"{first} {LONG}")   # the third at exactly 60 s joins
    for report in both(tmp_path, b):
        fan = report["metrics"]["fanout_bursts"]
        assert (fan["bursts"], fan["messages"]) == (1, 3)


# ---------------------------------------------------------------- kinds

def kind_board():
    b = Board({"w1": "claude-code", "c": "claude-code", "w2": "claude-code",
               "w3": "claude-code"})
    b.send("w1", "c", 0, 5, "CLAIM: tests/test_foo.py for the retry fix")        # CLAIM
    b.send("w1", "c", 50, 55, "SUITE-START #12 (pytest full)")                     # NOTICE
    b.send("w1", "w2", 100, 105, "SUITE-END #12 green, 11000 passed - you're up")  # HANDOFF
    b.send("w1", "c", 130, 135, "SUITE-END #12 green")                           # NOTICE (a copy)
    b.send("w2", "c", 1000, 1005, "SUITE-END #13 green")                         # HANDOFF
    b.send("w3", "c", 1100, 1105, "can you review PR 14 when free?")             # REQUEST
    b.send("w3", "c", 1200, 1205, "DEPLOY-NEEDED: #15 merged, needs a daemon rebuild")  # NEEDS-HUMAN
    b.send("c", "w3", 1300, 1305, "DEPLOY-NEEDED: fyi only")                     # NOTICE
    b.send("c", "w1", 1400, 1405, "thanks, noted")                               # NOTICE
    b.send("c", "w2", 1500, 1505, "RELEASE tests/test_foo.py")                   # CLAIM
    return b


def test_kind_mix_uses_the_tag_first_heuristic_and_says_so(tmp_path):
    for report in both(tmp_path, kind_board()):
        kinds = report["metrics"]["kind_mix"]
        assert kinds["definition"] and kinds["heuristic"] is True
        assert (kinds["declared_messages"], kinds["heuristic_messages"]) == (0, 10)
        assert kinds["counts"] == {"NOTICE": 4, "REQUEST": 1, "CLAIM": 2, "HANDOFF": 2,
                                   "NEEDS-HUMAN": 1}
        assert kinds["shares"]["NOTICE"] == pytest.approx(0.4)
        # The hub (most distinct counterparties) is the coordinator, named
        # only by its first-seen pseudonym.
        assert report["coordinator"]["agent"] == "claude-code-2"


def test_kind_mix_without_a_coordinator(tmp_path):
    for report in both(tmp_path, kind_board(), coordinator="none"):
        assert report["metrics"]["kind_mix"]["counts"] == {
            "NOTICE": 4, "REQUEST": 1, "CLAIM": 2, "HANDOFF": 3, "NEEDS-HUMAN": 0}
        assert report["coordinator"]["agent"] is None


def test_an_operator_named_coordinator_is_reported_by_pseudonym_only(tmp_path):
    path = write_legacy(tmp_path, kind_board())
    report = run(path, coordinator=aid("w3"))
    assert report["coordinator"]["agent"] == "claude-code-4"
    assert aid("w3") not in json.dumps(report)
    with pytest.raises(ValueError, match="coordinator"):
        run(path, coordinator="0" * 32)


def test_a_declared_kind_wins_over_the_heuristic(tmp_path):
    b = Board({"a": "claude-code", "b": "codex"})
    b.send("a", "b", 0, 5, "FYI the suite is green", kind="request")
    b.send("a", "b", 10, 15, "can you look?", kind="made-up-kind")
    b.send("a", "b", 20, 25, "can you look?")
    for report in both(tmp_path, b):
        kinds = report["metrics"]["kind_mix"]
        assert (kinds["declared_messages"], kinds["heuristic_messages"]) == (2, 1)
        assert kinds["heuristic"] is True   # one message still fell back
        assert kinds["counts"]["REQUEST"] == 2 and kinds["counts"]["OTHER"] == 1
        assert "made-up-kind" not in json.dumps(report).lower()


def test_an_empty_declared_kind_is_no_declaration(tmp_path):
    b = Board({"a": "claude-code", "b": "codex"})
    b.send("a", "b", 0, 5, "can you look?", kind="  ")
    for report in both(tmp_path, b):
        kinds = report["metrics"]["kind_mix"]
        assert (kinds["declared_messages"], kinds["counts"]["REQUEST"]) == (0, 1)
        assert "OTHER" not in kinds["counts"]


def test_an_auto_coordinator_tie_names_no_hub(tmp_path):
    """With no single hub the coordinator rules would fire on an arbitrary
    agent; the report says so and leaves them off."""
    b = Board({"a": "claude-code", "b": "claude-code", "c": "codex"})
    b.send("a", "b", 0, 5)
    b.send("b", "c", 10, 15)
    b.send("c", "a", 20, 25)
    for report in both(tmp_path, b):
        assert report["coordinator"]["agent"] is None
        assert "tied" in report["coordinator"]["chosen_by"]


# The tag-first heuristic, phrase by phrase: (body, sent to the coordinator, kind).
PHRASES = [
    ("CLAIM tests/x.py", False, "CLAIM"),
    ("FYI CLAIMS tests/x.py and tests/y.py", False, "CLAIM"),
    ("RELEASED tests/x.py", False, "CLAIM"),
    ("SUITE-START #5", False, "NOTICE"),
    ("SUITE-RETRACT #5, aborted", False, "NOTICE"),
    ("SUITE-END #5 green", False, "HANDOFF"),
    ("SUITE-END #5 green", True, "HANDOFF"),          # no copy to a peer beside it
    ("BATON: to you", False, "HANDOFF"),
    ("GO: start now", False, "HANDOFF"),
    ("you're up next after the lock frees", False, "HANDOFF"),
    ("it is your turn on the suite", False, "HANDOFF"),
    ("expect your turn after the durability run", False, "NOTICE"),
    ("your turn around 03:00, not yet", False, "NOTICE"),
    ("go ahead with the merge", False, "HANDOFF"),
    ("start your full suite now", False, "HANDOFF"),
    ("the slot is free and yours", False, "HANDOFF"),
    ("slot is free for you", False, "HANDOFF"),
    ("pass the baton to the next session NOW", False, "HANDOFF"),
    ("thanks, will do?", False, "NOTICE"),             # an ack beats the question mark
    ("chip=relay -> w2: thanks, anything else?", False, "NOTICE"),   # routing header
    ("relay (0a1b2c3d) -> w2: noted, anything else?", False, "NOTICE"),
    ("DEPLOY-NEEDED: #9 merged", True, "NEEDS-HUMAN"),
    ("DEPLOY-NEEDED: #9 merged", False, "NOTICE"),
    ("this needs the maintainer's decision before merge", True, "NEEDS-HUMAN"),
    ("this needs the maintainer's decision before merge", False, "NOTICE"),
    ("parking the rotation for the morning", True, "NEEDS-HUMAN"),
    ("waiting on the user to pick an order", True, "NEEDS-HUMAN"),
    ("the maintainer approved it; needs the maintainer's decision on order", True, "NOTICE"),
    ("please confirm the head sha", False, "REQUEST"),
    ("can you look at #12", False, "REQUEST"),
    ("let me know when the lock frees", False, "REQUEST"),
    ("status: idle, nothing queued", False, "NOTICE"),
]


@pytest.mark.parametrize("text,to_coordinator,kind", PHRASES)
def test_the_kind_heuristic_phrase_by_phrase(text, to_coordinator, kind):
    message = report_mod.Message(sender="s", recipient="coord" if to_coordinator else "peer",
                                 sent=0.0, acked=None, text=text, reply_to=None, kind=None,
                                 message_id="m")
    assert report_mod.heuristic_kinds([message], "coord") == [kind]


# ---------------------------------------------------------------- suite baton

def test_suite_baton_counts_tags_and_groups_one_senders_copies(tmp_path):
    for report in both(tmp_path, kind_board()):
        baton = report["metrics"]["suite_baton"]
        assert baton["definition"]
        assert (baton["suite_start"], baton["suite_end"], baton["baton_passes"]) == (1, 3, 2)


def test_baton_copies_are_grouped_per_sender_when_senders_interleave(tmp_path):
    """Two sessions finishing at once interleave their copies; grouping only
    against the latest group counted each copy as a pass."""
    b = Board({"a": "claude-code", "d": "claude-code", "b": "claude-code",
               "e": "claude-code", "c": "claude-code"})
    b.send("a", "b", 0, 5, "SUITE-END #1 green - you're up")
    b.send("d", "e", 5, 9, "SUITE-END #2 green - you're up")
    b.send("a", "c", 10, 15, "SUITE-END #1 green")
    b.send("d", "c", 15, 20, "SUITE-END #2 green")
    b.send("a", "b", 200, 205, "SUITE-END #3 green")     # 200 s after a's first: a new pass
    for report in both(tmp_path, b):
        assert report["metrics"]["suite_baton"]["baton_passes"] == 3


# ---------------------------------------------------------------- proposals

def test_proposals_list_resources_coordinated_by_hand_repeatedly(tmp_path):
    b = Board({"w1": "claude-code", "w2": "claude-code", "w3": "codex"})
    b.send("w1", "w2", 0, 5, "holding the GPU for a bench run until 19:00")
    b.send("w2", "w1", 60, 65, "ok, I'll wait for the GPU")
    b.send("w3", "w1", 120, 125, "is the GPU busy? hold it for me after yours")
    b.send("w1", "w3", 180, 185, "GPU lock is yours after mine")
    b.send("w2", "w3", 240, 245, "hold off on daemon restarts please")
    b.send("w2", "w3", 300, 305, "ZQXW-START on the shared box")
    b.send("w3", "w2", 360, 365, "ZQXW-END")
    for report in both(tmp_path, b):
        props = report["metrics"]["proposals"]
        assert props["definition"]
        by = {r["resource"]: r for r in props["resources"]}
        gpu = by["gpu"]
        assert (gpu["messages"], gpu["senders"], gpu["pairs"], gpu["repeated_pairs"]) == (4, 3, 2, 2)
        assert gpu["markers"] == {"hold": 2, "busy": 1, "wait_for": 1, "lock": 1}
        assert gpu["proposed"] is True and "gpu" in gpu["proposal"]
        daemon = by["daemon"]
        assert (daemon["messages"], daemon["proposed"], daemon["proposal"]) == (1, False, None)
        # A START/END tag outside the fixed vocabulary is counted, never named.
        assert props["unlisted_tagged_messages"] == 2
        assert "zqxw" not in json.dumps(report).lower()


def test_suite_start_and_end_tags_are_hand_coordination_of_the_suite(tmp_path):
    for report in both(tmp_path, kind_board()):
        suite = {r["resource"]: r for r in report["metrics"]["proposals"]["resources"]}["suite"]
        assert (suite["start_tags"], suite["end_tags"]) == (1, 3)
        # w1-c exchanged two suite messages, the other pairs one each.
        assert (suite["messages"], suite["pairs"], suite["repeated_pairs"]) == (4, 3, 1)
        assert suite["proposed"] is False


# ---------------------------------------------------------------- status staleness

def test_status_staleness_at_the_end_of_an_audit_export(tmp_path):
    b = Board({"a": "claude-code", "b": "claude-code", "c": "codex", "d": "claude-code",
               "e": "claude-code"}, statuses={"a": "starting", "b": "starting", "d": "x",
                                              "e": "x"})
    b.send("a", "b", 20000, 20005)      # the last event: the window ends at T0 + 20005
    b.event(20005 - 3 * 3600, "update", "a", {"fields": {"status": "suite=running"},
                                              "before": {"status": "starting"}})
    b.event(20005 - 600, "update", "b", {"fields": {"status": "reviewing"},
                                         "before": {"status": "starting"}})
    b.event(20005 - 700, "update", "e", {"fields": {"task": "other"}, "before": {"task": "task"}})
    b.event(100, "prune", None, {"message_ids": [], "agent_ids": [aid("d")]})
    report = run(write_audit(tmp_path, b))
    stale = report["metrics"]["status_staleness"]
    assert stale["definition"] and stale["available"] is True
    assert stale["threshold_s"] == 7200
    # a: 3 h old, stale. b: 10 min, fresh. c: blank, never stale. d: pruned.
    # e: registered with a status at T0 - 600 that a task-only update left in place.
    assert (stale["agents_with_status"], stale["stale"]) == (3, 2)
    assert stale["share"] == pytest.approx(2 / 3, abs=5e-5)


def test_status_staleness_is_null_for_the_legacy_export(tmp_path):
    report = run(write_legacy(tmp_path, ack_board()))
    stale = report["metrics"]["status_staleness"]
    assert stale["available"] is False and stale["value"] is None and stale["note"]


def test_staleness_leaves_out_ended_and_revoked_sessions(tmp_path):
    """The peer list never shows a detached session's status, and a revoked
    address has no session at all; counting them read every finished session
    as a stale status until prune removed it."""
    names = ("a", "b", "c", "d", "e")
    b = Board({n: "claude-code" for n in names}, statuses={n: "x" for n in names})
    attach = {"generation": 1, "lease_until": 0, "wake_enabled": False, "renewed": False}
    for n in names:
        b.event(10, "attach", n, attach)
    b.event(100, "detach", "a", {"generation": 1})                 # ended
    b.event(100, "detach", "b", {"generation": 1})
    b.event(200, "attach", "b", {**attach, "generation": 2})       # came back
    b.event(300, "recover", None, {"agent_ids": [aid("c"), aid("d")]}, actor="operator")
    b.event(400, "rebind", "d", {"principal": "claude-code"}, actor="operator")
    b.send("e", "b", 20000, 20005)
    stale = run(write_audit(tmp_path, b))["metrics"]["status_staleness"]
    # b, d and e remain, every status set at T0 - 600: all stale.
    assert (stale["agents_with_status"], stale["stale"]) == (3, 3)
    assert (stale["excluded_detached"], stale["excluded_revoked"]) == (1, 1)


def test_since_and_until_scope_messages_but_replay_earlier_rows_for_state(tmp_path):
    """Scoping inside the report keeps the state a filtered export loses:
    principals and statuses set before the scope."""
    b = Board({"c": "claude-code", "w1": "claude-code", "x1": "codex"},
              statuses={"w1": "idle", "x1": "idle"})
    b.send("c", "x1", 100, 150)                       # before the scope
    b.send("c", "w1", 5000, 5100)                     # inside it
    b.send("w1", "x1", 5200)                          # inside; x1 never acts in scope
    b.event(4000, "update", "w1", {"fields": {"status": "reviewing"},
                                   "before": {"status": "idle"}})
    b.send("c", "w1", 9500, 9600)                     # after it
    path = write_audit(tmp_path, b)
    report = run(path, since=T0 + 1000, until=T0 + 9000)
    assert report["volume"]["messages"] == 2
    assert report["input"]["scope"] == {"since": "2026-09-23T06:16:40+00:00",
                                        "until": "2026-09-23T08:30:00+00:00"}
    lat = report["metrics"]["ack_latency"]["overall"]
    assert set(lat) == {"all", "claude-code", "codex"}       # x1 resolved from its register
    assert lat["codex"]["never_acked"] == 1
    stale = report["metrics"]["status_staleness"]
    # At T0 + 9000: w1 set 5000 s before (fresh), x1 9600 s before (stale), c blank.
    assert (stale["agents_with_status"], stale["stale"]) == (2, 1)
    assert stale["agents_without_status_event"] == 0
    # The same export filtered at the source loses both.
    rows = [row for row in b.audit() if row["created_at"] >= T0 + 1000]
    filtered = tmp_path / "filtered.jsonl"
    filtered.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    report = run(filtered, until=T0 + 9000)
    assert report["input"]["filtered_export"] is True
    # c and x1 registered before the cut; x1 never acted after it.
    assert report["metrics"]["status_staleness"]["agents_without_status_event"] == 2
    assert "unknown" in report["metrics"]["ack_latency"]["overall"]
    assert run(path)["input"]["filtered_export"] is False


def test_an_export_with_a_gap_in_its_sequence_is_flagged(tmp_path):
    rows = ack_board().audit()
    del rows[3]
    path = tmp_path / "gap.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert run(path)["input"]["filtered_export"] is True
    # A log that retention cut starts after its recorded anchor: not filtered.
    rows = ack_board().audit()
    kept = rows[3:]
    kept.append({**kept[-1], "seq": kept[-1]["seq"] + 1, "event": "audit_prune",
                 "actor": "daemon", "principal": "", "agent_id": "",
                 "recipient_agent_id": None, "message_id": None, "project": "", "task": "",
                 "payload": {"through_seq": rows[2]["seq"], "through_hash": "0" * 64,
                             "removed": 3, "cutoff": 0, "retention_days": 90}})
    path.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
    assert run(path)["input"]["filtered_export"] is False


# ---------------------------------------------------------------- placeholders

def test_metrics_that_need_future_schema_are_null_with_a_note(tmp_path):
    report = run(write_audit(tmp_path, ack_board()))
    for key in ("wakes_per_session_hour", "requests_past_reply_by",
                "needs_human_time_to_answer"):
        metric = report["metrics"][key]
        assert metric["value"] is None and metric["note"] and metric["definition"]


def test_both_formats_agree_on_every_message_metric(tmp_path):
    board = kind_board()
    for m in ack_board().messages:
        board.messages.append({**m, "id": uuid.uuid5(uuid.NAMESPACE_OID, "merged" + m["id"]).hex,
                               "sender": {"x1": "w3"}.get(m["sender"], m["sender"]),
                               "recipient": {"x1": "w3"}.get(m["recipient"], m["recipient"]),
                               "sent": m["sent"] + 5000,
                               "acked": None if m["acked"] is None else m["acked"] + 5000})
    legacy, audit = both(tmp_path, board, windows=WINDOWS)
    assert legacy["input"]["format"] == "legacy-board-export"
    assert audit["input"]["format"] == "audit-export"
    for key in legacy["metrics"]:
        if key != "status_staleness":
            assert legacy["metrics"][key] == audit["metrics"][key], key
    assert legacy["volume"] == audit["volume"]
    assert legacy["coordinator"] == audit["coordinator"]


def test_a_v46_export_reads_the_body_beside_the_hashed_digest(tmp_path):
    """Schema v46 moves a send's body out of the hashed payload into a
    top-level ``body``; read only from the payload, every v46 message would
    lose its text and the body-driven metrics would degrade silently."""
    board = kind_board()
    v45 = run(write_audit(tmp_path, board))
    rows = as_v46(board.audit())
    v46 = run(write_rows(tmp_path, rows, "v46.jsonl"))
    assert v46["metrics"] == v45["metrics"]
    assert v46["input"]["bodies_missing"] == 0
    # A redacted body is null: its message still counts, with no text to read.
    first_send = next(row for row in rows if row["event"] == "send")
    first_send["body"] = None
    redacted = run(write_rows(tmp_path, rows, "redacted.jsonl"))
    assert redacted["volume"] == v45["volume"]
    assert redacted["input"]["bodies_missing"] == 1
    kinds = redacted["metrics"]["kind_mix"]
    assert kinds["heuristic_messages"] == 10
    assert (v45["metrics"]["kind_mix"]["counts"]["CLAIM"], kinds["counts"]["CLAIM"]) == (2, 1)


def test_a_v46_export_of_an_upgraded_bank_keeps_the_older_bodies(tmp_path):
    """Sends written before the upgrade come out of a v46 export with a null
    ``body`` and their text still in the payload: they are read, not lost."""
    board = kind_board()
    v45 = run(write_audit(tmp_path, board))
    rows = as_v46(board.audit(), upgraded_at=T0 + 1000)
    assert any(r["event"] == "send" and r["body"] is None for r in rows)
    assert any(r["event"] == "send" and r["body"] is not None for r in rows)
    mixed = run(write_rows(tmp_path, rows, "mixed.jsonl"))
    assert mixed["metrics"] == v45["metrics"]
    assert mixed["input"]["bodies_missing"] == 0


# ---------------------------------------------------------------- windows

def test_window_arguments_take_iso_with_an_offset_or_epoch_seconds():
    parse = report_mod.parse_window
    assert parse("evening=2026-09-23T16:00+10:00/2026-09-23T21:30+10:00") == (
        "evening", T0, T0 + 5.5 * 3600)
    assert parse(f"late={T0 + 60}/") == ("late", T0 + 60, None)
    for bad in ("no-equals", "x=2026-09-23T16:00/2026-09-23T17:00", "x=1/2/3", "=1/2", "x=2/1"):
        with pytest.raises(ValueError):
            parse(bad)


def test_window_labels_are_short_plain_names():
    """A label is written to both files verbatim, so it cannot carry a path
    or an address."""
    parse = report_mod.parse_window
    assert parse("16:00-21:30 AEST=1/2")[0] == "16:00-21:30 AEST"
    for bad in ("C:\\tmp=1/2", "a/b=1/2", "me@example.com=1/2", "x" * 41 + "=1/2"):
        with pytest.raises(ValueError, match="label"):
            parse(bad)


# ---------------------------------------------------------------- CLI and files

def test_cli_writes_json_and_markdown_and_never_overwrites(tmp_path):
    source = write_audit(tmp_path, ack_board())
    out = tmp_path / "report.json"
    window = ["--window", "evening=2026-09-23T16:00+10:00/2026-09-23T16:00:55+10:00"]
    assert report_mod.main([str(source), "--out", str(out), *window]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["harness"] == "evals/coordination_report.py"
    assert data["metrics"]["ack_latency"]["windows"]["evening"]["by_principal"]["claude-code"][
        "acked"] == 4
    md = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "## Acknowledgement latency" in md and "claude-code" in md

    # Either file of the pair existing blocks the run, and nothing is written.
    target = tmp_path / "second.json"
    for blocker, sibling in ((target, target.with_suffix(".md")),
                             (target.with_suffix(".md"), target)):
        blocker.write_text("keep", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            report_mod.main([str(source), "--out", str(target)])
        assert exc.value.code == 2
        assert blocker.read_text(encoding="utf-8") == "keep"
        assert not sibling.exists()
        blocker.unlink()
    before = out.read_text(encoding="utf-8")
    out.write_text("stale", encoding="utf-8")
    assert report_mod.main([str(source), "--out", str(out), "--force", *window]) == 0
    assert out.read_text(encoding="utf-8") != "stale"
    assert json.loads(out.read_text(encoding="utf-8"))["metrics"] == json.loads(before)["metrics"]
    with pytest.raises(SystemExit):
        report_mod.main([str(source), "--out", str(tmp_path / "report.txt")])
    with pytest.raises(SystemExit):
        report_mod.main([str(source)])          # --out is required


def test_unreadable_input_is_refused(tmp_path):
    for content in ("not json at all", json.dumps({"no": "messages"}), json.dumps([1, 2])):
        bad = tmp_path / "bad.json"
        bad.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            report_mod.load_board(bad)


def test_duplicate_message_ids_are_refused_in_both_formats(tmp_path):
    b = ack_board()
    b.messages.append({**b.messages[0], "sent": 900, "acked": None})
    for path in (write_legacy(tmp_path, b), write_audit(tmp_path, b)):
        with pytest.raises(ValueError, match="duplicate"):
            report_mod.load_board(path)


def test_a_board_export_saved_with_a_byte_order_mark_still_loads(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(ack_board().legacy(300)).encode())
    assert report_mod.load_board(path).format == "legacy-board-export"


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
def test_the_stale_threshold_must_be_a_positive_number(tmp_path, value):
    source = write_audit(tmp_path, ack_board())
    with pytest.raises(SystemExit):
        report_mod.main([str(source), "--out", str(tmp_path / "r.json"), f"--stale-after={value}"])
    assert not (tmp_path / "r.json").exists()


def test_the_input_is_never_an_output_even_with_force(tmp_path):
    """--force replaces results, never the private export they came from."""
    for name, out in (("board.json", "board.json"), ("night.md", "night.json")):
        source = write_legacy(tmp_path, ack_board(), name=name)
        before = source.read_bytes()
        with pytest.raises(SystemExit):
            report_mod.main([str(source), "--out", str(tmp_path / out), "--force"])
        assert source.read_bytes() == before


def test_a_rendering_failure_writes_neither_file(tmp_path, monkeypatch):
    source = write_audit(tmp_path, ack_board())
    out = tmp_path / "r.json"

    def broken(report):
        raise RuntimeError("render failed")

    monkeypatch.setattr(report_mod, "render_markdown", broken)
    with pytest.raises(RuntimeError):
        report_mod.main([str(source), "--out", str(out)])
    assert not out.exists() and not out.with_suffix(".md").exists()


# ---------------------------------------------------------------- privacy

SENTINEL = "SENTINEL-7f3a-do-not-leak"
HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")


def sentinel_board():
    # A principal shaped like a plain name is still an operator's choice
    # (a username, a host): only known role names are written.
    names = {"c": "claude-code", "w1": "claude-code", "w2": "sentinel-user", "w3": "claude-code",
             "x1": "codex"}
    # Windows paths use the placeholder shape the tracked-tree identifier
    # guard sanctions (tests/test_release_ux.py); "sentinel-user" is still
    # checked for below.
    b = Board(names, statuses={n: f"{SENTINEL} status C:\\Users\\<sentinel-user>\\wt"
                               for n in names}, scope=f"{SENTINEL}-scope")
    texts = [f"SUITE-START {SENTINEL} C:\\Users\\<sentinel-user>\\repo\\tests\\x.py",
             f"SUITE-END {SENTINEL} /home/sentinel-user/repo green - you're up",
             f"CLAIM {SENTINEL} holding the GPU lock, wait for me /Users/sentinel-user/x",
             f"can you review {SENTINEL}? DEPLOY-NEEDED for the maintainer",
             f"HOLD {SENTINEL} nobody start a full suite (daemon busy)"]
    t = 0
    for rnd in range(3):
        for i, text in enumerate(texts):
            for w in ("w1", "w2", "w3", "x1"):
                t += 7
                b.send("c", w, t, t + 40 if (i + rnd) % 3 else None, text)
                b.send(w, "c", t + 1, t + 90, text + f" reply {rnd}")
    b.event(t + 100, "update", "w1", {"fields": {"status": f"{SENTINEL} again",
                                                  "task": SENTINEL},
                                       "before": {"status": "old", "task": "task"}})
    return b


@pytest.mark.parametrize("fmt", ["audit", "v46", "legacy"])
def test_no_body_label_status_path_or_agent_id_reaches_either_output(tmp_path, fmt):
    board = sentinel_board()
    if fmt == "v46":
        rows = as_v46(board.audit())
        assert all(SENTINEL in r["body"] for r in rows if r["event"] == "send")
        source = write_rows(tmp_path, rows, "board.jsonl")
    else:
        source = (write_audit if fmt == "audit" else write_legacy)(tmp_path, board)
    assert SENTINEL in source.read_text(encoding="utf-8")
    out = tmp_path / "out" / "report.json"
    out.parent.mkdir()
    assert report_mod.main([str(source), "--out", str(out), "--window",
                            f"all={T0}/{T0 + 10 ** 6}"]) == 0
    for path in (out, out.with_suffix(".md")):
        text = path.read_text(encoding="utf-8")
        assert SENTINEL not in text
        assert "sentinel-user" not in text
        assert "agent-label" not in text
        assert not HEX_ID.search(text), HEX_ID.search(text)
        assert all(aid(n) not in text for n in board.agents)
        assert str(tmp_path) not in text and source.name not in text
    data = json.loads(out.read_text(encoding="utf-8"))
    # Real work happened: the classifiers ran over the sentinel bodies.
    assert data["metrics"]["suite_baton"]["suite_start"] == 3 * 4 * 2
    assert data["metrics"]["fanout_bursts"]["bursts"] > 0


def test_only_known_role_principals_are_written(tmp_path):
    b = Board({"a": "claude-code", "b": "Sentinel User <sentinel-user@example.com>",
               "c": "john.smith", "d": "10.0.0.5", "e": "Codex", "f": "sentinel-user"})
    for i, other in enumerate("bcdef"):
        b.send("a", other, i, i + 5)
    path = write_legacy(tmp_path, b)
    report = run(path)
    text = json.dumps(report) + report_mod.render_markdown(report)
    for leak in ("sentinel-user", "example.com", "john.smith", "10.0.0.5"):
        assert leak not in text
    assert set(report["metrics"]["ack_latency"]["overall"]) == {
        "all", "principal-1", "principal-2", "principal-3", "codex", "principal-4"}
    assert report["volume"]["by_sender_principal"] == {"claude-code": 5}
    # An operator can keep a name it knows to be a role, never a reserved one.
    kept = run(path, keep_principals=("john.smith",))
    assert "john.smith" in kept["metrics"]["ack_latency"]["overall"]
    for reserved in ("all", "unknown", "principal-2"):
        with pytest.raises(ValueError, match="principal"):
            run(path, keep_principals=(reserved,))


# ---------------------------------------------------------------- the real store

def test_a_real_board_audit_export_reads_back(store, pg_url, monkeypatch, tmp_path):
    """Drive the real store, export through the real CLI, report on it: the
    synthetic audit rows above cannot drift from what the daemon writes."""
    from pseudolife_memory.board_audit_cli import main as board_audit
    a = store.register("alice", status="starting")
    b = store.register("alice", status="idle")
    c = store.register("alice")
    ids = []
    for n in range(3):
        store.test_time[0] += 10
        ids.append(store.send(*creds(a), to=b["agent_id"], text=f"SUITE-END #{n} {SENTINEL}",
                              request_id=f"r{n}")["message_id"])
    store.send(*creds(a), to=c["agent_id"], text="can you look?", request_id="rc")
    store.test_time[0] += 30
    store.receive(*creds(b))
    store.ack(*creds(b), message_id=",".join(ids))
    store.test_time[0] += 3 * 3600
    store.update(*creds(b), status="reviewing")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    export = tmp_path / "board.jsonl"
    assert board_audit(["export", "--out", str(export)]) == 0
    report = run(export, keep_principals=("alice",))
    assert report["input"]["format"] == "audit-export"
    assert report["input"]["filtered_export"] is False
    assert report["volume"]["messages"] == 4
    lat = report["metrics"]["ack_latency"]["overall"]["alice"]
    assert (lat["messages"], lat["acked"], lat["never_acked"]) == (4, 3, 1)
    assert lat["median_s"] == 40.0          # sent at +10/+20/+30, one ack at +60
    batch = report["metrics"]["batch_acks"]
    assert (batch["batch_instants"], batch["messages_in_batches"]) == (1, 3)
    assert report["metrics"]["suite_baton"]["suite_end"] == 3
    stale = report["metrics"]["status_staleness"]
    # a's "starting" is 3 h old at the window's end; b just updated; c is blank.
    assert (stale["agents_with_status"], stale["stale"]) == (2, 1)
    assert SENTINEL not in json.dumps(report)
    # Not a known role name: kept only when asked for.
    assert set(run(export)["metrics"]["ack_latency"]["overall"]) == {"all", "principal-1"}


# ---------------------------------------------------------------- the committed baseline

BASELINE = REPO / "evals" / "results" / "coordination-baseline-20260924.json"


def test_the_committed_baseline_is_aggregate_only():
    text = BASELINE.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["harness"] == "evals/coordination_report.py"
    assert data["input"]["format"] == "legacy-board-export"
    # No agent or message id; the input's 64-hex sha256 has no boundary at 32.
    assert not HEX_ID.search(text)
    for leak in (":\\", "/Users/", "/home/", "C:/", "@"):
        assert leak not in text, leak
    # Every metric states what it measures.
    assert all(metric.get("definition") for metric in data["metrics"].values())


# The one-off aggregate analysis of the same export (2026-09-25, not
# committed: its output named agents by id prefix). The report reproduces
# every figure of it below. Two definitions were corrected here: baton passes
# group SUITE-END copies per sender rather than against the latest group only
# (the trial had no interleaved copies, so its 39 stands), and fan-out
# similarity no longer lets difflib's autojunk drop common characters on
# texts of 200+ characters, which moves the one figure left out of this
# table: the analysis counted 35 bursts covering 165 messages (20.2%), the
# report 39 covering 186.
TRIAL_ANALYSIS = {
    ("volume", "messages"): 816, ("volume", "directed_pairs"): 171,
    ("volume", "senders"): 25, ("volume", "self_messages"): 2,
    ("batch_acks", "acked_messages"): 804, ("batch_acks", "batch_instants"): 53,
    ("batch_acks", "messages_in_batches"): 263,
    ("pair_peak_60min", "max"): 10,
    ("suite_baton", "suite_start"): 99, ("suite_baton", "suite_end"): 93,
    ("suite_baton", "baton_passes"): 39,
}
TRIAL_LATENCY = {   # (window or None for overall, principal): messages, acked, median, p90
    ("16:00-21:30 AEST", "claude-code"): (358, 355, 296.8, 5261.2),
    ("21:30-01:00 AEST", "claude-code"): (201, 197, 87.1, 1060.5),
    ("01:00-07:05 AEST", "claude-code"): (229, 227, 31.7, 140.2),
    ("16:00-21:30 AEST", "codex"): (9, 9, 114.0, 1615.5),
    ("21:30-01:00 AEST", "codex"): (2, 2, 2752.6, 4844.1),
    ("01:00-07:05 AEST", "codex"): (17, 14, 154.5, 413.4),
    (None, "claude-code"): (788, 779, 95.3, 1544.2),
    (None, "codex"): (28, 25, 125.3, 1151.0),
    (None, "all"): (816, 804, 96.4, 1559.0),
}
TRIAL_KINDS = {"NOTICE": 542, "REQUEST": 92, "CLAIM": 89, "HANDOFF": 84, "NEEDS-HUMAN": 9}


def test_the_baseline_reproduces_the_trial_analysis_where_definitions_agree():
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    metrics = data["metrics"]
    for (section, key), expected in TRIAL_ANALYSIS.items():
        source = data["volume"] if section == "volume" else metrics[section]
        assert source[key] == expected, (section, key)
    assert metrics["pair_peak_60min"]["pairs_over"] == {"3": 49, "6": 6, "10": 0}
    lat = metrics["ack_latency"]
    for (window, principal), (messages, acked, median, p90) in TRIAL_LATENCY.items():
        stats = (lat["overall"] if window is None
                 else lat["windows"][window]["by_principal"])[principal]
        assert (stats["messages"], stats["acked"], stats["median_s"], stats["p90_s"]) == (
            messages, acked, median, p90), (window, principal)
    assert metrics["kind_mix"]["counts"] == TRIAL_KINDS
