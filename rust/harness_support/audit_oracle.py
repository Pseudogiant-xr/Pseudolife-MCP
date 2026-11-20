"""Audit comparison rules frozen from the Python baseline at 1bf44c714.

These helpers check harness observations and prepare deliberate archive edits.
They are independent of the Rust implementation being compared.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


AUDIT_FORMAT = "pseudolife-coordination-audit-v1"
GENESIS_HASH = "0" * 64
_SALT = re.compile(r"[0-9a-f]{32}")


def body_commitment(salt_hex, body):
    """sha256(salt || body), the value a v46 send's hashed payload holds;
    None when the salt or body is not the shape one is written in."""
    if (not isinstance(salt_hex, str) or _SALT.fullmatch(salt_hex) is None
            or not isinstance(body, str)):
        return None
    try:
        return hashlib.sha256(bytes.fromhex(salt_hex) + body.encode("utf-8")).hexdigest()
    except (ValueError, UnicodeEncodeError):
        return None


def canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_hash(prev_hash: str, row) -> str:
    """sha256(prev_hash || canonical row), over every column but the two
    hashes and ``body``. ``payload`` is hashed as the stored text; a parsed
    payload (a JSON-lines export read back) is re-canonicalized to that same
    text. A v46 send event's payload commits to its body by sha256, which
    ``verify_audit_chain`` checks separately."""
    payload = row["payload"]
    if not isinstance(payload, str):
        payload = canonical_payload(payload)
    material = json.dumps(
        [AUDIT_FORMAT, row["seq"], row["event"], row["actor"], row["principal"],
         row["agent_id"], row["recipient_agent_id"], row["project"], row["task"],
         row["message_id"], float(row["created_at"]), row["hlc"], payload],
        ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev_hash + material).encode("utf-8")).hexdigest()


def _broken(seq, reason, **detail):
    return {"ok": False, "seq": seq, "reason": reason, **detail}


def audit_cutoff(now: float, retention_days: int) -> int:
    """The newest time retention removes: the UTC day boundary at or before
    ``now - retention_days``. Cutting on day boundaries makes at most one
    cut, and one ``audit_prune`` row, a day. A cutoff that moved with every
    once-a-minute prune pass cut again on almost every pass, and each cut's
    own record aged out a window later and fed the next one (review of
    15f31aec, 2026-09-24: 30 cutting passes a day from 31 events)."""
    return math.floor((now - retention_days * 86400) / 86400) * 86400


def _cut(row):
    """The fields of an ``audit_prune`` row, or None when they are not shaped
    like the ones prune writes."""
    payload = row["payload"]
    try:
        cut = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        return None
    if not isinstance(cut, dict):
        return None
    through_seq, through_hash = cut.get("through_seq"), cut.get("through_hash")
    cutoff, days = cut.get("cutoff"), cut.get("retention_days")
    created_at = row["created_at"]
    # No clock or config produces a non-finite time or a window beyond a
    # century; refusing them here keeps audit_cutoff from overflowing.
    if (type(through_seq) is not int or not isinstance(through_hash, str)
            or type(cutoff) not in (int, float) or type(days) is not int
            or not 0 <= days <= 36500
            or type(created_at) not in (int, float) or not math.isfinite(created_at)):
        return None
    return {"seq": row["seq"], "created_at": float(created_at), "cutoff": cutoff,
            "retention_days": days, "through_seq": through_seq,
            "through_hash": through_hash, "actor": row["actor"]}


def _parsed_payload(row):
    """The row's payload as a dict, or None when it is not a JSON object."""
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def _body_matches(body, salt, payload) -> bool:
    """Whether a present body and its salt open the commitment a v46 send
    payload holds."""
    if not isinstance(body, str) or payload is None:
        return False
    commitment = payload.get("text_commitment")
    if not isinstance(commitment, str):
        return False
    return body_commitment(salt, body) == commitment


def verify_audit_chain(rows, *, expect_head=None) -> dict:
    """Walk rows in ``seq`` order and report the first break.

    A row fails as ``sequence_gap`` (a missing seq), ``broken_link`` (its
    prev_hash is not the previous row's hash) or ``hash_mismatch`` (its
    content changed). Bodies are outside the hash (schema v46), so they are
    checked against what the hashed payload commits to: a present ``body``
    must be on a send event whose payload carries ``text_commitment``, and
    with its ``body_salt`` open it (``body_mismatch``,
    also for a salt left without its body; a body on any other row is
    content the chain never vouched for, and one on a send that a later
    operator ``redact`` event names was written back after its redaction,
    since redaction blanks it in the same transaction). A v46 send event
    without its body must be named by a later ``redact`` event, written by
    the operator, whose payload gives that event's ``seq`` and
    ``message_id`` and says the body was removed (``audit_copy:
    removed``); otherwise it fails as ``body_missing``, reported once the
    walk ends, since the redact comes after it, or as ``body_not_exported``
    when the row has no ``body`` field at all (an export written by a
    pre-v46 CLI). Send events from before v46 keep ``text`` inside the
    hashed payload and carry no body; redacting one takes only the live copy
    and says so (``audit_copy: kept``). A log whose oldest rows were removed must start right
    after a cut recorded later in the same chain (an ``audit_prune`` naming
    the last removed row) whose own fields add up: written by the daemon, a
    window of at least a day, the cutoff that window gives at its time, and
    a first surviving row no older than that cutoff. Otherwise the start is
    ``unanchored_start``. ``expect_head=(seq, hash)``, a head recorded
    elsewhere earlier, fails as ``head_missing``, ``head_mismatch``, or
    ``head_pruned`` when the log now starts after it.

    What this cannot see, with no secret involved: the newest rows dropped,
    a rewrite that recomputes every hash, the oldest rows removed by
    someone who also appends a consistent cut record, or a body removed by
    someone who also appends a consistent redact record (that record, with
    its reason, stays in the log). An expected head catches the first two,
    not the last two. The report names the cut the log starts from
    (``start_cut``) so an operator can check it against the retention
    window they configured.

    Returns ``{ok: True, events, first_seq, head_seq, head_hash,
    head_created_at, start_cut}``
    or ``{ok: False, seq, reason}`` (plus ``start_cut`` on ``head_pruned``).
    """
    first = prev = None
    count = 0
    cuts = {}
    expected = None
    absent = {}                     # seq -> (message_id, body field present) of a bodiless v46 send
    kept = set()                                # seqs of v46 sends that keep their body
    for row in rows:
        seq = row["seq"]
        if prev is None:
            first = row
            if seq == 1 and row["prev_hash"] != GENESIS_HASH:
                return _broken(seq, "broken_link")
        elif seq != prev["seq"] + 1:
            return _broken(seq, "sequence_gap")
        elif row["prev_hash"] != prev["hash"]:
            return _broken(seq, "broken_link")
        if audit_hash(row["prev_hash"], row) != row["hash"]:
            return _broken(seq, "hash_mismatch")
        event, body, salt = row["event"], row.get("body"), row.get("body_salt")
        payload = (_parsed_payload(row)
                   if body is not None or event in ("send", "redact") else None)
        if body is None and salt is not None:
            # A redaction removes both; a salt left behind would make the
            # removed body guessable against its commitment again.
            return _broken(seq, "body_mismatch")
        if body is not None:
            if not _body_matches(body, salt, payload if event == "send" else None):
                return _broken(seq, "body_mismatch")
            kept.add(seq)
        elif event == "send" and payload is not None and "text_commitment" in payload:
            absent[seq] = (row["message_id"], "body" in row)
        if event == "redact" and row["actor"] == "operator" and payload is not None:
            named, message_id = payload.get("seq"), payload.get("message_id")
            if type(named) is int:
                if named in kept:
                    # Redaction blanks the body in the redact's own
                    # transaction, so a body still there was written back.
                    return _broken(named, "body_mismatch")
                if (payload.get("audit_copy") == "removed" and isinstance(message_id, str)
                        and named in absent and absent[named][0] == message_id):
                    del absent[named]
        if event == "audit_prune" and (cut := _cut(row)) is not None:
            cuts[(cut["through_seq"], cut["through_hash"])] = cut
        if expect_head is not None and seq == expect_head[0]:
            expected = row["hash"]
        prev = row
        count += 1
    start_cut = None
    if first is not None and first["seq"] != 1:
        cut = cuts.get((first["seq"] - 1, first["prev_hash"]))
        if (cut is None or cut["actor"] != "daemon" or cut["retention_days"] < 1
                or cut["cutoff"] != audit_cutoff(cut["created_at"], cut["retention_days"])
                or float(first["created_at"]) < cut["cutoff"]):
            return _broken(first["seq"], "unanchored_start")
        start_cut = {key: cut[key] for key in
                     ("seq", "created_at", "cutoff", "retention_days", "through_seq")}
    if absent:
        missing = min(absent)
        return _broken(missing, "body_missing" if absent[missing][1] else "body_not_exported")
    if expect_head is not None:
        seq, digest = expect_head
        if expected is None:
            if first is not None and seq < first["seq"]:
                return _broken(seq, "head_pruned", start_cut=start_cut)
            return _broken(seq, "head_missing")
        if expected != digest:
            return _broken(seq, "head_mismatch")
    return {"ok": True, "events": count,
            "first_seq": first["seq"] if first else None,
            "head_seq": prev["seq"] if prev else None,
            "head_hash": prev["hash"] if prev else None,
            "head_created_at": float(prev["created_at"]) if prev else None,
            "start_cut": start_cut}
