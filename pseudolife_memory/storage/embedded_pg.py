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
* Each bank's superuser has its own random password (``_cluster_secret``)
  and SCRAM authentication; a bank made with pg0's fixed
  ``postgres``/``postgres`` is moved to its secret on its next owned start
  (``_move_under_trust`` / ``_move_over_scram``).
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
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

    pg0 is never given the bank's secret: it runs with its defaults (role,
    password and database all ``postgres``), so it makes no connection of
    its own and the secret appears on no command line and in no pg0
    record. This module connects, creates the bank's database and builds
    the DSN itself.
    """
    pgdata = Path(data_dir) / "embedded_pg"
    _preflight_path(pgdata)
    _guard_pg_major(pgdata)
    pgdata.mkdir(parents=True, exist_ok=True)

    import pg0

    # libpq 16+ (require_auth) is what keeps the secret off the wire.
    if not _libpq_has_require_auth():
        raise RuntimeError(
            "the lite tier needs libpq 16 or newer, so the bank's password is "
            "only ever offered over SCRAM; this install's psycopg uses an "
            "older one. Upgrade it: pip install -U \"psycopg[binary]\" (and set "
            "PSYCOPG_IMPL=binary if a psycopg-c build is installed)."
        )

    # Cross-process lock around the start attempt: two daemons racing at
    # first boot (two shims spawning `serve` at logon) must not run
    # initdb concurrently on the same fresh pgdata — pg0's own locking
    # across that window is unverified, and a corrupted first bank is not
    # a recoverable failure mode. The loser waits, then attaches. The OS
    # releases the lock if the holder dies, so there is no stale-lock
    # state. Holding it also proves no other process is mid-migration.
    with _start_lock(pgdata):
        secret, created = _cluster_secret(pgdata)
        progress = {"altered": False}
        try:
            return _attach_or_start_locked(pg0, pgdata, secret, created, progress)
        except BaseException:
            # A secret this call created is unused until a role change may
            # have given it to the role; before that a failed attempt drops
            # it (the trust marker lets the next start re-key regardless),
            # after that it is kept, since the server may already require it.
            if created and not progress["altered"]:
                _secret_path(pgdata).unlink(missing_ok=True)
            raise


def _attach_or_start_locked(pg0, pgdata: Path, secret: str, created: bool,
                            progress: dict):
    # A running server is found through its own postmaster.pid, not pg0's
    # record (pg0's `info` can report a live server stopped, and it checks
    # with the password pg0 was given, which is never the secret).
    port = _running_port(pgdata)
    matched = _probe(port, pgdata, (secret, _LEGACY_PASSWORD)) if port else None
    if matched is not None:
        _settle_running(matched, port, pgdata, secret, progress)
        # A starter killed after the move but before creating the bank's
        # database leaves it missing; create it here too.
        _ensure_database(port, secret)
        logger.info(
            "storage: embedded postgres already running — attached "
            "(pgdata=%s)", pgdata,
        )
        return _dsn(secret, port, scram=True), None

    hba_path = pgdata / "pg_hba.conf"
    found_hba = hba_path.read_text(encoding="utf-8") if hba_path.exists() else None
    # A bank not yet on SCRAM (or whose secret file was just recreated)
    # starts with loopback trust, which is also the migration's progress
    # marker: a crash anywhere before the SCRAM rewrite leaves trust behind,
    # and the next start under the lock finishes the move.
    marked = found_hba is not None and (
        bool(_managed_methods(found_hba) - {"scram-sha-256"}) or created
    )
    if port and _postmaster_alive(pgdata):
        # The bank's server is running but took neither password (busy, still
        # starting, or a lost secret). A start cannot succeed over it, the
        # trust marker would open it to every local account at once on
        # Windows, and stopping it is not this call's to do: refuse.
        raise RuntimeError(_running_refusal(pgdata, port))
    if marked:
        _write_hba(pgdata, _rewrite_hba(found_hba, "trust", loopback_only=True))
    inst = pg0.Pg0(name=_instance_name(pgdata), data_dir=str(pgdata))
    try:
        info = inst.start()
    except BaseException as exc:
        # The server may be running under another owner (pg0 refuses
        # without a record of it): put its file back as it was, so a
        # configuration reload cannot open it to trust.
        if marked:
            _write_hba(pgdata, found_hba)
        if isinstance(exc, pg0.Pg0AlreadyRunningError):
            raise RuntimeError(_running_refusal(pgdata, None)) from exc
        # pg0 can fail after postgres launched (a readiness timeout); never
        # leave that server running unowned. pg0's stop is a no-op otherwise.
        inst.stop()
        raise
    logger.info("storage: embedded postgres started (pgdata=%s)", pgdata)
    try:
        port = getattr(info, "port", None) or _running_port(pgdata)
        if not port:
            raise RuntimeError(
                "embedded postgres started but reported no port; check "
                "`pg0 logs` / ~/.pg0 for the underlying failure."
            )
        if found_hba is None:
            # pg0's initdb gave the role its default password.
            _move_over_scram(port, pgdata, secret, progress)
        elif marked:
            _move_under_trust(port, pgdata, secret, progress)
        else:
            # The rules say SCRAM, but a move interrupted between its rewrite
            # and its role change, with the server stopped since, leaves the
            # role on the public password: ask the server which it takes.
            matched = _probe(port, pgdata, (secret, _LEGACY_PASSWORD))
            if matched is None:
                raise RuntimeError(
                    f"embedded postgres for {pgdata} does not accept the "
                    f"password in {_secret_path(pgdata)}. Move that file "
                    "aside and start again: the next start gives the bank's "
                    "superuser a new password."
                )
            _settle_running(matched, port, pgdata, secret, progress)
        _ensure_database(port, secret)
    except BaseException:
        # Never leave a server this call started running unowned (and
        # possibly on trust): stop it; the next start finishes the move.
        inst.stop()
        raise
    return _dsn(secret, port, scram=True), inst


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


_LEGACY_PASSWORD = "postgres"  # pg0's fixed default, still held by old banks


def _dsn(password: str, port: int, database: str | None = None, *, scram: bool = False) -> str:
    """A DSN for the bank's server. ``scram``: the client refuses any
    authentication but SCRAM (libpq 16+), so the secret itself is never
    sent, not even to an impostor on the recorded port."""
    dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/{database or _DB_NAME}"
    return dsn + "?require_auth=scram-sha-256" if scram else dsn


def _libpq_has_require_auth() -> bool:
    import psycopg

    return psycopg.pq.version() >= 160000


def split_password(dsn: str) -> tuple[str, str | None]:
    """A DSN this module built, without its password, and the password,
    for a child process that should get it through ``PGPASSWORD``."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn, None
    netloc = f"{parts.username}@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc)), parts.password


def _secret_path(pgdata: Path) -> Path:
    """The bank's superuser password file, beside the pgdata (initdb needs
    the pgdata itself empty, and a pgdata copy must not carry it)."""
    return pgdata.parent / (pgdata.name + ".secret")


def _cluster_secret(pgdata: Path) -> tuple[str, bool]:
    """This bank's superuser password, read or created owner-only, and
    whether this call created it.

    Written through the same owner-only writer as bearer-token files (an
    explicit owner-only ACL on Windows, mode 0600 elsewhere, set before the
    first byte), and read back through the same validator."""
    from pseudolife_memory import credentials

    path = _secret_path(pgdata)
    try:
        if path.exists() or path.is_symlink():
            secret, _ = credentials._open_token(path)
            created = False
        else:
            secret = secrets.token_hex(32)
            credentials._write_token_file(path, secret)
            created = True
    except credentials.CredentialError as exc:
        raise RuntimeError(
            f"embedded postgres password file {path} cannot be used ({exc}). "
            "Move it aside and start again: the next start gives the bank's "
            "superuser a new password."
        ) from exc
    # It goes into a SQL string literal and a URI: only the shape this
    # module writes is accepted.
    if not re.fullmatch(r"[0-9a-f]{64}", secret):
        raise RuntimeError(
            f"embedded postgres password file {path} holds an unexpected "
            "value. Move it aside and start again: the next start gives the "
            "bank's superuser a new password."
        )
    return secret, created


def _running_port(pgdata: Path) -> int | None:
    """The port in the pgdata's postmaster.pid (line 4), if there is one."""
    try:
        lines = (pgdata / "postmaster.pid").read_text(
            encoding="utf-8", errors="replace").splitlines()
        return int(lines[3].strip())
    except (OSError, IndexError, ValueError):
        return None


def _probe(port: int, pgdata: Path, passwords) -> str | None:
    """The first password the server on ``port`` accepts, provided that
    server is this pgdata's own (a recorded port another PostgreSQL has
    since taken must not be mistaken for the bank). The secret is offered
    only over SCRAM; the public old password as is."""
    import psycopg

    def canon(path: str) -> str:
        resolved = str(Path(path).resolve())
        return resolved.lower() if _PLATFORM == "win32" else resolved

    for password in passwords:
        scram = password != _LEGACY_PASSWORD
        try:
            with psycopg.connect(
                _dsn(password, port, "postgres", scram=scram), connect_timeout=5,
            ) as conn:
                (reported,) = conn.execute("SHOW data_directory").fetchone()
        except psycopg.Error:
            continue
        if canon(reported) == canon(str(pgdata)):
            return password
    return None


def _postmaster_alive(pgdata: Path) -> bool:
    """Whether the pid in postmaster.pid (line 1) is a live postgres process.
    A stale file (its server gone, its port perhaps reused by an unrelated
    program) is not; pg0 removes it at the next start."""
    import psutil

    try:
        lines = (pgdata / "postmaster.pid").read_text(
            encoding="utf-8", errors="replace").splitlines()
        pid, started = int(lines[0].strip()), float(lines[2].strip())
    except (OSError, IndexError, ValueError):
        return False
    try:
        process = psutil.Process(pid)
        if not process.name().lower().startswith("postgres"):
            return False
        # postmaster.pid's third line is the postmaster's start time, stamped
        # a little after the process began (seconds on a slow start). Only a
        # process created well after it is another postgres that took the
        # recorded pid after a reboot; anything else counts as the bank's
        # server (fail closed).
        return process.create_time() <= started + 60
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.Error:
        # Alive but not inspectable: assume it is (fail closed).
        return True


def _running_refusal(pgdata: Path, port: int | None) -> str:
    where = f" on port {port}" if port else ""
    return (
        f"embedded postgres for {pgdata} is running{where} but accepts "
        "neither this bank's password nor the old fixed one (it may be busy "
        "or still starting: try again in a moment). If it persists, stop it "
        f"with: python -c \"import pg0; pg0.stop('{_instance_name(pgdata)}')\" "
        "and start again."
    )


def _settle_running(matched: str, port: int, pgdata: Path, secret: str,
                    progress: dict) -> None:
    """Finish what a dead starter left on a running server.

    Under the start lock nobody else is migrating. This module's own trust
    marker means a move was interrupted with the server running on trust:
    finish it the trust way. A server that still takes the public old
    password (a fresh bank killed before its move, an older release's
    server, or trust still loaded behind a SCRAM file) is moved the SCRAM
    way. A server that took the secret over SCRAM is already moved."""
    marked = _is_marked((pgdata / "pg_hba.conf").read_text(encoding="utf-8"))
    if marked and matched == secret:
        # The rules it runs are SCRAM (the secret was offered with SCRAM
        # required) while the file says trust: put the file back to SCRAM.
        _switch_to_scram(port, pgdata, secret)
        _wait_for_scram(port, secret)
    elif marked:
        _move_under_trust(port, pgdata, secret, progress)
    elif matched == _LEGACY_PASSWORD:
        _move_over_scram(port, pgdata, secret, progress)


# pg_hba.conf methods this module manages, on its rule lines. A rule with
# any other method (reject, peer, cert, ...) and every other line (include
# directives, comments) are someone's deliberate edits and stay as found.
_MANAGED_METHODS = frozenset({"trust", "password", "md5", "scram-sha-256"})
_RULE_TYPES = frozenset(
    {"local", "host", "hostssl", "hostnossl", "hostgssenc", "hostnogssenc"})


def _rule_method(fields: list[str]) -> int | None:
    """Index of the managed method in a rule line's fields, if any."""
    if not fields or fields[0] not in _RULE_TYPES:
        return None
    return next((i for i in range(3, len(fields)) if fields[i] in _MANAGED_METHODS), None)


_LOOPBACK = {"127.0.0.1": "255.255.255.255", "::1": "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"}


def _is_loopback_rule(fields: list[str]) -> bool:
    """A ``local`` rule, or a ``host*`` rule for the loopback address only."""
    if fields[0] == "local":
        return True
    if len(fields) < 4:
        return False
    address, _, prefix = fields[3].partition("/")
    if address == "localhost" and not prefix:
        return True
    if address not in _LOOPBACK:
        return False
    if prefix:
        return prefix == ("32" if address == "127.0.0.1" else "128")
    return len(fields) > 4 and fields[4].lower() == _LOOPBACK[address]


def _is_marked(text: str) -> bool:
    """This module's progress marker: a local or loopback rule on trust."""
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        index = _rule_method(fields)
        if index is not None and fields[index] == "trust" and _is_loopback_rule(fields):
            return True
    return False


def _managed_methods(text: str) -> set[str]:
    """The managed authentication methods on pg_hba.conf's rule lines."""
    methods = set()
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        index = _rule_method(fields)
        if index is not None:
            methods.add(fields[index])
    return methods


def _rewrite_hba(text: str, method: str, *, loopback_only: bool = False) -> str:
    """Set every managed rule line's method to ``method`` (``loopback_only``:
    only on local and loopback rules, so the trust marker never opens a rule
    a user pointed at the network), keeping comments, columns and every
    other line byte-for-byte."""
    out = []
    for line in text.splitlines(keepends=True):
        rule, hash_, comment = line.partition("#")
        parts = re.split(r"(\s+)", rule)
        positions = [i for i, part in enumerate(parts) if part and not part.isspace()]
        fields = [parts[i] for i in positions]
        index = _rule_method(fields)
        if index is not None and (not loopback_only or _is_loopback_rule(fields)):
            parts[positions[index]] = method
        out.append("".join(parts) + hash_ + comment)
    return "".join(out)


def _write_hba(pgdata: Path, text: str) -> None:
    """Replace pg_hba.conf atomically and durably, so a crash or power
    loss leaves the old file or the new one, never an empty one."""
    target = pgdata / "pg_hba.conf"
    temporary = pgdata / "pg_hba.conf.tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as stream:
        if os.name != "nt":
            # Keep the mode initdb gave the file (0600), not the umask's.
            try:
                mode = target.stat().st_mode & 0o777
            except FileNotFoundError:
                mode = 0o600
            os.fchmod(stream.fileno(), mode)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    # A scanner or indexer can hold the file for a moment on Windows; the
    # token writer retries the same way (20 x 5 ms).
    for attempt in range(20):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.005)


def _run_sql(dsn: str, statements: list[str]) -> list:
    """Run ``statements`` in order; the rows of the last one, if any."""
    import psycopg

    rows: list = []
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        for statement in statements:
            cursor = conn.execute(statement)
            rows = cursor.fetchall() if cursor.description else []
    return rows


def _move_under_trust(port: int, pgdata: Path, secret: str, progress: dict) -> None:
    """Move a server running on the trust marker: the role gets the secret
    (idempotent; trust accepts the connection whatever the role's password
    was, so a lost secret re-keys too), then the methods move to SCRAM and
    the server reloads. Every interruption leaves the trust marker."""
    progress["altered"] = True
    _alter_role(_dsn(_LEGACY_PASSWORD, port, "postgres"), secret)
    _switch_to_scram(port, pgdata, secret)
    _wait_for_scram(port, secret)


def _move_over_scram(port: int, pgdata: Path, secret: str, progress: dict) -> None:
    """Move a server whose role still takes the public old password: the
    methods move to SCRAM and the server reloads first, then the role gets
    the secret over SCRAM. No state has the role on the secret while the
    server still asks for a cleartext password, so a later probe, which
    offers the secret over SCRAM only, always recognizes the result."""
    _switch_to_scram(port, pgdata, secret)
    if not _takes_secret(port, secret):
        # The role is still on the public password (not already moved by an
        # interrupted earlier move): give it the secret over SCRAM.
        progress["altered"] = True
        try:
            _alter_role(_dsn(_LEGACY_PASSWORD, port, "postgres"), secret)
        except Exception:  # noqa: BLE001 - checked by the wait below
            # Off Windows the reload applies asynchronously: the role may
            # already hold the secret while trust was still in force a
            # moment ago. The wait below is the real check.
            pass
        _wait_for_scram(port, secret)
    logger.info(
        "storage: embedded postgres uses its own password (%s) and SCRAM "
        "authentication", _secret_path(pgdata),
    )


def _takes_secret(port: int, secret: str) -> bool:
    """Whether the server takes the secret over SCRAM now."""
    try:
        _run_sql(_dsn(secret, port, "postgres", scram=True), ["SELECT 1"])
    except Exception:  # noqa: BLE001 - any refusal means "not yet"
        return False
    return True


def _wait_for_scram(port: int, secret: str, timeout_s: float = 10.0) -> None:
    """Wait until the server takes the secret over SCRAM: off Windows,
    pg_reload_conf() returns before the server applies the new rules."""
    deadline = time.monotonic() + timeout_s
    while not _takes_secret(port, secret):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "embedded postgres did not accept the bank's password over "
                f"SCRAM within {timeout_s:.0f}s of reloading its rules; start "
                "again (the move resumes where it stopped)."
            )
        time.sleep(0.1)


def _alter_role(dsn: str, secret: str) -> None:
    """Give the role the secret as a SCRAM verifier computed here, so the
    secret itself never reaches the server (or anything posing as it)."""
    _run_sql(dsn, [f"ALTER ROLE \"postgres\" PASSWORD '{_scram_verifier(secret)}'"])


def _scram_verifier(secret: str, salt: bytes | None = None,
                    iterations: int = 4096) -> str:
    """PostgreSQL's stored SCRAM-SHA-256 verifier for ``secret`` (RFC 5802
    and 7677; the secret is ASCII hex, so SASLprep leaves it unchanged)."""
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", secret.encode("ascii"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _switch_to_scram(port: int, pgdata: Path, secret: str) -> None:
    """Rewrite the managed methods to SCRAM, then reload.

    Which rules a new connection meets before the reload differs by OS: on
    Windows a changed pg_hba.conf applies to new connections at once, so
    SCRAM is already in force; elsewhere the loaded rules (trust, or a
    password method with the role on the public password) still apply. So
    the reload tries the secret with SCRAM required (nothing is sent if the
    server asks for anything else), then the public password."""
    hba = (pgdata / "pg_hba.conf").read_text(encoding="utf-8")
    _write_hba(pgdata, _rewrite_hba(hba, "scram-sha-256"))
    error: Exception | None = None
    for dsn in (_dsn(secret, port, "postgres", scram=True),
                _dsn(_LEGACY_PASSWORD, port, "postgres")):
        try:
            _run_sql(dsn, ["SELECT pg_reload_conf()"])
            return
        except Exception as exc:  # noqa: BLE001 - the next password may work
            error = exc
    raise error


def _ensure_database(port: int, secret: str) -> None:
    """Create the bank's database when it is missing (checked first, as
    pg0 does, rather than parsing the localized duplicate error)."""
    admin = _dsn(secret, port, "postgres", scram=True)
    if not _run_sql(admin, [f"SELECT 1 FROM pg_database WHERE datname = '{_DB_NAME}'"]):
        _run_sql(admin, [f"CREATE DATABASE \"{_DB_NAME}\""])


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
