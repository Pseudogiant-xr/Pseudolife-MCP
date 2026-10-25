"""Write the golden sequences for the Rust port of ``DreamStallTracker``
(``src/dream/run.rs``: ``record``, ``check_overdue``, ``take_notice``,
``mark_noticed``, ``snapshot``) and ``dream_stall_notice_text``.

Every expected value comes from the real Python tracker in this checkout,
driven by a fake clock through scripted sequences: a fallback warning that
escalates into a hard stall, repeats inside and outside the repeat window,
an undelivered notice that stays owed, a recovery and its clear, the
overdue rule opening a stall when no dream serves a due backlog, a flapping
extractor announced at most once per window, and the write-phase hold that
says nothing. The notice texts are rendered at fixed UTC offsets, so the
golden does not depend on the machine's zone.

Output: ``goldens/stall-tracker.json``.

usage: python gen_stall_tracker_goldens.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "stall-tracker.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory import service_dream  # noqa: E402
from pseudolife_memory.memory import dream as dream_mod  # noqa: E402

FAIL = {"pulled": 3, "extractor_failed": True,
        "extractor_error": {"reason": "extractor_unreachable", "error": "connection refused"}}
LOGIN = {"pulled": 2, "extractor_failed": True,
         "extractor_error": {"reason": "login_expired", "error": "401"}}
OK = {"pulled": 3}
EMPTY = {"pulled": 0}
QUARANTINED = {"pulled": 2, "quarantined": 2}
HOLD = {"pulled": 2, "extractor_failed": True, "hold_phase": "write"}
SKIPPED = {"skipped": "dream_in_progress", "pulled": 0}

# (op, args...) steps; "at" advances the fake clock to an absolute time.
SEQUENCES = {
    "warning-escalates": [
        ("at", 1000.0), ("record", OK, False),
        ("at", 1600.0), ("record", OK, True),
        ("at", 2200.0), ("record", OK, True),
        ("take", 21600.0), ("mark", "begin"), ("take", 21600.0),
        ("at", 2800.0), ("record", FAIL, False), ("take", 21600.0), ("mark", "escalate"),
        ("at", 3400.0), ("take", 21600.0),
        ("at", 30000.0), ("take", 21600.0), ("mark", "repeat"),
        ("at", 31000.0), ("record", OK, False), ("take", 21600.0), ("mark", "clear"),
        ("take", 21600.0),
    ],
    "undelivered-stays-owed": [
        ("at", 100.0), ("record", LOGIN, False), ("at", 200.0), ("record", LOGIN, False),
        ("take", 3600.0), ("at", 300.0), ("take", 3600.0), ("mark", "begin"),
        ("at", 400.0), ("record", OK, False), ("take", 3600.0),
        # a new incident before the clear went out: the clear is stale
        ("at", 500.0), ("record", FAIL, False), ("at", 600.0), ("record", FAIL, False),
        ("take", 3600.0), ("at", 4300.0), ("take", 3600.0),
    ],
    "overdue-opens-a-stall": [
        ("at", 0.0), ("overdue", 5, 3, True, 60.0), ("at", 100.0), ("overdue", 5, 3, True, 60.0),
        ("at", 181.0), ("overdue", 5, 3, True, 60.0), ("take", 21600.0), ("mark", "begin"),
        ("at", 200.0), ("overdue", 1, 3, True, 60.0), ("overdue", 5, 3, False, 60.0),
        ("at", 400.0), ("record", OK, False), ("take", 21600.0),
        # a success resets the due clock: the next due tick starts afresh
        ("at", 500.0), ("overdue", 5, 3, True, 60.0), ("at", 600.0),
        ("overdue", 5, 3, True, 60.0), ("take", 21600.0),
    ],
    "due-clock-resets-on-success": [
        ("at", 0.0), ("overdue", 5, 3, True, 60.0), ("at", 150.0), ("record", OK, False),
        ("at", 200.0), ("overdue", 5, 3, True, 60.0), ("take", 21600.0),
        ("at", 381.0), ("overdue", 5, 3, True, 60.0), ("take", 21600.0),
    ],
    "flapping-is-announced-once-per-window": [
        ("at", 0.0), ("record", FAIL, False), ("at", 10.0), ("record", FAIL, False),
        ("take", 3600.0), ("mark", "begin"),
        ("at", 20.0), ("record", OK, False), ("take", 3600.0), ("mark", "clear"),
        ("at", 30.0), ("record", FAIL, False), ("at", 40.0), ("record", FAIL, False),
        ("take", 3600.0), ("at", 3700.0), ("take", 3600.0),
    ],
    "silent-results": [
        ("at", 0.0), ("record", HOLD, False), ("record", SKIPPED, True), ("record", EMPTY, True),
        ("record", {"error": "boom", "pulled": 0}, False),
        ("at", 5.0), ("record", QUARANTINED, False), ("at", 6.0), ("record", QUARANTINED, False),
        ("take", 3600.0), ("at", 7.0), ("record", EMPTY, False), ("take", 3600.0),
        ("at", 8.0), ("record", OK, False), ("take", 3600.0),
    ],
}

OFFSETS = [0, 11 * 3600, -5 * 3600 - 1800]
TEXT_RECORDS = [
    ("begin", {"since": 1792891234.9, "reason": "extractor_unreachable"}),
    ("repeat", {"since": 1792891259.9999996, "reason": "login_expired"}),
    ("escalate", {"since": 1700000000.0, "reason": "something_new"}),
    ("begin", {"since": 1700000000.0, "reason": "served_by_fallback"}),
    ("begin", {"since": 1700000000.0, "reason": None}),
    ("clear", {"since": 1700000000.0, "reason": "extractor_error", "recovered_at": 1700003599.5}),
]


def run(steps):
    now = [0.0]
    t = service_dream.DreamStallTracker(clock=lambda: now[0])
    out = []
    for step in steps:
        op, args = step[0], step[1:]
        if op == "at":
            now[0] = args[0]
            continue
        if op == "record":
            t.record(args[0], served_by_fallback=args[1])
            got = None
        elif op == "overdue":
            t.check_overdue(backlog=args[0], min_batch=args[1], would_fire=args[2],
                            interval=args[3])
            got = None
        elif op == "take":
            due = t.take_notice(args[0])
            got = None if due is None else [due[0], due[1]]
        elif op == "mark":
            t.mark_noticed(args[0])
            got = None
        out.append({"op": op, "args": list(args), "now": now[0], "got": got,
                    "snapshot": t.snapshot()})
    return out


def text_at(kind, record, offset):
    """``dream_stall_notice_text`` with the zone fixed at ``offset``."""
    zone = timezone(timedelta(seconds=offset))
    saved = dream_mod._local_minute
    dream_mod._local_minute = lambda ts: datetime.fromtimestamp(
        float(ts), tz=zone).isoformat(sep=" ", timespec="minutes")
    try:
        return dream_mod.dream_stall_notice_text(kind, record)
    finally:
        dream_mod._local_minute = saved


def main() -> int:
    golden = {
        "sequences": {name: run(steps) for name, steps in SEQUENCES.items()},
        "texts": [{"kind": k, "record": r, "offset": off, "text": text_at(k, r, off)}
                  for k, r in TEXT_RECORDS for off in OFFSETS],
    }
    TARGET.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)}: {len(golden['sequences'])} sequences, "
          f"{len(golden['texts'])} texts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
