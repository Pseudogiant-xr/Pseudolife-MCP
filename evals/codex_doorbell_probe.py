"""Recorder for the live Codex doorbell probe (docs/specs/2026-09-12-codex-coordination.md).

The doorbell (``pseudolife_memory/codex_doorbell.py``) shipped 2026-09-23
without a run against a real Codex home, and default-on (2026-09-28) relies on
that path. This records one live run from the two host-side traces it leaves,
so the validation record can say what happened and when, without a body, an
agent id or a path reaching the artifact:

- the digest ledger (``<digest dir>/ledger.log``), where the Codex shim appends
  a ``bell`` line each time it queued a notice with ``codex queue``;
- the board audit log's export (``pseudolife-mcp board-audit export --out
  board.jsonl``), which carries the ``send`` of the clearing message to the
  task, the task's first ``read`` of its mail, its ``ack``, and every other
  event the task caused after the bell (the evidence that it took a turn).

Run it in three steps: ``--watch <seconds>`` while the task is parked and the
message is sent (it prints the moment a bell is queued); export the audit log
once the task has answered; then rerun with ``--export`` to write the record.
Both steps write the same JSON, the second with the timeline filled in.

Only counts, timestamps and version strings leave; the recipient is named by
the first eight hex digits of its id's sha256, never the id itself.

Run with ``python -m evals.codex_doorbell_probe --recipient <agent id>
--out evals/results/codex-doorbell-probe-<date>.json``.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HARNESS = "evals/codex_doorbell_probe.py"
RECORD_VERSION = 1
MAIL_EVENTS = ("send", "read", "ack", "attempt")


def _ledger_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    root = os.environ.get("PSEUDOLIFE_DIGEST_DIR") or str(Path.home() / ".pseudolife-mcp" / "digests")
    return Path(root) / "ledger.log"


def read_bells(ledger: Path, *, since: float) -> list[float]:
    """Times of the ``bell`` lines the shim appended after ``since``."""
    try:
        lines = ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    bells = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == "bell":
            try:
                at = float(parts[0])
            except ValueError:
                continue
            if at >= since:
                bells.append(at)
    return bells


def read_export(path: Path) -> list[dict]:
    from pseudolife_memory.board_audit_cli import _read_export
    rows = []
    for row in _read_export(path):
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        rows.append({**row, "payload": payload if isinstance(payload, dict) else {}})
    return rows


def timeline(rows: list[dict], recipient: str, *, since: float, bells: list[float]) -> dict:
    """The probe's timeline for one recipient: the clearing send, the bell,
    the first event the task caused after it, its first read and its ack.
    Deltas are seconds; a step that never happened is ``null``."""
    sends = sorted(float(r["created_at"]) for r in rows
                   if r["event"] == "send" and r.get("recipient_agent_id") == recipient
                   and float(r["created_at"]) >= since)
    sent_at = sends[0] if sends else None
    bell_at = next((b for b in bells if sent_at is None or b >= sent_at), None)
    own = sorted((float(r["created_at"]), r["event"]) for r in rows
                 if r.get("agent_id") == recipient and r.get("actor") == "agent"
                 and float(r["created_at"]) >= (bell_at or sent_at or since))
    first_turn = own[0] if own else None
    reads = [at for at, event in own if event == "read"]
    acks = [at for at, event in own if event == "ack"]
    read_at = reads[0] if reads else None
    ack_at = acks[0] if acks else None

    def delta(later, earlier):
        return None if later is None or earlier is None else round(later - earlier, 3)

    if sent_at is None:
        outcome = "no send to the recipient in the window"
    elif bell_at is None:
        outcome = "no bell: the shim queued nothing"
    elif first_turn is None:
        outcome = "bell but no turn: the task caused no board event after it"
    elif ack_at is None:
        outcome = "turn but no acknowledgment"
    else:
        outcome = "rung, turn taken, acknowledged"
    return {
        "sent_at": sent_at, "bell_at": bell_at,
        "first_event_after_bell": None if first_turn is None else {
            "at": first_turn[0], "event": first_turn[1]},
        "read_at": read_at, "ack_at": ack_at,
        "send_to_bell_s": delta(bell_at, sent_at),
        "bell_to_first_event_s": delta(None if first_turn is None else first_turn[0], bell_at),
        "bell_to_ack_s": delta(ack_at, bell_at),
        "counts": {"sends": len(sends), "bells": len(bells), "reads": len(reads),
                   "acks": len(acks), "events_by_recipient_after_bell": len(own)},
        "outcome": outcome,
    }


def _versions() -> dict:
    versions = {}
    try:
        versions["pseudolife-mcp"] = importlib.metadata.version("pseudolife-mcp")
    except importlib.metadata.PackageNotFoundError:
        versions["pseudolife-mcp"] = "not installed"
    try:
        from pseudolife_memory.codex_doorbell import resolve_codex_command
        command = resolve_codex_command()
        if command is not None:
            out = subprocess.run([*command, "--version"], capture_output=True, text=True,
                                 timeout=20, stdin=subprocess.DEVNULL)
            versions["codex"] = (out.stdout or out.stderr).strip().splitlines()[0][:80] \
                if (out.stdout or out.stderr).strip() else "unknown"
        else:
            versions["codex"] = "no codex CLI found"
    except Exception as error:  # noqa: BLE001 - the version is a label, never a gate
        versions["codex"] = f"unknown ({type(error).__name__})"
    return versions


def record(*, recipient: str, since: float, bells: list[float], rows: list[dict] | None) -> dict:
    return {
        "harness": HARNESS, "record_version": RECORD_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_start": since,
        "recipient": hashlib.sha256(recipient.encode()).hexdigest()[:8],
        "versions": _versions(),
        "bells": bells,
        "timeline": None if rows is None else timeline(rows, recipient, since=since, bells=bells),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.codex_doorbell_probe",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--recipient", required=True, help="the parked Codex task's board agent id")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ledger", help="digest ledger (default: the digest directory's ledger.log)")
    parser.add_argument("--export", type=Path, help="board-audit export (JSON lines) to build the timeline from")
    parser.add_argument("--since", type=float, default=None,
                        help="window start, epoch seconds (default: now, or the export's earliest send)")
    parser.add_argument("--watch", type=float, default=0.0,
                        help="seconds to wait for a bell line before writing")
    parser.add_argument("--force", action="store_true", help="overwrite an existing record")
    args = parser.parse_args(argv)
    if args.out.exists() and not args.force:
        print(f"{args.out} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    since = args.since if args.since is not None else time.time()
    ledger = _ledger_path(args.ledger)
    bells = read_bells(ledger, since=since)
    if args.watch > 0:
        print(f"watching for a bell for {args.watch:g} s; send the clearing message now",
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + args.watch
        while not bells and time.monotonic() < deadline:
            time.sleep(2)
            bells = read_bells(ledger, since=since)
        print("bell queued" if bells else "no bell within the window", file=sys.stderr)
    rows = None
    if args.export is not None:
        from pseudolife_memory.board_audit_cli import AuditCliError
        try:
            rows = read_export(args.export)
        except AuditCliError as error:
            print(str(error), file=sys.stderr)
            return 2
        if args.since is None:
            sends = [float(r["created_at"]) for r in rows
                     if r["event"] == "send" and r.get("recipient_agent_id") == args.recipient]
            if sends:
                since = min(sends)
                bells = read_bells(ledger, since=since)
    result = record(recipient=args.recipient, since=since, bells=bells, rows=rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "bells": len(bells),
                      "outcome": None if rows is None else result["timeline"]["outcome"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
