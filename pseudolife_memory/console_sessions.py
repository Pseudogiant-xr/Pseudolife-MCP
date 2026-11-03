"""Console sign-in sessions (schema v59; spec 2026-11-02, sections 3 and 4).

A **Console session** is a bearer the browser minted and holds in
localStorage, which the daemon resolves to an existing principal. The
browser sends only the SHA-256 of its token, so the daemon never sees the
token itself, as with pairing.

This module holds:

* :class:`SessionSnapshot`, the daemon's in-memory view of
  ``console_sessions``, keyed by token hash. The principals refresher
  (``principal_store.PrincipalRefresher``) reloads it on its own connection
  every 10 s, so resolving a bearer never touches the database.
* :func:`resolve`, the session resolver. Only the call sites the spec names
  use it: the ``/api`` transport gate (except ``/api/coordination/*``,
  ``/api/hook/*``, ``/api/console/link`` and ``/api/daemon-notice``), and
  the board snapshot and maintainer routes behind it. Hooks, ``/mcp`` and
  the board's mailbox routes never do, so a session can never register an
  agent or send mail.
* the operations behind the routes: :func:`issue_code` (``POST
  /api/console/link``), :func:`redeem_code` (``POST /api/console/session``),
  :func:`list_sessions` and :func:`revoke_session`, and
  :func:`revoke_principal_sessions`, which revoking or re-pairing a
  principal calls in its own transaction.

The snapshot and the resolver are standard library only; psycopg is
imported by the functions that use a connection.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import secrets
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit

from pseudolife_memory.principals import (
    DEFAULT_PRINCIPAL, SOURCE_ENVIRONMENT, SOURCE_STORE, PrincipalsUnavailable,
    presented_bearer, secret_sha256,
)

logger = logging.getLogger("pseudolife-mcp.console-sessions")

# Lifetimes from the spec (2026-11-02, section 3). Conventions, not
# measurements: the spec lists them as an open question for review.
SESSION_IDLE_S = 30 * 86400
SESSION_MAX_S = 90 * 86400
CODE_TTL_S = 5 * 60
# A session's last use is written back at most this often (the refresher's
# activity cadence), so a busy Console costs one UPDATE a minute.
USE_FLUSH_S = 60.0
# How long a spent or expired code row is kept before issue_code prunes it.
CODE_KEEP_S = 86400

METHOD_LINK = "link"
METHOD_PASSKEY = "passkey"
METHODS = (METHOD_LINK, METHOD_PASSKEY)

LABEL_MAX = 64
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# A Console route id a sign-in link may land on (``&to=``). The Console
# checks it against its own routes; the daemon only checks its shape.
_ROUTE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
_NOW = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"


# -- origins -------------------------------------------------------------------

def normalize_origin(value) -> str | None:
    """The origin a browser would send for ``value``, or ``None`` when it is
    not a plain ``http``/``https`` origin: the scheme and host lower-cased,
    the scheme's default port and a trailing slash dropped. A path, query,
    fragment or credentials are refused, so a stored origin always equals
    what a browser's ``Origin`` header carries."""
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or "#" in value:
        return None
    host = parts.hostname
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port == {"http": 80, "https": 443}[scheme]:
        port = None
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def origin_key(origin: str) -> str:
    """The form a code's origin is stored and compared in: a loopback
    origin's host is ``localhost`` whichever alias names it (``127.0.0.1``,
    ``[::1]``), so a code made through one redeems in a Console opened at
    another. Safe because a page from any other site never sends a loopback
    ``Origin``; scheme and port still have to match."""
    parts = urlsplit(origin)
    if (parts.hostname or "") in _LOOPBACK_HOSTS:
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://localhost{port}"
    return origin


def is_loopback_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in _LOOPBACK_HOSTS


def valid_route_id(value) -> bool:
    return isinstance(value, str) and _ROUTE_ID.fullmatch(value) is not None


def clean_label(value) -> str | None:
    """A session label as stored: printable text, at most
    :data:`LABEL_MAX` characters, or ``None`` when ``value`` is not one."""
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    if len(label) > LABEL_MAX or any(not ch.isprintable() for ch in label):
        return None
    return label


def bearer_hashes(auth_header: str | None) -> list[str]:
    """The SHA-256 hex of the presented bearer, as the resolver judges it:
    its UTF-8 bytes, then its latin-1 bytes when those differ (header text
    is latin-1-decoded, so a UTF-8 token arrives as its latin-1 text)."""
    presented = presented_bearer(auth_header)
    if presented is None:
        return []
    candidates = [presented.encode("utf-8")]
    try:
        candidates.append(presented.encode("latin-1"))
    except UnicodeEncodeError:
        pass
    return list(dict.fromkeys(hashlib.sha256(c).hexdigest() for c in candidates))


def environment_issuers(token_map: dict[str, str], single_token: str | None) -> dict[str, str]:
    """``{sha256(token): principal}`` for the daemon's environment tokens:
    a link session issued by one of them resolves only while that same
    token still names the same principal."""
    out = {secret_sha256(tok): principal for tok, principal in (token_map or {}).items()}
    if single_token:
        out.setdefault(secret_sha256(single_token), DEFAULT_PRINCIPAL)
    return out


# -- the snapshot -----------------------------------------------------------------

@dataclass(frozen=True)
class ConsoleSession:
    session_id: str
    token_hash: str
    principal: str
    source: str             # the issuing credential's: environment or store
    issuer: str             # sha256 of the issuing bearer, or a passkey credential id
    method: str             # link or passkey
    label: str
    created_at: float
    last_used_at: float | None
    expires_at: float


class SessionSnapshot:
    """The daemon's view of the live rows of ``console_sessions`` (not
    revoked, not past their absolute expiry). Thread-safe; every read is a
    dict lookup under a short lock. Like ``PrincipalSnapshot``, an
    :meth:`add` (a sign-in that just committed) or a :meth:`drop` (a
    revocation that just committed) holds until a refresh that started
    after it completes, so a refresh whose read began earlier cannot undo
    it. Uses are counted in memory (:meth:`note_use`) and written back by
    the refresher (:meth:`take_uses`)."""

    def __init__(self, *, max_age: float = 60.0, clock: Callable[[], float] | None = None):
        self._lock = threading.Lock()
        self._max_age = max_age
        self._clock = clock or time.monotonic
        self._by_hash: dict[str, ConsoleSession] = {}
        self._added: dict[str, tuple[int, ConsoleSession]] = {}
        self._dropped: dict[str, int] = {}
        self._used: dict[str, float] = {}
        self._unflushed: dict[str, float] = {}
        self._seq = 0
        self._loaded_at: float | None = None
        self.active_passkeys: frozenset[str] = frozenset()
        # Keys a passkey sign-in just proved active, and keys just revoked:
        # held, like _added and _dropped, until a refresh that started after.
        self._keys_added: dict[str, int] = {}
        self._keys_dropped: dict[str, int] = {}

    def refresh(self, load: Callable[[], tuple[list[ConsoleSession], Iterable[str]]]) -> None:
        """Replace the view with ``load()``'s ``(sessions, active passkey
        credential ids)``. A failing ``load`` raises and changes nothing."""
        with self._lock:
            started_seq = self._seq
        started_at = self._clock()
        rows, passkeys = load()
        with self._lock:
            self._added = {k: v for k, v in self._added.items() if v[0] > started_seq}
            self._dropped = {k: v for k, v in self._dropped.items() if v > started_seq}
            self._keys_added = {k: v for k, v in self._keys_added.items() if v > started_seq}
            self._keys_dropped = {k: v for k, v in self._keys_dropped.items()
                                  if v > started_seq}
            merged = {row.token_hash: row for row in rows
                      if row.session_id not in self._dropped}
            for _seq, row in self._added.values():
                merged[row.token_hash] = row
            self._by_hash = merged
            live = {row.session_id for row in merged.values()}
            # Uses of sessions that are gone have nothing left to extend.
            self._used = {sid: t for sid, t in self._used.items() if sid in live}
            self.active_passkeys = (frozenset(passkeys) | frozenset(self._keys_added)) - frozenset(
                self._keys_dropped)
            self._loaded_at = started_at

    def add(self, row: ConsoleSession) -> None:
        """A sign-in that just committed: usable at once, unless a
        revocation of it already committed and was dropped here. A
        redemption publishes after its COMMIT, so an operator can list and
        revoke the session in between; the late publish must not bring it
        back."""
        with self._lock:
            if row.session_id in self._dropped:
                return
            self._seq += 1
            self._added[row.session_id] = (self._seq, row)
            self._by_hash[row.token_hash] = row
            if row.method == METHOD_PASSKEY:
                # complete() just verified the key active: a sign-in in the
                # 10 s after its confirm must not bounce back to the dialog.
                self._keys_added[row.issuer] = self._seq
                self._keys_dropped.pop(row.issuer, None)
                self.active_passkeys = self.active_passkeys | {row.issuer}

    def drop_key(self, credential_id: str) -> None:
        """A key just revoked: its passkey sessions and the key leave the
        view at once."""
        self.drop_issuer(credential_id)
        with self._lock:
            self._seq += 1
            self._keys_dropped[credential_id] = self._seq
            self._keys_added.pop(credential_id, None)
            self.active_passkeys = self.active_passkeys - {credential_id}

    def drop(self, session_ids: Iterable[str]) -> None:
        """Revocations that just committed: gone at once."""
        ids = set(session_ids)
        if not ids:
            return
        with self._lock:
            self._seq += 1
            for sid in ids:
                self._dropped[sid] = self._seq
                self._added.pop(sid, None)
            self._by_hash = {h: row for h, row in self._by_hash.items()
                             if row.session_id not in ids}

    def drop_issuer(self, issuer: str, *, keep_principal: str | None = None) -> None:
        """A key's passkey sessions just revoked (all, or all but those
        signed in as ``keep_principal``)."""
        with self._lock:
            ids = [row.session_id for row in self._by_hash.values()
                   if row.method == METHOD_PASSKEY and row.issuer == issuer
                   and row.principal != keep_principal]
        self.drop(ids)

    def drop_principal(self, principal: str) -> None:
        with self._lock:
            ids = [row.session_id for row in self._by_hash.values() if row.principal == principal]
        self.drop(ids)

    def available(self) -> bool:
        with self._lock:
            loaded = self._loaded_at
        return loaded is not None and self._clock() - loaded <= self._max_age

    def lookup(self, token_hash: str) -> ConsoleSession | None:
        with self._lock:
            return self._by_hash.get(token_hash)

    def rows(self) -> list[ConsoleSession]:
        with self._lock:
            return list(self._by_hash.values())

    def last_used(self, row: ConsoleSession) -> float:
        with self._lock:
            local = self._used.get(row.session_id)
        stored = row.last_used_at if row.last_used_at is not None else row.created_at
        return max(stored, local) if local is not None else stored

    def note_use(self, session_id: str, now: float) -> None:
        with self._lock:
            self._used[session_id] = now
            self._unflushed[session_id] = now

    def take_uses(self) -> dict[str, float]:
        with self._lock:
            out, self._unflushed = self._unflushed, {}
        return out

    def give_back_uses(self, uses: dict[str, float]) -> None:
        """A write that failed: keep its uses for the next one."""
        with self._lock:
            for sid, stamp in uses.items():
                if self._unflushed.get(sid, 0.0) < stamp:
                    self._unflushed[sid] = stamp

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_hash)


# The daemon's snapshot, installed beside the stored-principal one; None
# everywhere else (clients, tests, a daemon without Postgres), where no
# session resolves.
_SESSIONS: SessionSnapshot | None = None


def install_sessions(snapshot: SessionSnapshot | None) -> None:
    global _SESSIONS
    _SESSIONS = snapshot


def installed_sessions() -> SessionSnapshot | None:
    return _SESSIONS


# -- resolution -----------------------------------------------------------------

@dataclass(frozen=True)
class SessionHit:
    principal: str
    issued_by: str          # environment or store
    session: ConsoleSession


def _principal_resolves(principal: str, source: str, env_principals, store) -> bool:
    """Whether ``principal`` still resolves on its own, from ``source``."""
    if source == SOURCE_ENVIRONMENT:
        return principal in env_principals
    if source == SOURCE_STORE:
        if store is None:
            return False
        if not store.available():
            raise PrincipalsUnavailable()
        return store.resolves(principal)
    return False


def resolve(auth_header: str | None, *, sessions: SessionSnapshot | None,
            env_issuers: dict[str, str], env_principals, store,
            now: float | None = None) -> SessionHit | None:
    """The Console session ``auth_header`` presents, or ``None``. A session
    stops resolving, each alone, when it is revoked (not in the snapshot),
    30 days past its last use, 90 days past its creation, when its principal
    no longer resolves on its own, or when its issuer is gone: a link
    session's issuing token no longer names the same principal, or a
    passkey session's key is no longer active.

    Raises :class:`PrincipalsUnavailable` when the answer depends on a view
    that is stale: the session snapshot itself, or the stored-principal
    snapshot for a session a stored principal issued. That is a 503, never
    a 401, which the Console would read as "sign in again"."""
    if sessions is None:
        return None
    hashes = bearer_hashes(auth_header)
    if not hashes:
        return None
    if not sessions.available():
        raise PrincipalsUnavailable()
    row = None
    for token_hash in hashes:
        row = sessions.lookup(token_hash)
        if row is not None:
            break
    if row is None:
        return None
    now = time.time() if now is None else now
    if not still_resolves(row, sessions=sessions, env_issuers=env_issuers,
                          env_principals=env_principals, store=store, now=now):
        return None
    sessions.note_use(row.session_id, now)
    return SessionHit(row.principal, row.source, row)


def still_resolves(row: ConsoleSession, *, sessions: SessionSnapshot, env_issuers: dict[str, str],
                   env_principals, store, now: float) -> bool:
    """Whether ``row`` still signs a browser in: both lifetimes, its issuer
    and its principal (see :func:`resolve`). Raises
    :class:`PrincipalsUnavailable` when that depends on a stale stored view."""
    if now >= row.expires_at or now >= row.created_at + SESSION_MAX_S:
        return False
    if now - sessions.last_used(row) > SESSION_IDLE_S:
        return False
    if row.method == METHOD_LINK:
        if row.source == SOURCE_ENVIRONMENT:
            if env_issuers.get(row.issuer) != row.principal:
                return False
        elif row.source == SOURCE_STORE:
            if store is None:
                return False
            if not store.available():
                raise PrincipalsUnavailable()
            issuing = store.lookup(row.issuer)
            if issuing is None or issuing.principal != row.principal:
                return False
        else:
            return False
    elif row.method == METHOD_PASSKEY:
        if row.issuer not in sessions.active_passkeys:
            return False
    else:
        return False
    return _principal_resolves(row.principal, row.source, env_principals, store)


# -- the database side ---------------------------------------------------------

def _connect(dsn: str, *, autocommit: bool):
    from pseudolife_memory.principal_store import _connect as connect
    return connect(dsn, autocommit=autocommit)


_SESSION_COLUMNS = ("session_id, token_hash, principal, source, issuer, method, label, "
                    "created_at, last_used_at, expires_at")


def _row(r) -> ConsoleSession:
    return ConsoleSession(session_id=r[0], token_hash=r[1], principal=r[2], source=r[3],
                          issuer=r[4], method=r[5], label=r[6] or "", created_at=float(r[7]),
                          last_used_at=None if r[8] is None else float(r[8]),
                          expires_at=float(r[9]))


def load_sessions(conn) -> tuple[list[ConsoleSession], list[str]]:
    """The live sessions and the active passkey credential ids, on an
    autocommit connection. A missing table (a bank before v59 whose schema
    the daemon has not applied yet) reads as empty. Never runs DDL."""
    import psycopg

    try:
        rows = conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM public.console_sessions "
            f"WHERE revoked_at IS NULL AND expires_at > {_NOW}").fetchall()
    except psycopg.errors.UndefinedTable:
        rows = []
    try:
        keys = [r[0] for r in conn.execute(
            "SELECT credential_id FROM public.maintainer_passkeys WHERE state = 'active'")]
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        keys = []
    return [_row(r) for r in rows], keys


def flush_uses(conn, uses: dict[str, float]) -> None:
    """Write sessions' last use (never moving it backwards)."""
    if not uses:
        return
    with conn.transaction():
        for sid, stamp in sorted(uses.items()):
            conn.execute(
                "UPDATE public.console_sessions SET last_used_at = GREATEST("
                "COALESCE(last_used_at, 0), %s) WHERE session_id = %s", (stamp, sid))


def issue_code(dsn: str, *, principal: str, source: str, issuer: str, origin: str) -> dict:
    """A one-time sign-in code for ``principal`` (the pairing-code
    generator: 12 characters, 60 bits), redeemable at ``origin`` for
    :data:`CODE_TTL_S` seconds. Only its hash is stored. Old rows are
    pruned on the way."""
    import psycopg

    from pseudolife_memory.principal_store import new_pairing_code

    with _connect(dsn, autocommit=False) as conn:
        # For the whole connection: the prune commits before the insert.
        conn.execute("SET statement_timeout = '5s'")
        conn.execute(f"DELETE FROM public.console_signin_codes "
                     f"WHERE expires_at < {_NOW} - %s", (CODE_KEEP_S,))
        # Signed-out and expired sessions are kept a day for the record,
        # then go, so the refresher's scan stays small.
        conn.execute(f"DELETE FROM public.console_sessions "
                     f"WHERE revoked_at < {_NOW} - %(keep)s OR expires_at < {_NOW} - %(keep)s",
                     {"keep": CODE_KEEP_S})
        conn.commit()
        for _attempt in range(5):
            code = new_pairing_code()
            try:
                with conn.transaction():
                    row = conn.execute(
                        f"""
                        INSERT INTO public.console_signin_codes
                               (code_hash, principal, source, issuer, origin, expires_at, created_at)
                        VALUES (%s, %s, %s, %s, %s, {_NOW} + %s, {_NOW})
                     RETURNING expires_at
                        """, (secret_sha256(code), principal, source, issuer, origin,
                              CODE_TTL_S)).fetchone()
            except psycopg.errors.UniqueViolation:
                continue
            conn.commit()
            return {"code": code, "expires_at": float(row[0])}
    raise RuntimeError("could not draw an unused sign-in code")


def redeem_code(dsn: str, code_hash: str, token_hash: str, *, origin: str, label: str,
                still_valid: Callable[[str, str, str], bool]) -> ConsoleSession | None:
    """Redeem a sign-in code at ``origin`` for the browser's ``token_hash``:
    the code is spent and the session created in one transaction, on the
    database clock, so two redeemers cannot both win. ``still_valid(
    principal, source, issuer)`` re-checks, inside that transaction, that
    the issuing credential still resolves; a refusal rolls back. ``None``
    is a refusal of any kind (unknown, expired, spent, another origin, the
    hash already in use). Database errors propagate."""
    import psycopg

    with _connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL statement_timeout = '5s'")
        code = conn.execute(
            f"""
            UPDATE public.console_signin_codes SET used_at = {_NOW}
             WHERE code_hash = %s AND used_at IS NULL AND expires_at > {_NOW} AND origin = %s
         RETURNING principal, source, issuer
            """, (code_hash, origin)).fetchone()
        if code is None or not still_valid(code[0], code[1], code[2]):
            conn.rollback()
            return None
        try:
            session = insert_session(conn, token_hash=token_hash, principal=code[0],
                                     source=code[1], issuer=code[2], method=METHOD_LINK,
                                     label=label)
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return None
        conn.commit()
    return session


def list_sessions(dsn: str, *, principal: str | None = None) -> list[dict]:
    """The sessions that still resolve by their dates (not revoked, inside
    both lifetimes), newest first; ``principal`` limits them to one."""
    where = "" if principal is None else " AND principal = %(principal)s"
    with _connect(dsn, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '5s'")
        rows = conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS} FROM public.console_sessions
             WHERE revoked_at IS NULL AND expires_at > {_NOW}
               AND COALESCE(last_used_at, created_at) > {_NOW} - %(idle)s{where}
             ORDER BY created_at DESC, session_id
            """, {"idle": SESSION_IDLE_S, "principal": principal}).fetchall()
    return [describe(_row(r)) for r in rows]


def describe(row: ConsoleSession) -> dict:
    """A session as the routes and the CLI show it: never its token hash
    or issuer."""
    return {"session_id": row.session_id, "principal": row.principal, "method": row.method,
            "label": row.label, "created_at": row.created_at,
            "last_used_at": row.last_used_at, "expires_at": row.expires_at}


def revoke_session(dsn: str, session_id: str, *, principal: str | None = None) -> bool:
    """Revoke one session (``principal`` limits it to that principal's).
    False when there is no such live session."""
    with _connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL statement_timeout = '5s'")
        where = "" if principal is None else " AND principal = %(principal)s"
        row = conn.execute(
            f"""
            UPDATE public.console_sessions SET revoked_at = {_NOW}
             WHERE session_id = %(sid)s AND revoked_at IS NULL{where}
         RETURNING session_id
            """, {"sid": session_id, "principal": principal}).fetchone()
        conn.commit()
    return row is not None


_HAS_SESSIONS_SQL = "SELECT to_regclass('public.console_sessions') IS NOT NULL"


def revoke_principal_sessions(conn, principal: str, *, method: str | None = None,
                              keep_issuer: str | None = None) -> int:
    """Revoke ``principal``'s live sessions on ``conn``, inside the caller's
    transaction (revoking or re-pairing a principal). ``method`` limits it
    to one sign-in method; ``keep_issuer`` spares the sessions that
    credential issued. A bank before v59 has no sessions: 0."""
    if not conn.execute(_HAS_SESSIONS_SQL).fetchone()[0]:
        return 0
    clauses = ["principal = %(principal)s", "revoked_at IS NULL"]
    if method is not None:
        clauses.append("method = %(method)s")
    if keep_issuer is not None:
        clauses.append("issuer <> %(keep)s")
    rows = conn.execute(
        f"UPDATE public.console_sessions SET revoked_at = {_NOW} "
        f"WHERE {' AND '.join(clauses)} RETURNING session_id",
        {"principal": principal, "method": method, "keep": keep_issuer}).fetchall()
    return len(rows)


def revoke_issuer_sessions(conn, issuer: str | None, *, keep_principal: str | None = None) -> int:
    """Revoke the live passkey sessions ``issuer`` (a credential id; every
    passkey's with ``None``) signed in, on ``conn`` inside the caller's
    transaction; ``keep_principal`` spares those signed in as it (a rebind
    to the same principal). A bank before v59 has no sessions: 0."""
    if not conn.execute(_HAS_SESSIONS_SQL).fetchone()[0]:
        return 0
    clauses = ["method = %(method)s", "revoked_at IS NULL"]
    if issuer is not None:
        clauses.append("issuer = %(issuer)s")
    if keep_principal is not None:
        clauses.append("principal <> %(keep)s")
    rows = conn.execute(
        f"UPDATE public.console_sessions SET revoked_at = {_NOW} "
        f"WHERE {' AND '.join(clauses)} RETURNING session_id",
        {"method": METHOD_PASSKEY, "issuer": issuer, "keep": keep_principal}).fetchall()
    return len(rows)


def insert_session(conn, *, token_hash: str, principal: str, source: str, issuer: str,
                   method: str, label: str) -> ConsoleSession:
    """Insert a session on ``conn`` inside the caller's transaction (on the
    database clock); a token hash already in use raises ``UniqueViolation``."""
    row = conn.execute(
        f"""
        INSERT INTO public.console_sessions
               (session_id, token_hash, principal, source, issuer, method, label,
                created_at, last_used_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, {_NOW}, {_NOW}, {_NOW} + %s)
     RETURNING {_SESSION_COLUMNS}
        """, (secrets.token_hex(8), token_hash, principal, source, issuer, method, label,
              SESSION_MAX_S)).fetchone()
    return _row(row)


def with_last_use(rows: list[dict], sessions: SessionSnapshot | None) -> list[dict]:
    """``rows`` with each last use the daemon has seen but not written yet."""
    if sessions is None:
        return rows
    out = []
    for item in rows:
        with sessions._lock:   # noqa: SLF001 - one read of the in-memory stamp
            local = sessions._used.get(item["session_id"])
        if local is not None and (item["last_used_at"] or 0) < local:
            item = {**item, "last_used_at": local}
        out.append(item)
    return out


__all__ = [
    "CODE_TTL_S", "ConsoleSession", "METHOD_LINK", "METHOD_PASSKEY", "SESSION_IDLE_S",
    "SESSION_MAX_S", "SessionHit", "SessionSnapshot", "bearer_hashes", "clean_label",
    "describe", "environment_issuers", "flush_uses", "install_sessions",
    "insert_session", "installed_sessions", "is_loopback_origin", "issue_code",
    "list_sessions", "revoke_issuer_sessions",
    "load_sessions", "normalize_origin", "origin_key", "redeem_code", "resolve", "still_resolves",
    "revoke_principal_sessions", "revoke_session", "valid_route_id", "with_last_use",
]
