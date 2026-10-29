"""Write the golden sequences for the Rust port of ``ActivityRecorder``
(``src/principal_activity.rs``: ``note``, ``seen``, ``rejected``, ``due``,
``take``, ``give_back``, ``refusal_log_line``) and the bearer prefix rules
(schema v58).

Every expected value comes from the real Python recorder in this checkout,
driven by fake wall and monotonic clocks through scripted sequences: the
first write due at once, the once-a-minute hold, repeat successes keeping
the latest time, refusals counting with first and last times, a failed write
given back and merged, the per-batch prefix cap, the gate's outcome rules
(open mode, environment, store, no bearer, another scheme) and the prefix of
a latin-1 header carrying UTF-8.

Output: ``goldens/principal-activity.json``.

usage: python gen_principal_activity_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "principal-activity.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory import principal_activity as activity  # noqa: E402

UTF8_HEADER = "Bearer " + "fixture-tökén".encode("utf-8").decode("latin-1")

# (op, args...) steps. "tick" advances both clocks; "take" records the batch
# (or None); "give_back" returns the most recent taken batch.
SEQUENCES = {
    "first-write-due-at-once": [
        ("due",), ("take",), ("seen", "laptop"), ("due",), ("take",), ("due",)],
    "once-a-minute": [
        ("seen", "laptop"), ("take",), ("tick", 10.0), ("seen", "laptop"), ("due",),
        ("tick", 49.9), ("due",), ("tick", 0.1), ("due",), ("take",)],
    "latest-success-wins": [
        ("seen", "laptop"), ("tick", 5.0), ("seen", "laptop"), ("seen", "desk"), ("take",)],
    "refusals-count": [
        ("rejected", "ab12cd34"), ("tick", 3.0), ("rejected", "ab12cd34"),
        ("rejected", "00ff00ff"), ("take",), ("log",)],
    "give-back-merges": [
        ("seen", "laptop"), ("rejected", "ab12cd34"), ("take",), ("tick", 2.0),
        ("seen", "laptop"), ("rejected", "ab12cd34"), ("rejected", "11111111"),
        ("give_back",), ("due",), ("tick", 60.0), ("due",), ("take",)],
    "older-given-back-time-loses": [
        ("seen", "laptop"), ("take",), ("tick", 4.0), ("seen", "laptop"), ("give_back",),
        ("tick", 60.0), ("take",)],
    "gate-outcomes": [
        ("note", "Bearer x", "laptop", "store"), ("note", "Bearer y", "desk", "environment"),
        ("note", None, "default", "open"), ("note", "Bearer fixture-refused", None, None),
        ("note", None, None, None), ("note", "Basic Zm9vOmJhcg==", None, None),
        ("note", "bearer \tfixture-refused \t", None, None), ("note", "Bearer ", None, None),
        ("note", UTF8_HEADER, None, None), ("take",), ("log",)],
}
CAPPED = {
    "prefix-cap": (3, [
        ("rejected", "00000001"), ("rejected", "00000002"), ("rejected", "00000003"),
        ("rejected", "00000004"), ("rejected", "00000005"), ("rejected", "00000001"),
        ("take",), ("log",)]),
    "log-names-five": (100, [("rejected", f"0000000{i}") for i in range(1, 8) for _ in range(i)]
                       + [("take",), ("log",)]),
}


def batch_json(batch):
    if batch is None:
        return None
    return {"seen": dict(sorted(batch.seen.items())),
            "rejected": {k: list(v) for k, v in sorted(batch.rejected.items())},
            "dropped": batch.dropped}


def run(steps, max_rejected=activity.MAX_PENDING_REJECTED):
    clocks = {"wall": 1_800_000_000.0, "mono": 1000.0}
    recorder = activity.ActivityRecorder(wall=lambda: clocks["wall"], clock=lambda: clocks["mono"],
                                         max_rejected=max_rejected)
    out, last = [], None
    for step in steps:
        op, args = step[0], step[1:]
        if op == "tick":
            clocks["wall"] += args[0]
            clocks["mono"] += args[0]
            out.append(None)
        elif op == "seen":
            recorder.seen(*args)
            out.append(None)
        elif op == "rejected":
            recorder.rejected(*args)
            out.append(None)
        elif op == "note":
            activity.note(recorder, *args)
            out.append(None)
        elif op == "due":
            out.append(recorder.due())
        elif op == "take":
            batch = recorder.take()
            if batch is not None:
                last = batch
            out.append(batch_json(batch))
        elif op == "give_back":
            recorder.give_back(last)
            out.append(None)
        elif op == "log":
            out.append(activity.refusal_log_line(last))
        else:
            raise ValueError(op)
    return out


def main() -> int:
    sequences = []
    for name, steps in SEQUENCES.items():
        sequences.append({"name": name, "max_rejected": activity.MAX_PENDING_REJECTED,
                          "steps": [list(s) for s in steps], "expect": run(steps)})
    for name, (cap, steps) in CAPPED.items():
        sequences.append({"name": name, "max_rejected": cap,
                          "steps": [list(s) for s in steps], "expect": run(steps, cap)})
    prefixes = [{"header": h, "presented": activity.presented_bearer(h),
                 "prefix": (activity.bearer_hash_prefix(activity.presented_bearer(h))
                            if activity.presented_bearer(h) else None)}
                for h in ("Bearer abc", "bearer \tabc \t", "Basic abc", "Bearer ", "", "Bearerabc",
                          "Bearer a b", UTF8_HEADER)]
    tokens = [{"token": t, "prefix": activity.token_hash_prefix(t)}
              for t in ("fixture-doctor-token", "fixture-tökén")]
    golden = {
        "source": "pseudolife_memory/principal_activity.py",
        "constants": {"flush_interval_s": activity.FLUSH_INTERVAL_S,
                      "hash_prefix_len": activity.HASH_PREFIX_LEN,
                      "max_pending_rejected": activity.MAX_PENDING_REJECTED,
                      "max_rejected_rows": activity.MAX_REJECTED_ROWS,
                      "rejected_retention_s": activity.REJECTED_RETENTION_S,
                      "log_prefixes": activity.LOG_PREFIXES},
        "start": {"wall": 1_800_000_000.0, "mono": 1000.0},
        "sequences": sequences, "prefixes": prefixes, "tokens": tokens,
    }
    TARGET.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)}: {len(sequences)} sequences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
