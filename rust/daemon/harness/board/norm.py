"""Rule-based normalizers for the board harness (spec "Free" list).

Random values are replaced by ordinals assigned in first-appearance order
(responses walked in step order, object keys sorted, so key order never
matters); wall-clock values by ``T<step>+<offset>`` inside each arm's own
captured window; HLC stamps by ``H<step>:<logical>``. Every rule checks the
shape of what it replaces, so a malformed value shows as a diff.
"""
from __future__ import annotations

import json
import re

HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
HLC = re.compile(r"(\d{13}):(\d+)")
PREFIX = re.compile(r"\b[0-9a-f]{8,31}\b")
HISTORY_CURSOR = re.compile(r"([0-9a-f]{32}):history:(\d+)")

# Keys whose values are wall-clock times, with the offsets (seconds after
# the step's own clock reading) the producer adds. ``None`` offsets come
# from the scenario (a ttl, an expected duration, a park expiry it chose).
TIME_KEYS = {
    "created_at": (0,), "last_activity": (0,), "acquired_at": (0,), "freed_at": (0,),
    "enqueued_at": (0,), "served_at": (0,), "name_set_at": (0,), "park_set_at": (0,),
    "first_read_at": (0,), "acknowledged_at": (0,), "attempt_at": (0,), "since": (0,),
    "status_set_at": (0,), "snapshot_at": (0,), "redacted_at": (0,), "repudiated_at": (0,),
    "ring_at": (0, 30, 60, 90, 120, 150, 180, 210, 240),
    "lease_until": (60,), "ring_armed_until": (60, 0),
    "expires_at": (86400, 30, 60, 120, 300, 600, 3600, 86400),
    "message_expires_at": (86400,),
    "status_expires_at": (60, 120, 600, 3600), "expected_end": (60, 120, 600, 3600),
    "park_expires": (43200, 600, 3600, 7200),
}
# Keys that are random identifiers or digests (replaced, shape-checked).
DIGEST_KEYS = {"credential_hash", "hash", "prev_hash", "fingerprint", "text_commitment",
               "proof", "through_hash"}
SALT_KEYS = {"body_salt"}
# Lists both producers sort by raw random ids; compared as sets.
UNORDERED_KEYS = {"participants", "agent_ids", "message_ids"}
# Lists of objects both producers sort by a raw random id field.
SORTED_BY = {"receipts": "recipient_agent_id"}


class Windows:
    """One arm's step windows: [(t0, t1)] in wall-clock seconds."""

    def __init__(self):
        self.steps: list[tuple[float, float]] = []
        self.shifts: list[tuple[int, float]] = []   # (after step, seconds aged)

    def match(self, value: float, offsets) -> str | None:
        for c in offsets:
            for k in range(len(self.steps) - 1, -1, -1):
                t0, t1 = self.steps[k]
                aged = sum(s for after, s in self.shifts if after >= k)
                for shift in (0.0, aged) if aged else (0.0,):
                    base = value - c + shift
                    if t0 - 0.003 <= base <= t1 + 0.003:
                        return f"T{k}+{c}" + ("~aged" if shift else "")
        return None

    def hlc(self, phys: int) -> str | None:
        for k in range(len(self.steps) - 1, -1, -1):
            t0, t1 = self.steps[k]
            if t0 * 1000 - 3 <= phys <= t1 * 1000 + 3:
                return f"H{k}"
        return None


class Normalizer:
    def __init__(self, windows: Windows):
        self.windows = windows
        self.ids: dict[str, int] = {}
        # Audit seqs relabelled inside a fan-out burst: both producers write
        # a burst's send events in raw recipient-id order, which is free.
        self.seq_map: dict[int, int] = {}
        self.problems: list[str] = []

    # -- id registry ------------------------------------------------------
    def learn(self, value) -> None:
        """Register every 32-hex id in ``value`` in a key-order-free walk."""
        if isinstance(value, dict):
            for k in sorted(value):
                if k in DIGEST_KEYS or k in SALT_KEYS:
                    continue
                if k in SORTED_BY and isinstance(value[k], list):
                    self.learn_list(k, value[k])
                    continue
                self.learn(value[k])
        elif isinstance(value, list):
            for v in value:
                self.learn(v)

        elif isinstance(value, str):
            for m in HEX32.finditer(value):
                self.ids.setdefault(m.group(0), len(self.ids))

    def learn_list(self, key, value):
        """A list sorted by a raw id: learn its members in the order of
        that id's ordinal, so new ids pair with the same recipient."""
        field = SORTED_BY[key]
        known = lambda v: self.ids.get(v.get(field), 1 << 30) if isinstance(v, dict) else 0  # noqa: E731
        for v in sorted(value, key=known):
            self.learn(v)

    def refresh_seqs(self, sends: list[dict]) -> None:
        """``sends``: every send event (seq, agent_id, recipient_agent_id,
        created_at, request_id) in seq order."""
        groups: list[list[dict]] = []
        for r in sends:
            key = (r["agent_id"], r["created_at"], r["request_id"])
            if groups and groups[-1][0]["_key"] == key and groups[-1][-1]["seq"] + 1 == r["seq"]:
                groups[-1].append({**r, "_key": key})
            else:
                groups.append([{**r, "_key": key}])
        self.seq_map = {}
        for g in groups:
            if len(g) < 2:
                continue
            raw = [r["recipient_agent_id"] for r in g]
            if raw != sorted(raw):
                self.problems.append(f"burst at seq {g[0]['seq']} not in raw recipient order")
            by_ordinal = sorted(g, key=lambda r: self.ids.get(r["recipient_agent_id"], 1 << 30))
            for new, r in zip(sorted(x["seq"] for x in g), by_ordinal):
                self.seq_map[r["seq"]] = new

    def _id_text(self, s: str) -> str:
        def full(m):
            n = self.ids.get(m.group(0))
            return f"<id{n}>" if n is not None else "<id?>"
        out = HEX32.sub(full, s)

        def prefix(m):
            p = m.group(0)
            hits = [n for i, n in self.ids.items() if i.startswith(p)]
            return f"<id{hits[0]}:{len(p)}>" if len(hits) == 1 else m.group(0)
        return PREFIX.sub(prefix, out)

    # -- values -----------------------------------------------------------
    def value(self, v, key: str | None = None):
        if isinstance(v, dict):
            return {k: self.value(v[k], k) for k in v}
        if isinstance(v, list):
            if key in UNORDERED_KEYS and v != sorted(v, key=str):
                return f"<{key} not sorted by raw value>"
            if key in SORTED_BY:
                field = SORTED_BY[key]
                raw = [x.get(field) for x in v if isinstance(x, dict)]
                if raw != sorted(raw, key=str):
                    return f"<{key} not sorted by {field}>"
                return sorted((self.value(x, key) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
            out = [self.value(x, key) for x in v]
            # Sorted by raw random id on both arms: its order is free.
            return sorted(out, key=json.dumps) if key in UNORDERED_KEYS else out
        if key in DIGEST_KEYS and isinstance(v, str):
            return "<sha256>" if HEX64.fullmatch(v) else f"<bad digest {v[:12]}>"
        if key in SALT_KEYS and isinstance(v, str):
            return "<salt>" if HEX32.fullmatch(v) else f"<bad salt {v[:12]}>"
        if key == "credential" and isinstance(v, str):
            return "<credential>" if TOKEN.fullmatch(v) else f"<bad credential {len(v)}>"
        if key == "bank_id" and isinstance(v, str):
            return "<bank>" if UUID.fullmatch(v) else f"<bad bank {v}>"
        if key in TIME_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e9:
            return self.windows.match(float(v), TIME_KEYS[key]) or f"<unmatched {key}>"
        if key in ("cutoff", "retained_since") and isinstance(v, int):
            return "<utc-day>" if v % 86400 == 0 else f"<bad day {v}>"
        if key == "hlc" and isinstance(v, str) and v:
            m = HLC.fullmatch(v)
            if not m:
                return f"<bad hlc {v}>"
            step = self.windows.hlc(int(m.group(1)))
            return f"{step}:{m.group(2)}" if step else "<unmatched hlc>"
        if key in ("seq", "through_seq") and isinstance(v, int) and not isinstance(v, bool):
            return self.seq_map.get(v, v)
        if isinstance(v, str):
            m = HISTORY_CURSOR.fullmatch(v)
            if m:
                v = f"{m.group(1)}:history:{self.seq_map.get(int(m.group(2)), int(m.group(2)))}"
            return self._id_text(v)
        return v

    def payload(self, text: str):
        """An audit payload: parsed, then normalized like any value."""
        try:
            return self.value(json.loads(text))
        except ValueError:
            return f"<bad payload {text[:40]}>"
