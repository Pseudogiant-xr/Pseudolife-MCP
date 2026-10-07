"""``evals/codex_doorbell_probe.py``: the live doorbell probe's recorder.

Pinned on a hand-written ledger and audit export: the timeline it derives,
the outcome it names for each step that did not happen, and its privacy by
construction (no agent id, body or path reaches the record).
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from evals import codex_doorbell_probe as probe

T0 = 1_790_500_000.0
TASK = uuid.uuid5(uuid.NAMESPACE_OID, "doorbell-probe-task").hex
SENDER = uuid.uuid5(uuid.NAMESPACE_OID, "doorbell-probe-sender").hex
COLUMNS = ("seq", "event", "actor", "principal", "agent_id", "recipient_agent_id",
           "project", "task", "message_id", "payload", "created_at", "hlc", "prev_hash", "hash")


def _row(seq, event, agent, at, *, recipient="", message_id="", actor="agent", payload=None):
    row = {"seq": seq, "event": event, "actor": actor, "principal": "codex", "agent_id": agent,
           "recipient_agent_id": recipient, "project": "p", "task": "t",
           "message_id": message_id, "payload": json.dumps(payload or {}),
           "created_at": at, "hlc": f"{int(at)}-0", "prev_hash": "0" * 64, "hash": "1" * 64}
    assert set(row) == set(COLUMNS)
    return row


def _export(tmp_path, rows):
    path = tmp_path / "board.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _ledger(tmp_path, lines):
    path = tmp_path / "ledger.log"
    path.write_text("".join(f"{int(at)}\t{kind}\tabcd1234\t3\t120\n" for at, kind in lines))
    return path


@pytest.fixture(autouse=True)
def _no_cli(monkeypatch):
    monkeypatch.setattr(probe, "_versions", lambda: {"pseudolife-mcp": "fixture", "codex": "fixture"})


def test_a_complete_run_is_rung_turned_and_acknowledged(tmp_path):
    message = uuid.uuid4().hex
    rows = [
        _row(1, "send", SENDER, T0 + 10, recipient=TASK, message_id=message,
             payload={"body": "peer text that must never be copied"}),
        _row(2, "update", TASK, T0 + 40, payload={"status": "woken: reading mail"}),
        _row(3, "read", TASK, T0 + 41, message_id=message),
        _row(4, "ack", TASK, T0 + 45, message_id=message),
    ]
    ledger = _ledger(tmp_path, [(T0 - 100, "bell"), (T0 + 12, "hint"), (T0 + 30, "bell")])
    out = tmp_path / "record.json"
    assert probe.main(["--recipient", TASK, "--out", str(out), "--ledger", str(ledger),
                       "--export", str(_export(tmp_path, rows)), "--since", str(T0)]) == 0
    record = json.loads(out.read_text())
    line = record["timeline"]
    assert record["bells"] == [T0 + 30]                       # the old bell is outside the window
    assert (line["sent_at"], line["bell_at"], line["read_at"], line["ack_at"]) == (
        T0 + 10, T0 + 30, T0 + 41, T0 + 45)
    assert line["first_event_after_bell"] == {"at": T0 + 40, "event": "update"}
    assert (line["send_to_bell_s"], line["bell_to_first_event_s"], line["bell_to_ack_s"]) == (
        20.0, 10.0, 15.0)
    assert line["counts"] == {"sends": 1, "bells": 1, "reads": 1, "acks": 1,
                              "events_by_recipient_after_bell": 3}
    assert line["outcome"] == "rung, turn taken, acknowledged"
    text = out.read_text()
    for secret in (TASK, SENDER, message, "peer text", str(tmp_path), "woken"):
        assert secret not in text
    assert record["recipient"] == hashlib.sha256(TASK.encode()).hexdigest()[:8]


@pytest.mark.parametrize("rows,bells,outcome", [
    ([], [], "no send to the recipient in the window"),
    (["send"], [], "no bell: the shim queued nothing"),
    (["send"], [T0 + 30], "bell but no turn: the task caused no board event after it"),
    (["send", "read"], [T0 + 30], "turn but no acknowledgment"),
])
def test_each_missing_step_is_named(tmp_path, rows, bells, outcome):
    message = uuid.uuid4().hex
    export = []
    if "send" in rows:
        export.append(_row(1, "send", SENDER, T0 + 10, recipient=TASK, message_id=message))
    if "read" in rows:
        export.append(_row(2, "read", TASK, T0 + 40, message_id=message))
    out = tmp_path / "record.json"
    assert probe.main(["--recipient", TASK, "--out", str(out),
                       "--ledger", str(_ledger(tmp_path, [(b, "bell") for b in bells])),
                       "--export", str(_export(tmp_path, export)), "--since", str(T0)]) == 0
    assert json.loads(out.read_text())["timeline"]["outcome"] == outcome


def test_the_watch_step_writes_a_record_without_a_timeline(tmp_path):
    out = tmp_path / "record.json"
    ledger = _ledger(tmp_path, [(T0 + 5, "bell")])
    assert probe.main(["--recipient", TASK, "--out", str(out), "--ledger", str(ledger),
                       "--since", str(T0), "--watch", "0.1"]) == 0
    record = json.loads(out.read_text())
    assert record["timeline"] is None and record["bells"] == [T0 + 5]
    # Never overwrites its own record without --force.
    assert probe.main(["--recipient", TASK, "--out", str(out), "--ledger", str(ledger)]) == 2


def test_a_missing_ledger_or_bad_export_is_reported_not_raised(tmp_path):
    out = tmp_path / "record.json"
    assert probe.read_bells(tmp_path / "absent.log", since=0) == []
    bad = tmp_path / "board.jsonl"
    bad.write_text("{not json\n")
    assert probe.main(["--recipient", TASK, "--out", str(out), "--ledger", str(tmp_path / "absent.log"),
                       "--export", str(bad)]) == 2
    assert not out.exists()
