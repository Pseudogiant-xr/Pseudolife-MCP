"""Replay board rows captured at 88787b0e2 on disposable PostgreSQL.

The capture fixed the clock and used synthetic IDs and salts. Replay
rebinds those fields and the audit and lease sequence positions to the
running scenario's bank. The paging fixture repeats captured row shapes.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import secrets
import uuid

from psycopg.rows import dict_row

from rust.harness_support.audit_oracle import (
    GENESIS_HASH, audit_hash, body_commitment, canonical_payload,
)
from rust.harness_support.fixture_rows import checked_fixture_insert


@lru_cache(maxsize=1)
def _captured():
    folder = Path(__file__).with_name("fixtures")
    captured = {}
    for file in ("seed_rows.json", "host_seed_rows.json"):
        captured.update(json.loads((folder / file).read_text(encoding="utf-8")))
    return captured


def _bind(value, bindings):
    if isinstance(value, str) and value.startswith("$"):
        return bindings[value[1:]]
    if isinstance(value, dict):
        return {key: _bind(field, bindings) for key, field in value.items()}
    return value


class BoardRows:
    """Bind run-local IDs, clocks and sequence positions to concrete rows."""

    def __init__(self, conn, now):
        self.conn = conn
        self.now = now
        self.events = []

    def row(self, fixture, **bindings):
        return _bind(_captured()[fixture], {"now": self.now, **bindings})

    def insert(self, table, rows):
        self.conn.execute(*checked_fixture_insert(self.conn, table, rows))

    def one(self, statement, params):
        with self.conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement, params)
            [row] = cursor.fetchall()
        return row

    def next(self, sequence):
        return self.conn.execute("SELECT nextval(%s::regclass)", ("public." + sequence,)).fetchone()[0]

    def event(self, fixture, **bindings):
        self.events.append((fixture, bindings))

    def append_events(self):
        if not self.events:
            return
        # Use the same chain lock as the daemon, acquired after the board
        # changes, so a concurrent append cannot fork a seed's chain.
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                          ("coordination-audit-chain",))
        head = self.conn.execute("SELECT seq, hash FROM coordination_events ORDER BY seq DESC LIMIT 1")
        seq, previous = head.fetchone() or (0, GENESIS_HASH)
        rows = []
        for fixture, bindings in self.events:
            seq += 1
            row = self.row(fixture, seq=seq, prev_hash=previous, **bindings)
            row["payload"] = canonical_payload(row["payload"])
            row["hash"] = previous = audit_hash(previous, row)
            rows.append(row)
        self.insert("coordination_events", rows)


def conductor_legacy(name, who, record=None):
    """An old operator event whose name can differ from the live lease."""
    def replay(rows, ctx):
        agent = ctx.id(who)
        principal = rows.one("SELECT principal FROM coordination_agents WHERE agent_id=%s",
                             (agent,))["principal"]
        fence = rows.next("coordination_lease_fence")
        rows.insert("coordination_leases", [rows.row(
            "legacy_lease", name=name, agent=agent, principal=principal,
            fence=fence, expires=rows.now + 3600)])
        rows.event("legacy_grant", name=record or name, agent=agent, fence=fence)
    return replay


# These recorded limits must still fill the live daemon's first grant page
# and the recipient's mailbox. test_lease_recovery.py checks the Rust policy.
PENDING_MAIL = 256
MAIL_PER_SENDER = 60
FIRST_GRANT_PAGE = 50
GRANT_WINDOW = 300
MESSAGE_TTL = 86400


def _register(rows):
    agent = uuid.uuid4().hex
    credential_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
    rows.insert("coordination_agents", [rows.row(
        "registered_agent", agent=agent, credential_hash=credential_hash)])
    rows.event("register", agent=agent)
    return agent


def _mail(rows, sender, recipient, text, request_id, reason=None):
    message = uuid.uuid4().hex
    salt = secrets.token_hex(16)
    seq, = rows.conn.execute(
        "UPDATE coordination_agents SET next_sequence=next_sequence+1 WHERE agent_id=%s "
        "RETURNING next_sequence", (recipient,)).fetchone()
    rows.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                      (rows.now, sender))
    expires = rows.now + MESSAGE_TTL
    fingerprint = hashlib.sha256(canonical_payload([recipient, text, None, None]).encode()).hexdigest()
    record = "pending" if reason is None else "prior"
    bindings = dict(message=message, sender=sender, recipient=recipient, request=request_id,
                    expires=expires, sequence=seq, fingerprint=fingerprint)
    rows.insert("coordination_messages", [rows.row(record + "_message", **bindings)])
    rows.event(record + "_send", **bindings, salt=salt, commitment=body_commitment(salt, text))
    if reason is not None:
        wake_id = rows.next("coordination_wakes_wake_id_seq")
        rows.insert("coordination_wakes", [rows.row(
            "prior_wake", wake_id=wake_id, sender=sender, recipient=recipient,
            message=message, reason=reason)])


def _grant(rows, name, previous_holder, holder, old_fence, window):
    fence = rows.next("coordination_lease_fence")
    rows.insert("coordination_leases", [rows.row(
        "granted_lease", name=name, holder=holder, fence=fence, expires=rows.now + window)])
    rows.event("lease_release", name=name, holder=previous_holder, fence=old_fence)
    rows.event("lease_grant", name=name, holder=holder, fence=fence, window=window)
    rows.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                      (rows.now, previous_holder))


def _acquired_fence(rows, name, sender):
    fence = rows.next("coordination_lease_fence")
    rows.event("lease_acquire", name=name, holder=sender, fence=fence)
    return fence


def _grant_after_queue(rows, name, sender, recipient, fence):
    # The first fence and the queue ticket have already been consumed at
    # the crash boundary even though only the granted lease survives.
    rows.next("coordination_lease_waiters_ticket_seq")
    rows.event("lease_queue", name=name, holder=recipient)
    _grant(rows, name, sender, recipient, fence, GRANT_WINDOW)
    rows.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                      (rows.now, recipient))


def queued_grant(rows, ctx):
    name, old, new = "resource", ctx.id("A"), ctx.id("B")
    fence, = rows.conn.execute(
        "DELETE FROM coordination_leases WHERE name=%s AND holder_agent_id=%s RETURNING fence",
        (name, old)).fetchone()
    ttl, = rows.conn.execute(
        "DELETE FROM coordination_lease_waiters WHERE name=%s AND agent_id=%s RETURNING ttl",
        (name, new)).fetchone()
    _grant(rows, name, old, new, fence, min(ttl, GRANT_WINDOW))


def grant_pages(rows, ctx):
    recipient = ctx.id("B")
    for index in range(PENDING_MAIL):
        if index % MAIL_PER_SENDER == 0:
            sender = _register(rows)
        _mail(rows, sender, recipient, "pending", f"pending-{index}")
    for index in range(FIRST_GRANT_PAGE + 1):
        holder = recipient if index < FIRST_GRANT_PAGE else ctx.id("C")
        name, sender = f"resource-{index:02}", ctx.id("A")
        _grant_after_queue(rows, name, sender, holder, _acquired_fence(rows, name, sender))


def prior_wake(reason):
    def replay(rows, ctx):
        rows.conn.execute("DELETE FROM coordination_wakes")
        _mail(rows, ctx.id("A"), ctx.id("B"), "prior", f"prior-{reason}", reason)
    return replay


def startup_grant(rows, ctx):
    name = "startup-resource"
    sender = _register(rows)
    fence = _acquired_fence(rows, name, sender)
    recipient = _register(rows)
    _grant_after_queue(rows, name, sender, recipient, fence)
    ctx.agents.update(A={"agent_id": sender}, B={"agent_id": recipient})
