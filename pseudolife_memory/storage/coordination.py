"""Durable instance identities and addressed mail, separate from retrieval.

Mutation paths: bank identity establishment, register, update, attach, heartbeat,
detach, send, receive (first read), acknowledge, attempt, prune, restore
recovery, operator rebind and operator redaction, and the v45 resource
leases: acquire (grant, queue, renew), release, operator break, and the
settling any lease call, listing, prune or recovery does (expire, grant).
Every one of them except a heartbeat and a plain lease renewal appends to
the audit log (``coordination_events``) in its own transaction; see
``_append``. A renewal that changes a lease's purpose or estimate is
logged. Only redaction (a send event's ``body``, which is outside the hash)
and prune's retention cut change existing log rows, both under the chain
lock. There is no derived cache.
Callers serialize access to the mailbox connection with the coordination lock,
never the service lock (``CoordinationConnection`` below). SQL row locks also
protect independent connections; send locks both agents in ID order to avoid
reciprocal-send deadlocks. Recovery/rebind/redaction are operator-only
entry points: the HTTP/service layer must never expose them as agent tools.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
import unicodedata
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from pseudolife_memory.storage.postgres import connect_retrying_local_ports
from pseudolife_memory.storage.schema import COORDINATION_SCHEMA_SQL

logger = logging.getLogger("pseudolife-mcp")


class CoordinationConnection:
    """Dedicated autocommit connection for the mailbox, never the shared
    service connection.

    Mailbox calls must not queue behind the service lock: a heartbeat or
    identity check that waits on a consolidation pass expires the adapter's
    lease and fails its 5s context check (2026-09-20 daemon log: dispatch
    waited 8.6s, autosave 47s). The rows are guarded by their own SQL row
    locks, so a second connection is safe; callers still serialize this
    one with the coordination lock, because psycopg transaction blocks on
    one connection must never interleave across threads.

    Session setup mirrors ``PostgresStorage._connect`` and the commit check
    mirrors ``PostgresStorage._txn``: same lock timeout, same public
    search_path, same refusal to report a transaction the server rolled
    back when the connection broke mid-block. No schema work happens here;
    the shared connection ensures the schema before this one is opened.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._in_txn = False
        self._conn = self._connect()

    def _connect(self) -> psycopg.Connection:
        conn = connect_retrying_local_ports(
            self.dsn, connect_timeout=10, autocommit=True)
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("SET search_path TO public")
        return conn

    @property
    def conn(self) -> psycopg.Connection:
        """Heal on next use after a Postgres restart, like the shared one.

        Never mid-block: a statement after the connection broke inside a
        ``_txn`` must fail with the block, not commit alone on a fresh
        connection (the shared connection pins the same way)."""
        c = self._conn
        if not self._in_txn and (c.closed or c.broken):
            logger.warning("coordination connection lost (closed=%s broken=%s); "
                           "reconnecting", c.closed, c.broken)
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — never fail a shutdown on close
            pass

    @contextmanager
    def _txn(self):
        conn = self.conn
        self._in_txn = True
        try:
            with conn.transaction() as tx:
                yield
        finally:
            self._in_txn = False
        if tx.status is not tx.Status.COMMITTED:
            raise psycopg.OperationalError(
                f"transaction did not commit (status={tx.status.name}); "
                "connection lost during the block")


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
# An address that has had neither its own activity nor a lease for this long
# and is referenced by no retained message is removed by the same prune pass;
# a lease renewal counts as activity only when the shim saw a turn, but a
# held lease still keeps a live shim's address.
AGENT_RETENTION = DEDUPE_RETENTION
# Peers the default list shows: holding a lease, or active this recently.
# Measured 2026-09-20 on the live bank: 90 registered addresses, 11 leased,
# 67 of them Codex threads whose shim had been killed with the row still
# marked attached, 5 to 160 hours idle, no task, no mail; and every parked shim
# looked as busy as a working one because its heartbeat bumped
# last_activity. An hour keeps a session that just ended visible for a
# handover and hides the rest; they are still counted.
ACTIVE_WINDOW = 3600
# A status line older than this is marked stale in the peer list. Measured
# 2026-09-25 on the live audit log (582 events over its first 21.6 h): agents
# that were working refreshed their status at p50 8.7 min, p95 34 min; the
# longest silence between one agent's own board actions inside a work block
# was 51 min, and the shortest silence that ended one was 5.4 h (sessions
# left idle overnight whose statuses still named merged PRs). Two hours is
# over twice the longest working gap and well under the shortest idle one.
STATUS_STALE_AFTER = 2 * 3600
# A peer holding a lease is listed while its own last action is this recent;
# after that it is counted like any idle peer. A shim's heartbeat holds the
# lease for as long as the process lives, so on 2026-09-25 the default list
# carried a dozen sessions silent for hours. Same measurement as above: this
# shows a quiet session with its (by then stale) status for one more
# ACTIVE_WINDOW, still under the 5.4 h shortest idle stretch.
ATTACHED_IDLE_WINDOW = STATUS_STALE_AFTER + ACTIVE_WINDOW
# An address whose adapter registered ``resumable: false`` has no state file
# behind it, so nothing can attach to it again once its lease lapses; it is
# removed after this much idleness instead of AGENT_RETENTION. Same
# measurement: the Claude shims without a state path left one new address
# per launch. An address that did not declare either way predates the flag
# and keeps the long window, so nothing an older adapter can still resume
# is removed early.
EPHEMERAL_AGENT_RETENTION = ACTIVE_WINDOW
# The attach/heartbeat answer previews this many of the oldest pending
# messages, each cut to this many characters, so the shim can show a turn
# digest without a receive call. Five lines of a hundred characters plus
# the header fit the 200-300 token budget the per-turn check-in design set
# for a routine change (2026-09-20); the count says what the preview omits.
PREVIEW_LIMIT = 5
PREVIEW_EXCERPT = 100
# Live delivery attempts per message across attachments. Each attachment
# may attempt a pending message once; past this total the message is left
# for explicit receive so one unacknowledged message cannot wake the host on
# every restart.
MAX_ATTEMPTS = 3
ATTACHMENT_LEASE = 60
SEND_RATE = 60
# Resource leases (schema v45): named holds on shared resources such as a full
# test suite, the GPU or a daemon maintenance window. Not the adapter's
# attachment lease (``lease_until``) above. These bounds are starting points,
# not measurements: a holder renews at least every LEASE_TTL_MIN, a
# session-held lease (``coordinator:``, ``claim:``) lasts at most a day
# between renewals, and an expected end past a week is a typo.
MAX_LEASE_NAME = 120
LEASE_TTL_MIN = 30
LEASE_TTL_MAX = 86400
LEASE_EXPECT_MAX = 7 * 86400
LEASE_QUEUE_MAX = 64
# A freed lease is granted to the head of its queue, which must renew within
# this window (or its own ttl, if shorter) or lose it to the next waiter. The
# 2026-09-24 overnight relay stalled 20 min (02:39-02:59) on a turn handed to
# a session that never took it; five minutes bounds that class at a quarter
# of it, and is about 60 polls of ``pseudolife-mcp lease run``.
LEASE_GRANT_WINDOW = 300
# Waiters named per lease in a listing; the count beside them is exact.
LEASE_LIST_QUEUE = 10
HLC_META_KEY = "coordination_hlc_highwater"
WRITER_EPOCH_META_KEY = "writer_lease_epoch"
BANK_ID_META_KEY = "coordination_bank_id"
# Every message a recipient reads is agent-origin collaboration, never the
# operator's authority; the label rides on the row so no consumer infers it.
MESSAGE_ORIGIN = "agent"
# The audit log (schema v42): one append-only row per board mutation, written
# in the mutation's own transaction and chained by sha256(prev_hash ||
# canonical row), so an edited, inserted, reordered or removed row fails
# ``verify_audit_chain``. Heartbeats are lease renewals, not board events, and
# are not logged: at the shim's 20 s cadence a single session would add about
# 4,300 rows a day, and attach/detach already bracket each lease.
AUDIT_FORMAT = "pseudolife-coordination-audit-v1"
GENESIS_HASH = "0" * 64
# Serializes chain appends across connections (the daemon's mailbox connection,
# the offline recovery CLI, any second process). Always taken after every
# board-row lock a mutation takes: its holder then only reads, inserts new log
# rows and changes old ones (prune's cut deletes them, redact blanks a body)
# that nobody touches without holding it, so it never waits on anything and
# cannot close a deadlock cycle.
AUDIT_LOCK_KEY = "coordination-audit-chain"
# The columns every event has, hashed or a hash. A send event (v46) also
# carries ``body`` and ``body_salt``, outside the hash: its hashed payload
# holds sha256(salt || body) instead, and nothing else derived from the body
# (not even its length), so the body can be redacted and the chain still
# verifies, and once the salt goes with it a guess at the body has nothing to
# be checked against (maintainer decision, 2026-09-26). Rows written before
# v46 keep the body inside the payload.
AUDIT_COLUMNS = ("seq", "event", "actor", "principal", "agent_id", "recipient_agent_id",
                 "project", "task", "message_id", "payload", "created_at", "hlc",
                 "prev_hash", "hash")
_AUDIT_INSERT = ("INSERT INTO coordination_events (" + ",".join(AUDIT_COLUMNS)
                 + ") VALUES (" + ",".join(["%s"] * len(AUDIT_COLUMNS)) + ")")
# Named only for rows that carry a body: the offline recovery CLI appends its
# bodiless events to a restored bank before any schema pass, which on a v42-v45
# backup has no body column yet.
_AUDIT_INSERT_BODY = ("INSERT INTO coordination_events (" + ",".join(AUDIT_COLUMNS)
                      + ",body,body_salt) VALUES ("
                      + ",".join(["%s"] * (len(AUDIT_COLUMNS) + 2)) + ")")
# Bytes of random salt per body (hex in the column): enough that the salt
# itself can never be guessed.
BODY_SALT_BYTES = 16
# Exactly the form secrets.token_hex writes. bytes.fromhex alone would skip
# whitespace, and a salt decoding to fewer bytes would move the salt/body
# split, which nothing else in the commitment pins.
_SALT = re.compile("[0-9a-f]{%d}" % (2 * BODY_SALT_BYTES))


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
MAX_REDACT_REASON = 240


class CoordinationError(ValueError):
    """Stable public code; never includes supplied credentials or bodies."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CoordinationClockChanged(RuntimeError):
    """The bank's writer changed since the service clock was reseeded."""


def _string(value: Any, limit: int, field: str, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise CoordinationError(f"invalid_{field}")
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise CoordinationError(f"invalid_{field}")
    return value


def _excerpt(text: Any) -> str:
    """One line of a message body for a digest: whitespace and every
    control or format character (C0, C1, bidi overrides, zero-width marks)
    collapse to single spaces, then a hard cut."""
    if not isinstance(text, str):
        return ""
    cleaned = " ".join("".join(" " if unicodedata.category(c)[0] == "C" else c
                               for c in text).split())
    return cleaned if len(cleaned) <= PREVIEW_EXCERPT else cleaned[:PREVIEW_EXCERPT] + "..."


_ID_CHARS = frozenset("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_")


def _message_ids(value: Any) -> tuple[list[str], bool]:
    """One id, or several: comma-separated, or a JSON array of strings, which
    is how a host that stringifies list parameters sends a list. Items are
    id-shaped only, so a bracketed fragment fails loudly instead of landing
    in ``missing``; duplicates drop in order; at most MAX_PAGE. Returns the
    ids and whether the caller used list syntax. The 8192-character bound on
    the whole string is incidental; MAX_PAGE is the limit."""
    _string(value, 8192, "message_id", empty=False)
    text = value.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            raise CoordinationError("invalid_message_id") from None
        if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items):
            raise CoordinationError("invalid_message_id")
        batch = True
    else:
        items = text.split(",")
        batch = "," in text
    ids: list[str] = []
    for item in items:
        item = item.strip()
        if not item or len(item) > 120 or any(c not in _ID_CHARS for c in item):
            raise CoordinationError("invalid_message_id")
        if item not in ids:
            ids.append(item)
    if len(ids) > MAX_PAGE:
        raise CoordinationError("invalid_message_id")
    return ids, batch


# ── secret-shaped text ────────────────────────────────────────────────────
#
# The board keeps what agents write: a message body for the audit log's
# retention window (and in every backup taken meanwhile); a status, label,
# project, task, episode, lease name or purpose, or request id hashed into
# the chain for good. Text shaped like a credential is refused where it
# would be kept, with ``secret_like_body`` and never an echo of the text. A
# net for the common shapes, not a guarantee: a v46 message body it misses
# can still be removed with ``redact``. Checked 2026-09-26 against the board
# export of the 2026-09-23/24 fifteen-session trial: no hits in its 816
# message bodies or 25 non-empty statuses. The 2026-09-26 review added this
# deployment's own shapes (a principal token map, the adapter's key header,
# a DSN password, a bearer token) and removed its false positives (branch
# names and paths after a key, truncated digests, names without digits).


def _char_classes(value: str) -> int:
    """How many of lower-case, upper-case and digit ``value`` mixes."""
    return (any(c.islower() for c in value) + any(c.isupper() for c in value)
            + any(c.isdigit() for c in value))


def _random_looking(value: str) -> bool:
    return _char_classes(value) >= 2


def _three_classes(value: str) -> bool:
    return _char_classes(value) == 3


def _letter_and_digit(value: str) -> bool:
    return any(c.isalpha() for c in value) and any(c.isdigit() for c in value)


def _has_digit(value: str) -> bool:
    return any(c.isdigit() for c in value)


def _random_token(value: str) -> bool:
    """A run that looks generated rather than written: letters and digits,
    and either all three of lower case, upper case and digits, or 32 or more
    characters in one unbroken run (no ``_`` or ``-``: generated single-case
    keys have none, word-joined identifiers such as
    ``created_at_2026_09_26_then_agent_id`` do). Words, names
    (``PSEUDOLIFE_MCP_TOKEN_FILE``, camelCase), branch-like ``name-2026``
    strings and counts are not."""
    return _letter_and_digit(value) and (
        _three_classes(value)
        or (len(value) >= 32 and "_" not in value and "-" not in value))


def _dsn_password(value: str) -> bool:
    """A DSN's password, unless it is a placeholder (``<password>``,
    ``${POSTGRES_PASSWORD}``, ``*****``)."""
    return value[0] not in "<${[(*" and not set(value) <= set("*xX.")


# (name, pattern, check on group 1 or None). Prefixes are strong evidence on
# their own; where a prefix also starts ordinary words or identifiers
# (``sk-learn-...``, ``github_pat_tests_...``, ``ghs_<lowercase id>``,
# ``ASIAPACIFIC...``), the tail must look random. Searched in this order;
# ``secret_kind`` names the first that matches.
_SECRET_SHAPES = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY(?: BLOCK)?-----"),
     None),
    ("github_pat", re.compile(r"(?<![A-Za-z0-9])github_pat_([A-Za-z0-9_]{22,})"),
     _three_classes),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_([A-Za-z0-9]{36,})"),
     _three_classes),
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9_./-])sk-ant-([A-Za-z0-9_-]{20,})"),
     _three_classes),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9_./-])sk-([A-Za-z0-9_-]{32,})"),
     _three_classes),
    ("aws_access_key_id",
     re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)([A-Z0-9]{16})(?![A-Za-z0-9])"), _has_digit),
    ("aws_secret_key",
     re.compile(r"(?i:aws_?secret_?(?:access_?)?key)[\"']?[ \t]{0,4}[:=][ \t]{0,4}[\"']?"
                r"([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])"), _letter_and_digit),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-([A-Za-z0-9-]{10,})"), _has_digit),
    ("huggingface_token", re.compile(r"(?<![A-Za-z0-9_])hf_([A-Za-z0-9]{30,})"),
     _three_classes),
    ("stripe_key", re.compile(r"(?<![A-Za-z0-9_])[sr]k_live_([A-Za-z0-9]{20,})"),
     _letter_and_digit),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
     None),
    ("gitlab_token", re.compile(r"(?<![A-Za-z0-9_-])glpat-([A-Za-z0-9_-]{20,})"),
     _random_looking),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"
                       r"\.[A-Za-z0-9_-]{8,}"), None),
    ("bearer_token", re.compile(r"(?<![A-Za-z0-9])(?i:bearer)[ \t]+([A-Za-z0-9_.~+/-]{16,}={0,2})"),
     _random_token),
    ("dsn_password", re.compile(r"://[^\s/:@]+:([^\s@/]{6,})@"), _dsn_password),
)
# A key naming a secret, then ``:`` or ``=`` (or, for a ``--flag``,
# whitespace) and a value. ``token`` excludes tokenize/tokenise; ``key`` and
# ``auth`` cover private_key, access_key, auth and the adapter's
# X-PL-Agent-Key header.
_SECRET_KEY = re.compile(
    r"(?i:secret|token(?!i[sz])|password|passwd|credential|key|auth|bearer)", re.ASCII)
# Matched forward from each key with bounded runs, and the scan resumes after
# the value it consumed, so a long body is read once. The value keeps ``:``,
# ``,`` and ``=`` so a principal map (``codex:<token>,claude:<token>``) is
# split into its pieces rather than cut at the first name.
_VALUE = r"([A-Za-z0-9_+/=:,-]+)"
_SECRET_ASSIGNMENT = re.compile(
    r"[A-Za-z0-9_.-]{0,40}[\"']?[ \t]{0,4}[:=][ \t]{0,4}[\"']?" + _VALUE, re.ASCII)
_CLI_FLAG = re.compile(r"(?<![A-Za-z0-9_-])--([A-Za-z0-9_-]{1,60})[ \t]+[\"']?" + _VALUE,
                       re.ASCII)
_PIECES = re.compile(r"[,:=]")
_HEX = frozenset("0123456789abcdefABCDEF")
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_SRI = re.compile(r"sha(?:1|256|384|512)-", re.IGNORECASE)


def _assigned_secret(value: str) -> bool:
    """Whether a value given to a secret-named key holds one. It is split on
    ``,``, ``:`` and ``=``; a piece counts when it is 20 or more characters
    and looks generated (``_random_token``). What this board carries after
    such keys in ordinary traffic does not: anything with a ``/`` (a path, a
    branch, ``owner/repo``), all-hex (an id, a git SHA, a sha256 digest, whole
    or truncated), anything holding a UUID, a subresource hash
    (``sha256-...``), names, words and counts."""
    for piece in _PIECES.split(value):
        if (len(piece) >= 20 and "/" not in piece
                and not all(c in _HEX for c in piece)
                and _UUID.search(piece) is None and _SRI.match(piece) is None
                and _random_token(piece)):
            return True
    return False


def secret_kind(text: str) -> str | None:
    """The name of the first credential shape found in ``text``, or None."""
    if not isinstance(text, str):
        return None
    for name, pattern, check in _SECRET_SHAPES:
        for match in pattern.finditer(text):
            if check is None or check(match.group(1)):
                return name
    for flag in _CLI_FLAG.finditer(text):
        if _SECRET_KEY.search(flag.group(1)) and _assigned_secret(flag.group(2)):
            return "cli_flag"
    pos = 0
    while (key := _SECRET_KEY.search(text, pos)) is not None:
        assigned = _SECRET_ASSIGNMENT.match(text, key.end())
        if assigned is None:
            pos = key.end()
            continue
        if _assigned_secret(assigned.group(1)):
            return "key_value"
        pos = assigned.end()
    return None


def looks_like_secret(text: str) -> bool:
    """Whether ``text`` holds something shaped like a credential: a GitHub,
    GitLab, Hugging Face, Anthropic, OpenAI-style, Stripe, Slack or Google
    key or token, an AWS access key id or secret key, a JWT, a bearer token,
    a DSN password, a PEM private-key header, or a secret-named key or
    ``--flag`` given a generated-looking value. Ordinary prose about tokens
    and secrets, git SHAs, digests, UUIDs, message and agent ids, paths,
    branch names and placeholders are not."""
    return secret_kind(text) is not None


def _refuse_secret(text: str) -> None:
    if looks_like_secret(text):
        raise CoordinationError("secret_like_body")


def _hash(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_hash(prev_hash: str, row) -> str:
    """sha256(prev_hash || canonical row), over every column but the two
    hashes and ``body``. ``payload`` is hashed as the stored text; a parsed
    payload (a JSON-lines export read back) is re-canonicalized to that same
    text. A v46 send event's payload commits to its body by sha256, which
    ``verify_audit_chain`` checks separately."""
    payload = row["payload"]
    if not isinstance(payload, str):
        payload = _canonical(payload)
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


def audit_has_body_column(conn) -> bool:
    """Whether the log has the v46 ``body`` column. A restored v42-v45 bank
    read before any v46 daemon has started does not: every send body there
    is inside its hashed payload."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE "
                    "attrelid=to_regclass('coordination_events') AND attname='body' "
                    "AND attnum>0 AND NOT attisdropped)")
        return cur.fetchone()[0]


def audit_events(conn, *, project=None, task=None, agent_id=None, since=None, until=None):
    """Stream the audit log in chain order through a server-side cursor.

    Each row carries ``body`` and ``body_salt`` too (None on every event but
    a v46 send, and on a redacted one). ``agent_id`` matches the acting agent or a message's
    recipient; ``since`` is inclusive and ``until`` exclusive, in epoch
    seconds. A filtered stream is a slice for reading, not a chain
    ``verify_audit_chain`` can check."""
    clauses, params = [], []
    for column, value in (("project", project), ("task", task)):
        if value is not None:
            clauses.append(f"{column}=%s")
            params.append(value)
    if agent_id is not None:
        clauses.append("(agent_id=%s OR recipient_agent_id=%s)")
        params += [agent_id, agent_id]
    if since is not None:
        clauses.append("created_at>=%s")
        params.append(since)
    if until is not None:
        clauses.append("created_at<%s")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY seq"
    with conn.transaction():
        # created_at is hashed as the float written; a server configured for
        # rounded float output would make every row read back as tampered.
        conn.execute("SET LOCAL extra_float_digits = 3")
        if audit_has_body_column(conn):
            body = "body,body_salt"
        else:
            body = "NULL::text AS body,NULL::text AS body_salt"
        sql = "SELECT " + ",".join(AUDIT_COLUMNS) + "," + body + " FROM coordination_events" + where
        with conn.cursor(name="coordination_audit", row_factory=dict_row) as cur:
            cur.itersize = 1000
            cur.execute(sql, params)
            yield from cur


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

    @staticmethod
    def _event(event, payload, *, actor="agent", principal="", agent_id="", recipient=None,
               project="", task="", message_id=None, hlc="", body=None, body_salt=None):
        """One audit row before its chain position. ``principal`` is the
        bearer principal the transport verified, empty for the daemon's own
        maintenance and for the offline operator; never a credential.
        ``body`` (a send's text) and ``body_salt`` are stored outside the
        hash; the payload must commit to both."""
        return {"event": event, "actor": actor, "principal": principal, "agent_id": agent_id,
                "recipient_agent_id": recipient, "project": project, "task": task,
                "message_id": message_id, "hlc": hlc, "payload": _canonical(payload),
                "body": body, "body_salt": body_salt}

    def _chain_head(self):
        """Take the chain lock, then read the head ``(seq, hash)``.

        Transaction-scoped, so it must run inside the mutation's transaction:
        outside one the lock would end with the statement and two writers
        could read the same head."""
        conn = self.storage.conn
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
            raise RuntimeError("audit events are appended inside the mutation's transaction")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (AUDIT_LOCK_KEY,))
        row = self._one("SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1")
        return (row["seq"], row["hash"]) if row else (0, GENESIS_HASH)

    def _append(self, events, now, *, head=None):
        """Chain and insert ``events`` as the mutation's last statements, so
        they commit or roll back with it and the log cannot diverge from the
        board. ``head`` comes from a ``_chain_head`` the caller already took
        (prune reads it before removing old rows, so the chain continues from
        a removed head instead of restarting at genesis)."""
        if not events:
            return
        seq, prev = self._chain_head() if head is None else head
        plain, bodied = [], []
        for event in events:
            seq += 1
            row = {**event, "seq": seq, "created_at": float(now), "prev_hash": prev}
            row["hash"] = prev = audit_hash(prev, row)
            values = tuple(row[column] for column in AUDIT_COLUMNS)
            if row.get("body") is None:
                plain.append(values)
            else:
                bodied.append(values + (row["body"], row.get("body_salt")))
        with self.storage.conn.cursor() as cur:
            if plain:
                cur.executemany(_AUDIT_INSERT, plain)
            if bodied:
                cur.executemany(_AUDIT_INSERT_BODY, bodied)
        return seq, prev

    def _audit_present(self):
        """Whether the log exists. Only the offline recovery paths ask: they
        can run on a restored pre-v42 backup before any schema pass."""
        return self._one("SELECT to_regclass('coordination_events') IS NOT NULL "
                         "AS present")["present"]

    @staticmethod
    def _bank_id(value):
        if not isinstance(value, str):
            raise CoordinationError("invalid_bank_identity")
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError):
            raise CoordinationError("invalid_bank_identity") from None
        if str(parsed) != value:
            raise CoordinationError("invalid_bank_identity")
        return value

    def context(self, principal, *, agent_id=None, nonce=None):
        """Return this logical bank's durable identity and optional mailbox proof."""
        if (agent_id is None) != (nonce is None):
            raise CoordinationError("missing_parameter")
        for value, field in ((agent_id, "agent_id"), (nonce, "nonce")):
            if value is not None and (
                    not isinstance(value, str) or len(value) != 32
                    or value != value.lower()
                    or any(c not in "0123456789abcdef" for c in value)):
                raise CoordinationError(f"invalid_{field}")
        candidate = str(uuid.uuid4())
        with self.storage._txn():
            created = self.storage.conn.execute(
                "INSERT INTO meta (key,value) VALUES (%s,%s) "
                "ON CONFLICT (key) DO NOTHING",
                (BANK_ID_META_KEY, Jsonb(candidate))).rowcount
            bank_id = self._bank_id(self._one(
                "SELECT value FROM meta WHERE key=%s", (BANK_ID_META_KEY,))["value"])
            if created:
                self._append([self._event("bank_identity", {"bank_id": bank_id},
                                          principal=principal)], self.clock())
            result = {"bank_id": bank_id, "principal": principal}
            if agent_id is None:
                return result
            row = self._one(
                "SELECT principal,credential_hash FROM coordination_agents "
                "WHERE agent_id=%s", (agent_id,))
            credential_hash = row and row["credential_hash"]
            if (row is None or row["principal"] != principal
                    or not isinstance(credential_hash, str)
                    or len(credential_hash) != 64
                    or credential_hash != credential_hash.lower()
                    or any(c not in "0123456789abcdef" for c in credential_hash)):
                raise CoordinationError("invalid_credential")
            message = json.dumps(
                ["pseudolife-context-v1", bank_id, principal, agent_id, nonce],
                separators=(",", ":"), ensure_ascii=True).encode("ascii")
            result["proof"] = hmac.new(
                bytes.fromhex(credential_hash), message, hashlib.sha256).hexdigest()
            return result

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
            self._append([self._event("register", fields, principal=principal, agent_id=agent_id,
                                      project=fields["project"], task=fields["task"])], now)
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
                # Hashed into the audit chain for good: every field in the
                # register event, and project and task into the columns of
                # every later event by this agent.
                _refuse_secret(value)
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

    def update(self, principal, agent_id, credential, *, expect=None, **fields):
        """Change the caller's own row. ``expect`` (seconds) says when the
        status stops being true: past it the peer list marks the row
        ``status_overdue``. A new status without one clears the old
        expectation; ``expect`` alone re-times the current status."""
        fields = self._fields(**fields)
        if expect is not None and (type(expect) is not int
                                   or not 1 <= expect <= LEASE_EXPECT_MAX):
            raise CoordinationError("invalid_expect")
        with self.storage._txn():
            row = self._auth(principal, agent_id, credential, lock=True)
            now = self.clock()
            # Only a real change reaches the row and the log, so a status
            # update that never used an expectation logs what it always did.
            if expect is not None:
                fields["status_expires_at"] = now + expect
            elif "status" in fields and row["status_expires_at"] is not None:
                fields["status_expires_at"] = None
            assignments = [f"{key}=%s" for key in fields]
            values = [Jsonb(v) if k == "capabilities" else v for k, v in fields.items()]
            self.storage.conn.execute(
                "UPDATE coordination_agents SET " + ",".join(assignments + ["last_activity=%s"])
                + " WHERE agent_id=%s", (*values, now, agent_id))
            # The live row keeps only the newest value; the log keeps each one
            # and what it replaced, which is the status history.
            self._append([self._event(
                "update", {"fields": fields, "before": {key: row[key] for key in fields}},
                principal=principal, agent_id=agent_id,
                project=fields.get("project", row["project"]),
                task=fields.get("task", row["task"]))], now)
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
        now = self.clock()
        scope = "SELECT * FROM coordination_agents WHERE " + " AND ".join(clauses)
        leased = "(attachment_id IS NOT NULL AND coalesce(lease_until,0)>%s)"
        # Only peers whose own last action is recent: within ACTIVE_WINDOW,
        # or ATTACHED_IDLE_WINDOW while they hold a lease. Reachable adapters
        # come first: a burst of idle addresses must not push the peers that
        # can actually receive live mail off a bounded page. The rest are
        # counted, not listed; one extra row tells whether the page cut
        # active peers too.
        recent = f"last_activity>CASE WHEN {leased} THEN %s ELSE %s END"
        windows = (now, now - ATTACHED_IDLE_WINDOW, now - ACTIVE_WINDOW)
        rows = self._all(scope + f" AND {recent} ORDER BY {leased} DESC,"
                         "last_activity DESC,agent_id LIMIT %s",
                         (*values, *windows, now, limit + 1))
        idle = self._one("SELECT count(*) AS n FROM coordination_agents WHERE " + " AND ".join(clauses)
                         + f" AND NOT {recent}", (*values, *windows))["n"]
        agents = []
        for row in rows[:limit]:
            agent = self._public(row)
            agent["status_expires_at"] = row["status_expires_at"]
            agent["status_overdue"] = (row["status_expires_at"] is not None
                                       and now > row["status_expires_at"])
            agents.append(agent)
        self._stamp_status_age(agents, now)
        leases = self.list_leases(limit=MAX_PAGE)
        return {"agents": agents, "truncated": len(rows) > limit, "idle_omitted": idle,
                "leases": leases["leases"], "leases_truncated": leases["truncated"]}

    def _stamp_status_age(self, agents, now):
        """Say when each listed peer's status was set, and mark it stale past
        STATUS_STALE_AFTER. The live row keeps only the newest value, so the
        time comes from the audit log: the newest ``register`` (which always
        sets the status, blank included) or ``update`` that carried one.
        When the log holds neither (the status predates the v42 log, or
        retention cut the event), the log's oldest row is a lower bound on
        the age, reported as such. A blank status is never stale."""
        if not agents:
            return
        # Lazy: the helper's package loads the embedding stack, which the
        # daemon has already imported and the offline CLIs never need.
        from pseudolife_memory.memory.context_builder import _relative_time
        # CASE fixes the evaluation order: only update payloads are cast.
        set_at = {row["agent_id"]: row["set_at"] for row in self._all(
            "SELECT agent_id,max(created_at) AS set_at FROM coordination_events "
            "WHERE agent_id=ANY(%s) AND CASE event WHEN 'register' THEN true "
            "WHEN 'update' THEN (payload::jsonb->'fields') ? 'status' ELSE false END "
            "GROUP BY agent_id", ([agent["agent_id"] for agent in agents],))}
        oldest = None
        if len(set_at) < len(agents):
            oldest = self._one("SELECT min(created_at) AS t FROM coordination_events")["t"]
        for agent in agents:
            when = set_at.get(agent["agent_id"])
            if when is not None:
                age, text = now - when, _relative_time(when, now)
            elif oldest is not None and now - oldest >= 60:
                age, text = now - oldest, "more than " + _relative_time(oldest, now)
            else:
                age, text = None, "unknown"
            agent["status_set_at"] = when
            agent["status_age"] = text
            agent["status_stale"] = bool(agent["status"]) and age is not None \
                and age >= STATUS_STALE_AFTER

    # ── resource leases (v45) ─────────────────────────────────────────────
    #
    # One row per lease name, kept after each hold so ``fence`` keeps rising;
    # a queue per name in ticket order. Every change settles the lease first:
    # a hold past ``expires_at`` expires, and a free lease goes to the head of
    # its queue for LEASE_GRANT_WINDOW. Settling happens on any lease call,
    # any listing and the prune pass, so a queue advances even when its
    # holder died without a word. Lock order: lease row, then waiter rows,
    # then the caller's agent row, then (in _append) the audit chain. Renewals
    # are not logged, like heartbeats; grants, releases and expiries are.

    @staticmethod
    def _lease_args(name, ttl=None, expect=None, purpose=None):
        # No control, format (zero-width, bidi), surrogate or unassigned
        # characters: two names that render alike must be one lease.
        if (not isinstance(name, str) or not name or name != name.strip()
                or len(name) > MAX_LEASE_NAME
                or any(unicodedata.category(c)[0] == "C" for c in name)):
            raise CoordinationError("invalid_lease")
        # The name is in every lease event's hashed payload, for good.
        _refuse_secret(name)
        if ttl is not None and (type(ttl) is not int
                                or not LEASE_TTL_MIN <= ttl <= LEASE_TTL_MAX):
            raise CoordinationError("invalid_ttl")
        if expect is not None and (type(expect) is not int
                                   or not 1 <= expect <= LEASE_EXPECT_MAX):
            raise CoordinationError("invalid_expect")
        if purpose is not None:
            _string(purpose, 240, "purpose")
            # Hashed into the audit chain for good (lease_acquire/queue/grant).
            _refuse_secret(purpose)

    def _grant(self, name, agent_id, principal, *, now, hold, expect, purpose):
        # The fence comes from one sequence for every lease, never from the
        # row: prune forgets a row left free for a week, and a counter kept
        # on it would restart and hand a later grant a fence already used.
        return self._one(
            "UPDATE coordination_leases SET holder_agent_id=%s,holder_principal=%s,purpose=%s,"
            "fence=nextval('coordination_lease_fence'),acquired_at=%s,expires_at=%s,expect=%s,"
            "expected_end=%s,freed_at=NULL WHERE name=%s RETURNING *",
            (agent_id, principal, purpose, now, now + hold, expect,
             None if expect is None else now + expect, name))

    def _vacate(self, name, now):
        return self._one(
            "UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',"
            "purpose='',acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,"
            "freed_at=%s WHERE name=%s RETURNING *", (now, name))

    def _settle(self, name, now, events):
        """Expire a lapsed hold on ``name`` and grant a free lease to the head
        of its queue, inside the caller's transaction. Returns the locked row,
        or None when no lease has that name; appends the audit rows it causes
        to ``events``. The expiry and grant are the daemon's bookkeeping, so
        their actor is the daemon and their agent the one affected."""
        row = self._one("SELECT * FROM coordination_leases WHERE name=%s FOR UPDATE", (name,))
        if row is None:
            return None
        if row["holder_agent_id"] is not None and row["expires_at"] <= now:
            events.append(self._event("lease_expire", {"name": name, "fence": row["fence"]},
                                      actor="daemon", agent_id=row["holder_agent_id"]))
            row = self._vacate(name, now)
        if row["holder_agent_id"] is None:
            # A waiter that could never take its turn up leaves the queue
            # here, under the lease lock just taken, so every path locks a
            # lease before its queue (prune once deleted departed waiters
            # first, which deadlocked against a break holding the lease:
            # reproduced 2026-09-26). Departed: the agent row is gone, or it
            # has been idle past its retention window with no live
            # attachment (prune's test for removing it, less the retained-
            # mail condition, which keeps an address but not a turn). A
            # revoked waiter (restore recovery) is passed over below.
            window = ("CASE WHEN a.capabilities->>'resumable'='false' THEN %s ELSE %s END")
            gone = [r["agent_id"] for r in self._all(
                "DELETE FROM coordination_lease_waiters w WHERE w.name=%s AND (NOT EXISTS "
                "(SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id) OR EXISTS "
                "(SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id "
                f"AND (a.lease_until IS NULL OR a.lease_until<={window}) "
                f"AND a.last_activity<={window})) RETURNING w.agent_id",
                (name, *(now - EPHEMERAL_AGENT_RETENTION, now - AGENT_RETENTION) * 2))]
            for agent in gone:
                events.append(self._event("lease_dequeue", {"name": name, "reason": "departed"},
                                          actor="daemon", agent_id=agent))
            waiter = self._one(
                "SELECT w.* FROM coordination_lease_waiters w JOIN coordination_agents a "
                "ON a.agent_id=w.agent_id AND a.credential_hash IS NOT NULL "
                "WHERE w.name=%s ORDER BY w.ticket LIMIT 1 FOR UPDATE OF w", (name,))
            if waiter is not None:
                self.storage.conn.execute(
                    "DELETE FROM coordination_lease_waiters WHERE name=%s AND agent_id=%s",
                    (name, waiter["agent_id"]))
                hold = min(waiter["ttl"], LEASE_GRANT_WINDOW)
                row = self._grant(name, waiter["agent_id"], waiter["principal"], now=now,
                                  hold=hold, expect=waiter["expect"], purpose=waiter["purpose"])
                events.append(self._event(
                    "lease_grant", {"name": name, "fence": row["fence"], "window": hold,
                                    "expect": waiter["expect"], "purpose": waiter["purpose"]},
                    actor="daemon", agent_id=waiter["agent_id"]))
        return row

    def _settle_due(self, now, events, *, name=None):
        """Settle every lease with a lapsed hold or a free lease with waiters,
        in name order so concurrent settlers lock rows in one order."""
        rows = self._all(
            "SELECT name FROM coordination_leases l WHERE ((l.holder_agent_id IS NOT NULL "
            "AND l.expires_at<=%s) OR (l.holder_agent_id IS NULL AND EXISTS (SELECT 1 FROM "
            "coordination_lease_waiters w WHERE w.name=l.name)))"
            + (" AND l.name=%s" if name is not None else "")
            + " ORDER BY l.name FOR UPDATE",
            (now,) if name is None else (now, name))
        for row in rows:
            self._settle(row["name"], now, events)

    @staticmethod
    def _holder(row):
        if row["holder_agent_id"] is None:
            return None
        return {"agent_id": row["holder_agent_id"], "label": row.get("label") or "",
                "principal": row["holder_principal"], "purpose": row["purpose"],
                "acquired_at": row["acquired_at"], "expires_at": row["expires_at"],
                "expected_end": row["expected_end"]}

    def _lease_row(self, name):
        return self._one(
            "SELECT l.*,a.label FROM coordination_leases l LEFT JOIN coordination_agents a "
            "ON a.agent_id=l.holder_agent_id WHERE l.name=%s", (name,))

    def _lease_view(self, name, agent_id):
        row = self._lease_row(name)
        queued = self._one("SELECT count(*) AS n FROM coordination_lease_waiters WHERE name=%s",
                           (name,))["n"]
        mine = row["holder_agent_id"] == agent_id
        position = None
        if not mine:
            ahead = self._one(
                "SELECT count(*) AS n FROM coordination_lease_waiters w, "
                "coordination_lease_waiters me WHERE me.name=%s AND me.agent_id=%s "
                "AND w.name=me.name AND w.ticket<=me.ticket", (name, agent_id))["n"]
            position = ahead or None
        return {"name": name, "state": "held" if mine else "queued",
                "fence": row["fence"] if mine else None,
                "expires_at": row["expires_at"] if mine else None,
                "expected_end": row["expected_end"] if mine else None,
                "position": position, "queued": queued, "holder": self._holder(row)}

    def acquire_lease(self, principal, agent_id, credential, *, name, ttl=120, expect=None,
                      purpose=None):
        """Acquire or renew ``name`` for the caller, or queue it once.

        Holding it already, the caller's expiry moves to now + ``ttl`` (and
        the expected end to now + ``expect`` when given); a free lease with
        nobody queued is the caller's at once; otherwise the caller joins the
        queue, and asking again keeps its place and writes nothing."""
        self._lease_args(name, ttl, expect, purpose)
        with self.storage._txn():
            agent = self._auth(principal, agent_id, credential)
            now = self.clock()
            self.storage.conn.execute(
                "INSERT INTO coordination_leases (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (name,))
            events = []
            row = self._settle(name, now, events)
            scope = {"principal": principal, "agent_id": agent_id,
                     "project": agent["project"], "task": agent["task"]}
            if row["holder_agent_id"] == agent_id:
                # A renewal only extends the hold. Repeating the estimate
                # the hold already carries leaves its expected end alone,
                # so a stalled holder that keeps renewing still reads
                # stale; a new estimate or purpose is a logged change.
                sets, values, changed = ["expires_at=%s"], [now + ttl], {}
                if expect is not None and expect != row["expect"]:
                    sets += ["expect=%s", "expected_end=%s"]
                    values += [expect, now + expect]
                    changed["expect"] = expect
                if purpose is not None and purpose != row["purpose"]:
                    sets.append("purpose=%s")
                    values.append(purpose)
                    changed["purpose"] = purpose
                self.storage.conn.execute(
                    "UPDATE coordination_leases SET " + ",".join(sets) + " WHERE name=%s",
                    (*values, name))
                if changed:
                    events.append(self._event(
                        "lease_update", {"name": name, "fence": row["fence"], **changed},
                        **scope))
            elif row["holder_agent_id"] is None:
                row = self._grant(name, agent_id, principal, now=now, hold=ttl, expect=expect,
                                  purpose=purpose or "")
                events.append(self._event(
                    "lease_acquire", {"name": name, "fence": row["fence"], "ttl": ttl,
                                      "expect": expect, "purpose": purpose or ""}, **scope))
            elif self._one("SELECT 1 AS q FROM coordination_lease_waiters WHERE name=%s "
                           "AND agent_id=%s", (name, agent_id)) is None:
                if self._one("SELECT count(*) AS n FROM coordination_lease_waiters WHERE name=%s",
                             (name,))["n"] >= LEASE_QUEUE_MAX:
                    raise CoordinationError("lease_queue_full")
                self.storage.conn.execute(
                    "INSERT INTO coordination_lease_waiters (name,agent_id,principal,purpose,ttl,"
                    "expect,enqueued_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (name, agent_id, principal, purpose or "", ttl, expect, now))
                events.append(self._event(
                    "lease_queue", {"name": name, "ttl": ttl, "expect": expect,
                                    "purpose": purpose or ""}, **scope))
            self.storage.conn.execute(
                "UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                (now, agent_id))
            self._append(events, now)
            return self._lease_view(name, agent_id)

    def release_lease(self, principal, agent_id, credential, *, name):
        """Give up a held lease (the next waiter is granted it) or leave its
        queue. Neither holding nor queued: ``lease_not_held``."""
        self._lease_args(name)
        with self.storage._txn():
            agent = self._auth(principal, agent_id, credential)
            now = self.clock()
            scope = {"principal": principal, "agent_id": agent_id,
                     "project": agent["project"], "task": agent["task"]}
            row = self._one("SELECT * FROM coordination_leases WHERE name=%s FOR UPDATE", (name,))
            events = []
            if row is not None and row["holder_agent_id"] == agent_id:
                self._vacate(name, now)
                events.append(self._event("lease_release",
                                          {"name": name, "fence": row["fence"]}, **scope))
                self._settle(name, now, events)
                result = {"name": name, "released": True, "dequeued": False}
            elif self._one("DELETE FROM coordination_lease_waiters WHERE name=%s AND agent_id=%s "
                           "RETURNING ticket", (name, agent_id)) is not None:
                events.append(self._event("lease_dequeue", {"name": name}, **scope))
                result = {"name": name, "released": False, "dequeued": True}
            else:
                raise CoordinationError("lease_not_held")
            self.storage.conn.execute(
                "UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                (now, agent_id))
            self._append(events, now)
        return result

    def list_leases(self, *, name=None, limit=MAX_PAGE):
        """Held and queued leases, name order, each with its first
        LEASE_LIST_QUEUE waiters and an exact count. Free leases are omitted.
        ``stale`` means past the holder's expected end. Due leases are settled
        first, so a listing never shows a hold that has already lapsed."""
        if name is not None:
            self._lease_args(name)
        self._limit(limit)
        now = self.clock()
        with self.storage._txn():
            events = []
            self._settle_due(now, events, name=name)
            self._append(events, now)
        busy = ("(l.holder_agent_id IS NOT NULL OR EXISTS (SELECT 1 FROM "
                "coordination_lease_waiters w WHERE w.name=l.name))")
        rows = self._all(
            "SELECT l.*,a.label FROM coordination_leases l LEFT JOIN coordination_agents a "
            "ON a.agent_id=l.holder_agent_id WHERE " + busy
            + (" AND l.name=%s" if name is not None else "")
            # Resource leases (suite, GPU, coordinator) before the day-long
            # work-area claims, so a page of claims never hides them.
            + " ORDER BY l.name LIKE 'claim:%%', l.name LIMIT %s",
            ((name,) if name is not None else ()) + (limit + 1,))
        leases = []
        for row in rows[:limit]:
            queue = self._all(
                "SELECT w.agent_id,coalesce(a.label,'') AS label,w.enqueued_at,w.purpose "
                "FROM coordination_lease_waiters w LEFT JOIN coordination_agents a "
                "ON a.agent_id=w.agent_id WHERE w.name=%s ORDER BY w.ticket LIMIT %s",
                (row["name"], LEASE_LIST_QUEUE))
            queued = self._one("SELECT count(*) AS n FROM coordination_lease_waiters "
                               "WHERE name=%s", (row["name"],))["n"]
            leases.append({
                "name": row["name"], "holder": self._holder(row), "fence": row["fence"],
                "expires_at": row["expires_at"], "expected_end": row["expected_end"],
                "stale": row["expected_end"] is not None and now > row["expected_end"],
                "queued": queued, "queue": [dict(w) for w in queue]})
        return {"leases": leases, "truncated": len(rows) > limit}

    def break_lease(self, name):
        """Operator only (the offline CLI; never exposed to agents): free a
        held lease whatever its holder says, and grant it to the next
        waiter. The break is logged with the operator as its actor."""
        self._lease_args(name)
        with self.storage._txn():
            now = self.clock()
            row = self._one("SELECT * FROM coordination_leases WHERE name=%s FOR UPDATE", (name,))
            if row is None or row["holder_agent_id"] is None:
                return {"name": name, "broken": False, "was_held_by": None}
            self._vacate(name, now)
            events = [self._event("lease_break", {"name": name, "fence": row["fence"]},
                                  actor="operator", agent_id=row["holder_agent_id"])]
            self._settle(name, now, events)
            self._append(events, now)
        return {"name": name, "broken": True, "was_held_by": row["holder_agent_id"]}

    def _pending_count(self, agent_id):
        return self._one("SELECT count(*) AS n FROM coordination_messages WHERE recipient_agent_id=%s "
                         "AND acknowledged_at IS NULL AND expires_at>%s", (agent_id, self.clock()))["n"]

    def _pending_preview(self, agent_id):
        """The oldest pending messages, bounded, for the shim's per-turn digest.

        Oldest first so a backlog shows what has waited longest; the count
        beside it says how much the preview omits. Reading is not delivery:
        nothing here touches attempts or acknowledgements."""
        rows = self._all(
            "SELECT m.message_id,m.sender_agent_id,m.created_at,m.text,a.label AS sender_label "
            "FROM coordination_messages m LEFT JOIN coordination_agents a ON a.agent_id=m.sender_agent_id "
            "WHERE m.recipient_agent_id=%s AND m.acknowledged_at IS NULL AND m.expires_at>%s "
            "ORDER BY m.recipient_sequence LIMIT %s", (agent_id, self.clock(), PREVIEW_LIMIT))
        return [{"message_id": r["message_id"], "sender_agent_id": r["sender_agent_id"],
                 "sender_label": r["sender_label"] or "", "created_at": r["created_at"],
                 "excerpt": _excerpt(r["text"])} for r in rows]

    def _mailbox_state(self, agent_id):
        return {"pending_count": self._pending_count(agent_id),
                "pending_preview": self._pending_preview(agent_id)}

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
            # Only a new attachment, a process starting, is the agent's own
            # act. The id the row already holds is the adapter recovering its
            # lease after a daemon outage, a host sleep or a pull downgrade:
            # on 2026-09-25 a 9-minute sleep lapsed every idle shim's lease,
            # and the re-attach wave on wake made a dozen sessions idle for
            # hours read as active within 28 s of each other.
            started = row["attachment_id"] != attachment_id
            self.storage.conn.execute(
                "UPDATE coordination_agents SET attachment_id=%s,generation=%s,lease_until=%s,"
                "last_activity=CASE WHEN %s THEN %s ELSE last_activity END,"
                "wake_enabled=%s,lifecycle='attached' WHERE agent_id=%s",
                (attachment_id, generation, now + ATTACHMENT_LEASE, started, now,
                 wake_enabled, agent_id))
            mailbox = self._mailbox_state(agent_id)
            self._append([self._event(
                "attach", {"generation": generation, "lease_until": now + ATTACHMENT_LEASE,
                           "wake_enabled": wake_enabled, "renewed": alive},
                principal=principal, agent_id=agent_id, project=row["project"],
                task=row["task"])], now)
        return {"agent_id": agent_id, "generation": generation, "lease_until": now + ATTACHMENT_LEASE,
                **mailbox}

    def _attachment(self, row, attachment_id, generation):
        if (row["attachment_id"] != attachment_id or row["generation"] != generation
                or not row["attachment_id"] or (row["lease_until"] or 0) <= self.clock()):
            raise CoordinationError("stale_attachment")

    def check_attachment(self, principal, agent_id, credential, *, attachment_id, generation):
        row = self._auth(principal, agent_id, credential)
        self._attachment(row, attachment_id, generation)
        return {"agent_id": agent_id, "generation": generation,
                "lease_until": row["lease_until"], "wake_enabled": row["wake_enabled"]}

    def heartbeat(self, principal, agent_id, credential, *, attachment_id, generation,
                  active=False):
        """Renew the lease. ``active`` says the shim forwarded a tool call
        since its previous heartbeat; only then does the renewal count as
        activity, so a parked shim is not ranked or retained as a working
        one."""
        if not isinstance(active, bool):
            raise CoordinationError("invalid_active")
        with self.storage._txn():
            row = self._auth(principal, agent_id, credential, lock=True)
            self._attachment(row, attachment_id, generation)
            now = self.clock()
            until = now + ATTACHMENT_LEASE
            self.storage.conn.execute(
                "UPDATE coordination_agents SET lease_until=%s,"
                "last_activity=CASE WHEN %s THEN %s ELSE last_activity END WHERE agent_id=%s",
                (until, active, now, agent_id))
            mailbox = self._mailbox_state(agent_id)
        return {"agent_id": agent_id, "generation": generation, "lease_until": until,
                **mailbox}

    def detach(self, principal, agent_id, credential, *, attachment_id, generation):
        with self.storage._txn():
            row = self._auth(principal, agent_id, credential, lock=True)
            self._attachment(row, attachment_id, generation)
            self.storage.conn.execute(
                "UPDATE coordination_agents SET attachment_id=NULL,lease_until=NULL,"
                "generation=generation+1,lifecycle='detached' WHERE agent_id=%s", (agent_id,))
            self._append([self._event("detach", {"generation": generation}, principal=principal,
                                      agent_id=agent_id, project=row["project"],
                                      task=row["task"])], self.clock())
        return {"detached": True}

    def _receipt(self, row):
        state = ("acknowledged" if row["acknowledged_at"] is not None else
                 "expired" if row["expires_at"] <= self.clock() else
                 "attempted" if row["attempt_at"] is not None else "queued")
        return {"message_id": row["message_id"], "state": state,
                "created_at": row["created_at"], "expires_at": row["expires_at"],
                "acknowledged_at": row["acknowledged_at"]}

    def send(self, principal, agent_id, credential, *, to, text, request_id,
             reply_to=None, expires_at=None, hlc="", expected_writer_epoch=None,
             enforce_writer_epoch=False):
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
        # Refused before this call reads or writes a row: the audit log would
        # keep the body for its whole retention window, and the request id
        # in the send event's hashed payload for good.
        _refuse_secret(text)
        _refuse_secret(request_id)
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
            if enforce_writer_epoch:
                if (type(expected_writer_epoch) is not int
                        or expected_writer_epoch < 1):
                    raise CoordinationClockChanged("service clock is not ready")
                # Hold this row lock through the message commit. A new writer
                # cannot complete its epoch bump (and start canonical writes)
                # between this comparison and the committed HLC high-water.
                epoch_row = self.storage.conn.execute(
                    "SELECT value FROM meta WHERE key=%s FOR SHARE",
                    (WRITER_EPOCH_META_KEY,),
                ).fetchone()
                if (epoch_row is None or type(epoch_row[0]) is not int
                        or epoch_row[0] < 1):
                    raise RuntimeError("invalid writer lease epoch")
                if epoch_row[0] != expected_writer_epoch:
                    raise CoordinationClockChanged("writer lease epoch changed")
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
            # The body lives on in the event's body column after prune blanks
            # the live copy. The hashed payload holds a salted commitment to
            # it, not the text (v46), so ``redact`` can remove body and salt
            # and the chain holds, with nothing left to test a guess against.
            salt = secrets.token_hex(BODY_SALT_BYTES)
            self._append([self._event(
                "send", {"text_commitment": body_commitment(salt, text),
                         "reply_to": reply_to, "request_id": request_id,
                         "recipient_sequence": seq, "expires_at": expiry},
                principal=principal, agent_id=agent_id, recipient=to, project=sender["project"],
                task=sender["task"], message_id=message_id, hlc=hlc, body=text,
                body_salt=salt)], now)
        return self._receipt(row)

    @staticmethod
    def _limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            raise CoordinationError("invalid_limit")

    def receive(self, principal, agent_id, credential, *, after=None, limit=50,
                for_delivery=False):
        """Page pending mail. ``for_delivery`` is the adapter's live path: it
        skips messages whose attempts are exhausted, which an explicit receive
        still returns. The first time a message is served, by either path, its
        ``first_read_at`` is stamped and a ``read`` event logged; a replay of
        unacknowledged mail writes nothing."""
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
        now = self.clock()
        params = [agent_id, seq, now] + ([MAX_ATTEMPTS] if for_delivery else []) + [limit]
        rows = self._all("SELECT * FROM coordination_messages WHERE recipient_agent_id=%s "
                         "AND recipient_sequence>%s AND acknowledged_at IS NULL AND expires_at>%s"
                         + attempt_clause + " ORDER BY recipient_sequence LIMIT %s", params)
        unread = [r["message_id"] for r in rows if r["first_read_at"] is None]
        # Only a first read writes, so an empty poll or a replay stays one
        # autocommit read. The IS NULL guard stamps and logs each message
        # once even when two receives race on the same page.
        if unread:
            with self.storage._txn():
                # The mailbox row first, as ack and mark_attempt take it, so
                # a first read and an acknowledgment on separate connections
                # lock message rows in the same order.
                self._one("SELECT 1 FROM coordination_agents WHERE agent_id=%s FOR UPDATE",
                          (agent_id,))
                first = {r["message_id"] for r in self._all(
                    "UPDATE coordination_messages SET first_read_at=%s WHERE message_id=ANY(%s) "
                    "AND first_read_at IS NULL RETURNING message_id", (now, unread))}
                self._append([self._event(
                    "read", {"path": "delivery" if for_delivery else "pull",
                             "sender_agent_id": r["sender_agent_id"]},
                    principal=principal, agent_id=agent_id, recipient=agent_id,
                    project=r["project"], task=r["task"], message_id=r["message_id"])
                    for r in rows if r["message_id"] in first], now)
        keys = ("message_id", "sender_agent_id", "sender_principal", "recipient_agent_id",
                "project", "task", "text", "reply_to", "recipient_sequence", "hlc", "created_at", "expires_at")
        return {"messages": [{**{k: r[k] for k in keys}, "origin": MESSAGE_ORIGIN} for r in rows],
                "after": f"{agent_id}:{rows[-1]['recipient_sequence'] if rows else seq}"}

    def ack(self, principal, agent_id, credential, *, message_id):
        """Acknowledge one message, or several comma-separated (at most
        MAX_PAGE). One id returns its receipt or message_not_found; several
        return the receipts in the order given plus the ids that were not this
        mailbox's to acknowledge, so a batch never fails because one id went
        stale. The list is a string because some hosts stringify list
        parameters. Acknowledging is not completion; a cursor is never an
        acknowledgement."""
        ids, batch = _message_ids(message_id)
        with self.storage._txn():
            # The agent row lock serializes acknowledgements per mailbox, so
            # two overlapping batches cannot deadlock on message rows.
            self._auth(principal, agent_id, credential, lock=True)
            now = self.clock()
            # Two statements for the whole batch: the calls run under the
            # coordination lock, which heartbeats wait on.
            found = {row["message_id"]: row for row in self._all(
                "SELECT * FROM coordination_messages WHERE recipient_agent_id=%s "
                "AND message_id = ANY(%s) FOR UPDATE", (agent_id, ids))}
            pending = [one for one in ids if one in found and found[one]["acknowledged_at"] is None]
            if pending:
                # New acknowledgments count as activity. Replays leave both
                # the mailbox and its audit trail unchanged.
                self.storage.conn.execute("UPDATE coordination_agents SET last_activity=%s "
                                          "WHERE agent_id=%s", (now, agent_id))
                for row in self._all("UPDATE coordination_messages SET acknowledged_at=%s "
                                     "WHERE message_id = ANY(%s) RETURNING *", (now, pending)):
                    found[row["message_id"]] = row
            missing = [one for one in ids if one not in found]
            if not batch and missing:
                raise CoordinationError("message_not_found")
            self._append([self._event(
                "ack", {"sender_agent_id": found[one]["sender_agent_id"]}, principal=principal,
                agent_id=agent_id, recipient=agent_id, project=found[one]["project"],
                task=found[one]["task"], message_id=one) for one in pending], now)
        receipts = [self._receipt(found[one]) for one in ids if one in found]
        return {"receipts": receipts, "missing": missing} if batch else receipts[0]

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
                now = self.clock()
                row = self._one("UPDATE coordination_messages SET attempt_at=%s,attempt_generation=%s,"
                                "attempts=attempts+1 WHERE message_id=%s RETURNING *",
                                (now, generation, message_id))
                self._append([self._event(
                    "attempt", {"generation": generation, "attempts": row["attempts"],
                                "sender_agent_id": row["sender_agent_id"]},
                    principal=principal, agent_id=agent_id, recipient=agent_id,
                    project=row["project"], task=row["task"], message_id=message_id)], now)
        return self._receipt(row)

    def prune(self, *, audit_retention_days=0):
        """Expire bodies, discard terminal retry metadata after seven days, and
        remove addresses that are idle, unleased and referenced by no retained
        message (the message rows go first, so a referenced address outlives
        its mail by the retention window). An address goes once it has had
        neither its own activity nor a lease for its window:
        EPHEMERAL_AGENT_RETENTION when registered as not resumable,
        AGENT_RETENTION otherwise. A parked shim whose lease lapsed during a
        daemon restart or a host sleep keeps its address, because the first
        heartbeat afterwards prunes before it is served and the adapter
        re-attaches within a minute; a resumable one keeps it across any
        outage shorter than AGENT_RETENTION.

        The pass logs what it blanked (``expire``) and removed (``prune``).
        With ``audit_retention_days`` > 0 it also removes the audit log's
        prefix older than that window, cut on a UTC day boundary
        (``audit_cutoff``), and logs the cut (``audit_prune``, naming the last
        removed row, which anchors the surviving chain); 0 keeps the log
        forever."""
        if (type(audit_retention_days) is not int or audit_retention_days < 0):
            raise ValueError("audit_retention_days must be a whole number of days, 0 or more")
        now = self.clock()
        ephemeral = "a.capabilities->>'resumable'='false'"
        with self.storage._txn():
            # Resource leases first, lease rows before their queues as on
            # every other path (an offline `lease break` runs on another
            # connection). Settling drops a departed waiter's place before
            # anything is granted, so no turn goes to a session that is gone,
            # and expires a lapsed hold before the address that held it can
            # be removed, so the log says who lost it. A live holder is
            # never removed below.
            window = f"CASE WHEN {ephemeral} THEN %s ELSE %s END"
            removable = ("(a.lease_until IS NULL OR a.lease_until<={w}) AND a.last_activity<={w} "
                         "AND NOT EXISTS (SELECT 1 FROM coordination_messages m "
                         "WHERE m.sender_agent_id=a.agent_id OR m.recipient_agent_id=a.agent_id)"
                         ).format(w=window)
            retention = (now - EPHEMERAL_AGENT_RETENTION, now - AGENT_RETENTION) * 2
            events = []
            self._settle_due(now, events)
            expired = sorted(r["message_id"] for r in self._all(
                "UPDATE coordination_messages SET text=NULL "
                "WHERE expires_at<=%s AND text IS NOT NULL RETURNING message_id", (now,)))
            removed = sorted(r["message_id"] for r in self._all(
                "DELETE FROM coordination_messages WHERE created_at<=%s "
                "AND expires_at<=%s RETURNING message_id", (now - DEDUPE_RETENTION, now)))
            # The retention window runs from the later of the address's own
            # last action and its last lease. A lapsed lease may belong to a
            # live shim cut off by a restart or a host sleep, whose recovery
            # re-attach does not refresh last_activity; only a lease gone for
            # the whole window, or a detach, says the process has ended.
            agents = sorted(r["agent_id"] for r in self._all(
                "DELETE FROM coordination_agents a WHERE " + removable
                + " AND NOT EXISTS (SELECT 1 FROM coordination_leases l "
                "WHERE l.holder_agent_id=a.agent_id) RETURNING a.agent_id", retention))
            # A removed address leaves no place in any queue (the lease tables
            # carry no foreign keys; see the v45 DDL).
            self.storage.conn.execute(
                "DELETE FROM coordination_lease_waiters w WHERE NOT EXISTS "
                "(SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id)")
            # A lease row outlives each hold so its fence keeps rising; one
            # left free and unqueued for the request-key window is forgotten,
            # well past the longest hold (a day) whose fence could matter.
            leases = sorted(r["name"] for r in self._all(
                "DELETE FROM coordination_leases l WHERE l.holder_agent_id IS NULL "
                "AND l.freed_at<=%s AND NOT EXISTS (SELECT 1 FROM coordination_lease_waiters w "
                "WHERE w.name=l.name) RETURNING l.name", (now - DEDUPE_RETENTION,)))
            if expired:
                events.append(self._event("expire", {"message_ids": expired}, actor="daemon"))
            if removed or agents or leases:
                pruned = {"message_ids": removed, "agent_ids": agents}
                if leases:
                    pruned["leases"] = leases
                events.append(self._event("prune", pruned, actor="daemon"))
            head, audit_removed = None, 0
            if audit_retention_days:
                cutoff = audit_cutoff(now, audit_retention_days)
                # The head is read before the cut: when every row is older than
                # the window the head itself goes, and the chain must continue
                # from it rather than restart at genesis.
                head = self._chain_head()
                # Writers sample time before taking the chain lock, so
                # timestamps need not follow sequence order. Only remove an
                # expired prefix; an older row after a recent one must wait.
                cut = self._one(
                    "WITH retained AS (SELECT min(seq) AS first_seq FROM coordination_events "
                    "WHERE created_at>=%s) SELECT seq,hash FROM coordination_events,retained "
                    "WHERE retained.first_seq IS NULL OR seq<retained.first_seq "
                    "ORDER BY seq DESC LIMIT 1", (cutoff,))
                if cut is not None:
                    audit_removed = self.storage.conn.execute(
                        "DELETE FROM coordination_events WHERE seq<=%s", (cut["seq"],)).rowcount
                    events.append(self._event(
                        "audit_prune", {"through_seq": cut["seq"], "through_hash": cut["hash"],
                                        "removed": audit_removed, "cutoff": cutoff,
                                        "retention_days": audit_retention_days},
                        actor="daemon"))
            self._append(events, now, head=head)
        return {"bodies_expired": len(expired), "removed": len(removed),
                "agents_removed": len(agents), "audit_removed": audit_removed}

    def recover(self):
        """Operator-only restore reset; never call on an ordinary restart.

        ``audited`` is False when the restored bank predates the audit log;
        the revocation still happens, unrecorded."""
        with self.storage._txn():
            revoked = sorted(r["agent_id"] for r in self._all(
                "UPDATE coordination_agents SET credential_hash=NULL,"
                "wake_enabled=FALSE,attachment_id=NULL,lease_until=NULL,generation=generation+1,"
                "lifecycle='revoked' RETURNING agent_id"))
            # Every credential is revoked, so no holder can renew or release
            # and no waiter can take a turn up: free every lease and empty
            # every queue. A bank restored from before v45 has neither table.
            freed, waiters = [], 0
            if self._one("SELECT to_regclass('coordination_leases') IS NOT NULL "
                         "AS present")["present"]:
                now = self.clock()
                freed = sorted(r["name"] for r in self._all(
                    "UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',"
                    "purpose='',acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,"
                    "freed_at=%s WHERE holder_agent_id IS NOT NULL RETURNING name", (now,)))
                waiters = self.storage.conn.execute(
                    "DELETE FROM coordination_lease_waiters").rowcount
            audited = self._audit_present()
            if audited:
                recovered = {"agent_ids": revoked}
                if freed or waiters:
                    recovered.update(leases_freed=freed, waiters_removed=waiters)
                self._append([self._event("recover", recovered, actor="operator")],
                             self.clock())
        result = {"revoked": len(revoked), "audited": audited}
        if freed or waiters:
            result.update(leases_freed=len(freed), waiters_removed=waiters)
        return result

    def rebind(self, agent_id, principal):
        """Operator-only reissue to the same owner, retaining pending mail."""
        credential = secrets.token_urlsafe(32)
        with self.storage._txn():
            row = self._one("SELECT * FROM coordination_agents WHERE agent_id=%s FOR UPDATE", (agent_id,))
            if row is None or row["principal"] != principal or row["credential_hash"] is not None:
                raise CoordinationError("invalid_rebind")
            self.storage.conn.execute("UPDATE coordination_agents SET credential_hash=%s,"
                                      "lifecycle='registered' WHERE agent_id=%s", (_hash(credential), agent_id))
            audited = self._audit_present()
            if audited:
                self._append([self._event("rebind", {"principal": principal}, actor="operator",
                                          agent_id=agent_id, project=row["project"],
                                          task=row["task"])], self.clock())
            result = self.authenticate(principal, agent_id, credential)
        return {**result, "credential": credential, "audited": audited}

    def redact(self, message_id, reason):
        """Operator only (``pseudolife-mcp board-audit redact``; never exposed
        to agents): remove one message body from the board.

        In one transaction: blank the ``body`` and ``body_salt`` of the
        message's send event, blank the live copy (and its request
        fingerprint, a digest of the text kept seven days for retries, so a
        retry of the request is refused as ``request_conflict``) if prune has
        not already, end its delivery,
        and append a chained ``redact`` event (actor ``operator``) whose
        payload names the send event's ``seq``, the operator's reason, and
        ``audit_copy: removed``. The send event's hashed payload keeps the
        salted commitment, so the chain still verifies, and
        ``verify_audit_chain`` accepts the absent body only behind that
        event. A message sent before v46 has its body inside the hashed
        payload, which cannot change; while its live copy is still there, that
        copy is blanked and taken out of delivery all the same, and the event
        says ``audit_copy: kept``. Copies outside the bank (a backup, an
        export, what the recipient already read) are untouched.

        Returns the send event's ``seq``, the redact event's ``redact_seq``
        and ``redact_hash`` (with ``expect_head``, the two as ``verify
        --expect-head`` takes them: recorded outside the bank, they catch a
        later removal of this record), ``live_body_cleared`` and
        ``audit_copy``.

        Refused: ``invalid_message_id``, ``invalid_reason`` (blank, over
        MAX_REDACT_REASON characters, or holding a control, format or
        line/paragraph separator character), ``secret_like_body`` (the reason
        is hashed for good), ``message_not_found`` (no send event for the id:
        unknown, or removed by audit retention), ``body_in_hashed_payload``
        (sent before v46 and no live copy left to take), ``already_redacted``."""
        if (not isinstance(message_id, str) or not message_id or len(message_id) > 120
                or any(c not in _ID_CHARS for c in message_id)):
            raise CoordinationError("invalid_message_id")
        if (not isinstance(reason, str) or not reason.strip()
                or len(reason) > MAX_REDACT_REASON
                or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")
                       for c in reason)):
            raise CoordinationError("invalid_reason")
        _refuse_secret(reason)
        with self.storage._txn():
            now = self.clock()
            # The board row first, as every mutation takes it.
            live = self._one("SELECT text FROM coordination_messages WHERE message_id=%s "
                             "FOR UPDATE", (message_id,))
            # Then the chain lock, before any log row is read or changed:
            # prune deletes the log's oldest rows while holding it, so a
            # redaction that locked the send event's row first could wait on
            # prune while prune waits on it.
            head = self._chain_head()
            sent = self._one(
                "SELECT seq,payload,agent_id,recipient_agent_id,project,task "
                "FROM coordination_events WHERE event='send' AND message_id=%s "
                "ORDER BY seq LIMIT 1", (message_id,))
            if sent is None:
                # Audit retention under the seven days a mailbox row keeps
                # its request fingerprint can cut the send event first; the
                # fingerprint is still a digest of the body, so take it (and
                # any live text) and log that the audit copy was gone.
                live_row = self._one("SELECT text,fingerprint,sender_agent_id,"
                                     "recipient_agent_id,project,task FROM coordination_messages "
                                     "WHERE message_id=%s", (message_id,))
                if live_row is None:
                    raise CoordinationError("message_not_found")
                if live_row["text"] is None and live_row["fingerprint"] == "redacted":
                    raise CoordinationError("already_redacted")
                self.storage.conn.execute(
                    "UPDATE coordination_messages SET text=NULL,fingerprint='redacted',"
                    "expires_at=LEAST(expires_at,%s) WHERE message_id=%s", (now, message_id))
                seq, digest = self._append([self._event(
                    "redact", {"message_id": message_id, "seq": None, "reason": reason,
                               "audit_copy": "gone"},
                    actor="operator", agent_id=live_row["sender_agent_id"],
                    recipient=live_row["recipient_agent_id"], project=live_row["project"],
                    task=live_row["task"], message_id=message_id)], now, head=head)
                return {"message_id": message_id, "seq": None, "redact_seq": seq,
                        "redact_hash": digest, "expect_head": f"{seq}:{digest}",
                        "live_body_cleared": live_row["text"] is not None,
                        "audit_copy": "gone"}
            payload = _parsed_payload(sent)
            if payload is None or "text_commitment" not in payload:
                # Sent before v46: the audit copy stays. Decided before the
                # body column is touched, so a restored v42-v45 bank that has
                # none yet takes this path too.
                if live is None or live["text"] is None:
                    raise CoordinationError("body_in_hashed_payload")
                audit_copy = "kept"
            elif self._one("UPDATE coordination_events SET body=NULL,body_salt=NULL "
                           "WHERE seq=%s AND (body IS NOT NULL OR body_salt IS NOT NULL) "
                           "RETURNING seq", (sent["seq"],)) is None:
                raise CoordinationError("already_redacted")
            else:
                audit_copy = "removed"
            cleared = False
            if live is not None:
                cleared = live["text"] is not None
                self.storage.conn.execute(
                    "UPDATE coordination_messages SET text=NULL,fingerprint='redacted',"
                    "expires_at=LEAST(expires_at,%s) WHERE message_id=%s", (now, message_id))
            seq, digest = self._append([self._event(
                "redact", {"message_id": message_id, "seq": sent["seq"], "reason": reason,
                           "audit_copy": audit_copy},
                actor="operator", agent_id=sent["agent_id"],
                recipient=sent["recipient_agent_id"], project=sent["project"],
                task=sent["task"], message_id=message_id)], now, head=head)
        return {"message_id": message_id, "seq": sent["seq"], "redact_seq": seq,
                "redact_hash": digest, "expect_head": f"{seq}:{digest}",
                "live_body_cleared": cleared, "audit_copy": audit_copy}
