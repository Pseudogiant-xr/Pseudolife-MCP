"""Durable instance identities and addressed mail, separate from retrieval.

Mutation paths: register, update, attach, heartbeat, detach, send, acknowledge,
attempt, prune, restore recovery and operator rebind. There is no derived cache.
Callers serialize access to the storage connection with the service lock. SQL
row locks also protect independent connections; send locks both agents in ID
order to avoid reciprocal-send deadlocks. Recovery/rebind are operator-only
entry points: the HTTP/service layer must never expose them as agent tools.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import time
import uuid
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pseudolife_memory.storage.schema import COORDINATION_SCHEMA_SQL


# Conservative initial bounds for the experiment, not measured throughput
# claims. Small queues and one-day bodies bound unattended cost and stale work;
# a longer request-key window makes ordinary offline retries idempotent.
MAX_TEXT_BYTES = 8192
MAX_LABEL = 120
MAX_SCOPE = 120
MAX_PAGE = 50
MAX_PENDING = 256
MESSAGE_TTL = 86400
DEDUPE_RETENTION = 7 * 86400
# An address that has been idle this long, holds no lease and is referenced
# by no retained message is removed by the same prune pass. A shim without a
# state path registers a new address per launch, so without this the peer
# list fills with addresses nobody will ever read again.
AGENT_RETENTION = DEDUPE_RETENTION
# Live delivery attempts per message across attachments. Each attachment
# may attempt a pending message once; past this total the message is left
# for explicit receive so one unacknowledged message cannot wake the host on
# every restart.
MAX_ATTEMPTS = 3
ATTACHMENT_LEASE = 60
SEND_RATE = 60
HLC_META_KEY = "coordination_hlc_highwater"
# Every message a recipient reads is agent-origin collaboration, never the
# operator's authority; the label rides on the row so no consumer infers it.
MESSAGE_ORIGIN = "agent"




class CoordinationError(ValueError):
    """Stable public code; never includes supplied credentials or bodies."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _string(value: Any, limit: int, field: str, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise CoordinationError(f"invalid_{field}")
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise CoordinationError(f"invalid_{field}")
    return value


def _hash(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


class CoordinationStore:
    def __init__(self, storage, *, clock=time.time):
        self.storage = storage
        self.clock = clock

    def _one(self, sql, params=()):
        with self.storage.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql, params=()):
        with self.storage.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _auth(self, principal, agent_id, credential, *, lock=False):
        if not isinstance(credential, str) or not credential or len(credential) > 256:
            raise CoordinationError("invalid_credential")
        try:
            credential_hash = _hash(credential)
        except UnicodeError:
            raise CoordinationError("invalid_credential") from None
        row = self._one("SELECT * FROM coordination_agents WHERE agent_id=%s" +
                        (" FOR UPDATE" if lock else ""), (agent_id,))
        if row is None:
            raise CoordinationError("instance_not_found")
        if (row["principal"] != principal or not row["credential_hash"]
                or not hmac.compare_digest(row["credential_hash"], credential_hash)):
            raise CoordinationError("invalid_credential")
        return row

    def _public(self, row):
        keys = ("agent_id", "principal", "label", "project", "task", "status",
                "episode", "capabilities", "wake_enabled", "created_at",
                "last_activity", "lifecycle")
        result = {k: row[k] for k in keys}
        result["adapter_available"] = bool(row["attachment_id"] and
                                           (row["lease_until"] or 0) > self.clock())
        return result

    def register(self, principal, *, label="", project="", task="", episode="", status="",
                 capabilities=None, wake_enabled=False):
        _string(principal, 256, "principal", empty=False)
        fields = self._fields(label=label, project=project, task=task, episode=episode, status=status,
                              capabilities={} if capabilities is None else capabilities,
                              wake_enabled=wake_enabled)
        agent_id, credential = uuid.uuid4().hex, secrets.token_urlsafe(32)
        now = self.clock()
        with self.storage._txn():
            self.storage.conn.execute(
                "INSERT INTO coordination_agents (agent_id,principal,credential_hash,"
                "label,project,task,episode,status,capabilities,wake_enabled,created_at,last_activity) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (agent_id, principal, _hash(credential), fields["label"], fields["project"],
                 fields["task"], fields["episode"], fields["status"], Jsonb(fields["capabilities"]),
                 fields["wake_enabled"], now, now))
            out = self.authenticate(principal, agent_id, credential)
        return {**out, "credential": credential}

    def authenticate(self, principal, agent_id, credential):
        return self._public(self._auth(principal, agent_id, credential))

    @staticmethod
    def _fields(**fields):
        limits = {"label": MAX_LABEL, "project": MAX_SCOPE, "task": MAX_SCOPE,
                  "status": 240, "episode": 120}
        for key, value in fields.items():
            if key in limits:
                _string(value, limits[key], key)
            elif key == "wake_enabled":
                if not isinstance(value, bool):
                    raise CoordinationError("invalid_wake_enabled")
            elif key == "capabilities":
                if not isinstance(value, dict) or len(value) > 8:
                    raise CoordinationError("invalid_capabilities")
                for item, enabled in value.items():
                    _string(item, 40, "capabilities", empty=False)
                    if not isinstance(enabled, bool):
                        raise CoordinationError("invalid_capabilities")
            else:
                raise CoordinationError("invalid_update")
        return fields

    def update(self, principal, agent_id, credential, **fields):
        fields = self._fields(**fields)
        with self.storage._txn():
            self._auth(principal, agent_id, credential, lock=True)
            assignments = [f"{key}=%s" for key in fields]
            values = [Jsonb(v) if k == "capabilities" else v for k, v in fields.items()]
            self.storage.conn.execute(
                "UPDATE coordination_agents SET " + ",".join(assignments + ["last_activity=%s"])
                + " WHERE agent_id=%s", (*values, self.clock(), agent_id))
            return self.authenticate(principal, agent_id, credential)

    def list_agents(self, principal, agent_id, credential, *, project=None, task=None, limit=50):
        self._auth(principal, agent_id, credential)
        self._limit(limit)
        clauses, values = ["agent_id<>%s", "credential_hash IS NOT NULL"], [agent_id]
        for key, value in (("project", project), ("task", task)):
            if value is not None:
                _string(value, MAX_SCOPE, key)
                clauses.append(f"{key}=%s")
                values.append(value)
        # Reachable adapters first: a burst of idle addresses must not push
        # the peers that can actually receive live mail off a bounded page.
        rows = self._all("SELECT * FROM coordination_agents WHERE " + " AND ".join(clauses)
                         + " ORDER BY (attachment_id IS NOT NULL AND coalesce(lease_until,0)>%s) DESC,"
                         "last_activity DESC,agent_id LIMIT %s", (*values, self.clock(), limit))
        return {"agents": [self._public(row) for row in rows]}

    def _pending_count(self, agent_id):
        return self._one("SELECT count(*) AS n FROM coordination_messages WHERE recipient_agent_id=%s "
                         "AND acknowledged_at IS NULL AND expires_at>%s", (agent_id, self.clock()))["n"]

    def attach(self, principal, agent_id, credential, *, attachment_id, wake_enabled=False):
        _string(attachment_id, 120, "attachment_id", empty=False)
        self._fields(wake_enabled=wake_enabled)
        with self.storage._txn():
            row = self._auth(principal, agent_id, credential, lock=True)
            now = self.clock()
            alive = row["attachment_id"] is not None and (row["lease_until"] or 0) > now
            if alive and row["attachment_id"] != attachment_id:
                raise CoordinationError("attachment_busy")
            generation = row["generation"] if alive else row["generation"] + 1
            self.storage.conn.execute(
                "UPDATE coordination_agents SET attachment_id=%s,generation=%s,lease_until=%s,"
                "last_activity=%s,wake_enabled=%s,lifecycle='attached' WHERE agent_id=%s",
                (attachment_id, generation, now + ATTACHMENT_LEASE, now, wake_enabled, agent_id))
            pending = self._pending_count(agent_id)
        return {"agent_id": agent_id, "generation": generation, "lease_until": now + ATTACHMENT_LEASE,
                "pending_count": pending}

    def _attachment(self, row, attachment_id, generation):
        if (row["attachment_id"] != attachment_id or row["generation"] != generation
                or not row["attachment_id"] or (row["lease_until"] or 0) <= self.clock()):
            raise CoordinationError("stale_attachment")

    def check_attachment(self, principal, agent_id, credential, *, attachment_id, generation):
        row = self._auth(principal, agent_id, credential)
        self._attachment(row, attachment_id, generation)
        return {"agent_id": agent_id, "generation": generation,
                "lease_until": row["lease_until"], "wake_enabled": row["wake_enabled"]}

    def heartbeat(self, principal, agent_id, credential, *, attachment_id, generation):
        with self.storage._txn():
            row = self._auth(principal, agent_id, credential, lock=True)
            self._attachment(row, attachment_id, generation)
            until = self.clock() + ATTACHMENT_LEASE
            self.storage.conn.execute(
                "UPDATE coordination_agents SET lease_until=%s,last_activity=%s WHERE agent_id=%s",
                (until, self.clock(), agent_id))
            pending = self._pending_count(agent_id)
        return {"agent_id": agent_id, "generation": generation, "lease_until": until,
                "pending_count": pending}

    def detach(self, principal, agent_id, credential, *, attachment_id, generation):
        with self.storage._txn():
            row = self._auth(principal, agent_id, credential, lock=True)
            self._attachment(row, attachment_id, generation)
            self.storage.conn.execute(
                "UPDATE coordination_agents SET attachment_id=NULL,lease_until=NULL,"
                "generation=generation+1,lifecycle='detached' WHERE agent_id=%s", (agent_id,))
        return {"detached": True}

    def _receipt(self, row):
        state = ("acknowledged" if row["acknowledged_at"] is not None else
                 "expired" if row["expires_at"] <= self.clock() else
                 "attempted" if row["attempt_at"] is not None else "queued")
        return {"message_id": row["message_id"], "state": state,
                "created_at": row["created_at"], "expires_at": row["expires_at"],
                "acknowledged_at": row["acknowledged_at"]}

    def send(self, principal, agent_id, credential, *, to, text, request_id,
             reply_to=None, expires_at=None, hlc=""):
        _string(to, 120, "recipient", empty=False)
        _string(request_id, 120, "request_id", empty=False)
        _string(hlc, 120, "hlc")
        stamp = None
        if hlc:
            try:
                stamp = [int(part) for part in hlc.split(":")]
                if len(stamp) != 2 or any(part < 0 or part > 2**63 - 1 for part in stamp):
                    raise ValueError
            except ValueError:
                raise CoordinationError("invalid_hlc") from None
        try:
            valid_text = (isinstance(text, str) and bool(text.strip()) and "\x00" not in text
                          and len(text.encode("utf-8")) <= MAX_TEXT_BYTES)
        except UnicodeEncodeError:
            valid_text = False
        if not valid_text:
            raise CoordinationError("invalid_text")
        if reply_to is not None:
            _string(reply_to, 120, "reply", empty=False)
        if expires_at is not None and (isinstance(expires_at, bool) or
                not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at)):
            raise CoordinationError("invalid_expiry")
        fingerprint = _hash(json.dumps([to, text, reply_to, expires_at], ensure_ascii=False,
                                       separators=(",", ":")))
        with self.storage._txn():
            # Lock in a global order, including sender rate/idempotency state.
            self._all("SELECT agent_id FROM coordination_agents WHERE agent_id=ANY(%s) "
                      "ORDER BY agent_id FOR UPDATE", (sorted({agent_id, to}),))
            sender = self._auth(principal, agent_id, credential)
            existing = self._one("SELECT * FROM coordination_messages WHERE sender_agent_id=%s "
                                 "AND request_id=%s", (agent_id, request_id))
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise CoordinationError("request_conflict")
                return self._receipt(existing)
            recipient = self._one("SELECT * FROM coordination_agents WHERE agent_id=%s "
                                  "AND credential_hash IS NOT NULL", (to,))
            if recipient is None:
                raise CoordinationError("recipient_not_found")
            now = self.clock()
            expiry = now + MESSAGE_TTL if expires_at is None else float(expires_at)
            if not now < expiry <= now + MESSAGE_TTL:
                raise CoordinationError("invalid_expiry")
            if reply_to is not None:
                parent = self._one("SELECT sender_agent_id,recipient_agent_id FROM "
                                   "coordination_messages WHERE message_id=%s", (reply_to,))
                if parent is None or parent["sender_agent_id"] != to or parent["recipient_agent_id"] != agent_id:
                    raise CoordinationError("invalid_reply")
            if self._one("SELECT count(*) AS n FROM coordination_messages WHERE sender_agent_id=%s "
                         "AND created_at>%s", (agent_id, now - 60))["n"] >= SEND_RATE:
                raise CoordinationError("rate_limited")
            if self._one("SELECT count(*) AS n FROM coordination_messages WHERE recipient_agent_id=%s "
                         "AND acknowledged_at IS NULL AND expires_at>%s", (to, now))["n"] >= MAX_PENDING:
                raise CoordinationError("queue_full")
            seq = recipient["next_sequence"] + 1
            message_id = uuid.uuid4().hex
            self.storage.conn.execute("UPDATE coordination_agents SET next_sequence=%s WHERE agent_id=%s", (seq, to))
            self.storage.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s", (now, agent_id))
            row = self._one(
                "INSERT INTO coordination_messages (message_id,sender_agent_id,recipient_agent_id,"
                "sender_principal,project,task,text,reply_to,request_id,fingerprint,recipient_sequence,"
                "hlc,created_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (message_id, agent_id, to, principal, sender["project"], sender["task"], text,
                 reply_to, request_id, fingerprint, seq, hlc, now, expiry))
            if stamp is not None:
                # Retain clock history after message pruning. Numeric comparison
                # matters across digit boundaries and concurrent connections.
                self.storage.conn.execute(
                    "INSERT INTO meta (key,value) VALUES (%s,%s) ON CONFLICT (key) "
                    "DO UPDATE SET value=EXCLUDED.value WHERE "
                    "((meta.value->>0)::bigint,(meta.value->>1)::bigint) < "
                    "((EXCLUDED.value->>0)::bigint,(EXCLUDED.value->>1)::bigint)",
                    (HLC_META_KEY, Jsonb(stamp)))
        return self._receipt(row)

    @staticmethod
    def _limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            raise CoordinationError("invalid_limit")

    def receive(self, principal, agent_id, credential, *, after=None, limit=50,
                for_delivery=False):
        """Page pending mail. ``for_delivery`` is the adapter's live path: it
        skips messages whose attempts are exhausted, which an explicit receive
        still returns."""
        row = self._auth(principal, agent_id, credential)
        self._limit(limit)
        seq = 0
        if after is not None:
            try:
                mailbox, raw = after.split(":", 1)
                seq = int(raw)
                if mailbox != agent_id or not 0 <= seq <= row["next_sequence"]:
                    raise ValueError
            except (AttributeError, ValueError):
                raise CoordinationError("invalid_cursor") from None
        attempt_clause = " AND attempts<%s" if for_delivery else ""
        params = [agent_id, seq, self.clock()] + ([MAX_ATTEMPTS] if for_delivery else []) + [limit]
        rows = self._all("SELECT * FROM coordination_messages WHERE recipient_agent_id=%s "
                         "AND recipient_sequence>%s AND acknowledged_at IS NULL AND expires_at>%s"
                         + attempt_clause + " ORDER BY recipient_sequence LIMIT %s", params)
        keys = ("message_id", "sender_agent_id", "sender_principal", "recipient_agent_id",
                "project", "task", "text", "reply_to", "recipient_sequence", "hlc", "created_at", "expires_at")
        return {"messages": [{**{k: r[k] for k in keys}, "origin": MESSAGE_ORIGIN} for r in rows],
                "after": f"{agent_id}:{rows[-1]['recipient_sequence'] if rows else seq}"}

    def ack(self, principal, agent_id, credential, *, message_id):
        with self.storage._txn():
            self._auth(principal, agent_id, credential, lock=True)
            # An acknowledgment is activity for retention: a client that only
            # reads and acknowledges, holding no lease, must not count as idle.
            self.storage.conn.execute("UPDATE coordination_agents SET last_activity=%s "
                                      "WHERE agent_id=%s", (self.clock(), agent_id))
            row = self._one("SELECT * FROM coordination_messages WHERE message_id=%s "
                            "AND recipient_agent_id=%s FOR UPDATE", (message_id, agent_id))
            if row is None:
                raise CoordinationError("message_not_found")
            if row["acknowledged_at"] is None:
                row = self._one("UPDATE coordination_messages SET acknowledged_at=%s "
                                "WHERE message_id=%s RETURNING *", (self.clock(), message_id))
            return self._receipt(row)

    def mark_attempt(self, principal, agent_id, credential, *, message_id, attachment_id, generation):
        with self.storage._txn():
            agent = self._auth(principal, agent_id, credential, lock=True)
            self._attachment(agent, attachment_id, generation)
            if not agent["wake_enabled"]:
                raise CoordinationError("wake_disabled")
            row = self._one("SELECT * FROM coordination_messages WHERE message_id=%s "
                            "AND recipient_agent_id=%s AND acknowledged_at IS NULL AND expires_at>%s "
                            "FOR UPDATE", (message_id, agent_id, self.clock()))
            if row is None:
                raise CoordinationError("message_not_pending")
            if row["attempt_generation"] != generation:
                if row["attempts"] >= MAX_ATTEMPTS:
                    raise CoordinationError("attempts_exhausted")
                row = self._one("UPDATE coordination_messages SET attempt_at=%s,attempt_generation=%s,"
                                "attempts=attempts+1 WHERE message_id=%s RETURNING *",
                                (self.clock(), generation, message_id))
        return self._receipt(row)

    def prune(self):
        """Expire bodies, discard terminal retry metadata after seven days, and
        remove addresses that are idle, unleased and referenced by no retained
        message (the message rows go first, so a referenced address outlives
        its mail by the retention window)."""
        now = self.clock()
        with self.storage._txn():
            bodies = self.storage.conn.execute("UPDATE coordination_messages SET text=NULL "
                "WHERE expires_at<=%s AND text IS NOT NULL", (now,)).rowcount
            removed = self.storage.conn.execute("DELETE FROM coordination_messages WHERE created_at<=%s "
                "AND expires_at<=%s", (now - DEDUPE_RETENTION, now)).rowcount
            agents = self.storage.conn.execute(
                "DELETE FROM coordination_agents a WHERE (a.lease_until IS NULL OR a.lease_until<=%s) "
                "AND a.last_activity<=%s AND NOT EXISTS (SELECT 1 FROM coordination_messages m "
                "WHERE m.sender_agent_id=a.agent_id OR m.recipient_agent_id=a.agent_id)",
                (now, now - AGENT_RETENTION)).rowcount
        return {"bodies_expired": bodies, "removed": removed, "agents_removed": agents}

    def recover(self):
        """Operator-only restore reset; never call on an ordinary restart."""
        with self.storage._txn():
            count = self.storage.conn.execute("UPDATE coordination_agents SET credential_hash=NULL,"
                "wake_enabled=FALSE,attachment_id=NULL,lease_until=NULL,generation=generation+1,"
                "lifecycle='revoked'").rowcount
        return {"revoked": count}

    def rebind(self, agent_id, principal):
        """Operator-only reissue to the same owner, retaining pending mail."""
        credential = secrets.token_urlsafe(32)
        with self.storage._txn():
            row = self._one("SELECT * FROM coordination_agents WHERE agent_id=%s FOR UPDATE", (agent_id,))
            if row is None or row["principal"] != principal or row["credential_hash"] is not None:
                raise CoordinationError("invalid_rebind")
            self.storage.conn.execute("UPDATE coordination_agents SET credential_hash=%s,"
                                      "lifecycle='registered' WHERE agent_id=%s", (_hash(credential), agent_id))
            result = self.authenticate(principal, agent_id, credential)
        return {**result, "credential": credential}
