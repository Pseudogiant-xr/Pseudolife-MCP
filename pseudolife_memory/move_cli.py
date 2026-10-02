"""``pseudolife-mcp move``: move this host's Docker-tier bank to another host.

    pseudolife-mcp move --to <ssh-target> [--target-checkout PATH]
        [--target-url URL] [--no-keep-tokens] [--resume]
        [--dry-run] [--yes] [--json]

Run on the source host, a Docker-tier daemon. The target is a Docker-tier
checkout host where the installer has already run (its daemon and Postgres
containers exist), reached over key-based ssh (``ssh -o BatchMode=yes``;
nothing ever types a password). The order:

1. **Preflight, no changes.** The source is healthy, Docker tier and reports
   its bank fingerprint; ssh works; the target checkout exists; the target
   daemon is healthy, its schema is at least the source's, its fingerprint
   differs, and its bank is empty (no entries, facts or principals), unless
   ``--resume`` names this move's half-restored bank by its marker. The
   target URL is ``--target-url``, else the target's ``expose status``.
2. **Plan and confirm** (``--dry-run`` stops after the plan; a run that is
   not interactive needs ``--yes``).
3. **Expose the target** when it is not exposed, and check this host
   reaches ``<target-url>/health``.
4. **Stop the source daemon** (``docker stop -t 120``, never ``rm``), after
   pausing the target's unattended update and restart policy (either could
   start the target's daemon mid-move) and this host's unattended update,
   each only when it is enabled now.
5. **Final backup from the stopped container, then fence its database**:
   other backends are cut off, ``pg_dump`` runs through the Postgres
   container, then ``ALTER DATABASE <db> WITH ALLOW_CONNECTIONS false`` and a
   check that nothing is still connected; the daemon's ``/data`` through
   ``docker run --rm --volumes-from`` (it works on a stopped container), and
   a manifest with per-table row counts. Any part failing is a failure.
6. **Copy** the three files over ssh (``cat >``) and check their SHA-256 on
   the target.
7. **Restore on the target without starting it**: the resume marker
   (``<checkout>/data/move-<id>/marker.json`` on the target host, outside
   the daemon's ``/data``), ``ops/restore.sh --apply --no-start
   --backup-file ...``, then the restored bank identity (``meta``
   ``coordination_bank_id``) and row counts against the source's, and
   ``/data/move.json``, which keeps the target's daemon from starting
   outside the move.
8. **Carry the environment identities** (``PSEUDOLIFE_MCP_TOKEN``,
   ``_TOKENS``, ``_TIER_MAP``, ``_TOOLSET``, read from the container that
   ran) into the target's ``ops/.env`` over ssh stdin, the file backed up
   first and its owner and mode kept; ``--no-keep-tokens`` skips it.
9. **Start the target once** (``ops/update.sh``), wait (180 s, with a
   nudge a minute) for its ``/health`` bank fingerprint to equal the
   source's, check by SHA-256 that it runs with the carried values, restore
   what step 4 paused on the target, set the source's restart policy to
   ``no``, write ``/data/moved.json`` into the stopped source container,
   and remove the resume marker last.
10. **Re-point this machine's clients** (``pseudolife-mcp connect
    <target-url> --yes``).
11. **Report**: the manual rollback, every other machine's ``connect``
    line, the target's files that held its replaced token, held board
    leases, undelivered mail, the source leftovers to retire, and what the
    state archive replaced on the target.

A failure in steps 4-9, or SIGTERM / SIGHUP / Ctrl-C, rolls back in a fixed
order (Ctrl-C is ignored while it runs): stop the target daemon if it was
started (and put its ``/data/move.json`` back); restore the target's
``ops/.env``, unattended update and restart policy; lift the database
fence; remove the source's ``moved.json``; restore its restart policy and
schedule; ``docker start`` the source. The target is never left running
beside a running source: when it cannot be stopped, the source stays down.
Each step's progress, and the manual rollback, are written to the move
record (``~/.pseudolife-mcp/moves/<id>/record.json``) as they happen.

Exit codes: 0 moved; 1 failed and rolled back; 2 usage, declined, or no
TTY without ``--yes``; 4 refused before any change; 5 the bank moved but
re-pointing this machine's clients failed (no rollback: the target is
correct and the source is fenced); 6 failed and the rollback could not
finish (the error and the record say what is left).

Every external effect (``ssh``, ``docker``, the schedule and ``connect``
subprocesses, the HTTP reads) goes through :class:`Runner`, which the tests
replace with a recording fake. Secrets (the carried tokens) travel only on
ssh stdin, never in a command line. Standard library only at import time,
like the other client modes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from urllib.parse import unquote, urlsplit

DAEMON = "pseudolife-mcp-daemon"
POSTGRES = "pseudolife-mcp-postgres"
SOURCE_URL = "http://127.0.0.1:8765"
DEFAULT_CHECKOUT = "~/src/Pseudolife-MCP"
CARRIED_KEYS = ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS", "PSEUDOLIFE_MCP_TIER_MAP",
                "PSEUDOLIFE_MCP_TOOLSET")
DUMP_NAME = "pseudolife_memory.sql.gz"
STATE_NAME = "pseudolife_state.tgz"
MANIFEST_NAME = "manifest.json"
MARKER = "/data/move.json"      # on the target: a daemon refuses to start while it is there
MOVED = "/data/moved.json"      # on the source: the same, naming the new location
SSH_OPTS = ("ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")
BANK_ID_SQL = "SELECT value FROM meta WHERE key = 'coordination_bank_id'"
# Where a client on the target may hold a token (``grep -lsF`` reads the
# tokens from stdin; a path that does not exist is skipped).
TARGET_CLIENT_FILES = ("~/.pseudolife-mcp/*.token", "~/.claude.json", "~/.claude/settings.json",
                       "~/.codex/config.toml", "~/.codex/pseudolife/token", "~/.gemini/settings.json",
                       "~/.config/Claude/claude_desktop_config.json",
                       "~/'Library/Application Support/Claude/claude_desktop_config.json'")
_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))
INSTALL_LINE = ("git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git ~/src/Pseudolife-MCP && "
                "cd ~/src/Pseudolife-MCP && ops/install.sh")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 4
EXIT_PARTIAL = 5
EXIT_INCOMPLETE = 6

_DUMP_END = "-- PostgreSQL database dump complete"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_TABLE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_MOVE_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.,:=@/+~-]*$")
_TIMEOUT_LONG = 4 * 3600


# ── the runner: every external effect ───────────────────────────────────────

@dataclass
class Result:
    code: int
    out: bytes = b""
    err: bytes = b""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out or b"").decode("utf-8", "replace")

    def why(self, out: bool = True) -> str:
        """The last meaningful line of the output, for an error message.
        ``out=False`` reads stderr alone: for a command whose stdout can
        carry secrets (``docker inspect``, an env file)."""
        text = ((self.err or b"") + ((b"\n" + (self.out or b"")) if out else b"")).decode("utf-8", "replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.lower().startswith(("error", "fatal")):
                return line[:300]
        return (lines[-1][:300] if lines else f"exit {self.code}")


class Runner:
    """Runs a command (``argv``, never a shell string on this host) or reads
    a URL. ``stdin`` is bytes handed to the child; ``stdin_file`` streams a
    file into it; ``stdout_file`` takes the child's stdout (binary-safe: a
    dump or an archive)."""

    def run(self, argv, *, stdin: bytes | None = None, stdin_file: Path | None = None,
            stdout_file: Path | None = None, timeout: int = 600) -> Result:
        argv = [str(part) for part in argv]
        handles = []
        kwargs: dict = {}
        try:
            if stdin_file is not None:
                handles.append(open(stdin_file, "rb"))
                kwargs["stdin"] = handles[-1]
            elif stdin is not None:
                kwargs["input"] = stdin
            else:
                kwargs["stdin"] = subprocess.DEVNULL
            if stdout_file is not None:
                handles.append(open(stdout_file, "wb"))
                kwargs["stdout"] = handles[-1]
            else:
                kwargs["stdout"] = subprocess.PIPE
            proc = subprocess.run(argv, stderr=subprocess.PIPE, timeout=timeout, check=False, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(1, b"", f"{type(exc).__name__}: {exc}".encode("utf-8", "replace"))
        finally:
            for handle in handles:
                handle.close()
        return Result(proc.returncode, proc.stdout or b"", proc.stderr or b"")

    def get_json(self, url: str, *, token: str | None = None, timeout: float = 5.0) -> dict | None:
        """The JSON object at ``url`` (redirects refused), or ``None``."""
        import urllib.error
        import urllib.request

        from pseudolife_memory.daemon_url import _NoRedirectHandler

        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler)
            with opener.open(request, timeout=timeout) as response:  # noqa: S310 - the operator's daemon URL
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return data if isinstance(data, dict) else None


# ── pieces ──────────────────────────────────────────────────────────────────

class MoveError(Exception):
    """The move stops here; the message says why and what to do."""

    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


class SshLost(MoveError):
    """ssh itself failed (exit 255): the connection dropped or never came up."""


def rq(path: str) -> str:
    """``path`` quoted for the remote shell, keeping a leading ``~/``
    expandable there."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:]
        return "~/" + shlex.quote(rest) if rest else "~/"
    return shlex.quote(path)


def _lit(value: str) -> str:
    """A SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def fence_sql(db: str, allow: bool) -> str:
    """The fence (``allow=False``) and its lift. Run as the superuser
    against the ``postgres`` database: a database that refuses connections
    cannot be connected to in order to change it."""
    if not _IDENT.match(db):
        raise MoveError(f"refusing an unusual database name {db!r}", EXIT_REFUSED)
    # Unquoted: the name is lowercase letters, digits and underscores, so
    # the statement needs no quotes of its own and the manual-rollback line
    # can wrap it in double quotes for any shell.
    return f"ALTER DATABASE {db} WITH ALLOW_CONNECTIONS {'true' if allow else 'false'}"


def terminate_sql(db: str) -> str:
    return (f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {_lit(db)} "
            f"AND pid <> pg_backend_pid()")


def scan_dump(path: Path) -> tuple[bool, dict[str, int]]:
    """``(complete, {table: rows})`` of a plain-format gzip dump: pg_dump's
    closing marker read outside COPY data (a stored memory quoting it must
    not pass for the end), and each table's COPY rows (one per line; the
    data escapes embedded newlines), as ``ops/backup.sh`` counts them."""
    counts: dict[str, int] = {}
    complete, table, rows = False, None, 0
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="\n") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if table is not None:
                    if line == "\\.":
                        counts[table] = rows
                        table = None
                    else:
                        rows += 1
                    continue
                match = re.match(r"^COPY (\S+) .*FROM stdin;$", line)
                if match:
                    table, rows = match.group(1), 0
                elif line == _DUMP_END:
                    complete = True
    except (OSError, EOFError, ValueError):
        return False, {}
    return complete and table is None, counts


def parse_expose_status(text: str) -> tuple[str, str | None]:
    """What ``pseudolife-mcp expose status --json`` said: ``("exposed",
    url)``, ``("not-exposed", None)`` or ``("unknown", None)``. Tolerant of
    the shape: a JSON object with a ``url`` (null when not exposed), an
    ``exposed`` flag or a ``state``/``status`` text, possibly after other
    output lines."""
    from pseudolife_memory import codex_connection

    text = (text or "").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                try:
                    data = json.loads(text[start:end + 1])
                except ValueError:
                    data = None
    if isinstance(data, dict):
        url = data.get("url")
        if isinstance(url, str) and url:
            try:
                return "exposed", codex_connection._validated_daemon_url(url)
            except codex_connection.SetupError:
                return "unknown", None
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        if data.get("exposed") is False or "not exposed" in state or state in ("off", "not-exposed", "not_exposed"):
            return "not-exposed", None
        return "unknown", None
    if "not exposed" in text.lower():
        return "not-exposed", None
    return "unknown", None


def _env_value(key: str, value: str) -> str:
    if _SAFE_VALUE.match(value):
        return value
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    raise MoveError(f"the source's {key} holds characters that cannot be written to ops/.env safely; "
                    f"carry it by hand, or move with --no-keep-tokens")


def merge_env(content: str, carried: dict[str, str], move_id: str) -> str:
    """``content`` (an ``ops/.env``) with LF line endings, every line that
    sets a carried key removed, and the carried values appended."""
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if carried:
        pattern = re.compile(r"^\s*(?:export\s+)?(?:" + "|".join(map(re.escape, carried)) + r")\s*=")
        lines = [line for line in lines if not pattern.match(line)]
        block = [f"# Carried from the source host by pseudolife-mcp move {move_id}"]
        block += [f"{key}={_env_value(key, value)}" for key, value in carried.items()]
        lines += block
    return "\n".join(lines) + "\n"


def _dsn_parts(dsn: str | None) -> tuple[str, str]:
    """``(database, user)`` of the daemon's DSN (the compose defaults when
    it names neither). The password is never read out of it."""
    try:
        parsed = urlsplit(dsn or "")
        db = unquote(parsed.path.lstrip("/")) or "pseudolife_memory"
        user = unquote(parsed.username or "") or "pseudolife"
    except ValueError:
        db, user = "pseudolife_memory", "pseudolife"
    if not (_IDENT.match(db) and _IDENT.match(user)):
        raise MoveError("the daemon's database URL names a database or user this command does not handle "
                        "(lowercase letters, digits and underscores only); nothing was changed", EXIT_REFUSED)
    return db, user


def _container(inspect_text: str) -> dict | None:
    try:
        data = json.loads(inspect_text)
    except ValueError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _env_of(container: dict) -> dict[str, str]:
    env = {}
    for item in ((container.get("Config") or {}).get("Env") or []):
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    return env


def _size(n: int | None) -> str:
    if n is None:
        return "size unknown"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _new_move_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def _rows(text: str) -> list[list[str]]:
    return [line.split("|") for line in text.splitlines() if line.strip()]


# ── options and the move ────────────────────────────────────────────────────

@dataclass
class Options:
    target: str
    checkout: str = DEFAULT_CHECKOUT
    target_url: str | None = None
    keep_tokens: bool = True
    resume: bool = False
    dry_run: bool = False
    yes: bool = False
    as_json: bool = False
    health_delay: float = 2.0
    # A daemon's warmup thread builds storage at boot, and cached_bank_id()
    # backs off 60 s (BANK_ID_RETRY_SECONDS) after a failed or empty read,
    # so the fingerprint can stay null for over a minute after /health first
    # answers: three of those windows, with a nudge in each.
    health_timeout: float = 180.0
    nudge_every: float = 60.0


def _default_lock():
    from pseudolife_memory import update_cli
    return update_cli.UpdateLock()


class MoveInterrupted(BaseException):
    """A kill signal (SIGTERM, SIGHUP) arrived during the move: unwinds into
    the rollback like Ctrl-C does."""


# What the rollback must undo, in the order the move sets them; each flip
# is written to the move record with the manual rollback beside it.
FLAGS = ("target_timer_paused", "target_policy_paused", "schedule_paused", "source_stopped", "fenced",
         "marker_written", "target_restored", "gate_written", "env_backup", "env_written", "target_started",
         "source_policy_changed", "moved_written")


class Mover:
    def __init__(self, options: Options, runner: Runner | None = None, *, detect_schedule=None,
                 interactive=None, ask=None, sleep=None, clock=None, data_dir: Path | None = None, lock=None,
                 platform: str | None = None):
        from pseudolife_memory import connect_cli, update_cli

        self.o = options
        self.runner = runner or Runner()
        self.detect_schedule = detect_schedule or connect_cli.scheduled_update
        self.interactive = interactive or connect_cli.interactive
        self.ask = ask or connect_cli._ask
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.data_dir = Path(data_dir) if data_dir is not None else update_cli.data_dir()
        self.lock = lock or _default_lock
        self.platform = platform or sys.platform
        self.docker = update_cli.docker_cmd()
        self.move_id = _new_move_id()
        self.checkout = options.checkout.rstrip("/") or DEFAULT_CHECKOUT
        self.phase_code = EXIT_REFUSED     # what an ssh failure exits with before the source stops
        self.recording = False
        self.state = "planned"
        # what preflight learns
        self.source_bank: str | None = None
        self.source_meta = ""
        self.source_schema = None
        self.source_auth = None
        self.source_env: dict[str, str] = {}
        self.source_image = ""
        self.source_policy = ""
        self.db, self.db_user = "pseudolife_memory", "pseudolife"
        self.target_image = ""
        self.target_policy = ""
        self.target_old_tokens: list[str] = []
        self.target_registrations: list[str] | None = None
        self.target_timer_enabled = False
        self.target_db, self.target_user = "pseudolife_memory", "pseudolife"
        self.target_url: str | None = None
        self.needs_expose = False
        self.sizes: dict = {"database": None, "data": None}
        self.stored_principals: list[str] = []
        self.schedule = None
        self.schedule_enabled = False
        self.counts: dict[str, int] = {}
        self.board = {"leases": None, "mail": None}
        for flag in FLAGS:
            setattr(self, flag, None if flag == "env_backup" else False)
        self.exposed_now = False
        self.data = {"move_id": self.move_id, "dry_run": options.dry_run, "source": {}, "target": {},
                     "plan": [], "steps": [], "rollback": [], "report": {}, "connect": None,
                     "warnings": [], "error": None, "exit": None}

    # -- output ------------------------------------------------------------
    def say(self, line: str = "") -> None:
        if not self.o.as_json:
            print(line, flush=True)

    def step(self, line: str) -> None:
        self.data["steps"].append(line)
        self.say(f"==> {line}")

    def warn(self, line: str) -> None:
        self.data["warnings"].append(line)
        if not self.o.as_json:
            print(f"WARNING: {line}", file=sys.stderr, flush=True)

    def finish(self, code: int) -> int:
        self.data["exit"] = code
        if self.o.as_json:
            print(json.dumps(self.data, indent=2, default=str))
        return code

    def fail(self, code: int, message: str) -> int:
        self.data["error"] = message
        if not self.o.as_json:
            print(f"move: {message}", file=sys.stderr, flush=True)
        return self.finish(code)

    # -- commands ----------------------------------------------------------
    def local(self, argv, **kwargs) -> Result:
        return self.runner.run(argv, **kwargs)

    def ssh(self, command: str, **kwargs) -> Result:
        """``command`` on the target. Exit 255 is ssh's own failure (the
        connection dropped, or never came up), never the command's answer:
        it raises, so no caller can read it as "no such file"."""
        result = self.runner.run([*SSH_OPTS, self.o.target, command], **kwargs)
        if result.code == 255:
            raise SshLost(f"ssh to {self.o.target} failed while running `{command[:80]}` "
                          f"({result.why(out=False)}): move needs key-based ssh to the target that stays up",
                          self.phase_code)
        return result

    def psql_local(self, sql: str, database: str) -> Result:
        return self.local([self.docker, "exec", POSTGRES, "psql", "-v", "ON_ERROR_STOP=1", "-tA",
                           "-U", self.db_user, "-d", database, "-c", sql])

    def psql_remote(self, sql: str, database: str | None = None) -> Result:
        return self.ssh(f"docker exec {POSTGRES} psql -v ON_ERROR_STOP=1 -tA -U {shlex.quote(self.target_user)} "
                        f"-d {shlex.quote(database or self.target_db)} -c {shlex.quote(sql)}")

    @property
    def local_dir(self) -> Path:
        return self.data_dir / "moves" / self.move_id

    @property
    def remote_dir(self) -> str:
        return f"{self.checkout}/data/move-{self.move_id}"

    @property
    def marker_path(self) -> str:
        """The resume marker: on the target host, beside the copied backup,
        outside the daemon's /data (which the state restore replaces)."""
        return f"{self.remote_dir}/marker.json"

    @property
    def env_path(self) -> str:
        return f"{self.checkout}/ops/.env"

    def mark(self, **flags) -> None:
        """Flip progress flags and write them to the move record at once:
        a move killed at any point leaves what it had done, and how to undo
        it, on disk."""
        for name, value in flags.items():
            setattr(self, name, value)
        if self.recording:
            self.write_record(self.state)

    # -- entry -------------------------------------------------------------
    def run(self) -> int:
        target = self.o.target or ""
        if not target or target.startswith("-") or any(ch.isspace() or ord(ch) < 0x20 for ch in target):
            return self.fail(EXIT_USAGE, "--to takes an ssh target such as root@box or a ~/.ssh/config alias")
        if any(ch in self.checkout for ch in "\n\r\0"):
            return self.fail(EXIT_USAGE, "--target-checkout holds a control character")
        try:
            self.preflight()
            self.show_plan()
            if self.o.dry_run:
                self.say("dry run: nothing was changed")
                return self.finish(EXIT_OK)
            self.confirm()
            from pseudolife_memory import update_cli
            try:
                lock = self.lock()
                lock.__enter__()
            except update_cli.UpdateBusy as busy:
                raise MoveError(f"another pseudolife-mcp update is running (pid {busy.pid}); nothing was "
                                f"changed. Run move again when it has finished", EXIT_REFUSED) from None
            try:
                code = self.execute()
            finally:
                lock.__exit__(None, None, None)
        except MoveError as exc:
            return self.fail(exc.code, str(exc))
        return self.finish(code)

    # -- 1. preflight --------------------------------------------------------
    def refuse(self, message: str) -> MoveError:
        return MoveError(f"{message}. Nothing was changed", EXIT_REFUSED)

    def preflight(self) -> None:
        o = self.o
        health = self.runner.get_json(SOURCE_URL + "/health", timeout=5.0)
        if not isinstance(health, dict) or health.get("status") != "ok":
            status = f" (status: {health.get('status')})" if isinstance(health, dict) else ""
            raise self.refuse(f"the source daemon at {SOURCE_URL} did not answer /health with status ok{status}; "
                              f"move runs on the daemon's own host, with the daemon healthy")
        result = self.local([self.docker, "inspect", "--type", "container", DAEMON], timeout=60)
        container = _container(result.text) if result.ok else None
        if container is None:
            raise self.refuse(f"this host is not a Docker-tier daemon host (no {DAEMON} container: "
                              f"{result.why(out=False)}); move moves a Docker-tier bank, and a pip or lite bank "
                              f"moves with pseudolife-mcp export / import")
        if not (container.get("State") or {}).get("Running"):
            raise self.refuse(f"the {DAEMON} container is not running")
        env = _env_of(container)
        self.source_env = {key: env[key] for key in CARRIED_KEYS if key in env}
        self.source_image = str((container.get("Config") or {}).get("Image") or "")
        self.source_policy = _restart_policy(container)
        self.db, self.db_user = _dsn_parts(env.get("PSEUDOLIFE_MCP_DATABASE_URL"))
        bank = health.get("bank")
        if not isinstance(bank, str) or not bank:
            raise self.refuse("the source daemon reports no bank fingerprint (/health `bank` is missing or "
                              "null): it is older than this command, or has not loaded its bank identity yet. "
                              "Update the daemon, or make one memory call through it, then retry")
        self.source_bank, self.source_schema = bank, health.get("schema")
        self.source_auth = health.get("auth")
        if o.keep_tokens and self.source_auth is not True:
            raise self.refuse("the source daemon runs without a bearer token (auth: false): carrying its "
                              "identities would leave the target open on the network. Set "
                              "PSEUDOLIFE_MCP_TOKEN for the source daemon first, or move with --no-keep-tokens")
        result = self.psql_local(BANK_ID_SQL, self.db)
        self.source_meta = result.text.strip() if result.ok else ""
        if not self.source_meta:
            raise self.refuse(f"could not read the source bank's identity (meta coordination_bank_id: "
                              f"{result.why(out=False)})")
        self.data["source"] = {"bank": bank, "schema": self.source_schema, "database": self.db,
                               "image": self.source_image, "restart_policy": self.source_policy}

        result = self.ssh("true", timeout=60)
        if not result.ok:
            raise self.refuse(f"ssh -o BatchMode=yes {o.target} true failed ({result.why()}): move needs "
                              f"key-based ssh to the target (an ssh key loaded, the host key accepted)")
        result = self.ssh("command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && "
                          "command -v docker >/dev/null 2>&1", timeout=60)
        if not result.ok:
            raise self.refuse(f"the target must be a Linux or macOS Docker-tier checkout host: its login shell "
                              f"is POSIX and curl, python3 and docker are on PATH there (exit {result.code})")
        co = rq(self.checkout)
        result = self.ssh(f"test -f {co}/ops/restore.sh && test -f {co}/ops/update.sh && "
                          f"test -f {co}/ops/docker-compose.yml", timeout=60)
        if not result.ok:
            raise self.refuse(f"no Pseudolife-MCP checkout at {self.checkout} on {o.target}: install there "
                              f"first ({INSTALL_LINE}), or name the checkout with --target-checkout")
        result = self.ssh(f"grep -q -- --no-start {co}/ops/restore.sh", timeout=60)
        if not result.ok:
            raise self.refuse(f"the target checkout's ops/restore.sh has no --no-start: update the checkout "
                              f"on {o.target} (git pull) and retry")
        result = self.ssh(f"docker inspect --type container {DAEMON}", timeout=60)
        target = _container(result.text) if result.ok else None
        if target is None:
            raise self.refuse(f"the target has no {DAEMON} container ({result.why(out=False)}): run the "
                              f"installer there first ({INSTALL_LINE})")
        self.target_image = str((target.get("Config") or {}).get("Image") or "")
        self.target_policy = _restart_policy(target)
        target_env = _env_of(target)
        self.target_db, self.target_user = _dsn_parts(target_env.get("PSEUDOLIFE_MCP_DATABASE_URL"))
        from pseudolife_memory.principals import parse_token_map
        self.target_old_tokens = ([target_env["PSEUDOLIFE_MCP_TOKEN"]] if target_env.get("PSEUDOLIFE_MCP_TOKEN")
                                  else []) + list(parse_token_map(target_env.get("PSEUDOLIFE_MCP_TOKENS")))
        result = self.ssh(f"docker inspect -f '{{{{.State.Running}}}}' {POSTGRES}", timeout=60)
        if not result.ok or result.text.strip() != "true":
            raise self.refuse(f"the target's {POSTGRES} container is not running: start the stack there "
                              f"(docker compose -f ops/docker-compose.yml up -d) or run the installer")
        if not o.resume:
            result = self.ssh("curl -fsS --max-time 5 http://127.0.0.1:8765/health", timeout=60)
            try:
                target_health = json.loads(result.text) if result.ok else None
            except ValueError:
                target_health = None
            if not isinstance(target_health, dict) or target_health.get("status") != "ok":
                raise self.refuse(f"the target daemon on {o.target} did not answer /health with status ok "
                                  f"on its loopback port: start it there, or re-run its installer")
            schema = target_health.get("schema")
            if isinstance(self.source_schema, int) and (not isinstance(schema, int) or schema < self.source_schema):
                raise self.refuse(f"the target daemon's schema ({schema}) is older than the source's "
                                  f"({self.source_schema}): update the target first (git pull there, then "
                                  f"ops/update.sh)")
            if target_health.get("bank") == bank:
                raise self.refuse(f"the target daemon reports the same bank fingerprint as this one ({bank}): "
                                  f"the target already serves this bank (or a copy of it), and move never "
                                  f"runs two daemons on one bank")
        self.check_target_bank()
        self.read_exposure()
        self.read_sizes()
        self.read_stored_principals()
        self.read_schedules()
        self.read_target_registrations()
        self.data["target"] = {"ssh": o.target, "checkout": self.checkout, "url": self.target_url,
                               "needs_expose": self.needs_expose, "database": self.target_db,
                               "restart_policy": self.target_policy,
                               "unattended_update": self.target_timer_enabled}

    def check_target_bank(self) -> None:
        """The target bank is empty, or (``--resume``) a marker on the target
        names a move this host started from this bank."""
        if self.o.resume:
            markers = self.target_markers()
            if not markers:
                raise self.refuse(f"--resume needs this move's marker ({self.checkout}/data/move-<id>/marker.json "
                                  f"on the target), and the target has none: only a bank this command left "
                                  f"half-restored can be resumed")
            for marker in markers:
                move_id = marker.get("move_id")
                record = self.read_record(move_id) if isinstance(move_id, str) and _MOVE_ID.match(move_id) else None
                if (record is not None and marker.get("source_bank") == self.source_bank
                        and record.get("source_bank") == self.source_bank):
                    self.move_id = move_id
                    self.data["move_id"] = move_id
                    return
            named = ", ".join(f"{m.get('move_id')!r} from bank {m.get('source_bank')!r}" for m in markers)
            raise self.refuse(f"the target's move markers name {named}, none of them a move this host started "
                              f"from this bank: --resume overwrites only this move's own half-restored bank")
        result = self.psql_remote("SELECT relname FROM pg_class WHERE relkind = 'r' AND relnamespace = "
                                  "'public'::regnamespace AND relname IN ('entries', 'facts', 'principals') "
                                  "ORDER BY 1")
        if not result.ok:
            raise self.refuse(f"could not read the target bank ({result.why()})")
        tables = [line.strip() for line in result.text.splitlines() if line.strip()]
        counts = {}
        if tables:
            result = self.psql_remote("SELECT " + ", ".join(f"(SELECT count(*) FROM {t})" for t in tables))
            row = _rows(result.text)[0] if result.ok and _rows(result.text) else []
            if len(row) != len(tables):
                raise self.refuse(f"could not count the target bank's rows ({result.why()})")
            counts = dict(zip(tables, (int(value) if value.strip().isdigit() else -1 for value in row)))
        used = {table: n for table, n in counts.items() if n != 0}
        if used:
            listed = ", ".join(f"{table} {n}" for table, n in used.items())
            markers = self.target_markers()
            hint = (f"; it carries a move marker (move {markers[0].get('move_id')}), so if this host started that "
                    f"move, retry with --resume") if markers else ""
            raise self.refuse(f"the target bank is not empty ({listed}): move never merges into or "
                              f"overwrites a used bank{hint}")

    def target_markers(self) -> list[dict]:
        """Every move marker on the target (one JSON object per line); exit 1
        is cat's "none there"."""
        result = self.ssh(f"cat {rq(self.checkout)}/data/move-*/marker.json 2>/dev/null", timeout=60)
        markers = []
        for line in result.text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                markers.append(item)
        return markers

    def read_exposure(self) -> None:
        from pseudolife_memory import codex_connection

        if self.o.target_url:
            try:
                self.target_url = codex_connection._validated_daemon_url(self.o.target_url)
            except codex_connection.SetupError:
                raise MoveError("--target-url must be an http(s) origin such as http://100.64.0.2:8765", EXIT_USAGE)
            return
        result = self.ssh(f"cd {rq(self.checkout)} && python3 -m pseudolife_memory.cli expose status --json",
                          timeout=120)
        state, url = parse_expose_status(result.text if result.ok else "")
        if state == "exposed":
            self.target_url = url
        elif state == "not-exposed":
            self.needs_expose = True
        else:
            raise self.refuse(f"could not read the target's exposure (`pseudolife-mcp expose status` there: "
                              f"{result.why()}); pass --target-url <the URL clients will use>, or update the "
                              f"target checkout so it has `expose`")

    def read_sizes(self) -> None:
        result = self.psql_local(f"SELECT pg_database_size({_lit(self.db)})", "postgres")
        if result.ok and result.text.strip().isdigit():
            self.sizes["database"] = int(result.text.strip())
        result = self.local([self.docker, "exec", DAEMON, "du", "-sk", "/data"], timeout=120)
        head = result.text.split()[0] if result.ok and result.text.split() else ""
        if head.isdigit():
            self.sizes["data"] = int(head) * 1024
        self.data["source"]["sizes"] = dict(self.sizes)

    def read_stored_principals(self) -> None:
        """Invited machines (the ``principals`` table, when the bank has
        one): best effort, for the report."""
        result = self.psql_local("SELECT to_regclass('public.principals') IS NOT NULL", self.db)
        if not (result.ok and result.text.strip() == "t"):
            return
        result = self.psql_local("SELECT principal FROM principals WHERE revoked_at IS NULL ORDER BY 1", self.db)
        if result.ok:
            self.stored_principals = [line.strip() for line in result.text.splitlines() if line.strip()]

    def read_schedules(self) -> None:
        """The source's unattended update (paused for the move, when it is
        enabled now) and the target's (which could recreate the target's
        daemon mid-move)."""
        try:
            self.schedule = self.detect_schedule()
        except Exception:  # noqa: BLE001 - a schedule that cannot be read is not one to pause
            self.schedule = None
        if self.schedule:
            from pseudolife_memory import unattended_update
            if self.platform.startswith("win"):
                result = self.local(["schtasks", "/Query", "/TN", unattended_update.TASK_NAME, "/XML"], timeout=60)
                self.schedule_enabled = result.ok and "<enabled>false</enabled>" not in \
                    re.sub(r"\s", "", result.text).lower()
            else:
                result = self.local(["systemctl", "--user", "is-enabled", f"{unattended_update.UNIT_NAME}.timer"],
                                    timeout=60)
                self.schedule_enabled = result.text.strip() == "enabled"
        result = self.ssh("systemctl --user is-enabled pseudolife-update.timer 2>/dev/null", timeout=60)
        self.target_timer_enabled = result.text.strip() == "enabled"

    def read_target_registrations(self) -> None:
        """The files on the target that hold the token its installer minted,
        which the carried identities replace: found by feeding the tokens to
        grep on stdin, never naming them in a command line. Listed, never
        rewritten."""
        if not (self.o.keep_tokens and self.target_old_tokens):
            return
        result = self.ssh("grep -lsF -f - " + " ".join(TARGET_CLIENT_FILES),
                          stdin=("\n".join(self.target_old_tokens) + "\n").encode("utf-8"), timeout=60)
        self.target_registrations = [line.strip() for line in result.text.splitlines() if line.strip()]

    # -- 2. plan and confirm -----------------------------------------------
    def show_plan(self) -> None:
        o, url = self.o, self.target_url
        where = self.schedule.get("where") if isinstance(self.schedule, dict) and self.schedule_enabled else None
        pauses = []
        if self.target_timer_enabled:
            pauses.append("the target's unattended update")
        if self.target_policy not in ("", "no"):
            pauses.append(f"the target daemon's restart policy ({self.target_policy})")
        if where:
            pauses.append(f"this host's unattended update ({where})")
        plan = [
            f"source: this host's {DAEMON} (bank {self.source_bank}, schema {self.source_schema}); database "
            f"{self.db} {_size(self.sizes['database'])}, /data {_size(self.sizes['data'])}",
            f"target: {o.target}, checkout {self.checkout}"
            + (f" (--resume: over move {self.move_id}'s half-restored bank)" if o.resume else ""),
            f"target URL: {url}" if url else
            "target URL: not exposed yet; step 3 runs `pseudolife-mcp expose tailscale --yes` there",
            "1. preflight: done, nothing changed",
            ("3. expose the target (pseudolife-mcp expose tailscale --yes on the target), then check this host "
             "reaches <its URL>/health") if self.needs_expose else
            (f"3. the target daemon is stopped (--resume): {url}/health is checked after step 9 starts it"
             if o.resume else f"3. check this host reaches {url}/health"),
            "4. " + (f"pause {', '.join(pauses)}, then " if pauses else "")
            + f"stop the source daemon (docker stop -t 120 {DAEMON}; never rm or down; Postgres stays up)",
            f"5. final backup of the stopped source: pg_dump of {self.db} (about {_size(self.sizes['database'])} "
            f"before compression), then fence the database ({fence_sql(self.db, False)}) and check nothing is "
            f"still connected; the /data archive (about {_size(self.sizes['data'])}) and a manifest with "
            f"per-table row counts, under {self.local_dir}",
            f"6. copy the three files to {o.target}:{self.remote_dir} and check their SHA-256 there",
            "7. restore on the target without starting it (ops/restore.sh --apply --no-start --backup-file "
            "...; it takes its own safety dump first), check the restored bank identity and row counts, and "
            f"keep the daemon from starting outside the move ({MARKER})",
            ("8. carry PSEUDOLIFE_MCP_TOKEN, PSEUDOLIFE_MCP_TOKENS, PSEUDOLIFE_MCP_TIER_MAP and "
             "PSEUDOLIFE_MCP_TOOLSET from the source container into the target's ops/.env (backed up first; "
             "its installer-minted token is replaced)") if o.keep_tokens else
            "8. skipped (--no-keep-tokens): every environment principal must be invited on the target again",
            "9. start the target once (ops/update.sh), wait up to "
            f"{int(o.health_timeout)} s for its bank fingerprint to equal this bank's, check it runs with the "
            "carried identities, restore what step 4 paused on the target, stop this daemon restarting itself, "
            f"write {MOVED} into it",
            f"10. re-point this machine's clients: pseudolife-mcp connect {url or '<target URL>'} --yes",
            "11. report: the rollback, other machines' connect lines, held leases, undelivered mail, leftovers",
            "a failure in steps 4-9 rolls back: target stopped, its ops/.env and schedule restored, the fence "
            f"lifted, {MOVED} removed, the source's restart policy and schedule restored, the source started",
            "run it under tmux, screen or nohup: a closed terminal or a kill signal rolls the move back",
        ]
        self.data["plan"] = plan
        self.say(f"move: this host's bank -> {o.target}")
        for line in plan:
            self.say(f"  {line}")

    def confirm(self) -> None:
        if self.o.yes:
            return
        if not self.interactive():
            raise MoveError("this run is not interactive: re-run with --yes to apply the plan above "
                            "(nothing was changed)", EXIT_USAGE)
        if not self.ask("Move the bank as planned? [y/N] "):
            raise MoveError("not confirmed; nothing was changed", EXIT_USAGE)

    # -- the local move record (what --resume checks, and a killed move's trail) ----
    def read_record(self, move_id: str) -> dict | None:
        try:
            data = json.loads((self.data_dir / "moves" / move_id / "record.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_record(self, state: str) -> None:
        self.state = state
        try:
            self.local_dir.mkdir(parents=True, exist_ok=True)
            record = {"move_id": self.move_id, "target": self.o.target, "checkout": self.checkout,
                      "target_url": self.target_url, "source_bank": self.source_bank, "state": state,
                      "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "flags": {flag: getattr(self, flag) for flag in FLAGS},
                      "manual_rollback": self.manual_rollback()}
            temporary = self.local_dir / "record.json.tmp"
            temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.local_dir / "record.json")
        except OSError as exc:
            self.warn(f"could not write the move record in {self.local_dir} ({type(exc).__name__})")

    # -- signals -------------------------------------------------------------
    def _handle_kill(self, signum, _frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        raise MoveInterrupted(f"interrupted by {name}")

    def _set_signals(self, handler) -> dict:
        previous = {}
        for sig in _SIGNALS:
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):   # not the main thread, or not settable here
                pass
        return previous

    def _restore_signals(self, previous: dict) -> None:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass

    # -- 3-11 ----------------------------------------------------------------
    def execute(self) -> int:
        self.recording = True
        self.write_record("started")
        self.expose_and_reach()
        self.phase_code = EXIT_FAILED
        previous = {}
        for sig in _SIGNALS:
            if sig is signal.SIGINT:
                continue           # Ctrl-C already unwinds as KeyboardInterrupt
            try:
                previous[sig] = signal.signal(sig, self._handle_kill)
            except (ValueError, OSError):
                pass
        try:
            try:
                self.write_record("moving")
                self.stop_source()
                self.final_backup()
                self.copy_files()
                self.restore_target()
                self.carry_identities()
                self.start_target()
            except BaseException as exc:  # noqa: BLE001 - every failure here, a signal included, rolls back
                reason = str(exc) if isinstance(exc, (MoveError, MoveInterrupted)) and str(exc) else \
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                ignored = self._set_signals(signal.SIG_IGN)
                try:
                    complete = self.rollback()
                    self.write_record("rolled_back" if complete else "rollback_incomplete")
                finally:
                    self._restore_signals(ignored)
                if complete:
                    raise MoveError(f"{reason}. Rolled back: the source daemon runs again and the target is "
                                    f"stopped; the target keeps its move marker, so `move --resume` may overwrite "
                                    f"that half-restored bank on the next attempt", EXIT_FAILED) from None
                raise MoveError(f"{reason}. The rollback is INCOMPLETE (see the rollback steps, and the move "
                                f"record {self.local_dir / 'record.json'}): finish it by hand with the commands "
                                f"listed there", EXIT_INCOMPLETE) from None
        finally:
            self._restore_signals(previous)
        self.write_record("moved")
        code = self.repoint()
        self.final_report()
        return code

    def expose_and_reach(self) -> None:
        o = self.o
        if self.needs_expose:
            self.step(f"exposing the target: pseudolife-mcp expose tailscale --yes on {o.target}")
            result = self.ssh(f"cd {rq(self.checkout)} && python3 -m pseudolife_memory.cli expose tailscale "
                              f"--yes --json", timeout=300)
            if not result.ok:
                raise MoveError(f"`pseudolife-mcp expose tailscale` on {o.target} failed (exit {result.code}: "
                                f"{result.why()}). Fix that there and retry; nothing was changed and the source "
                                f"still runs", EXIT_REFUSED)
            self.exposed_now = True
            state, url = parse_expose_status(result.text)
            if state != "exposed":
                again = self.ssh(f"cd {rq(self.checkout)} && python3 -m pseudolife_memory.cli expose status "
                                 f"--json", timeout=120)
                state, url = parse_expose_status(again.text if again.ok else "")
            if state != "exposed" or not url:
                raise MoveError(f"`expose tailscale` on {o.target} reported no URL; check `pseudolife-mcp expose "
                                f"status` there and retry with --target-url. The source still runs", EXIT_REFUSED)
            self.target_url = url
            self.data["target"]["url"] = url
        if o.resume:
            self.step("--resume: the target daemon is stopped; its URL is checked once step 9 starts it")
            return
        health = self.runner.get_json(f"{self.target_url}/health", timeout=10.0)
        if not isinstance(health, dict) or health.get("status") != "ok":
            undo = (f" The target stays exposed (`pseudolife-mcp expose off` on {o.target} undoes it)."
                    if self.exposed_now else "")
            raise MoveError(f"this host cannot reach {self.target_url}/health: the URL clients will use must "
                            f"answer from here before the source stops. Nothing in either bank was changed; "
                            f"the source still runs.{undo}", EXIT_REFUSED)
        self.step(f"this host reaches {self.target_url}/health")

    def stop_source(self) -> None:
        if self.target_timer_enabled:
            self.mark(target_timer_paused=True)
            result = self.ssh("systemctl --user disable --now pseudolife-update.timer", timeout=120)
            if not result.ok:
                raise MoveError(f"could not pause the target's unattended update ({result.why()})")
            self.step("paused the target's unattended update")
        if self.target_policy not in ("", "no"):
            self.mark(target_policy_paused=True)
            result = self.ssh(f"docker update --restart=no {DAEMON}", timeout=120)
            if not result.ok:
                raise MoveError(f"could not pause the target daemon's restart policy ({result.why()})")
        if self.schedule and self.schedule_enabled:
            self.mark(schedule_paused=True)
            argv = self._schedule_argv(enable=False)
            self.step(f"pausing this host's unattended update ({self.schedule.get('where')}): {' '.join(argv)}")
            result = self.local(argv, timeout=120)
            if not result.ok:
                raise MoveError(f"could not pause the unattended update ({result.why()})")
        self.mark(source_stopped=True)
        self.step(f"stopping the source daemon (docker stop -t 120 {DAEMON}; Postgres stays up)")
        result = self.local([self.docker, "stop", "-t", "120", DAEMON], timeout=300)
        if not result.ok:
            raise MoveError(f"docker stop {DAEMON} failed ({result.why()})")
        result = self.local([self.docker, "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", DAEMON],
                            timeout=60)
        words = result.text.split()
        if not words or words[0] != "false":
            raise MoveError(f"{DAEMON} still reports running after docker stop")
        if len(words) > 1 and words[1] == "137":
            self.warn(f"the source daemon did not stop within 120 s and was killed (exit 137): its last unsaved "
                      f"in-memory state is lost; the bank in Postgres is what moves")

    def _schedule_argv(self, enable: bool) -> list[str]:
        from pseudolife_memory import unattended_update

        if self.platform.startswith("win"):
            return ["schtasks", "/Change", "/TN", unattended_update.TASK_NAME, "/ENABLE" if enable else "/DISABLE"]
        return ["systemctl", "--user", "enable" if enable else "disable", "--now",
                f"{unattended_update.UNIT_NAME}.timer"]

    def _backends(self) -> str:
        result = self.psql_local(f"SELECT count(*) FROM pg_stat_activity WHERE datname = {_lit(self.db)} "
                                 f"AND pid <> pg_backend_pid()", "postgres")
        return result.text.strip() if result.ok else "unreadable"

    def final_backup(self) -> None:
        directory = self.local_dir
        directory.mkdir(parents=True, exist_ok=True)
        dump, part = directory / DUMP_NAME, directory / (DUMP_NAME + ".part")
        # Writers other than the daemon (a psql window, a Console that kept
        # a connection, lease break) are cut off before the dump.
        self.psql_local(terminate_sql(self.db), "postgres")
        self.step(f"final backup: pg_dump of {self.db} from the stopped source")
        result = self.local([self.docker, "exec", POSTGRES, "pg_dump", "-U", self.db_user, "-d", self.db, "-Z9"],
                            stdout_file=part, timeout=_TIMEOUT_LONG)
        if not result.ok or not part.is_file() or part.stat().st_size == 0:
            raise MoveError(f"pg_dump failed inside {POSTGRES} ({result.why()})")
        complete, counts = scan_dump(part)
        if not complete:
            raise MoveError(f"the final dump is INCOMPLETE (pg_dump's end-of-dump marker is missing); the "
                            f"rejected artifact is {part}")
        part.replace(dump)
        self.counts = counts
        # The fence follows the dump at once: every direct-database writer
        # (lease break, board-audit, an older daemon image) stops here.
        self.mark(fenced=True)
        result = self.psql_local(fence_sql(self.db, False), "postgres")
        if not result.ok:
            raise MoveError(f"could not fence the source database ({result.why()})")
        for attempt in range(10):
            self.psql_local(terminate_sql(self.db), "postgres")
            backends = self._backends()
            if backends == "0":
                break
            self.sleep(1.0)
        else:
            raise MoveError(f"backends are still connected to {self.db} after the fence (pg_stat_activity "
                            f"counts {backends}): something kept writing; find it before moving")
        self.step(f"fenced the source database: {fence_sql(self.db, False)}")
        state, state_part = directory / STATE_NAME, directory / (STATE_NAME + ".part")
        result = self.local([self.docker, "run", "--rm", "--entrypoint", "tar", "--volumes-from", DAEMON,
                             self.source_image, "czf", "-", "--exclude=./moved.json", "--exclude=./move.json",
                             "-C", "/data", "."], stdout_file=state_part, timeout=_TIMEOUT_LONG)
        if not result.ok or not state_part.is_file() or state_part.stat().st_size == 0:
            raise MoveError(f"the /data archive failed ({result.why()})")
        try:
            with tarfile.open(state_part) as tar:
                names = [member.name for member in tar.getmembers()]
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise MoveError(f"the /data archive is unreadable ({type(exc).__name__})") from None
        markers = [name for name in names if name.lstrip("./") in ("moved.json", "move.json")]
        if markers:
            raise MoveError(f"the /data archive carries a move marker ({', '.join(markers)}); refusing to copy it")
        state_part.replace(state)
        manifest = {"move_id": self.move_id, "source_bank": self.source_bank, "schema": self.source_schema,
                    "database": self.db, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "dump": DUMP_NAME, "dump_sha256": _sha256(dump), "state": STATE_NAME,
                    "state_sha256": _sha256(state), "tables": counts}
        (directory / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.step(f"final backup: {dump.name} ({_size(dump.stat().st_size)}, {len(counts)} tables), "
                  f"{state.name} ({_size(state.stat().st_size)}), {MANIFEST_NAME}, in {directory}")

    def copy_files(self) -> None:
        result = self.ssh(f"mkdir -p {rq(self.remote_dir)}", timeout=60)
        if not result.ok:
            raise MoveError(f"could not create {self.remote_dir} on the target ({result.why()})")
        for name in (DUMP_NAME, STATE_NAME, MANIFEST_NAME):
            path, remote = self.local_dir / name, f"{self.remote_dir}/{name}"
            result = self.ssh(f"cat > {rq(remote)}", stdin_file=path, timeout=_TIMEOUT_LONG)
            if not result.ok:
                raise MoveError(f"copying {name} to the target failed ({result.why()})")
            self.verify_remote(remote, _sha256(path), name)
        self.step(f"copied the final backup to {self.o.target}:{self.remote_dir} (SHA-256 checked there)")

    def verify_remote(self, remote: str, digest: str, name: str) -> None:
        result = self.ssh(f"sha256sum {rq(remote)} 2>/dev/null || shasum -a 256 {rq(remote)}", timeout=600)
        words = result.text.split() if result.ok else []
        got = words[0].lower() if words else None
        if got != digest:
            raise MoveError(f"{name} did not arrive intact on the target (SHA-256 {got or 'unreadable'}, "
                            f"expected {digest})")

    def write_gate(self) -> Result:
        """``/data/move.json`` on the target: a daemon refuses to start while
        it is there (``daemon.moved_refusal``)."""
        gate = {"move_id": self.move_id, "source_bank": self.source_bank,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return self.ssh(f"docker run --rm -i --entrypoint sh --volumes-from {DAEMON} "
                        f"{shlex.quote(self.target_image)} -c {shlex.quote('cat > ' + MARKER)}",
                        stdin=(json.dumps(gate) + "\n").encode("utf-8"), timeout=300)

    def restore_target(self) -> None:
        marker = {"move_id": self.move_id, "source_bank": self.source_bank,
                  "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.mark(marker_written=True)
        result = self.ssh(f"cat > {rq(self.marker_path)}", stdin=(json.dumps(marker) + "\n").encode("utf-8"),
                          timeout=60)
        if not result.ok:
            raise MoveError(f"could not write the move marker on the target ({result.why()})")
        dump, state = f"{self.remote_dir}/{DUMP_NAME}", f"{self.remote_dir}/{STATE_NAME}"
        self.step("restoring on the target without starting it (ops/restore.sh --apply --no-start)")
        self.mark(target_restored=True)
        result = self.ssh(f"cd {rq(self.checkout)} && bash ops/restore.sh --apply --no-start --backup-file "
                          f"{rq(dump)} --state-archive {rq(state)} --db {shlex.quote(self.target_db)} "
                          f"--user {shlex.quote(self.target_user)}", timeout=_TIMEOUT_LONG)
        if not result.ok:
            raise MoveError(f"ops/restore.sh on the target failed ({result.why()})")
        # Deterministic, while the target is still stopped: the restored
        # bank's identity is the source's (no daemon, no cache, no backoff).
        result = self.psql_remote(BANK_ID_SQL)
        restored = result.text.strip() if result.ok else ""
        if restored != self.source_meta:
            raise MoveError(f"the restored bank's identity (meta coordination_bank_id) is "
                            f"{restored or 'unreadable'}, not the source's {self.source_meta}")
        tables = sorted(self.counts)
        bad = [table for table in tables if not _TABLE.match(table)]
        if bad:
            raise MoveError(f"cannot compare the restored row counts of {', '.join(bad)}")
        if tables:
            result = self.psql_remote(" UNION ALL ".join(f"SELECT {_lit(t)}, count(*) FROM {t}" for t in tables))
            if not result.ok:
                raise MoveError(f"could not count the restored rows on the target ({result.why()})")
            counted = {row[0]: int(row[1]) for row in _rows(result.text) if len(row) == 2 and row[1].isdigit()}
            wrong = [f"{t} {counted.get(t, 'missing')} != {self.counts[t]}" for t in tables
                     if counted.get(t) != self.counts[t]]
            if wrong:
                raise MoveError(f"the restored bank does not match the final backup's manifest: {', '.join(wrong)}")
        self.step(f"restored on the target; its bank identity and {len(tables)} tables' row counts match")
        self.mark(gate_written=True)
        result = self.write_gate()
        if not result.ok:
            raise MoveError(f"could not write {MARKER} on the target ({result.why()})")
        self.read_board()

    def read_board(self) -> None:
        """Held leases and undelivered mail in the restored bank: best
        effort, for the report."""
        result = self.psql_remote("SELECT name, holder_principal FROM coordination_leases WHERE holder_agent_id "
                                  "IS NOT NULL AND (expires_at IS NULL OR expires_at > extract(epoch FROM now())) "
                                  "ORDER BY name")
        if result.ok:
            self.board["leases"] = [(row[0], row[1] if len(row) > 1 else "") for row in _rows(result.text)]
        result = self.psql_remote("SELECT recipient_agent_id, count(*) FROM coordination_messages WHERE "
                                  "acknowledged_at IS NULL AND expires_at > extract(epoch FROM now()) GROUP BY 1 "
                                  "ORDER BY 1")
        if result.ok:
            self.board["mail"] = [(row[0], int(row[1])) for row in _rows(result.text)
                                  if len(row) == 2 and row[1].isdigit()]

    def carried(self) -> dict[str, str]:
        values = {}
        for key in CARRIED_KEYS:
            if key in self.source_env:
                if key == "PSEUDOLIFE_MCP_TOOLSET" and not self.source_env[key]:
                    continue
                values[key] = self.source_env[key]
            elif key != "PSEUDOLIFE_MCP_TOOLSET":
                values[key] = ""
        return values

    def carry_identities(self) -> None:
        if not self.o.keep_tokens:
            self.step("not carrying the environment identities (--no-keep-tokens)")
            return
        env = self.env_path
        # One command tells the cases apart: 0 present (its content follows),
        # 3 missing; anything else, ssh's 255 included, stops the step. A file
        # that exists must never be taken for a missing one: it holds the
        # Postgres password and the volume names.
        result = self.ssh(f"if [ -e {rq(env)} ]; then cat {rq(env)}; else exit 3; fi", timeout=60)
        if result.code == 0:
            existed, current = True, result.text
        elif result.code == 3:
            existed, current = False, ""
        else:
            raise MoveError(f"could not read the target's ops/.env (exit {result.code}: {result.why(out=False)})")
        content = merge_env(current, self.carried(), self.move_id)
        if existed:
            backup = f"{env}.pre-move-{self.move_id}"
            result = self.ssh(f"cp -p {rq(env)} {rq(backup)}", timeout=60)
            if not result.ok:
                raise MoveError(f"could not back up the target's ops/.env ({result.why()})")
            self.mark(env_backup=backup)
        self.mark(env_written=True)
        temporary = f"{env}.move-{self.move_id}.tmp"
        data = content.encode("utf-8")
        if existed:
            # A copy of the original keeps its owner and mode; it is rewritten
            # in place and renamed over the original.
            command = f"cp -p {rq(env)} {rq(temporary)} && cat > {rq(temporary)} && mv -f {rq(temporary)} {rq(env)}"
        else:
            command = f"umask 077 && cat > {rq(temporary)} && mv -f {rq(temporary)} {rq(env)}"
        result = self.ssh(command, stdin=data, timeout=60)
        if not result.ok:
            raise MoveError(f"writing the target's ops/.env failed ({result.why()})")
        self.verify_remote(env, hashlib.sha256(data).hexdigest(), "ops/.env")
        self.step("carried the source's environment identities into the target's ops/.env"
                  + (f" (its previous copy: {self.env_backup})" if self.env_backup else ""))

    def _nudge_token(self) -> str | None:
        if not self.o.keep_tokens:
            return None
        token = self.source_env.get("PSEUDOLIFE_MCP_TOKEN")
        if not token:
            from pseudolife_memory.principals import parse_token_map
            token = next(iter(parse_token_map(self.source_env.get("PSEUDOLIFE_MCP_TOKENS"))), None)
        return token or None

    def wait_target(self) -> str:
        """The target's ``/health`` ``bank`` once it is non-null, polled
        against a deadline: a cold daemon reports null until it has read its
        bank identity, and backs off after an empty read, so an authenticated
        read (with a carried token) nudges it once a minute."""
        deadline = self.clock() + self.o.health_timeout
        token = self._nudge_token()
        healthy, last_nudge = False, None
        while True:
            health = self.runner.get_json(f"{self.target_url}/health", timeout=10.0)
            if isinstance(health, dict) and health.get("status") == "ok":
                healthy = True
                bank = health.get("bank")
                if isinstance(bank, str) and bank:
                    return bank
                if token and (last_nudge is None or self.clock() - last_nudge >= self.o.nudge_every):
                    self.runner.get_json(f"{self.target_url}/api/stats", token=token, timeout=60.0)
                    last_nudge = self.clock()
            if self.clock() >= deadline:
                break
            self.sleep(self.o.health_delay)
        if not healthy:
            raise MoveError(f"the target did not report healthy at {self.target_url}/health within "
                            f"{int(self.o.health_timeout)} s")
        raise MoveError(f"the target's /health reported no bank fingerprint within {int(self.o.health_timeout)} s "
                        f"(null means unknown, so it cannot be shown to serve this bank)")

    def check_target_env(self) -> None:
        """The recreated target daemon runs with the carried identities:
        each value is compared by SHA-256, read from ``docker inspect`` on
        ssh's stdout and never named in a command line."""
        if not self.o.keep_tokens:
            return
        result = self.ssh(f"docker inspect --type container {DAEMON}", timeout=60)
        container = _container(result.text) if result.ok else None
        if container is None:
            raise MoveError(f"could not read the target daemon's environment ({result.why(out=False)})")
        live = _env_of(container)
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()  # noqa: E731
        wrong = [key for key, value in self.carried().items() if digest(live.get(key, "")) != digest(value)]
        if wrong:
            raise MoveError(f"the target daemon does not run with the carried {', '.join(wrong)} (a shell "
                            f"variable or another env file on the target may shadow ops/.env)")

    def start_target(self) -> None:
        if self.gate_written:
            result = self.ssh(f"docker run --rm --entrypoint rm --volumes-from {DAEMON} "
                              f"{shlex.quote(self.target_image)} -f {MARKER}", timeout=300)
            if not result.ok:
                raise MoveError(f"could not lift {MARKER} on the target ({result.why()})")
            self.mark(gate_written=False)
        self.mark(target_started=True)
        self.step("starting the target once: ops/update.sh (recreates it with the carried environment)")
        result = self.ssh(f"cd {rq(self.checkout)} && bash ops/update.sh", timeout=_TIMEOUT_LONG)
        if not result.ok:
            raise MoveError(f"ops/update.sh on the target failed ({result.why()})")
        bank = self.wait_target()
        if bank != self.source_bank:
            raise MoveError(f"the target reports bank {bank}, not this bank's {self.source_bank}: it is not "
                            f"serving the restored bank")
        self.step(f"the target serves this bank (fingerprint {bank})")
        self.check_target_env()
        # The target is the live daemon now: what step 4 paused there goes
        # back as it was. A failure here is reported, not rolled back.
        if self.target_policy_paused:
            result = self.ssh(f"docker update --restart={shlex.quote(self.target_policy)} {DAEMON}", timeout=120)
            if result.ok:
                self.mark(target_policy_paused=False)
            else:
                self.warn(f"could not restore the target daemon's restart policy: run docker update "
                          f"--restart={self.target_policy} {DAEMON} on {self.o.target}")
        if self.target_timer_paused:
            result = self.ssh("systemctl --user enable --now pseudolife-update.timer", timeout=120)
            if result.ok:
                self.mark(target_timer_paused=False)
            else:
                self.warn(f"could not resume the target's unattended update: run systemctl --user enable --now "
                          f"pseudolife-update.timer on {self.o.target}")
        if self.source_policy not in ("", "no"):
            self.mark(source_policy_changed=True)
            result = self.local([self.docker, "update", "--restart=no", DAEMON], timeout=120)
            if not result.ok:
                raise MoveError(f"could not stop the source daemon restarting itself ({result.why()})")
        moved = self.local_dir / "moved.json"
        moved.write_text(json.dumps({"moved_to": self.target_url, "move_id": self.move_id,
                                     "bank": self.source_bank,
                                     "moved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                    indent=2) + "\n", encoding="utf-8")
        self.mark(moved_written=True)
        result = self.local([self.docker, "cp", str(moved), f"{DAEMON}:{MOVED}"], timeout=120)
        if not result.ok:
            raise MoveError(f"could not write {MOVED} into the stopped source container ({result.why()})")
        self.step(f"wrote {MOVED} into the stopped source container")
        # The very last action: without the marker, --resume no longer takes
        # this bank over.
        result = self.ssh(f"rm -f {rq(self.marker_path)}", timeout=60)
        if result.ok:
            self.mark(marker_written=False)
        else:
            self.warn(f"could not remove the move marker {self.marker_path} on {self.o.target}; remove it there")

    # -- rollback ----------------------------------------------------------
    def rollback(self) -> bool:
        """Undo, in the fixed order; ``True`` when every step succeeded."""
        log = self.data["rollback"]

        def attempt(label: str, run) -> bool:
            try:
                result = run()
                ok, detail = result.ok, ("" if result.ok else result.why())
            except Exception as exc:  # noqa: BLE001 - a failed undo is reported, never raised
                ok, detail = False, (str(exc)[:300] if isinstance(exc, MoveError) else type(exc).__name__)
            log.append({"step": label, "ok": ok, "detail": detail})
            self.say(f"  rollback: {label}: {'done' if ok else 'FAILED ' + detail}")
            return ok

        self.say("==> rolling back")
        complete, can_start = True, True
        if self.target_started:
            if not attempt(f"stop the target daemon ({self.o.target})",
                           lambda: self.ssh(f"docker stop {DAEMON}", timeout=300)):
                log.append({"step": "the source stays stopped: two daemons must never serve one bank", "ok": False,
                            "detail": f"stop the target by hand (ssh {self.o.target} docker stop {DAEMON}), "
                                      f"then: {'; '.join(self.manual_rollback()[1:])}"})
                return False
            self.mark(target_started=False)
        if self.target_restored and not self.gate_written:
            if attempt(f"keep the target from starting outside the move ({MARKER})", self.write_gate):
                self.mark(gate_written=True)
            else:
                complete = False
        if self.env_written:
            if self.env_backup:
                ok = attempt("restore the target's ops/.env",
                             lambda: self.ssh(f"cp -p {rq(self.env_backup)} {rq(self.env_path)}", timeout=60))
            else:
                ok = attempt("remove the target's ops/.env this move wrote",
                             lambda: self.ssh(f"rm -f {rq(self.env_path)}", timeout=60))
            if ok:
                self.mark(env_written=False)
            complete = complete and ok
        if self.target_timer_paused:
            if attempt("resume the target's unattended update",
                       lambda: self.ssh("systemctl --user enable --now pseudolife-update.timer", timeout=120)):
                self.mark(target_timer_paused=False)
            else:
                complete = False
        if self.target_policy_paused:
            if attempt(f"restore the target daemon's restart policy ({self.target_policy})",
                       lambda: self.ssh(f"docker update --restart={shlex.quote(self.target_policy)} {DAEMON}",
                                        timeout=120)):
                self.mark(target_policy_paused=False)
            else:
                complete = False
        if self.fenced:
            if attempt("lift the source database fence", lambda: self.psql_local(fence_sql(self.db, True), "postgres")):
                self.mark(fenced=False)
            else:
                complete = can_start = False
        if self.moved_written:
            if attempt(f"remove the source's {MOVED}",
                       lambda: self.local([self.docker, "run", "--rm", "--entrypoint", "rm", "--volumes-from",
                                           DAEMON, self.source_image, "-f", MOVED], timeout=300)):
                self.mark(moved_written=False)
            else:
                complete = can_start = False
        if self.source_policy_changed:
            if attempt(f"restore the source daemon's restart policy ({self.source_policy})",
                       lambda: self.local([self.docker, "update", f"--restart={self.source_policy}", DAEMON],
                                          timeout=120)):
                self.mark(source_policy_changed=False)
            else:
                complete = False
        if self.schedule_paused:
            if attempt("resume this host's unattended update",
                       lambda: self.local(self._schedule_argv(enable=True), timeout=120)):
                self.mark(schedule_paused=False)
            else:
                complete = False
        if self.source_stopped:
            if can_start:
                started = attempt("start the source daemon", lambda: self.local([self.docker, "start", DAEMON],
                                                                                 timeout=300))
                complete = complete and started
                if started:
                    self.mark(source_stopped=False)
                    deadline = self.clock() + 120.0
                    while self.clock() < deadline:
                        health = self.runner.get_json(SOURCE_URL + "/health", timeout=5.0)
                        if isinstance(health, dict) and health.get("status") == "ok":
                            break
                        self.sleep(self.o.health_delay)
                    else:
                        self.warn(f"the source daemon was started but has not reported healthy yet: "
                                  f"docker logs {DAEMON}")
            else:
                log.append({"step": "the source stays stopped", "ok": False,
                            "detail": "it cannot start while the fence or moved.json is in place; finish those by "
                                      f"hand, then: docker start {DAEMON}"})
        return complete

    def manual_rollback(self) -> list[str]:
        """The rollback by hand, in order. The psql line puts its SQL in
        double quotes with none inside, so it reads the same in sh, cmd.exe
        and PowerShell."""
        lines = [f"ssh {self.o.target} docker stop {DAEMON}",
                 f'docker exec {POSTGRES} psql -U {self.db_user} -d postgres -c "{fence_sql(self.db, True)}"',
                 f"docker run --rm --entrypoint rm --volumes-from {DAEMON} {self.source_image} -f {MOVED}"]
        if self.source_policy not in ("", "no"):
            lines.append(f"docker update --restart={self.source_policy} {DAEMON}")
        if self.schedule and self.schedule_enabled:
            lines.append(" ".join(self._schedule_argv(enable=True)))
        lines += [f"docker start {DAEMON}", f"pseudolife-mcp connect {SOURCE_URL} --yes"]
        return lines

    # -- 10. re-point --------------------------------------------------------
    def repoint(self) -> int:
        url = self.target_url
        self.step(f"re-pointing this machine's clients: pseudolife-mcp connect {url} --yes")
        result = self.local([sys.executable, "-m", "pseudolife_memory.cli", "connect", url, "--yes", "--json"],
                            timeout=900)
        try:
            reply = json.loads(result.text)
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            reply = {}
        error = reply.get("error") if isinstance(reply.get("error"), str) else None
        rows = reply.get("rows") if isinstance(reply.get("rows"), list) else []
        self.data["connect"] = {"exit": result.code, "error": error,
                                "rows": [{"client": row.get("client"), "place": row.get("place"),
                                          "state": row.get("state")} for row in rows if isinstance(row, dict)]}
        for row in self.data["connect"]["rows"]:
            self.say(f"  {row['state']:<8} {row['client']} {row['place']}")
        if result.code == 0:
            return EXIT_OK
        if result.code == 3:
            self.step("connect found no client registration on this machine to re-point")
            return EXIT_OK
        self.data["error"] = (f"the bank moved, but re-pointing this machine's clients failed (connect exit "
                              f"{result.code}: {error or result.why()}); rerun: pseudolife-mcp connect {url} --yes")
        if not self.o.as_json:
            print(f"move: {self.data['error']}", file=sys.stderr, flush=True)
        return EXIT_PARTIAL

    # -- 11. report ----------------------------------------------------------
    def final_report(self) -> None:
        from pseudolife_memory.principals import parse_token_map

        o, url = self.o, self.target_url
        environment = []
        if self.source_env.get("PSEUDOLIFE_MCP_TOKEN"):
            environment.append("default")
        for principal in parse_token_map(self.source_env.get("PSEUDOLIFE_MCP_TOKENS")).values():
            if principal not in environment:
                environment.append(principal)
        principals, reinvite = [], []
        if o.keep_tokens:
            principals += [{"principal": p, "kind": "environment", "line": f"pseudolife-mcp connect {url}"}
                           for p in environment]
        else:
            for principal in environment:
                name = "<a name for that machine>" if principal == "default" else principal
                reinvite.append({"principal": principal,
                                 "line": f"ssh {o.target} pseudolife-mcp invite {name}, then on that machine: "
                                         f"pseudolife-mcp connect {url} --code <the code>"})
        known = {p["principal"] for p in principals}
        principals += [{"principal": p, "kind": "stored", "line": f"pseudolife-mcp connect {url}"}
                       for p in self.stored_principals if p not in known]
        leases = [{"name": name, "holder_principal": holder,
                   "line": f"ssh {o.target} docker exec {DAEMON} python -m pseudolife_memory.cli lease break {name}"}
                  for name, holder in (self.board["leases"] or [])]
        mail = {"addresses": [{"agent_id": agent, "pending": n} for agent, n in (self.board["mail"] or [])],
                "read": self.board["mail"] is not None,
                "pointer": ("sessions get new board addresses on the new URL; mail sent to an old address stays "
                            "in the bank, undelivered. To give an address back: pseudolife-mcp "
                            "coordination-recovery rebind (docs/guide/coordination-recovery.md)")}
        notes = [f"the source is stopped and fenced, not deleted; the final backup stays in {self.local_dir}, "
                 f"and the target's copy in {o.target}:{self.remote_dir}"]
        if o.keep_tokens:
            using = (f"; these files on {o.target} held it and need re-pointing there (move did not rewrite them): "
                     f"{', '.join(self.target_registrations)}" if self.target_registrations else
                     ("; no client file on the target held it" if self.target_registrations is not None else ""))
            notes.append(f"the target's own token, minted by its installer, was replaced by the source's"
                         + (f" (its previous ops/.env: {self.env_backup})" if self.env_backup else "") + using
                         + f". Re-point a client there with pseudolife-mcp connect {url} --token-file "
                           f"<a file holding one of the carried tokens>")
        report = {
            "rollback": self.manual_rollback(),
            "principals": principals,
            "reinvite": reinvite,
            "leases": leases,
            "mail": mail,
            "target_registrations": self.target_registrations or [],
            "leftovers": self.leftovers(),
            "replaced_on_target": [
                "config.yaml: the source's daemon settings (extractor endpoints, updates, coordination) replaced "
                "the target's; review them there (an extractor URL naming host.docker.internal now means the "
                "target host)",
                "last-backup.json: /health's last_backup names the source's last backup until the target records "
                "a backup of its own",
            ],
            "notes": notes,
        }
        self.data["report"] = report
        self.say("")
        self.say(f"moved: the bank now lives on {o.target} at {url}")
        for note in notes:
            self.say(f"  {note}")
        self.say("rollback, if ever needed (in this order):")
        for line in report["rollback"]:
            self.say(f"  {line}")
        if principals:
            self.say("other machines: run this on each (the principals the bank knows):")
            for item in principals:
                self.say(f"  {item['principal']} ({item['kind']}): {item['line']}")
        if reinvite:
            self.say("not carried (--no-keep-tokens): invite each of these on the target again:")
            for item in reinvite:
                self.say(f"  {item['principal']}: {item['line']}")
        if leases:
            self.say("board leases still held by the old sessions (claim leases last 24 h):")
            for item in leases:
                self.say(f"  {item['name']} (held by {item['holder_principal']}): {item['line']}")
        if mail["addresses"]:
            self.say("undelivered mail by old board address:")
            for item in mail["addresses"]:
                self.say(f"  {item['agent_id']}: {item['pending']}")
        elif not mail["read"]:
            self.say("undelivered mail: could not be read from the restored bank")
        self.say(f"  {mail['pointer']}")
        self.say("leftovers on this host to retire by hand:")
        for item in report["leftovers"]:
            self.say(f"  {item['what']}: {item['command']}")
        self.say("what the state archive replaced on the target:")
        for line in report["replaced_on_target"]:
            self.say(f"  {line}")

    def leftovers(self) -> list[dict]:
        windows = self.platform.startswith("win")
        unattended = "the unattended update" + (" (paused by this move; it would recreate the old daemon)"
                                                if self.schedule_paused else "")
        return [
            {"what": "the daily backup task (it would dump a fenced database)",
             "command": ("Unregister-ScheduledTask -TaskName 'Pseudolife-MCP daily backup' -Confirm:$false"
                         if windows else "crontab -e (remove the ops/backup.sh line), or the timer that runs it")},
            {"what": "the extractor shims serving the old daemon (the moved daemon's extractor settings may name "
                     "host.docker.internal on this host)",
             "command": ("Unregister-ScheduledTask -TaskName 'Pseudolife Claude Shim' -Confirm:$false; "
                         "Unregister-ScheduledTask -TaskName 'Pseudolife Codex Shim' -Confirm:$false"
                         if windows else
                         "systemctl --user disable --now pseudolife-sonnet-shim.service pseudolife-codex-shim.service")},
            {"what": "saved tunnel profiles that point at this host",
             "command": "pseudolife-mcp tunnel status (then remove the profiles that name this host)"},
            {"what": "the daemon autostart (this move set Docker's restart policy to no; a host install's logon "
                     "task is separate)",
             "command": (f"docker update --restart=no {DAEMON}"
                         + ("; Unregister-ScheduledTask -TaskName 'Pseudolife-MCP Daemon' -Confirm:$false"
                            if windows else ""))},
            {"what": "this host's tailnet serve of the old daemon", "command": "pseudolife-mcp expose off"},
            {"what": unattended, "command": "pseudolife-mcp update --unschedule"},
        ]


def _restart_policy(container: dict) -> str:
    return str(((container.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "")


# ── command line ────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp move",
        description="Move this host's Docker-tier bank to another Docker-tier checkout host over key-based "
                    "ssh: preflight, final backup from the stopped source, database fence, restore and "
                    "verify on the target, start it once, re-point this machine's clients. A failure rolls "
                    "back. Exit codes: 0 moved, 1 failed and rolled back, 2 usage or not confirmed, 4 refused "
                    "before any change, 5 moved but re-pointing failed, 6 failed and the rollback could not "
                    "finish. Run it under tmux, screen or nohup.")
    parser.add_argument("--to", required=True, metavar="SSH-TARGET",
                        help="the target host as ssh takes it (root@box, or a ~/.ssh/config alias)")
    parser.add_argument("--target-checkout", default=DEFAULT_CHECKOUT, metavar="PATH",
                        help=f"the checkout on the target (default {DEFAULT_CHECKOUT})")
    parser.add_argument("--target-url", default=None, metavar="URL",
                        help="the URL clients will use for the target (default: its `expose status`)")
    parser.add_argument("--no-keep-tokens", action="store_true",
                        help="do not carry the environment identities; invite each machine on the target again")
    parser.add_argument("--resume", action="store_true",
                        help="overwrite the half-restored bank an earlier attempt of this move left on the target")
    parser.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    parser.add_argument("--yes", action="store_true", help="move without asking")
    parser.add_argument("--json", action="store_true", help="one JSON report on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[2:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.target_url is not None:
        from pseudolife_memory import codex_connection
        try:
            codex_connection._validated_daemon_url(args.target_url)
        except codex_connection.SetupError:
            print("move: --target-url must be an http(s) origin such as http://100.64.0.2:8765 (no path, "
                  "query or credentials)", file=sys.stderr)
            return EXIT_USAGE
    options = Options(target=args.to, checkout=args.target_checkout, target_url=args.target_url,
                      keep_tokens=not args.no_keep_tokens, resume=args.resume, dry_run=args.dry_run,
                      yes=args.yes, as_json=args.json)
    if not args.to or args.to.startswith("-") or any(ch.isspace() for ch in args.to):
        print("move: --to takes an ssh target such as root@box or a ~/.ssh/config alias", file=sys.stderr)
        return EXIT_USAGE
    return Mover(options).run()


if __name__ == "__main__":
    sys.exit(main())
