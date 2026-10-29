"""When each principal last authenticated, and which bearers were refused
(schema v58).

A machine whose token stopped working and a machine that is switched off
look the same from the daemon's side unless something records both
outcomes. The HTTP gate calls :func:`note` once per request with what it
resolved: a success for an environment or stored principal updates that
name's last-seen time, and a refused bearer counts against the first
:data:`HASH_PREFIX_LEN` hex characters of its SHA-256. Neither touches the
database: :class:`ActivityRecorder` keeps them in two dicts under a short
lock, and the principals refresher (``principal_store.PrincipalRefresher``,
its own thread and connection) writes the batch at most once every
:data:`FLUSH_INTERVAL_S`, so the daemon's store/search interleave pays one
dict write per request.

A token is never stored, logged or kept in memory past the hash: eight hex
characters (32 bits) match a client's ``pseudolife-mcp doctor`` line and
are useless to a caller. Open (tokenless) daemons record nothing.

Standard library only; psycopg arrives with the connection the refresher
passes to :meth:`ActivityRecorder.flush`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
import threading
import time
from typing import Callable

# The gate's own parse of an Authorization value (re-exported for callers).
from pseudolife_memory.principals import (  # noqa: F401
    SOURCE_ENVIRONMENT, SOURCE_STORE, presented_bearer,
)

logger = logging.getLogger("pseudolife-mcp.principals")

# Design bounds (2026-10-30), not measurements. One write per principal per
# minute is fine-grained enough to tell "seen today" from "silent for
# eleven days" and costs the bank nothing measurable.
FLUSH_INTERVAL_S = 60.0
HASH_PREFIX_LEN = 8
# A spray of random tokens mints a new prefix per request: past this many
# distinct prefixes in one batch, new ones are counted as dropped, not kept.
MAX_PENDING_REJECTED = 1000
# The table keeps at most this many refused prefixes, and none older than
# the retention, so a spray cannot grow it without bound.
MAX_REJECTED_ROWS = 1000
REJECTED_RETENTION_S = 90 * 86400


def bearer_hash_prefix(token: str) -> str:
    """The first :data:`HASH_PREFIX_LEN` hex characters of the SHA-256 of
    the bytes the client sent. The gate reads headers as latin-1, so a
    header token's latin-1 encoding is those bytes (UTF-8 as a fallback for
    text no header produces); see :func:`token_hash_prefix`."""
    try:
        raw = token.encode("latin-1")
    except UnicodeEncodeError:
        raw = token.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:HASH_PREFIX_LEN]


def token_hash_prefix(token: str) -> str:
    """The same prefix for a client's own token text (``doctor``): the
    SHA-256 of its UTF-8, the bytes the client's header carries."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:HASH_PREFIX_LEN]


@dataclass
class Batch:
    """One write: ``seen`` maps a principal to its latest success,
    ``rejected`` a hash prefix to ``(count, first_seen, last_seen)``;
    ``dropped`` counts refusals past the prefix cap."""
    seen: dict[str, float] = field(default_factory=dict)
    rejected: dict[str, tuple[int, float, float]] = field(default_factory=dict)
    dropped: int = 0


class ActivityRecorder:
    """The gate's in-memory record. Thread-safe; :meth:`seen` and
    :meth:`rejected` are a dict write under a short lock.

    :meth:`due` is true when something is pending and the last write began
    at least ``interval`` seconds ago (or never), so each principal and
    prefix is written at most once per interval. :meth:`take` empties the
    pending batch and starts the interval; :meth:`give_back` returns a batch
    whose write failed, merged with what arrived meanwhile."""

    def __init__(self, *, wall: Callable[[], float] | None = None,
                 clock: Callable[[], float] | None = None,
                 interval: float = FLUSH_INTERVAL_S,
                 max_rejected: int = MAX_PENDING_REJECTED):
        self._lock = threading.Lock()
        self._wall = wall or time.time
        self._clock = clock or time.monotonic
        self._interval = interval
        self._max_rejected = max_rejected
        self._pending = Batch()
        self._last_write: float | None = None

    def seen(self, principal: str) -> None:
        now = self._wall()
        with self._lock:
            self._pending.seen[principal] = now

    def rejected(self, prefix: str) -> None:
        now = self._wall()
        with self._lock:
            entry = self._pending.rejected.get(prefix)
            if entry is not None:
                self._pending.rejected[prefix] = (entry[0] + 1, entry[1], now)
            elif len(self._pending.rejected) < self._max_rejected:
                self._pending.rejected[prefix] = (1, now, now)
            else:
                self._pending.dropped += 1

    def _has_pending(self) -> bool:
        p = self._pending
        return bool(p.seen or p.rejected or p.dropped)

    def due(self) -> bool:
        with self._lock:
            return self._has_pending() and (
                self._last_write is None or self._clock() - self._last_write >= self._interval)

    def take(self) -> Batch | None:
        """The pending batch, now empty here, or ``None`` when nothing is
        pending. Starts the interval."""
        with self._lock:
            if not self._has_pending():
                return None
            batch, self._pending = self._pending, Batch()
            self._last_write = self._clock()
            return batch

    def give_back(self, batch: Batch) -> None:
        with self._lock:
            pending = self._pending
            for name, at in batch.seen.items():
                if pending.seen.get(name, at) <= at:
                    pending.seen[name] = at
            for prefix, (count, first, last) in batch.rejected.items():
                newer = pending.rejected.get(prefix)
                if newer is None:
                    pending.rejected[prefix] = (count, first, last)
                else:
                    pending.rejected[prefix] = (count + newer[0], min(first, newer[1]),
                                                max(last, newer[2]))
            pending.dropped += batch.dropped

    def flush(self, conn) -> bool:
        """Write the pending batch on ``conn`` (autocommit) when it is due:
        one statement per table, in one transaction. ``False`` when nothing
        was due; a failed write gives the batch back and raises."""
        if not self.due():
            return False
        batch = self.take()
        if batch is None:
            return False
        try:
            write_batch(conn, batch, now=self._wall())
        except BaseException:
            self.give_back(batch)
            raise
        line = refusal_log_line(batch)
        if line:
            logger.warning("%s", line)
        return True


# At most this many prefixes are named in one log line.
LOG_PREFIXES = 5


def refusal_log_line(batch: Batch) -> str | None:
    """One line per write naming the refused prefixes, most refused first,
    or ``None`` when nothing was refused."""
    if not batch.rejected and not batch.dropped:
        return None
    ranked = sorted(batch.rejected.items(), key=lambda kv: (-kv[1][0], kv[0]))
    total = sum(r[0] for _p, r in ranked) + batch.dropped
    named = ", ".join(f"{p} x{r[0]}" for p, r in ranked[:LOG_PREFIXES])
    more = len(ranked) - LOG_PREFIXES
    tail = (f" and {more} more prefix(es)" if more > 0 else "") + (
        f", plus {batch.dropped} past the {MAX_PENDING_REJECTED}-prefix cap" if batch.dropped else "")
    return (f"refused {total} bearer token(s) since the last report, by SHA-256 prefix: "
            f"{named or '(none recorded)'}{tail}. `pseudolife-mcp doctor` on a client prints its "
            "own token's prefix; `pseudolife-mcp invite --list` shows them all")


UPSERT_SEEN_SQL = """
INSERT INTO public.principal_activity (principal, last_seen_at)
SELECT * FROM unnest(%s::text[], %s::double precision[])
ON CONFLICT (principal) DO UPDATE
   SET last_seen_at = GREATEST(principal_activity.last_seen_at, EXCLUDED.last_seen_at)
"""
UPSERT_REJECTED_SQL = """
INSERT INTO public.rejected_bearers (hash_prefix, rejections, first_seen_at, last_seen_at)
SELECT * FROM unnest(%s::text[], %s::bigint[], %s::double precision[], %s::double precision[])
ON CONFLICT (hash_prefix) DO UPDATE
   SET rejections = rejected_bearers.rejections + EXCLUDED.rejections,
       first_seen_at = LEAST(rejected_bearers.first_seen_at, EXCLUDED.first_seen_at),
       last_seen_at = GREATEST(rejected_bearers.last_seen_at, EXCLUDED.last_seen_at)
"""
PRUNE_REJECTED_SQL = """
DELETE FROM public.rejected_bearers
 WHERE last_seen_at < %s
    OR hash_prefix IN (SELECT hash_prefix FROM public.rejected_bearers
                        ORDER BY last_seen_at DESC, hash_prefix OFFSET %s)
"""


def write_batch(conn, batch: Batch, *, now: float) -> None:
    """The batch's upserts (and, with refusals, the prune) in one
    transaction. Times only move forward; counts add."""
    with conn.transaction():
        if batch.seen:
            names = sorted(batch.seen)
            conn.execute(UPSERT_SEEN_SQL, (names, [batch.seen[n] for n in names]))
        if batch.rejected:
            prefixes = sorted(batch.rejected)
            rows = [batch.rejected[p] for p in prefixes]
            conn.execute(UPSERT_REJECTED_SQL, (prefixes, [r[0] for r in rows],
                                               [r[1] for r in rows], [r[2] for r in rows]))
            conn.execute(PRUNE_REJECTED_SQL, (now - REJECTED_RETENTION_S, MAX_REJECTED_ROWS))


def note(recorder: ActivityRecorder | None, auth_header: str | None,
         principal: str | None, source: str | None) -> None:
    """The gate's one call per request, after a resolution that did not
    raise: a success from the environment or the store is seen; an
    unauthorized ``Bearer`` is refused by prefix. Open mode, and a request
    with no bearer, record nothing."""
    if recorder is None:
        return
    if principal is not None:
        if source in (SOURCE_ENVIRONMENT, SOURCE_STORE):
            recorder.seen(principal)
        return
    token = presented_bearer(auth_header)
    if token is not None:
        recorder.rejected(bearer_hash_prefix(token))


# The daemon's recorder, installed beside the stored-principal snapshot;
# None everywhere else (clients, tests, a daemon without Postgres).
_RECORDER: ActivityRecorder | None = None


def install_recorder(recorder: ActivityRecorder | None) -> None:
    global _RECORDER
    _RECORDER = recorder


def installed_recorder() -> ActivityRecorder | None:
    return _RECORDER
