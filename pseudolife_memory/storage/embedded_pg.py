"""Embedded Postgres for the zero-config lite tier (pg0-embedded).

The daemon resolves its storage exactly once at startup, in this order:

1. An explicit ``PSEUDOLIFE_MCP_DATABASE_URL`` always wins (untouched).
2. ``PSEUDOLIFE_MCP_STORAGE=files`` opts out of embedded Postgres.
3. If ``pg0-embedded`` is importable (the ``[lite]`` extra), start — or
   attach to — an embedded PostgreSQL under the data dir and export its
   DSN through the same env var the rest of the stack already reads.
4. Otherwise: the v0.1 file mode, unchanged.

This module is consulted ONLY by the daemon entrypoint (and the backup
CLI). MemoryService deliberately knows nothing about it: file-mode tests
and the ``embedded`` CLI escape hatch must not grow a Postgres dependency
just because pg0-embedded happens to be importable.

Safety invariants:

* ``pg0.drop()`` is never called here — nothing in shipped code deletes
  a data dir, ever.
* A pgdata initialized under a different PG major is refused loudly
  (``_guard_pg_major``) instead of letting postgres fail cryptically or —
  worse — anything attempting a re-init.
* Only instances THIS process started are stopped at exit; an instance we
  merely attached to belongs to whoever started it.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, MutableMapping

logger = logging.getLogger(__name__)

ENV_STORAGE = "PSEUDOLIFE_MCP_STORAGE"

# The PG major the pinned pg0-embedded wheel provides (pyproject [lite]:
# pg0-embedded 0.15.x bundles PostgreSQL 18.1 + pgvector 0.8.5, verified by
# wheel inspection and live smoke test 2026-08-14). If a future pin moves
# the major, bump this constant in the same change and ship a dump/restore
# migration path: postgres cannot start a pgdata across majors, and the
# guard below turns that into a clear refusal instead of a cryptic crash.
EXPECTED_PG_MAJOR = 18

_DB_NAME = "pseudolife_memory"

# Overridable in tests; reading sys.platform at call time would make the
# Windows-only preflight untestable from the Linux CI lane.
_PLATFORM = sys.platform

# Embedded instances this process STARTED (never ones it merely attached
# to), stopped at interpreter exit so a Ctrl-C'd ``serve`` doesn't leave a
# background postgres behind.
_owned: list = []
_atexit_registered = False


def available() -> bool:
    """True when the ``[lite]`` extra (pg0-embedded) is installed."""
    try:
        import pg0  # noqa: F401
    except ImportError:
        return False
    return True


def default_lite_data_dir() -> Path:
    """Stable per-user data dir for the lite tier.

    MemoryService's historical default is cwd-relative ``./data`` — fine
    for the explicit-DSN and file modes it has always served, but a
    per-launch-directory *Postgres bank* would scatter real data under
    whatever directory the shim happened to spawn the daemon from. The
    lite tier therefore defaults to one stable per-user location; an
    explicit ``PSEUDOLIFE_MCP_DATA_DIR`` always wins.
    """
    if _PLATFORM == "win32":
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or Path.home() / "AppData" / "Local"
        )
    elif _PLATFORM == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME")
            or Path.home() / ".local" / "share"
        )
    return base / "pseudolife-mcp"


def resolve_daemon_storage(environ: MutableMapping[str, str]) -> str:
    """Decide the daemon's storage mode, mutating ``environ`` in place.

    Returns ``"postgres-explicit"``, ``"postgres-embedded"`` or
    ``"files"``. On the embedded path, exports the resolved DSN as
    ``PSEUDOLIFE_MCP_DATABASE_URL`` and pins ``PSEUDOLIFE_MCP_DATA_DIR``
    so the daemon's MemoryService (which reads exactly those variables)
    needs no changes at all.
    """
    if environ.get("PSEUDOLIFE_MCP_DATABASE_URL"):
        return "postgres-explicit"
    mode = (environ.get(ENV_STORAGE) or "auto").strip().lower()
    if mode == "files":
        return "files"
    if mode != "auto":
        raise RuntimeError(
            f"PSEUDOLIFE_MCP_STORAGE={mode!r} is not recognized; "
            "expected 'auto' (default) or 'files'."
        )
    if not available():
        return "files"
    raw_dir = environ.get("PSEUDOLIFE_MCP_DATA_DIR")
    data_dir = Path(raw_dir) if raw_dir else default_lite_data_dir()
    dsn = start_embedded(data_dir)
    environ["PSEUDOLIFE_MCP_DATABASE_URL"] = dsn
    environ["PSEUDOLIFE_MCP_DATA_DIR"] = str(data_dir)
    return "postgres-embedded"


def attach_or_start(data_dir: Path) -> tuple[str, Any | None]:
    """Start — or attach to — the embedded PostgreSQL for ``data_dir``.

    Returns ``(dsn, instance_or_None)``: the instance handle is returned
    ONLY when this call actually started the server, so callers stop
    exactly what they own and nothing else (a backup attaching to the
    daemon's instance must never shut it down mid-flight). The pgdata
    lives at ``<data_dir>/embedded_pg``; the pg0 registry name derives
    from that path, so distinct banks never collide.
    """
    pgdata = Path(data_dir) / "embedded_pg"
    _preflight_path(pgdata)
    _guard_pg_major(pgdata)
    pgdata.mkdir(parents=True, exist_ok=True)

    import pg0

    inst = pg0.Pg0(
        name=_instance_name(pgdata),
        database=_DB_NAME,
        data_dir=str(pgdata),
    )
    # Cross-process lock around the start attempt: two daemons racing at
    # first boot (two shims spawning `serve` at logon) must not run
    # initdb concurrently on the same fresh pgdata — pg0's own locking
    # across that window is unverified, and a corrupted first bank is not
    # a recoverable failure mode. The loser waits, then lands in the
    # AlreadyRunning→attach path. The OS releases the lock if the holder
    # dies, so there is no stale-lock state.
    with _start_lock(pgdata):
        try:
            info = inst.start()
        except pg0.Pg0AlreadyRunningError:
            info = inst.info()
            logger.info(
                "storage: embedded postgres already running — attached "
                "(pgdata=%s)", pgdata,
            )
            started = None
        else:
            logger.info(
                "storage: embedded postgres started (pgdata=%s)", pgdata,
            )
            started = inst

    uri = getattr(info, "uri", None)
    if not uri:
        raise RuntimeError(
            "embedded postgres reported no connection URI; check "
            "`pg0 logs` / ~/.pg0 for the underlying failure."
        )
    return uri, started


def start_embedded(data_dir: Path) -> str:
    """``attach_or_start`` + lifecycle ownership: an instance started
    here is stopped at interpreter exit. Returns the DSN."""
    dsn, started = attach_or_start(data_dir)
    if started is not None:
        _owned.append(started)
        global _atexit_registered
        if not _atexit_registered:
            atexit.register(stop_embedded)
            _atexit_registered = True
    return dsn


def stop_embedded() -> None:
    """Stop every embedded instance this process started (idempotent)."""
    while _owned:
        inst = _owned.pop()
        try:
            inst.stop()
        except Exception as exc:  # noqa: BLE001 - best-effort shutdown
            logger.warning("embedded postgres stop failed: %s", exc)


@contextmanager
def _start_lock(pgdata: Path, timeout_s: float = 120.0):
    """Blocking cross-process file lock guarding the pg0 start attempt.

    The lock file sits BESIDE the pgdata dir (initdb requires the dir
    itself to start empty). OS-level byte/flock locks release
    automatically when the holding process dies — no stale-lock
    handling needed. ``timeout_s`` bounds the wait on Windows, where
    ``msvcrt.locking`` cannot block indefinitely (each LK_LOCK attempt
    retries for ~10s); 120s comfortably covers a first-boot initdb.
    """
    lock_path = pgdata.parent / (pgdata.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"timed out waiting for the embedded-postgres "
                            f"start lock ({lock_path}) — another process "
                            "has been starting this bank for over "
                            f"{timeout_s:.0f}s."
                        ) from None
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _preflight_path(pgdata: Path) -> None:
    """Refuse non-ASCII pgdata paths on Windows, with a remedy.

    pg0's Rust runtime fails on them ('stream did not contain valid
    UTF-8' — initdb emits the path in the ANSI codepage; verified live
    2026-08-14) and we cannot patch a compiled binary from here. Paths
    with spaces are fine (also verified). Non-Windows platforms are not
    known to be affected and pass through.
    """
    if _PLATFORM != "win32":
        return
    try:
        str(pgdata).encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            f"embedded postgres data dir {str(pgdata)!r} contains "
            "non-ASCII characters, which the embedded PostgreSQL runtime "
            "cannot handle on Windows. Set PSEUDOLIFE_MCP_DATA_DIR to an "
            "ASCII-only path (for example C:\\pseudolife-data) and "
            "restart the daemon."
        ) from exc


def _guard_pg_major(pgdata: Path) -> None:
    """Refuse a pgdata initialized under a different PostgreSQL major."""
    pv = pgdata / "PG_VERSION"
    if not pv.exists():
        return
    found = pv.read_text(encoding="ascii", errors="replace").strip()
    try:
        major = int(found.split(".")[0])
    except ValueError:
        raise RuntimeError(
            f"embedded pgdata at {pgdata} has an unreadable PG_VERSION "
            f"({found!r}); refusing to touch it."
        ) from None
    if major != EXPECTED_PG_MAJOR:
        raise RuntimeError(
            f"embedded pgdata at {pgdata} was initialized by PostgreSQL "
            f"{found}, but the installed pg0-embedded provides PostgreSQL "
            f"{EXPECTED_PG_MAJOR}. Refusing to touch it — a cross-major "
            "pgdata cannot start and must never be re-initialized in "
            "place. Restore a `pseudolife-mcp backup` dump into a fresh "
            "data dir, or install the pg0-embedded version matching this "
            "bank."
        )


def _instance_name(pgdata: Path) -> str:
    """Stable, path-scoped pg0 instance name.

    pg0 keys instances by NAME in a global per-user registry, so the name
    must be stable for one data dir and collision-free across banks.
    Lowercased before hashing: Windows paths are case-insensitive.
    """
    canon = str(Path(pgdata).resolve()).lower()
    digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]
    return f"pseudolife-{digest}"
