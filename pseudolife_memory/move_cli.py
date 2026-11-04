"""``pseudolife-mcp move``: move this host's Docker-tier bank to another host.

    pseudolife-mcp move --to <ssh-target> [--target-checkout PATH]
        [--target-url URL] [--expose-target] [--no-keep-tokens] [--resume]
        [--dry-run] [--yes] [--json]

Run on the source host, a Docker-tier daemon. The target has an installed
Docker bank, reached over key-based ssh (``ssh -o BatchMode=yes``). An owning
checkout supplies restore/update, or the target's installed CLI supplies
clone-free restore. ``--target-release`` selects the installed CLI when an
unrelated clone exists; ``--target-compose-file`` / ``--target-env-file``
name a stack manager's exported original deployment. The order:

1. **Preflight, no changes.** The source is healthy, Docker tier and reports
   its bank fingerprint; ssh works; the target deployment is validated; the target
   daemon is healthy, its schema is at least the source's, its fingerprint
   differs, and its bank has no user data in any table, unless
   ``--resume`` names this move's half-restored bank by its marker. The
   target URL is ``--target-url``, else the target's ``expose status``.
2. **Plan and confirm** (``--dry-run`` stops after the plan; a run that is
   not interactive needs ``--yes``).
3. **Expose the target** when it is not exposed, with separate consent
   (``--expose-target`` or its own interactive question), and check this host
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
7. **Stop and recheck the target**, then restore without starting it. The
   resume marker and verified original state archive stay on the target
   host, outside the daemon's ``/data``. Restore uses ``ops/restore.sh
   --apply --no-start`` or the installed ``restore --yes --leave-stopped``,
   then checks the restored bank identity (``meta``
   ``coordination_bank_id``) and row counts against the source's, and
   ``/data/move.json``, which keeps the target's daemon from starting
   outside the move.
8. **Carry the environment identities** (``PSEUDOLIFE_MCP_TOKEN``,
   ``_TOKENS``, ``_TIER_MAP``, ``_TOOLSET``, read from the container that
   ran) into the target's original environment file over ssh stdin, the file backed up
   first and its owner and mode kept; ``--no-keep-tokens`` skips it.
9. **Start the target once** (checkout update, same-version release update,
   or the unchanged container under ``--no-keep-tokens``), wait (180 s, with a
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

A failure in steps 3-9, or SIGTERM / SIGHUP / Ctrl-C, removes any target
serve this move created, then rolls back in a fixed
order (Ctrl-C is ignored while it runs): stop the target daemon if it was
started (and put its ``/data/move.json`` back); restore the target's
environment file, unattended update and restart policy; lift the database
fence; remove the source's ``moved.json``; restore its restart policy and
schedule; ``docker start`` the source. The target is never left running
beside a running source: when it cannot be stopped, the source stays down.
Each step's progress, and the manual rollback, are written to the move
record (``~/.pseudolife-mcp/moves/<id>/record.json``) as they happen.

Exit codes: 0 moved; 1 failed and rolled back; 2 usage, declined, or no
TTY without ``--yes``; 4 refused before any change; 5 the bank moved but
re-pointing this machine's clients failed, or a follow-up on the target
after the commit point (``moved.json`` written) did (no rollback: the target
is correct and the source is fenced); 6 failed and the rollback could not
finish (the error and the record say what is left). On Windows a closed
console ends the process with no rollback: the record's manual rollback
is the way back.

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
# What ops/restore.sh prints just before it drops the database it replaces.
RESTORE_BOUNDARY = "==> Dropping + recreating"
BANK_ID_SQL ="SELECT value FROM meta WHERE key = 'coordination_bank_id'"
# Only known fresh-bank seed rows may be replaced. Unknown meta keys carry
# user state, so future keys are protected without a client update.
EMPTY_TARGET_META_KEYS = ("schema_version", "coordination_bank_id", "maintainer_secret_v1",
                          "dream_ack_secret_v1", "flat_band_migration_v1", "curation_listing_spelling_v2",
                          "writer_lease_epoch", "coordination_hlc_highwater")
_STATE_SAFETY_CHECK = """import gzip, os, shutil, sys, tarfile
with gzip.open(sys.argv[1], 'rb') as source, open(os.devnull, 'wb') as sink:
    shutil.copyfileobj(source, sink)
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    archive.getmembers()
"""
_ARTIFACT_LOCATION = """# move artifact locations must be outside target bank mounts
import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
safe = True
try:
    sources = [os.path.realpath(m['Source']) for m in request['mounts'] if m.get('Source')]
    for directory in request['directories']:
        path = os.path.realpath(os.path.expanduser(directory))
        if any(os.path.commonpath([path, source]) == source for source in sources):
            safe = False
        if request['release'] and any((p / '.git').exists() for p in (Path(path), *Path(path).parents)):
            safe = False
except (OSError, ValueError):
    safe = False
print(json.dumps({'safe': safe}))
"""
_EXPOSE_TARGET = """# move expose tailscale: retain ownership even if ssh loses the reply
import fcntl, json, os, subprocess, sys
request = json.load(sys.stdin)
receipt = os.path.expanduser(request['receipt'])
directory = os.path.dirname(receipt)
os.makedirs(directory, mode=0o700, exist_ok=True)
os.chmod(directory, 0o700)
def save(ownership):
    temporary = receipt + '.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump({'move_id': request['move_id'], 'attempt': request['attempt'], 'ownership': ownership}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, receipt)
with open(os.path.join(os.path.dirname(directory), '.move-expose.lock'), 'a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        save('absent')
        sys.stderr.write('another move is exposing this target; retry after it finishes\\n')
        sys.exit(4)
    save('pending')
    result = subprocess.run([sys.executable, '-m', 'pseudolife_memory.cli', 'expose', 'tailscale', '--yes', '--json',
                             '--port', str(request.get('port', 8765))],
                            cwd=os.path.expanduser(request['checkout']), capture_output=True, text=True)
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        reply = {}
    state = reply.get('state') if isinstance(reply, dict) else None
    ownership = ('owned' if result.returncode == 0 and state == 'exposed' else
                 'borrowed' if state == 'already_exposed' else
                 'absent' if result.returncode in (2, 4) else 'unknown')
    save(ownership)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
sys.exit(result.returncode)
"""
_RELEASE_PREFLIGHT = """# move release preflight: render the same deployment that update will use
import ipaddress, json, os, subprocess, sys
from pathlib import Path
from pseudolife_memory import update_cli as up
request = json.load(sys.stdin)
try:
    options = up.Options(as_json=True, compose_files=[Path(os.path.expanduser(p)) for p in request['compose_files']],
                         env_file=os.path.expanduser(request['env_file']) if request['env_file'] else None)
    update = up.Update(options)
    update._bundled = lambda name: up.BUNDLED_COMPOSE / name  # render without materializing a cache
    plan = update.compose_context()
    files = plan['env_file']
    if not files:
        raise up.UpdateError('carrying tokens needs the original environment: pass --target-env-file <stack.env>, '
                             'or --no-keep-tokens to retain the existing target configuration')
    environment = up.compose_environment(files, dict(request['carried'], PSEUDOLIFE_IMAGE_TAG=request['version']))
    result = subprocess.run([update.docker, 'compose', *plan['args'], 'config', '--format', 'json'],
                            env=environment, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise up.UpdateError('cannot render the target deployment: verify the original compose and environment '
                             'with docker compose config, then retry with --target-compose-file and --target-env-file')
    config = json.loads(result.stdout)
    daemon = config['services']['pseudolife-daemon']
    live = daemon.get('environment') or {}
    if any(str(live.get(key, '')) != value for key, value in request['carried'].items()):
        raise up.UpdateError('the target compose file hardcodes or overrides carried identities: make those '
                             'environment keys interpolate from the env file, or pass --no-keep-tokens')
    if live.get('PSEUDOLIFE_MCP_DATABASE_URL') != request['database_url']:
        raise up.UpdateError('the exported environment would change the target database connection: export '
                             'the original stack environment and pass --target-env-file <stack.env>')
    from psycopg.conninfo import conninfo_to_dict
    def endpoint(values):
        connection = conninfo_to_dict(values['PSEUDOLIFE_MCP_DATABASE_URL'])
        user = connection.get('user') or values.get('PGUSER')
        database = connection.get('dbname') or values.get('PGDATABASE') or user
        return (user, database, connection.get('host') or values.get('PGHOST'),
                connection.get('hostaddr') or values.get('PGHOSTADDR'),
                connection.get('port') or values.get('PGPORT') or '5432',
                connection.get('password') or values.get('PGPASSWORD'),
                connection.get('service') or values.get('PGSERVICE'))
    original = dict(request.get('connection_defaults') or {}, PSEUDOLIFE_MCP_DATABASE_URL=request['database_url'])
    if endpoint(live) != endpoint(original):
        raise up.UpdateError('the exported libpq defaults would change the actual bank or its credentials: '
                             'export the original environment and pass --target-env-file <stack.env>')
    def check_volume(service, destination, actual):
        wanted = next((v for v in config['services'][service].get('volumes', []) if v['target'] == destination), None)
        if wanted is None:
            raise ValueError('missing data mount')
        source = wanted.get('source')
        if wanted['type'] == 'volume':
            source = config['volumes'][source].get('name', source)
        else:
            source = os.path.realpath(source)
        if not any(m.get('Destination') == destination and m.get('Type') == wanted['type']
                   and (m.get('Name') if wanted['type'] == 'volume' else os.path.realpath(m.get('Source', ''))) == source
                   for m in actual):
            raise ValueError('changed data mount')
    check_volume('pseudolife-daemon', '/data', request['daemon_mounts'])
    if request.get('host_network'):
        ports_match = daemon.get('network_mode') == 'host'
    else:
        actual = {(str(ipaddress.ip_address(b.get('HostIp') or '0.0.0.0')), int(b['HostPort']), int(key.split('/')[0]), key.split('/')[1])
                  for key, bindings in request['daemon_ports'].items() for b in bindings or []}
        def ports(value):
            parts = str(value).split('-')
            first, last = int(parts[0]), int(parts[-1])
            if len(parts) > 2 or not 1 <= first <= last <= 65535:
                raise ValueError('invalid published port range')
            return range(first, last + 1)
        wanted = set()
        for binding in daemon.get('ports', []):
            targets, published = ports(binding['target']), ports(binding.get('published'))
            if len(targets) != len(published):
                raise ValueError('unfixed published port')
            protocol = binding.get('protocol') or 'tcp'
            for target, port in zip(targets, published):
                if binding.get('host_ip'):
                    addresses = {str(ipaddress.ip_address(binding['host_ip']))}
                else:
                    # An omitted host_ip is Docker's dual-stack wildcard;
                    # preserve the daemon's existing enabled address families.
                    addresses = {ip for ip, host, container, proto in actual
                                 if host == port and container == target and proto == protocol and ip in ('0.0.0.0', '::')}
                    addresses = addresses or {'0.0.0.0', '::'}
                wanted.update((ip, port, target, protocol) for ip in addresses)
        ports_match = wanted == actual
    if not ports_match:
        raise up.UpdateError('the exported compose file would change target network bindings: export its original '
                             'settings and retry with --target-compose-file and --target-env-file')
    pg_mounts = [m for m in config['services']['pseudolife-pg'].get('volumes', [])
                 if m.get('target', '').startswith('/var/lib/postgresql')]
    if not pg_mounts:
        raise ValueError('missing database mount')
    for mount in pg_mounts:
        check_volume('pseudolife-pg', mount['target'], request['postgres_mounts'])
    print(json.dumps({'env_file': str(files[-1]), 'project': plan['project']}))
except up.UpdateError as error:
    message = str(error).replace('--compose-file', '--target-compose-file').replace('--env-file', '--target-env-file')
    print(json.dumps({'error': message}))
    sys.exit(4)
except Exception:
    print(json.dumps({'error': 'the exported deployment cannot be matched to the running target data volumes; '
                              'export its original compose/environment files, then retry with --target-compose-file '
                              'and --target-env-file, or use --no-keep-tokens to retain its existing configuration'}))
    sys.exit(4)
"""
# Where a client on the target may hold a token (``grep -lsF`` reads the
# tokens from stdin; a path that does not exist is skipped).
TARGET_CLIENT_FILES = ("~/.pseudolife-mcp/*.token", "~/.claude.json", "~/.claude/settings.json",
                       "~/.codex/config.toml", "~/.codex/pseudolife/token", "~/.gemini/settings.json",
                       "~/.config/Claude/claude_desktop_config.json",
                       "~/'Library/Application Support/Claude/claude_desktop_config.json'")
# SIGHUP is POSIX only, SIGBREAK (Ctrl-Break) Windows only.
_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
                 if hasattr(signal, name))
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
            stdout_file: Path | None = None, timeout: int = 600, new_group: bool = False) -> Result:
        """``new_group`` starts the child in its own process group on Windows,
        so a Ctrl-C at the console does not reach it (a rollback step)."""
        argv = [str(part) for part in argv]
        handles = []
        kwargs: dict = {}
        if new_group and os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
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
        except subprocess.TimeoutExpired:
            return Result(124, b"", f"command timed out after {timeout}s; its outcome is unknown".encode())
        except OSError as exc:
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


def _private_dir(path: Path) -> None:
    """Create a move directory (and its ``moves`` parent) owner-only: it
    holds the bank's dump, its state archive and the move record. On
    Windows the per-user profile already keeps other users out."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        for directory in (path.parent, path):
            os.chmod(directory, 0o700)


def _pg_major(result) -> int | None:
    """The major version from ``SHOW server_version_num`` (170006 -> 17)."""
    if not result.ok:
        return None
    text = result.text.strip()
    return int(text) // 10000 if text.isdigit() else None


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


def _dsn_parts(dsn: str | None, environment: dict[str, str] | None = None) -> tuple[str, str]:
    """Resolve the daemon's libpq defaults; container init defaults are unrelated."""
    environment = environment or {}
    fix = ("set an explicit PSEUDOLIFE_MCP_DATABASE_URL with the actual bank database and role in the "
           "daemon's original configuration, restart it, and retry move")
    if not dsn:
        raise MoveError(f"the daemon's database configuration is opaque: {fix}. Nothing was changed", EXIT_REFUSED)
    try:
        from psycopg.conninfo import conninfo_to_dict
        connection = conninfo_to_dict(dsn)
    except Exception:  # libpq errors can contain the credential-bearing input
        raise MoveError(f"the daemon's database configuration cannot be read: {fix}. Nothing was changed",
                        EXIT_REFUSED) from None
    if connection.get("service") or environment.get("PGSERVICE"):
        raise MoveError("the daemon's service-file connection cannot be verified outside its container: "
                        "replace service/PGSERVICE defaults with an explicit PSEUDOLIFE_MCP_DATABASE_URL, "
                        "restart the original daemon, and retry. Nothing was changed",
                        EXIT_REFUSED)
    user = connection.get("user") or environment.get("PGUSER")
    db = connection.get("dbname") or environment.get("PGDATABASE") or user
    if not user or not db:
        raise MoveError(f"the daemon's database defaults cannot be identified: {fix}. Nothing was changed", EXIT_REFUSED)
    if not (_IDENT.match(db) and _IDENT.match(user)):
        raise MoveError("the daemon's database URL names a database or user this command does not handle "
                        "(lowercase letters, digits and underscores only); use pseudolife-mcp export/import "
                        "for this bank instead. Nothing was changed", EXIT_REFUSED)
    return db, user


def _container(inspect_text: str) -> dict | None:
    try:
        data = json.loads(inspect_text)
    except ValueError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _meta_fingerprint(value: str) -> str | None:
    try:
        identity = json.loads(value)
    except ValueError:
        return None
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16] if isinstance(identity, str) else None


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
    checkout_explicit: bool = False
    target_url: str | None = None
    expose_target: bool = False
    target_release: bool = False
    target_env_file: str | None = None
    target_compose_files: tuple[str, ...] = ()
    target_work_dir: str | None = None
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
         "target_quiesced", "exposed_now", "expose_pending",
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
        self.release_target = options.target_release
        self.target_env_file: str | None = None
        self.target_version: str | None = None
        self.target_port = 8765
        self.target_local_url = SOURCE_URL
        self.phase_code = EXIT_REFUSED     # what an ssh failure exits with before the source stops
        self.recording = False
        self.in_rollback = False
        self.target_timer_unknown: str | None = None   # why the target's timer state could not be read
        self.follow_ups: list[dict] = []               # what is left to do after the commit point
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
        self.target_container: dict = {}
        self.target_postgres: dict = {}
        self.target_selection: dict = {}
        self.target_original_meta = ""
        self.resume_record: dict | None = None
        self.target_policy = ""
        self.target_old_tokens: list[str] = []
        self.target_registrations: list[str] | None = None
        self.target_timer_enabled = False
        self.target_db, self.target_user = "pseudolife_memory", "pseudolife"
        self.target_url: str | None = None
        self.expose_attempt: str | None = None
        self.needs_expose = False
        self.sizes: dict = {"database": None, "data": None}
        self.stored_principals: list[str] = []
        self.schedule = None
        self.schedule_enabled = False
        self.counts: dict[str, int] = {}
        self.board = {"leases": None, "mail": None}
        for flag in FLAGS:
            setattr(self, flag, None if flag == "env_backup" else False)
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
    def _new_group(self) -> bool:
        """Rollback steps on Windows run in their own process group: a
        second Ctrl-C at the console must not kill one half-way."""
        return self.in_rollback and self.platform.startswith("win")

    def local(self, argv, **kwargs) -> Result:
        return self.runner.run(argv, new_group=self._new_group(), **kwargs)

    def ssh(self, command: str, **kwargs) -> Result:
        """``command`` on the target. Exit 255 is ssh's own failure (the
        connection dropped, or never came up), never the command's answer:
        it raises, so no caller can read it as "no such file"."""
        result = self.runner.run([*SSH_OPTS, self.o.target, command], new_group=self._new_group(), **kwargs)
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
        if self.o.target_work_dir:
            return f"{self.o.target_work_dir.rstrip('/')}/{self.move_id}"
        return (f"~/.pseudolife-mcp/moves/{self.move_id}" if self.release_target
                else f"{self.checkout}/data/move-{self.move_id}")

    @property
    def marker_path(self) -> str:
        """The resume marker: on the target host, beside the copied backup,
        outside the daemon's /data (which the state restore replaces)."""
        return f"{self.remote_dir}/marker.json"

    @property
    def env_path(self) -> str:
        return self.target_env_file or f"{self.checkout}/ops/.env"

    def target_cli(self, arguments: str) -> str:
        prefix = "" if self.release_target else f"cd {rq(self.checkout)} && "
        if arguments.startswith("expose ") and self.target_port != 8765:
            arguments += f" --port {self.target_port}"
        return prefix + "python3 -m pseudolife_memory.cli " + arguments

    @property
    def exposure_receipt(self) -> str:
        return f"{self.remote_dir}/expose.json"

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
        paths = [self.o.target_env_file or "", self.o.target_work_dir or "", *self.o.target_compose_files]
        if any(ch in path for path in paths for ch in "\n\r\0"):
            return self.fail(EXIT_USAGE, "target environment, compose and work directory paths must contain no control characters")
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
        if health.get("ready") is False:
            raise self.refuse(f"the source daemon is still warming up: wait for {SOURCE_URL}/health "
                              "to report ready: true, then retry")
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
        self.db, self.db_user = _dsn_parts(env.get("PSEUDOLIFE_MCP_DATABASE_URL"), env)
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
        if _meta_fingerprint(self.source_meta) != bank:
            raise self.refuse("the selected source database does not match the daemon's bank fingerprint: "
                              "correct its database configuration or restart the source daemon to refresh its "
                              "bank identity, then retry")
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
        # Inspect existing resources before suggesting a fresh install: a
        # foreign stack without a clone would collide with the installer.
        target = self.inspect_target(DAEMON)
        if target is None:
            if self.inspect_target(POSTGRES) is not None:
                self.check_target_stack({})
            raise self.refuse(f"the target has no {DAEMON} container: run the "
                              f"installer there first ({INSTALL_LINE})")
        co = rq(self.checkout)
        result = self.ssh(f"test -f {co}/ops/restore.sh && test -f {co}/ops/update.sh && "
                          f"test -f {co}/ops/docker-compose.yml", timeout=60)
        explicit_release = self.release_target or bool(o.target_env_file or o.target_compose_files)
        if not result.ok and not explicit_release and (o.checkout_explicit or self.checkout != DEFAULT_CHECKOUT):
            raise self.refuse(f"no owning checkout at {self.checkout}: correct --target-checkout, or use "
                              "--target-release to select the target's installed CLI")
        self.release_target = self.release_target or bool(o.target_env_file or o.target_compose_files) or not result.ok
        if self.release_target:
            self.check_release_stack(target)
        else:
            self.check_target_stack(target)
            result = self.ssh(f"grep -q -- --no-start {co}/ops/restore.sh", timeout=60)
            if not result.ok:
                raise self.refuse(f"the target checkout's ops/restore.sh has no --no-start: update the checkout "
                                  f"on {o.target} (git pull) and retry")
        self.target_was_running = bool((target.get("State") or {}).get("Running"))
        self.target_image = str((target.get("Config") or {}).get("Image") or "")
        self.target_container = target
        bindings = ((target.get("NetworkSettings") or {}).get("Ports") or {}).get("8765/tcp") or []
        bindings = sorted(bindings, key=lambda b: b.get("HostIp") not in ("", "0.0.0.0", "127.0.0.1"))
        if bindings:
            try:
                from ipaddress import ip_address
                port = int(bindings[0]["HostPort"])
                address = ip_address(bindings[0].get("HostIp") or "127.0.0.1")
                host = "127.0.0.1" if address.is_unspecified else str(address)
                if ":" in host:
                    host = f"[{host}]"
                if not 1 <= port <= 65535:
                    raise ValueError("port")
            except (KeyError, TypeError, ValueError):
                raise self.refuse("the target reports invalid Docker port bindings: repair its published daemon "
                                  "port in the original stack and retry") from None
            self.target_port, self.target_local_url = port, f"http://{host}:{port}"
        self.target_policy = _restart_policy(target)
        target_env = _env_of(target)
        self.target_db, self.target_user = _dsn_parts(target_env.get("PSEUDOLIFE_MCP_DATABASE_URL"), target_env)
        from pseudolife_memory.principals import parse_token_map
        self.target_old_tokens = ([target_env["PSEUDOLIFE_MCP_TOKEN"]] if target_env.get("PSEUDOLIFE_MCP_TOKEN")
                                  else []) + list(parse_token_map(target_env.get("PSEUDOLIFE_MCP_TOKENS")))
        result = self.ssh(f"docker inspect -f '{{{{.State.Running}}}}' {POSTGRES}", timeout=60)
        if not result.ok or result.text.strip() != "true":
            raise self.refuse(f"the target's {POSTGRES} container is not running: start the stack there "
                              f"(docker start {POSTGRES}) and retry move --dry-run")
        self.check_postgres_versions()
        self.target_selection = self.database_selection()
        if not o.resume:
            result = self.ssh(f"curl -fsS --max-time 5 {shlex.quote(self.target_local_url + '/health')}", timeout=60)
            try:
                target_health = json.loads(result.text) if result.ok else None
            except ValueError:
                target_health = None
            if not isinstance(target_health, dict) or target_health.get("status") != "ok":
                raise self.refuse(f"the target daemon on {o.target} did not answer /health with status ok "
                                  f"on its loopback port: start it there, or re-run its installer")
            if target_health.get("ready") is False:
                raise self.refuse(f"the target daemon is still warming up: wait for {self.target_local_url}/health "
                                  "to report ready: true, then retry")
            schema = target_health.get("schema")
            if isinstance(self.source_schema, int) and (not isinstance(schema, int) or schema < self.source_schema):
                update = ("pseudolife-mcp update --daemon-only" if self.release_target else "git pull, then ops/update.sh")
                raise self.refuse(f"the target daemon's schema ({schema}) is older than the source's "
                                  f"({self.source_schema}): update the target first ({update} there)")
            if target_health.get("bank") == bank:
                raise self.refuse(f"the target daemon reports the same bank fingerprint as this one ({bank}): "
                                  f"the target already serves this bank (or a copy of it), and move never "
                                  f"runs two daemons on one bank")
            self.target_original_meta = self.read_target_meta()
            if (self.target_original_meta == self.source_meta or (target_health.get("bank") is not None
                    and _meta_fingerprint(self.target_original_meta) != target_health.get("bank"))):
                raise self.refuse("the selected target database does not match the daemon's different bank: "
                                  "correct its original database configuration and retry move --dry-run")
            self.target_version = target_health.get("version")
            if not o.keep_tokens and target_health.get("auth") is not True:
                raise self.refuse("--no-keep-tokens needs an authenticated target: configure its bearer token "
                                  "in the existing stack and retry")
        self.check_target_bank()
        self.check_artifact_location()
        if self.release_target and o.keep_tokens:
            self.check_release_deployment()
        self.read_exposure()
        self.read_sizes()
        self.read_stored_principals()
        self.read_schedules()
        self.read_target_registrations()
        self.data["target"] = {"ssh": o.target, "checkout": self.checkout, "url": self.target_url,
                               "needs_expose": self.needs_expose, "database": self.target_db,
                               "restart_policy": self.target_policy,
                               "unattended_update": self.target_timer_enabled}
        self.data["target"]["mode"] = "release" if self.release_target else "checkout"

    def inspect_target(self, name: str) -> dict | None:
        """Only a successful exact-name listing can prove absence."""
        result = self.ssh(f"docker inspect --type container {name}", timeout=60)
        container = _container(result.text) if result.ok else None
        if container is not None:
            return container
        if not result.ok:
            listing = self.ssh(f"docker ps -a --filter {shlex.quote('name=^/' + name + '$')} "
                               "--format '{{.Names}}'", timeout=60)
            if listing.ok and not listing.text.strip():
                return None
        cause = result.why(out=False) if not result.ok else "Docker returned invalid container JSON"
        raise self.refuse(f"could not inspect the target's {name} ({cause}): verify Docker is running and "
                          "the ssh user can run docker ps -a, then retry move --dry-run")

    def check_target_stack(self, daemon: dict) -> None:
        """The checkout used by restore/update must own both containers.

        Container names alone also match a stack manager's unrelated stack,
        or another checkout of the same compose project. Resolve paths on
        the target so a symlink to the owning checkout is legitimate.
        Overlays and explicit volume names do not change ownership.
        """
        postgres = self.inspect_target(POSTGRES)
        self.target_postgres = postgres or {}
        labels = [(c.get("Config") or {}).get("Labels") or {} for c in (daemon, postgres or {})]
        fix = ("Retry with --target-checkout pointing to the checkout that owns both containers, or "
               "--target-release to use the installed clone-free restore instead. "
               "For a stack managed elsewhere, first back up that bank, use its stack manager to "
               "remove its containers and retain its volumes (docker compose down without -v), "
               "then install a fresh target with distinct PSEUDOLIFE_BANK_VOLUME and "
               "PSEUDOLIFE_STATE_VOLUME names; retry move --dry-run")
        for name, service, label in zip((DAEMON, POSTGRES), ("pseudolife-daemon", "pseudolife-pg"), labels):
            if (label.get("com.docker.compose.project") != "pseudolife-mcp"
                    or label.get("com.docker.compose.service") != service):
                raise self.refuse(f"the target's {name} is not owned by this checkout's pseudolife-mcp "
                                  f"compose stack. {fix}")
        paths = [str(label.get("com.docker.compose.project.config_files") or "").split(",") for label in labels]
        script = ("import json,os,sys; "
                  "expected=os.path.realpath(os.path.join(os.path.expanduser(sys.argv[1]),'ops/docker-compose.yml')); "
                  "print(json.dumps([expected in [os.path.realpath(p) for p in group if p] "
                  "for group in json.load(sys.stdin)]))")
        result = self.ssh(f"python3 -c {shlex.quote(script)} {shlex.quote(self.checkout)}",
                          stdin=json.dumps(paths).encode(), timeout=60)
        try:
            managed = json.loads(result.text) if result.ok else None
        except ValueError:
            managed = None
        if managed != [True, True]:
            raise self.refuse(f"the target's running compose stack does not use "
                              f"{self.checkout}/ops/docker-compose.yml for both containers. {fix}")

    def check_release_stack(self, daemon: dict) -> None:
        postgres = self.inspect_target(POSTGRES)
        labels = [(c.get("Config") or {}).get("Labels") or {} for c in (daemon, postgres or {})]
        project = labels[0].get("com.docker.compose.project")
        if (not project or labels[1].get("com.docker.compose.project") != project
                or labels[0].get("com.docker.compose.service") != "pseudolife-daemon"
                or labels[1].get("com.docker.compose.service") != "pseudolife-pg"):
            raise self.refuse("the target containers are not the daemon and Postgres of one compose stack: "
                              "select the matching upstream stack before retrying move --dry-run")
        self.target_postgres = postgres
        result = self.ssh("python3 -m pseudolife_memory.cli restore --help", timeout=60)
        if not result.ok or "--leave-stopped" not in result.text:
            raise self.refuse("the target's installed CLI lacks clone-free restore: install the same CLI "
                              "release as this command (python3 -m pip install --upgrade pseudolife-mcp), "
                              "then retry move --dry-run; --target-checkout can select an owning checkout")

    def check_release_deployment(self) -> None:
        if not re.match(r"^ghcr\.io/pseudogiant-xr/pseudolife-daemon(?::|@)", self.target_image):
            raise self.refuse("the target uses a custom or checkout-built image: pass --no-keep-tokens to "
                              "restart that container unchanged, or --target-checkout to use its owning checkout")
        if not isinstance(self.target_version, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)", self.target_version):
            raise self.refuse("the target reports no release version to redeploy safely: pass --no-keep-tokens "
                              "to retain its current container, or start/update the target and retry")
        request = {"compose_files": list(self.o.target_compose_files), "env_file": self.o.target_env_file,
                   "version": self.target_version, "carried": self.carried(),
                   "database_url": _env_of(self.target_container).get("PSEUDOLIFE_MCP_DATABASE_URL"),
                   "daemon_mounts": self.target_container.get("Mounts", []),
                   "postgres_mounts": self.target_postgres.get("Mounts", []), "port": self.target_port,
                   "daemon_ports": (self.target_container.get("NetworkSettings") or {}).get("Ports") or {},
                   "host_network": (self.target_container.get("HostConfig") or {}).get("NetworkMode") == "host"}
        current = _env_of(self.target_container)
        request["connection_defaults"] = {key: current[key] for key in
            ("PGUSER", "PGDATABASE", "PGHOST", "PGHOSTADDR", "PGPORT", "PGPASSWORD", "PGSERVICE") if key in current}
        result = self.ssh(f"python3 -c {shlex.quote(_RELEASE_PREFLIGHT)}",
                          stdin=json.dumps(request).encode(), timeout=180)
        try:
            plan = json.loads(result.text)
        except ValueError:
            plan = None
        if (not result.ok or not isinstance(plan, dict) or not isinstance(plan.get("env_file"), str)
                or not plan.get("env_file")):
            error = plan.get("error") if isinstance(plan, dict) else None
            raise self.refuse(error or "could not validate the installed target updater: install the same CLI "
                              "release on the target and retry, or pass --no-keep-tokens")
        self.target_env_file = plan["env_file"]

    def check_artifact_location(self) -> None:
        directories = [self.remote_dir]
        if not self.release_target:
            directories.append(f"{self.checkout}/data/backups")  # legacy restore's database safety dump
        mounts = [m for m in self.target_container.get("Mounts", []) if m.get("Destination") == "/data"]
        mounts += self.target_postgres.get("Mounts", [])
        request = {"directories": directories, "mounts": mounts, "release": self.release_target}
        result = self.ssh(f"python3 -c {shlex.quote(_ARTIFACT_LOCATION)}", stdin=json.dumps(request).encode(), timeout=60)
        try:
            safe = json.loads(result.text).get("safe") is True if result.ok else False
        except (ValueError, AttributeError):
            safe = False
        if not safe:
            raise self.refuse("the target artifact or safety directory is inside a bank mount or checkout: "
                              "use --target-release --target-work-dir <directory outside bank mounts and checkouts>, "
                              "or configure /data as a dedicated state mount before retrying")

    def check_postgres_versions(self) -> None:
        """A plain-format dump restores into the same or a newer PostgreSQL
        major version, never an older one: pg_dump 17 and later emit
        settings (``SET transaction_timeout``) that an older server rejects
        part-way through the restore, after the target database was dropped."""
        source = _pg_major(self.psql_local("SHOW server_version_num", "postgres"))
        target = _pg_major(self.psql_remote("SHOW server_version_num", "postgres"))
        if source is None or target is None:
            raise self.refuse("could not read the PostgreSQL server version on both hosts "
                              "(SHOW server_version_num)")
        self.data["postgres"] = {"source_major": source, "target_major": target}
        if target < source:
            raise self.refuse(f"the target's PostgreSQL {target} is older than the source's {source}: a dump "
                              f"restores only into the same or a newer major version. Move the target's "
                              f"Postgres image to {source} or later first")

    def check_target_bank(self) -> None:
        """The target bank is empty, or (``--resume``) a marker on the target
        names a move this host started from this bank."""
        refuse = self.refuse if not self.recording else MoveError
        if self.o.resume:
            markers = self.target_markers()
            current_meta = self.read_target_meta()
            reasons = []
            if not markers:
                location = ("~/.pseudolife-mcp/moves/<id>/marker.json" if self.release_target
                            else f"{self.checkout}/data/move-<id>/marker.json")
                raise self.refuse(f"--resume needs this move's marker ({location} "
                                  f"on the target), and the target has none: only a bank this command left "
                                  f"half-restored can be resumed")
            for marker in markers:
                move_id = marker.get("move_id")
                record = (self.resume_record if self.resume_record and self.resume_record.get("move_id") == move_id else
                          self.read_record(move_id) if isinstance(move_id, str) and _MOVE_ID.match(move_id) else None)
                if (record is not None and marker.get("source_bank") == self.source_bank
                        and record.get("source_bank") == self.source_bank):
                    flags = record.get("flags") or {}
                    touched = isinstance(flags, dict) and flags.get("target_restored") is True
                    owned = bool(current_meta) and current_meta == self.source_meta
                    if not self.recording and self.target_was_running and (owned or (touched and not current_meta)):
                        reasons.append("the resumed bank is running or has an unreadable identity; stop its daemon before resuming")
                        continue
                    original = record.get("target_original_meta") or ""
                    if not owned:
                        if record.get("target_selection") != self.target_selection:
                            reasons.append("the target database/storage differs from the recorded move, or the older record cannot prove its identity")
                            continue
                        if current_meta or not touched:
                            self.check_target_empty()
                    self.move_id = move_id
                    self.data["move_id"] = move_id
                    self.resume_record = record
                    self.target_original_meta = original
                    if self.release_target:
                        image = self.target_container.get("Image") or self.target_image
                        if self.o.keep_tokens and record.get("target_image") not in (None, image):
                            reasons.append("the target image changed since this move began; use --no-keep-tokens to retain it")
                            continue
                        self.target_version = record.get("target_version")
                    return
            if reasons:
                raise refuse(reasons[0] + "; restore the original target database/storage selection, or choose fresh "
                             "PSEUDOLIFE_BANK_VOLUME/PSEUDOLIFE_STATE_VOLUME names and start a new move without --resume")
            named = ", ".join(f"{m.get('move_id')!r} from bank {m.get('source_bank')!r}" for m in markers)
            raise self.refuse(f"the target's move markers name {named}, none of them a move this host started "
                              f"from this bank: --resume overwrites only this move's own half-restored bank")
        self.check_target_empty()

    def read_target_meta(self) -> str:
        result = self.psql_remote(BANK_ID_SQL)
        return result.text.strip() if result.ok else ""

    def database_selection(self) -> dict:
        data = _env_of(self.target_postgres).get("PGDATA") or "/var/lib/postgresql/data"
        mounts = [m for m in self.target_postgres.get("Mounts", []) if m.get("Destination") and
                  (data == m["Destination"] or data.startswith(m["Destination"].rstrip("/") + "/"))]
        mount = max(mounts, key=lambda m: len(m["Destination"])) if mounts else {}
        identity = mount.get("Name") or mount.get("Source")
        storage = [mount.get("Destination"), mount.get("Type"), identity] if identity else [self.target_postgres.get("Id") or POSTGRES]
        return {"database": self.target_db, "storage": storage}

    def check_target_empty(self) -> None:
        refuse = self.refuse if not self.recording else MoveError
        result = self.psql_remote("SELECT coalesce(json_agg(relname ORDER BY relname), '[]'::json) "
                                  "FROM pg_class WHERE relkind IN ('r', 'p') AND relnamespace = "
                                  "'public'::regnamespace")
        if not result.ok:
            raise refuse(f"could not read the target bank ({result.why()}); check the target's PostgreSQL "
                         "logs and retry")
        try:
            tables = json.loads(result.text)
        except ValueError:
            tables = None
        if not isinstance(tables, list) or not all(isinstance(t, str) and t for t in tables):
            raise refuse("could not enumerate the target bank's tables: check the target's "
                         "PostgreSQL logs and retry")
        counts = {}
        if tables:
            # Quote identifiers from the catalog, including extension tables;
            # no table name can become SQL or an extra psql output row.
            filters = {
                "meta": " WHERE key NOT IN (" + ", ".join(_lit(k) for k in EMPTY_TARGET_META_KEYS)
                        + ") AND right(key, 15) <> '_schema_version'",
                "relations": " WHERE NOT builtin",
                "maintainer_bootstrap": " WHERE used_at IS NOT NULL OR credential_id IS NOT NULL",
                "cms_counters": " WHERE interaction_count <> 0 OR logical_turn_count <> 0 OR tier_queries <> 0 "
                                "OR EXISTS (SELECT 1 FROM jsonb_each(tier_hits) WHERE value <> '0'::jsonb) "
                                "OR EXISTS (SELECT 1 FROM jsonb_each(surprise_history) WHERE value <> '[]'::jsonb) "
                                "OR consolidation_events <> '[]'::jsonb",
            }
            result = self.psql_remote("SELECT " + ", ".join(
                '(SELECT count(*) FROM public."' + t.replace('"', '""') + '"'
                + filters.get(t, "") + ")" for t in tables))
            row = _rows(result.text)[0] if result.ok and _rows(result.text) else []
            if len(row) != len(tables):
                raise refuse(f"could not count the target bank's rows ({result.why()}); check the target's "
                             "PostgreSQL logs and retry")
            counts = dict(zip(tables, (int(value) if value.strip().isdigit() else -1 for value in row)))
        used = {table: n for table, n in counts.items() if n != 0}
        if used:
            listed = ", ".join(f"{table} {n}" for table, n in used.items())
            markers = self.target_markers()
            hint = (f"; it carries a move marker (move {markers[0].get('move_id')}), so if this host started that "
                    f"move, retry with --resume") if markers and not self.o.resume else (
                    "; back up and remove that bank before retrying, or install a fresh target with distinct "
                    "PSEUDOLIFE_BANK_VOLUME and PSEUDOLIFE_STATE_VOLUME names; use pseudolife-mcp export / "
                    "import to merge knowledge instead")
            raise refuse(f"the target bank is not empty ({listed}): move never merges into or "
                         f"overwrites a used bank{hint}")

    def target_markers(self) -> list[dict]:
        """Every move marker on the target (one JSON object per line); exit 1
        is cat's "none there"."""
        pattern = (f"{rq(self.o.target_work_dir)}/*/marker.json" if self.o.target_work_dir else
                   "~/.pseudolife-mcp/moves/*/marker.json" if self.release_target
                   else f"{rq(self.checkout)}/data/move-*/marker.json")
        result = self.ssh(f"cat {pattern} 2>/dev/null", timeout=60)
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
        result = self.ssh(self.target_cli("expose status --json"),
                          timeout=120)
        state, url = parse_expose_status(result.text if result.ok else "")
        if state == "exposed":
            self.target_url = url
        elif state == "not-exposed":
            self.needs_expose = True
            if self.o.resume:
                # expose requires a healthy local daemon. Resolve its future
                # URL without creating the serve while the target is gated.
                from ipaddress import IPv4Address
                result = self.ssh("tailscale ip -4", timeout=60)
                try:
                    ip = IPv4Address(result.text.strip()) if result.ok else None
                except ValueError:
                    ip = None
                if ip is None:
                    raise self.refuse("the resumed target has no readable Tailscale IPv4 address: "
                                      "pass --target-url <an existing reachable URL> and retry")
                self.target_url = f"http://{ip}:{self.target_port}"
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
        result = self.ssh("systemctl --user is-enabled pseudolife-update.timer 2>&1", timeout=60)
        answer = (result.text.strip().splitlines() or [""])[0].strip()
        self.target_timer_enabled = answer == "enabled"
        if answer not in ("enabled", "disabled", "static", "masked", "indirect", "linked", "generated",
                          "transient", "not-found", "alias", "enabled-runtime", "linked-runtime",
                          "masked-runtime"):
            # No user bus for the ssh user (no lingering session), say: the
            # timer state is unknown, and nothing is paused.
            self.target_timer_unknown = answer[:200] or f"exit {result.code}"
            self.warn(f"could not read the ssh user's systemd --user timers on {self.o.target} "
                      f"({self.target_timer_unknown}); an unattended update there is not paused")

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
        restore_tool = "pseudolife-mcp restore --yes --leave-stopped" if self.release_target else "ops/restore.sh --apply --no-start"
        start_tool = ("docker start (existing configuration)" if self.release_target and not o.keep_tokens else
                      "pseudolife-mcp update --reinstall (same release)" if self.release_target else "ops/update.sh")
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
            f"target: {o.target}, " + ("installed CLI (clone-free)" if self.release_target else f"checkout {self.checkout}")
            + (f" (--resume: over move {self.move_id}'s half-restored bank)" if o.resume else ""),
            f"target URL: {url}" if url else
            "target URL: not exposed yet; step 3 needs --expose-target or separate interactive consent",
            "1. preflight: done, nothing changed",
            ("3. expose the target with separate consent (pseudolife-mcp expose tailscale --yes on the target), "
             "then check this host "
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
            f"7. restore on the target without starting it ({restore_tool} --backup-file "
            "...; it takes its own safety dump first), check the restored bank identity and row counts, and "
            f"keep the daemon from starting outside the move ({MARKER})",
            ("8. carry PSEUDOLIFE_MCP_TOKEN, PSEUDOLIFE_MCP_TOKENS, PSEUDOLIFE_MCP_TIER_MAP and "
             f"PSEUDOLIFE_MCP_TOOLSET from the source container into {self.env_path} (backed up first; "
             "its previous token is replaced)") if o.keep_tokens else
            "8. skipped (--no-keep-tokens): every environment principal must be invited on the target again",
            f"9. start the target once ({start_tool}), wait up to "
            f"{int(o.health_timeout)} s for its bank fingerprint to equal this bank's, check it runs with the "
            "carried identities, restore what step 4 paused on the target, stop this daemon restarting itself, "
            f"write {MOVED} into it",
            f"10. re-point this machine's clients: pseudolife-mcp connect {url or '<target URL>'} --yes",
            "11. report: the rollback, other machines' connect lines, held leases, undelivered mail, leftovers",
            "a failure in steps 3-9 rolls back: new target exposure removed, target stopped, its environment file and "
            "schedule restored, the fence "
            f"lifted, {MOVED} removed, the source's restart policy and schedule restored, the source started",
            "run it under tmux, screen or nohup: a closed terminal or a kill signal rolls the move back",
        ]
        self.data["plan"] = plan
        self.say(f"move: this host's bank -> {o.target}")
        for line in plan:
            self.say(f"  {line}")

    def confirm(self) -> None:
        if self.needs_expose and not self.o.expose_target:
            if self.o.yes or not self.interactive():
                raise self.refuse("creating a persistent target tailnet serve needs separate consent: "
                                  "pass --expose-target, or --target-url <an existing reachable URL>; "
                                  "--yes confirms only the bank move")
            if not self.ask("Create a persistent Tailscale serve on the target for this bank? [y/N] "):
                raise MoveError("target exposure was not confirmed; pass --target-url <an existing reachable "
                                "URL> to move without creating a serve. Nothing was changed", EXIT_USAGE)
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
            _private_dir(self.local_dir)
            record = {"move_id": self.move_id, "target": self.o.target, "checkout": self.checkout,
                      "target_mode": "release" if self.release_target else "checkout",
                      "target_version": self.target_version,
                      "target_image": self.target_container.get("Image") or self.target_image,
                      "target_env_file": self.target_env_file,
                      "target_work_dir": self.o.target_work_dir,
                      "target_selection": self.target_selection,
                      "target_original_meta": self.target_original_meta,
                      "target_url": self.target_url, "source_bank": self.source_bank, "state": state,
                      "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "flags": {flag: getattr(self, flag) for flag in FLAGS},
                      "target_state_backup": self.data.get("target", {}).get("state_backup"),
                      "target_exposure_receipt": self.exposure_receipt if self.expose_pending or self.exposed_now
                                                 else None,
                      "target_exposure_attempt": self.expose_attempt,
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
                self.expose_and_reach()
                self.phase_code = EXIT_FAILED
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
                self.in_rollback = True
                try:
                    complete = self.rollback()
                    self.write_record("rolled_back" if complete else "rollback_incomplete")
                finally:
                    self.in_rollback = False
                    self._restore_signals(ignored)
                if complete:
                    target_note = ("the target is stopped and keeps its move marker, so `move --resume` may "
                                   "overwrite that half-restored bank on the next attempt" if self.target_restored
                                   else "the target's original bank was preserved")
                    raise MoveError(f"{reason}. Rolled back: the source daemon runs again; {target_note}",
                                    self.phase_code) from None
                left = "; ".join(f"{s['step']}" + (f" ({s['detail'][:200]})" if s.get("detail") else "")
                                 for s in self.data["rollback"] if not s.get("ok"))
                raise MoveError(f"{reason}. The rollback is INCOMPLETE: {left}. See the rollback steps, and the move "
                                f"record {self.local_dir / 'record.json'}, and finish it by hand with the commands "
                                f"listed there", EXIT_INCOMPLETE) from None
        finally:
            self._restore_signals(previous)
        self.write_record("moved")
        self.finish_target()
        code = self.repoint()
        self.final_report()
        if self.follow_ups:
            commands = "; ".join(item["command"] for item in self.follow_ups)
            message = f"the bank moved, but follow-ups need doing: {commands}"
            self.data["error"] = f"{self.data['error']}; {message}" if self.data.get("error") else message
            if not self.o.as_json:
                print(f"move: {message}", file=sys.stderr, flush=True)
            code = EXIT_PARTIAL
        return code

    def settle_exposure(self) -> bool:
        """Only a remote creation result establishes ownership; intent cannot."""
        result = self.ssh(f"cat {rq(self.exposure_receipt)}", timeout=60)
        try:
            receipt = json.loads(result.text) if result.ok else None
        except ValueError:
            receipt = None
        if (not isinstance(receipt, dict) or receipt.get("move_id") != self.move_id or not self.expose_attempt
                or receipt.get("attempt") != self.expose_attempt):
            return False
        ownership = receipt.get("ownership")
        if ownership not in ("owned", "borrowed", "absent"):
            return False
        self.mark(expose_pending=False, exposed_now=ownership == "owned")
        return True

    def expose_and_reach(self, *, after_start: bool = False) -> None:
        o = self.o
        if o.resume and self.needs_expose and not after_start:
            self.step("--resume: target exposure waits until its daemon has started")
            return
        if self.needs_expose:
            self.step(f"exposing the target: pseudolife-mcp expose tailscale --yes on {o.target}")
            # Intent and ownership are separate: the remote receipt survives
            # a lost ssh reply and distinguishes a new serve from a no-op.
            self.expose_attempt = secrets.token_hex(8)
            self.mark(expose_pending=True)
            request = {"move_id": self.move_id, "attempt": self.expose_attempt,
                       "checkout": "~" if self.release_target else self.checkout, "receipt": self.exposure_receipt,
                       "port": self.target_port}
            result = self.ssh(f"python3 -c {shlex.quote(_EXPOSE_TARGET)}",
                              stdin=json.dumps(request).encode(), timeout=300)
            if not self.settle_exposure():
                raise MoveError("could not attribute the target serve to this move; inspect "
                                f"{self.exposure_receipt} and pseudolife-mcp expose status on {o.target} "
                                "before removing any forward", EXIT_REFUSED)
            if not result.ok:
                raise MoveError(f"`pseudolife-mcp expose tailscale` on {o.target} failed (exit {result.code}: "
                                f"{result.why()}). Configure Tailscale there and retry, or pass "
                                f"--target-url <the reachable URL clients will use> to use another connection. "
                                f"The source still runs", EXIT_REFUSED)
            state, url = parse_expose_status(result.text)
            if state != "exposed":
                again = self.ssh(self.target_cli("expose status --json"), timeout=120)
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
        if isinstance(health, dict) and health.get("ready") is False:
            raise MoveError(f"the target at {self.target_url} is still warming up; wait for /health ready: true "
                            "and retry. The source still runs", EXIT_REFUSED)
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise MoveError(f"this host cannot reach {self.target_url}/health: the URL clients will use must "
                            f"answer from here before the source stops. Nothing in either bank was changed; "
                            f"the source still runs", EXIT_REFUSED)
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
        _private_dir(directory)
        dump, part = directory / DUMP_NAME, directory / (DUMP_NAME + ".part")
        # Writers other than the daemon (a psql window, a Console that kept
        # a connection, lease break) are cut off before the dump.
        self.psql_local(terminate_sql(self.db), "postgres")
        self.step(f"final backup: pg_dump of {self.db} from the stopped source")
        # PostgreSQL object owners/grants belong to the destination role;
        # per-bank MCP principal identities are carried in the bank itself.
        result = self.local([self.docker, "exec", POSTGRES, "pg_dump", "--no-owner", "--no-acl",
                             "-U", self.db_user, "-d", self.db, "-Z9"],
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
        # Owner-only: it receives the bank's dump and state archive.
        result = self.ssh(f"mkdir -p -m 700 {rq(self.remote_dir)} && chmod 700 {rq(self.remote_dir)}", timeout=60)
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
        # The preflight snapshot can become stale while the operator reads
        # the plan or the source is copied. Stop the target's writer before
        # its safety dump, then check again before replacing anything.
        self.mark(target_quiesced=self.target_was_running)
        result = self.ssh(f"docker stop {DAEMON}", timeout=300)
        if not result.ok:
            raise MoveError(f"could not stop the target before the final emptiness check ({result.why()})")
        result = self.ssh(f"docker inspect -f '{{{{.State.Running}}}}' {DAEMON}", timeout=60)
        if not result.ok or result.text.strip() != "false":
            raise MoveError("the target is still running: stop it before retrying move")
        self.check_target_bank()
        # restore.sh's safety backup uses docker exec for /data, which
        # cannot run in this quiesced daemon. Keep a verified archive of the
        # original state through a helper before restore can replace it.
        snapshot = f"{self.remote_dir}/target-original-state.tgz"
        partial = snapshot + ".part"
        result = self.ssh(f"if [ ! -f {rq(snapshot)} ]; then "
                          f"docker run --rm --entrypoint tar --volumes-from {DAEMON} "
                          f"{shlex.quote(self.target_image)} czf - -C /data . > {rq(partial)} && "
                          f"python3 -c {shlex.quote(_STATE_SAFETY_CHECK)} {rq(partial)} && "
                          f"mv -f {rq(partial)} {rq(snapshot)} || exit 1; fi; "
                          f"python3 -c {shlex.quote(_STATE_SAFETY_CHECK)} {rq(snapshot)}", timeout=_TIMEOUT_LONG)
        if not result.ok:
            raise MoveError(f"could not verify the target's state safety archive ({result.why()}); "
                            "the target's original bank and state were not replaced")
        self.data["target"]["state_backup"] = snapshot
        self.write_record(self.state)
        self.step(f"the target's original state safety archive is {snapshot}")
        marker = {"move_id": self.move_id, "source_bank": self.source_bank,
                  "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.mark(marker_written=True)
        result = self.ssh(f"cat > {rq(self.marker_path)}", stdin=(json.dumps(marker) + "\n").encode("utf-8"),
                          timeout=60)
        if not result.ok:
            raise MoveError(f"could not write the move marker on the target ({result.why()})")
        dump, state = f"{self.remote_dir}/{DUMP_NAME}", f"{self.remote_dir}/{STATE_NAME}"
        restore = (self.target_cli("restore --yes --leave-stopped --daemon-url " + shlex.quote(self.target_local_url)
                                  + " --safety-dir " + rq(self.remote_dir + "/safety")) if self.release_target
                   else f"cd {rq(self.checkout)} && bash ops/restore.sh --apply --no-start")
        self.step("restoring on the target without starting it (" +
                  ("installed restore --leave-stopped" if self.release_target else "ops/restore.sh --apply --no-start") + ")")
        # The bank is "restored" (gated on rollback) from the moment restore.sh
        # starts dropping it: its own safety dump comes first, and a failure
        # there leaves the target's original bank in place. restore.sh says
        # where it is ("==> Dropping + recreating"), so a failure whose
        # output stops short of that line un-marks it. An outcome that cannot
        # be read (ssh lost, a kill) stays marked: a gated original bank only
        # refuses to start, while an ungated clone could serve beside the
        # source.
        self.mark(target_restored=True)
        result = self.ssh(f"{restore} --backup-file "
                          f"{rq(dump)} --state-archive {rq(state)} --db {shlex.quote(self.target_db)} "
                          f"--user {shlex.quote(self.target_user)}", timeout=_TIMEOUT_LONG)
        if not result.ok:
            # A timeout or killed transport may discard stdout after DROP.
            # Only a completed, ordinary command failure can prove the
            # boundary was never reached; unknown outcomes stay gated.
            if 0 < result.code < 128 and result.code != 124 and RESTORE_BOUNDARY not in result.text:
                self.mark(target_restored=False)
            raise MoveError(f"restore on the target failed ({result.why()})"
                            + ("" if self.target_restored else "; the target's own bank was not touched"))
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
            raise MoveError(f"could not read the target environment file {env} (exit {result.code}: {result.why(out=False)})")
        content = merge_env(current, self.carried(), self.move_id)
        if existed:
            backup = f"{env}.pre-move-{self.move_id}"
            result = self.ssh(f"cp -p {rq(env)} {rq(backup)}", timeout=60)
            if not result.ok:
                raise MoveError(f"could not back up the target environment file {env} ({result.why()})")
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
            raise MoveError(f"writing the target environment file {env} failed ({result.why()})")
        self.verify_remote(env, hashlib.sha256(data).hexdigest(), "target env file")
        self.step(f"carried the source's environment identities into {env}"
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
            if isinstance(health, dict) and health.get("status") == "ok" and health.get("ready") is not False:
                healthy = True
                bank = health.get("bank")
                if isinstance(bank, str) and bank:
                    if health.get("auth") is not True:
                        raise MoveError("the restored target does not report auth: true; restore its bearer "
                                        "configuration in the original stack before retrying move --resume")
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
                            f"variable or compose setting may override {self.env_path}); make those keys "
                            "interpolate from the original environment file and retry")

    def start_target(self) -> None:
        if self.gate_written:
            result = self.ssh(f"docker run --rm --entrypoint rm --volumes-from {DAEMON} "
                              f"{shlex.quote(self.target_image)} -f {MARKER}", timeout=300)
            if not result.ok:
                raise MoveError(f"could not lift {MARKER} on the target ({result.why()})")
            self.mark(gate_written=False)
        self.mark(target_started=True)
        if self.release_target and not self.o.keep_tokens:
            command = f"docker start {DAEMON}"
        elif self.release_target:
            arguments = (f"update --reinstall --daemon-only --tag {shlex.quote(self.target_version)} "
                         f"--daemon-url {shlex.quote(self.target_local_url)}")
            if self.o.target_env_file:
                arguments += f" --env-file {rq(self.o.target_env_file)}"
            for path in self.o.target_compose_files:
                arguments += f" --compose-file {rq(path)}"
            command = self.target_cli(arguments)
        else:
            environment = (f"PSEUDOLIFE_MCP_DAEMON_URL={shlex.quote(self.target_local_url)} "
                           if self.target_local_url != SOURCE_URL else "")
            command = f"cd {rq(self.checkout)} && {environment}bash ops/update.sh"
        self.step("starting the target once: " + command)
        result = self.ssh(command, timeout=_TIMEOUT_LONG)
        if not result.ok:
            raise MoveError(f"starting the target failed ({result.why()})")
        if self.o.resume and self.needs_expose:
            self.expose_and_reach(after_start=True)
        bank = self.wait_target()
        if bank != self.source_bank:
            raise MoveError(f"the target reports bank {bank}, not this bank's {self.source_bank}: it is not "
                            f"serving the restored bank")
        self.step(f"the target serves this bank (fingerprint {bank})")
        self.check_target_env()
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
        # The commit point: the target serves the bank and both checks
        # passed, and the source now refuses to start. Nothing after this
        # line rolls back (finish_target).
        self.step(f"wrote {MOVED} into the stopped source container: the bank has moved")

    def finish_target(self) -> None:
        """After the commit point: put back what step 4 paused on the target
        (it is the live daemon now) and remove the resume marker last. A
        failure, an ssh drop included, never rolls a moved bank back: it is
        recorded as a follow-up with its exact command (exit 5)."""
        steps = []
        if self.target_policy_paused:
            steps.append(("restore the target daemon's restart policy",
                          f"docker update --restart={shlex.quote(self.target_policy)} {DAEMON}",
                          "target_policy_paused"))
        if self.target_timer_paused:
            steps.append(("resume the target's unattended update",
                          "systemctl --user enable --now pseudolife-update.timer", "target_timer_paused"))
        if self.marker_written:
            steps.append(("remove the resume marker", f"rm -f {rq(self.marker_path)}", "marker_written"))
        for what, command, flag in steps:
            try:
                result = self.ssh(command, timeout=120)
                ok, why = result.ok, ("" if result.ok else result.why())
            except MoveError as exc:
                ok, why = False, str(exc)
            if ok:
                self.mark(**{flag: False})
                continue
            self.follow_ups.append({"what": what, "command": f"ssh {self.o.target} {command}", "why": why[:300]})
            self.warn(f"the bank moved, but this follow-up failed: {what} ({why[:200]}); run: "
                      f"ssh {self.o.target} {command}")

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
        complete, can_start, gate_missing = True, True, False
        if self.expose_pending:
            def resolve_exposure():
                if self.settle_exposure():
                    return Result(0)
                return Result(1, err=(f"ownership unknown: inspect {self.exposure_receipt} and run "
                                      f"pseudolife-mcp expose status on {self.o.target}; "
                                      "no unowned serve was removed").encode())
            complete = attempt("resolve the target exposure ownership", resolve_exposure)
        if self.exposed_now:
            if attempt("undo the target expose (pseudolife-mcp expose off --yes)",
                       lambda: self.ssh(self.target_cli("expose off --yes --json"), timeout=300)):
                self.mark(exposed_now=False)
            else:
                complete = False

        def blocked(reason: str) -> bool:
            log.append({"step": "the source stays stopped: two daemons must never serve one bank", "ok": False,
                        "detail": f"{reason}; stop the target by hand (ssh {self.o.target} docker stop {DAEMON}), "
                                  f"then: {'; '.join(self.manual_rollback()[1:])}"})
            return False

        was_started = self.target_started
        if was_started:
            if not attempt(f"stop the target daemon ({self.o.target})",
                           lambda: self.ssh(f"docker stop {DAEMON}", timeout=300)):
                return blocked("the target daemon could not be stopped")
            self.mark(target_started=False)
        if self.target_restored and not self.gate_written:
            if attempt(f"keep the target from starting outside the move ({MARKER})", self.write_gate):
                self.mark(gate_written=True)
            else:
                complete, gate_missing = False, True
        if was_started:
            # A remote update.sh may keep going after the local ssh child
            # died, and bring the target back up: only a target that reports
            # stopped lets the source start.
            for _attempt in range(3):
                try:
                    state = self.ssh(f"docker inspect -f '{{{{.State.Running}}}}' {DAEMON}", timeout=60)
                    running = state.text.strip() if state.ok else "unknown"
                except MoveError:
                    running = "unknown"
                if running == "false":
                    break
                attempt(f"stop the target daemon again (it reports running: {running})",
                        lambda: self.ssh(f"docker stop {DAEMON}", timeout=300))
            else:
                return blocked("the target daemon is still running after three stops")
        if self.env_written:
            if self.env_backup:
                ok = attempt(f"restore the target environment file ({self.env_path})",
                             lambda: self.ssh(f"cp -p {rq(self.env_backup)} {rq(self.env_path)}", timeout=60))
            else:
                ok = attempt(f"remove the target environment file this move wrote ({self.env_path})",
                             lambda: self.ssh(f"rm -f {rq(self.env_path)}", timeout=60))
            if ok:
                self.mark(env_written=False)
            complete = complete and ok
        if gate_missing and (self.target_timer_paused or self.target_policy_paused):
            # Without the gate, either one could start the restored clone
            # beside the source: both stay paused.
            log.append({"step": "the target's unattended update and restart policy are left paused", "ok": False,
                        "detail": f"the gate {MARKER} could not be written, so either could start the restored "
                                  f"clone beside the source; restore them once the target holds its own bank "
                                  f"again"})
        elif self.target_timer_paused:
            if attempt("resume the target's unattended update",
                       lambda: self.ssh("systemctl --user enable --now pseudolife-update.timer", timeout=120)):
                self.mark(target_timer_paused=False)
            else:
                complete = False
        if self.target_policy_paused and not gate_missing:
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
        if self.target_quiesced and not self.target_restored:
            if attempt("restart the target's original bank",
                       lambda: self.ssh(f"docker start {DAEMON}", timeout=300)):
                self.mark(target_quiesced=False)
            else:
                complete = False
        return complete

    def manual_rollback(self) -> list[str]:
        """The rollback by hand, in order, listing only what the move has
        done so far (after a successful move, all of it). The target is
        stopped and gated before anything else, so its next scheduled update
        cannot bring the clone back beside the source. The psql line puts its
        SQL in double quotes with none inside, so it reads the same in sh,
        cmd.exe and PowerShell."""
        t, lines = self.o.target, []
        if self.target_started:
            lines.append(f"ssh {t} docker stop {DAEMON}")
        if self.target_restored and not self.gate_written:
            lines.append(f'ssh {t} "docker run --rm --entrypoint touch --volumes-from {DAEMON} '
                         f'{self.target_image} {MARKER}"')
        if self.env_written:
            lines.append(f"ssh {t} cp -p {rq(self.env_backup)} {rq(self.env_path)}" if self.env_backup
                         else f"ssh {t} rm -f {rq(self.env_path)}")
        if self.target_timer_paused:
            lines.append(f"ssh {t} systemctl --user enable --now pseudolife-update.timer")
        if self.target_policy_paused:
            lines.append(f"ssh {t} docker update --restart={self.target_policy} {DAEMON}")
        if self.fenced:
            lines.append(f'docker exec {POSTGRES} psql -U {self.db_user} -d postgres -c "{fence_sql(self.db, True)}"')
        if self.moved_written:
            lines.append(f"docker run --rm --entrypoint rm --volumes-from {DAEMON} {self.source_image} -f {MOVED}")
        if self.source_policy_changed:
            lines.append(f"docker update --restart={self.source_policy} {DAEMON}")
        if self.schedule_paused:
            lines.append(" ".join(self._schedule_argv(enable=True)))
        if self.source_stopped:
            lines.append(f"docker start {DAEMON}")
        if self.target_quiesced and not self.target_restored:
            lines.append(f"ssh {t} docker start {DAEMON}")
        if self.moved_written:
            lines.append(f"pseudolife-mcp connect {SOURCE_URL} --yes")
        if self.exposed_now:
            lines.append(f'ssh {t} "{self.target_cli("expose off --yes")}"')
        elif self.expose_pending:
            lines.append(f"ssh {t} cat {rq(self.exposure_receipt)}")
            lines.append(f'ssh {t} "{self.target_cli("expose status --json")}"')
        return lines

    # -- 10. re-point --------------------------------------------------------
    def repoint(self) -> int:
        url = self.target_url
        self.step(f"re-pointing this machine's clients: pseudolife-mcp connect {url} --yes")
        from pseudolife_memory import child_python
        result = self.local(child_python.cli_argv("connect", url, "--yes", "--json"), timeout=900)
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
        credentials = " --token-file <target-token-file>" if not self.o.keep_tokens else ""
        self.data["error"] = (f"the bank moved, but re-pointing this machine's clients failed (connect exit "
                              f"{result.code}: {error or result.why()}); rerun: pseudolife-mcp connect {url}"
                              f"{credentials} --yes")
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
        if self.data["target"].get("state_backup"):
            notes.append(f"the target's original /data safety archive is "
                         f"{o.target}:{self.data['target']['state_backup']}")
        if o.keep_tokens:
            using = (f"; these files on {o.target} held it and need re-pointing there (move did not rewrite them): "
                     f"{', '.join(self.target_registrations)}" if self.target_registrations else
                     ("; no client file on the target held it" if self.target_registrations is not None else ""))
            origin = "previous environment token" if self.release_target else "own token, minted by its installer,"
            notes.append(f"the target's {origin} was replaced by the source's"
                         + (f" (its previous environment file: {self.env_backup})" if self.env_backup else "") + using
                         + f". Re-point a client there with pseudolife-mcp connect {url} --token-file "
                           f"<a file holding one of the carried tokens>")
        if not self.target_timer_enabled:
            seen = (f"could not be read ({self.target_timer_unknown})" if self.target_timer_unknown
                    else "was not found")
            notes.append(f"the target's unattended update was looked up as the ssh user only (systemctl --user) "
                         f"and {seen}; a timer another user installed on {o.target} is not visible to the move "
                         f"and was not paused: check it there as that user (systemctl --user list-timers)")
        report = {
            "rollback": self.manual_rollback(),
            "follow_ups": self.follow_ups,
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
    """The restart policy as ``docker update --restart`` takes it back:
    ``on-failure:N`` keeps its retry count."""
    policy = (container.get("HostConfig") or {}).get("RestartPolicy") or {}
    name = str(policy.get("Name") or "")
    count = policy.get("MaximumRetryCount")
    if name == "on-failure" and isinstance(count, int) and count > 0:
        return f"on-failure:{count}"
    return name


# ── command line ────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp move",
        description="Move this host's Docker-tier bank to another Docker host with an owning checkout or installed CLI over key-based "
                    "ssh: preflight, final backup from the stopped source, database fence, restore and "
                    "verify on the target, start it once, re-point this machine's clients. A failure rolls "
                    "back. Exit codes: 0 moved, 1 failed and rolled back, 2 usage or not confirmed, 4 refused "
                    "before any change, 5 moved but re-pointing failed, 6 failed and the rollback could not "
                    "finish. Run it under tmux, screen or nohup.")
    parser.add_argument("--to", required=True, metavar="SSH-TARGET",
                        help="the target host as ssh takes it (root@box, or a ~/.ssh/config alias)")
    parser.add_argument("--target-checkout", default=None, metavar="PATH",
                        help=f"the checkout on the target (default {DEFAULT_CHECKOUT})")
    parser.add_argument("--target-url", default=None, metavar="URL",
                        help="the URL clients will use for the target (default: its `expose status`)")
    parser.add_argument("--no-keep-tokens", action="store_true",
                        help="do not carry the environment identities; invite each machine on the target again")
    parser.add_argument("--resume", action="store_true",
                        help="overwrite the half-restored bank an earlier attempt of this move left on the target")
    parser.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    parser.add_argument("--expose-target", action="store_true",
                        help="consent to creating a persistent Tailscale serve on the target if needed")
    parser.add_argument("--target-release", action="store_true",
                        help="use the target's installed CLI even when an unrelated checkout exists")
    parser.add_argument("--target-env-file", help="original stack environment on the target (clone-free mode)")
    parser.add_argument("--target-compose-file", action="append", default=[],
                        help="original exported target compose file; repeat for overlays (clone-free mode)")
    parser.add_argument("--target-work-dir", help="target artifact directory outside bank mounts and checkouts")
    parser.add_argument("--yes", action="store_true", help="confirm the bank move (exposure needs --expose-target)")
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
    options = Options(target=args.to, checkout=args.target_checkout or DEFAULT_CHECKOUT,
                      checkout_explicit=args.target_checkout is not None, target_url=args.target_url,
                      expose_target=args.expose_target,
                      target_release=args.target_release, target_env_file=args.target_env_file,
                      target_compose_files=tuple(args.target_compose_file),
                      target_work_dir=args.target_work_dir,
                      keep_tokens=not args.no_keep_tokens, resume=args.resume, dry_run=args.dry_run,
                      yes=args.yes, as_json=args.json)
    if not args.to or args.to.startswith("-") or any(ch.isspace() for ch in args.to):
        print("move: --to takes an ssh target such as root@box or a ~/.ssh/config alias", file=sys.stderr)
        return EXIT_USAGE
    return Mover(options).run()


if __name__ == "__main__":
    sys.exit(main())
