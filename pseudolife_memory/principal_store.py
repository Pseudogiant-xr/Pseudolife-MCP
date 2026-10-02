"""Principals stored in the bank (spec 2026-10-02, Part 2b; schema v53).

``pseudolife-mcp invite <name>`` creates a row in ``principals`` with the
SHA-256 of a short-lived pairing code; ``pseudolife-mcp pair`` redeems the
code at ``POST /api/pair`` with the SHA-256 of a token it minted itself.
Neither a token nor a code is ever stored, logged or returned in plaintext.

This module holds the three parts that touch the table:

* :class:`PrincipalSnapshot`, the daemon's in-memory view, keyed by token
  hash, that :func:`principals.resolve_principal` reads. Resolution never
  touches the database: an unknown bearer costs one dict lookup.
* :class:`PrincipalRefresher`, the background thread that reloads the
  snapshot every 10 s on its own connection. It never takes the service or
  coordination lock and never runs on the event loop.
* the operations: :func:`redeem` (the endpoint's one conditional UPDATE,
  single use under concurrency, on the database clock) and the ``invite``
  operations (:func:`create_invite`, :func:`list_principals`,
  :func:`revoke`).

The snapshot itself is standard library only; psycopg is imported by the
functions that use a connection.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import logging
import secrets
import threading
import time
from typing import Callable, Iterable

from pseudolife_memory.principals import (
    PAIRING_CODE_ALPHABET, PAIRING_CODE_LENGTH, RESERVED_PRINCIPALS, secret_sha256,
)

logger = logging.getLogger("pseudolife-mcp.principals")

# Design bounds from the spec (2026-10-02), not measurements: a revocation
# lands within one refresh; past the staleness limit a non-environment
# bearer is answered 503 rather than resolved from an old view.
REFRESH_INTERVAL_S = 10.0
STALE_AFTER_S = 60.0
# How long a redeemed code still answers an idempotent retry from the
# client that minted the token.
RETRY_WINDOW_S = 600.0
# Global failed-redemption budget (spec: per-address limits are one bucket
# behind tailscale serve or Docker's bridge).
FAILED_REDEMPTIONS_PER_MINUTE = 20
DEFAULT_CODE_TTL_S = 15 * 60
MAX_CODE_TTL_S = 24 * 3600

_TIERS = ("minimal", "core", "full")
_NOW = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"


@dataclass(frozen=True)
class StoredPrincipal:
    principal: str
    token_hash: str | None
    tier: str | None
    board: bool
    revoked: bool


def bank_fingerprint(bank_id) -> str | None:
    """``/health``'s ``bank``: the first 16 hex of the SHA-256 of the
    coordination bank id, or ``None`` when there is none."""
    if not isinstance(bank_id, str) or not bank_id:
        return None
    return hashlib.sha256(bank_id.encode("utf-8")).hexdigest()[:16]


class PrincipalSnapshot:
    """The daemon's view of ``principals``. Thread-safe; every read is a
    dict lookup under a short lock.

    Rows whose name the environment already uses (``shadowed``, plus the
    reserved ``default`` and ``daemon``) are left out: the environment
    always wins, so such a row neither authenticates nor admits.

    :meth:`add` is the redemption's immediate entry. It is kept until a
    refresh that *started* after it completes, so a refresh whose read began
    before the redemption committed cannot drop it."""

    def __init__(self, *, shadowed: Iterable[str] = (), max_age: float = STALE_AFTER_S,
                 clock: Callable[[], float] | None = None):
        self._lock = threading.Lock()
        self._shadowed = frozenset(shadowed) | RESERVED_PRINCIPALS
        self._max_age = max_age
        self._clock = clock or time.monotonic
        self._rows: dict[str, StoredPrincipal] = {}
        self._by_hash: dict[str, StoredPrincipal] = {}
        self._pending: dict[str, tuple[int, StoredPrincipal]] = {}
        self._seq = 0
        self._loaded_at: float | None = None
        self._shadowed_rows: list[str] = []
        self.bank: str | None = None

    # -- maintenance -------------------------------------------------------

    def refresh(self, load: Callable[[], tuple[list[StoredPrincipal], str | None]]) -> None:
        """Replace the view with ``load()``'s rows. A failing ``load``
        raises and changes nothing, so the view ages towards stale."""
        with self._lock:
            started_seq = self._seq
        started_at = self._clock()
        rows, bank = load()
        with self._lock:
            # Adds made after this read began are newer than it.
            self._pending = {name: entry for name, entry in self._pending.items()
                             if entry[0] > started_seq}
            merged: dict[str, StoredPrincipal] = {}
            shadowed = set()
            for row in rows:
                if row.principal in self._shadowed:
                    shadowed.add(row.principal)
                    continue
                merged[row.principal] = _clean(row)
            for _seq, row in self._pending.values():
                merged[row.principal] = row
            self._rows = merged
            self._rebuild()
            self._shadowed_rows = sorted(shadowed)
            self._loaded_at = started_at
            self.bank = bank

    def add(self, row: StoredPrincipal) -> None:
        """A redemption that just committed: visible at once, replacing the
        principal's previous token hash (``invite --replace``)."""
        if row.principal in self._shadowed:
            return
        row = _clean(row)
        with self._lock:
            self._seq += 1
            self._pending[row.principal] = (self._seq, row)
            self._rows[row.principal] = row
            self._rebuild()

    def _rebuild(self) -> None:
        self._by_hash = {row.token_hash: row for row in self._rows.values()
                         if row.token_hash and not row.revoked}

    # -- reads -------------------------------------------------------------

    def available(self) -> bool:
        with self._lock:
            loaded = self._loaded_at
        return loaded is not None and self._clock() - loaded <= self._max_age

    def lookup(self, token_hash: str) -> StoredPrincipal | None:
        with self._lock:
            return self._by_hash.get(token_hash)

    def has(self, principal: str) -> bool:
        with self._lock:
            return principal in self._rows

    def admitted(self, principal: str) -> bool:
        with self._lock:
            row = self._rows.get(principal)
        return row is not None and row.board and not row.revoked

    def tier_of(self, principal: str) -> str | None:
        with self._lock:
            row = self._rows.get(principal)
        return None if row is None or row.revoked else row.tier

    @property
    def excluded_names(self) -> frozenset[str]:
        """Names a redemption must not pair: the environment's and the
        reserved ones. A row carrying one would be ignored anyway, so its
        code is refused rather than spent."""
        return self._shadowed

    @property
    def shadowed_rows(self) -> list[str]:
        with self._lock:
            return list(self._shadowed_rows)

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


def _clean(row: StoredPrincipal) -> StoredPrincipal:
    if row.tier is None or row.tier in _TIERS:
        return row
    return StoredPrincipal(row.principal, row.token_hash, None, row.board, row.revoked)


# -- the database side ---------------------------------------------------------

def _connect(dsn: str, *, autocommit: bool):
    from pseudolife_memory.storage.postgres import connect_retrying_local_ports
    return connect_retrying_local_ports(dsn, connect_timeout=5, autocommit=autocommit)


def load_rows(conn) -> tuple[list[StoredPrincipal], str | None]:
    """Every row of ``principals`` and the bank fingerprint, on an
    autocommit connection. A missing table (a v53 daemon whose storage has
    not started, so its schema has not been applied) is an empty table.
    Never runs DDL."""
    import psycopg

    from pseudolife_memory.storage.coordination import BANK_ID_META_KEY

    try:
        rows = conn.execute(
            "SELECT principal, token_hash, tier, board, revoked_at IS NOT NULL "
            "FROM public.principals").fetchall()
    except psycopg.errors.UndefinedTable:
        rows = []
    try:
        found = conn.execute("SELECT value FROM public.meta WHERE key = %s",
                             (BANK_ID_META_KEY,)).fetchone()
    except psycopg.errors.UndefinedTable:
        found = None
    return ([StoredPrincipal(principal=r[0], token_hash=r[1], tier=r[2], board=bool(r[3]),
                             revoked=bool(r[4])) for r in rows],
            bank_fingerprint(found[0] if found else None))


class PrincipalRefresher:
    """Reloads a :class:`PrincipalSnapshot` every ``interval`` seconds on a
    connection of its own, opened from the daemon's DSN. It holds no
    reference to the service, so it cannot take the service lock or the
    coordination lock, and it runs on its own thread, never the event loop.

    Startup warnings (logged once, after the first successful load): rows
    exist on a daemon with no authentication configured, or a row's name is
    shadowed by the environment."""

    def __init__(self, dsn: str, snapshot: PrincipalSnapshot, *,
                 auth_configured: bool, interval: float = REFRESH_INTERVAL_S):
        self._dsn = dsn
        self._snapshot = snapshot
        self._auth_configured = auth_configured
        self._interval = interval
        self._conn = None
        self._failing = False
        self._warned = False
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def _read(self):
        if self._conn is None or self._conn.closed:
            self._conn = _connect(self._dsn, autocommit=True)
            self._conn.execute("SET statement_timeout = '5s'")
        return load_rows(self._conn)

    def refresh_once(self) -> bool:
        try:
            self._snapshot.refresh(self._read)
        except Exception as exc:  # noqa: BLE001 - the view ages to stale; the type only
            if not self._failing:
                logger.warning("stored principals: refresh failed (%s); bearers that match "
                               "nothing in the environment get 503 once the view is %ds old",
                               type(exc).__name__, int(STALE_AFTER_S))
            self._failing = True
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return False
        if self._failing:
            logger.info("stored principals: refresh recovered")
        self._failing = False
        if not self._warned:
            self._warned = True
            self._startup_warnings()
        return True

    def _startup_warnings(self) -> None:
        count = len(self._snapshot)
        if count and not self._auth_configured:
            logger.warning(
                "the bank stores %d invited principal(s), but this daemon has no "
                "PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS: stored principals do not turn "
                "authentication on, and on an open daemon they mean nothing", count)
        for name in self._snapshot.shadowed_rows:
            logger.warning(
                "stored principal %r is shadowed by the environment (PSEUDOLIFE_MCP_TOKENS, or "
                "a reserved name): its stored token is ignored; revoke it with "
                "`pseudolife-mcp invite --revoke %s`", name, name)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            self._stop.wait(self._interval)

    def start(self) -> threading.Thread:
        self.thread = threading.Thread(target=self._run, name="pl-principals", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self) -> None:
        self._stop.set()


# Redemptions in flight at once (security review, 2026-10-02): each opens
# a database connection, so a burst of bad codes must not fan out across
# the shared executor. Two lets a client's retry run beside one slow
# attempt; anything beyond is answered 429 at once. A design bound, not a
# measurement.
REDEMPTION_SLOTS = 2


class FailureLimiter:
    """The endpoint's global budget of failed redemptions: at most ``limit``
    in any ``window`` seconds. Every attempt is counted atomically before it
    goes near the store (:meth:`reserve`), so concurrent bad codes cannot
    all slip under the budget; a success, or an attempt that never reached
    the store, is given back (:meth:`refund`)."""

    def __init__(self, limit: int = FAILED_REDEMPTIONS_PER_MINUTE, window: float = 60.0,
                 clock: Callable[[], float] | None = None):
        self._limit = limit
        self._window = window
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._failures and now - self._failures[0] >= self._window:
            self._failures.popleft()

    def reserve(self) -> float | None:
        """Count one attempt and return its stamp, or ``None`` when the
        budget is spent (answer 429)."""
        with self._lock:
            now = self._clock()
            self._trim(now)
            if len(self._failures) >= self._limit:
                return None
            self._failures.append(now)
            return now

    def refund(self, stamp: float) -> None:
        """Give back an attempt that succeeded or never reached the store."""
        with self._lock:
            try:
                self._failures.remove(stamp)
            except ValueError:
                pass    # already aged out of the window


class RedemptionGate:
    """At most ``slots`` redemptions in flight; a full gate is a refusal
    (429), never a queue. Redemptions run on the gate's own executor, so
    they never take threads from the daemon's shared pool."""

    def __init__(self, slots: int = REDEMPTION_SLOTS):
        from concurrent.futures import ThreadPoolExecutor
        self._slots = slots
        self._lock = threading.Lock()
        self._in_flight = 0
        self.executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="pl-pairing")

    def try_enter(self) -> bool:
        with self._lock:
            if self._in_flight >= self._slots:
                return False
            self._in_flight += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._in_flight -= 1


def redeem(dsn: str, code_hash: str, token_hash: str, *,
           excluded: Iterable[str] = ()) -> StoredPrincipal | None:
    """Redeem a pairing code: the matching pending row takes ``token_hash``
    and the code is spent, in one conditional UPDATE on the database clock,
    so two concurrent redeemers cannot both win. ``None`` is a refusal of
    any kind (unknown, expired, used, revoked, the hash already in use).

    A repeat with the same code and the same token hash within
    :data:`RETRY_WINDOW_S` of the pairing returns the same row: only the
    client that minted the token knows that hash, so a lost response is
    recoverable and nothing else is. A row named in ``excluded`` (a name
    the environment or a reservation already uses) is refused without
    spending its code. Database errors propagate."""
    import psycopg

    with _connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL statement_timeout = '5s'")
        try:
            row = conn.execute(
                f"""
                UPDATE public.principals
                   SET token_hash = %(token)s, paired_at = {_NOW},
                       paired_code_hash = code_hash, code_hash = NULL, code_expires_at = NULL
                 WHERE code_hash = %(code)s AND code_expires_at > {_NOW} AND revoked_at IS NULL
                   AND NOT (principal = ANY(%(excluded)s))
             RETURNING principal, tier, board
                """, {"token": token_hash, "code": code_hash,
                      "excluded": sorted(excluded)}).fetchone()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return None
        if row is None:
            row = conn.execute(
                f"""
                SELECT principal, tier, board FROM public.principals
                 WHERE paired_code_hash = %(code)s AND token_hash = %(token)s
                   AND revoked_at IS NULL AND paired_at > {_NOW} - %(window)s
                """, {"token": token_hash, "code": code_hash, "window": RETRY_WINDOW_S}).fetchone()
        # A spent code answers retries for the window only.
        conn.execute(
            f"UPDATE public.principals SET paired_code_hash = NULL "
            f"WHERE paired_code_hash IS NOT NULL AND paired_at <= {_NOW} - %s",
            (RETRY_WINDOW_S,))
        conn.commit()
    if row is None:
        return None
    return StoredPrincipal(principal=row[0], token_hash=token_hash, tier=row[1],
                           board=bool(row[2]), revoked=False)


# -- invite's operations (out of process, on the daemon host) -------------------

class InviteRefused(Exception):
    """An invite the table's state refuses; the message is for the operator."""


def new_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def create_invite(conn, name: str, *, tier: str | None, board: bool, ttl_seconds: float,
                  replace: bool) -> dict:
    """Create or reset ``name``'s row with a new pending code and return
    ``{"code", "expires_at", "state"}`` (``state``: the row before:
    ``new``, ``pending``, ``revoked`` or ``paired``). The code is returned
    once, here, and only its hash is written. A paired name needs
    ``replace``: its old token keeps working until the new code is
    redeemed. A revoked name is reset (token and revocation cleared)."""
    import psycopg

    for _attempt in range(5):
        code = new_pairing_code()
        code_hash = secret_sha256(code)
        try:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT token_hash IS NOT NULL, revoked_at IS NOT NULL "
                    "FROM public.principals WHERE principal = %s FOR UPDATE", (name,)).fetchone()
                params = {"name": name, "tier": tier, "board": board, "code": code_hash,
                          "ttl": float(ttl_seconds)}
                if existing is None:
                    state = "new"
                    row = conn.execute(
                        f"""
                        INSERT INTO public.principals
                            (principal, tier, board, code_hash, code_expires_at, created_at)
                        VALUES (%(name)s, %(tier)s, %(board)s, %(code)s, {_NOW} + %(ttl)s, {_NOW})
                        RETURNING code_expires_at
                        """, params).fetchone()
                else:
                    paired, revoked = existing
                    state = "revoked" if revoked else "paired" if paired else "pending"
                    if state == "paired" and not replace:
                        raise InviteRefused(
                            f"{name} is already paired; pass --replace to give it a new code (its "
                            "current token keeps working until the new code is redeemed), or "
                            f"--revoke {name} first")
                    # A revoked name starts again: token and revocation cleared.
                    reset = (", token_hash = NULL, paired_at = NULL, revoked_at = NULL"
                             if state == "revoked" else "")
                    row = conn.execute(
                        f"""
                        UPDATE public.principals
                           SET tier = %(tier)s, board = %(board)s,
                               code_hash = %(code)s, code_expires_at = {_NOW} + %(ttl)s,
                               paired_code_hash = NULL{reset}
                         WHERE principal = %(name)s
                     RETURNING code_expires_at
                        """, params).fetchone()
        except psycopg.errors.UniqueViolation:
            continue    # a code-hash collision: draw again
        return {"code": code, "expires_at": row[0], "state": state}
    raise InviteRefused("could not draw an unused pairing code; try again")


def list_principals(conn) -> list[dict]:
    """Every stored principal, for ``invite --list``: never a token or a
    code, only whether one is set and when the pending code expires."""
    rows = conn.execute(
        f"""
        SELECT principal, tier, board, token_hash IS NOT NULL, code_hash IS NOT NULL,
               code_expires_at, created_at, paired_at, revoked_at, {_NOW}
          FROM public.principals ORDER BY principal
        """).fetchall()
    out = []
    for (name, tier, board, paired, pending, expires, created, paired_at, revoked, now) in rows:
        if revoked is not None:
            state = "revoked"
        elif pending and expires is not None and expires > now:
            state = "pending"
        elif pending:
            state = "expired"
        elif paired:
            state = "paired"
        else:
            state = "unpaired"
        out.append({"principal": name, "state": state, "paired": bool(paired),
                    "tier": tier, "board": bool(board),
                    "code_expires_at": expires if pending else None,
                    "created_at": created, "paired_at": paired_at, "revoked_at": revoked})
    return out


def revoke(conn, name: str) -> bool:
    """Revoke ``name``: set ``revoked_at`` and clear any pending code. The
    daemon drops it at its next refresh. False when there is no such row."""
    with conn.transaction():
        row = conn.execute(
            f"""
            UPDATE public.principals
               SET revoked_at = COALESCE(revoked_at, {_NOW}),
                   code_hash = NULL, code_expires_at = NULL, paired_code_hash = NULL
             WHERE principal = %s
         RETURNING principal
            """, (name,)).fetchone()
    return row is not None
