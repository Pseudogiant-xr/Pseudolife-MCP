"""``pseudolife-mcp invite``: give another machine its own identity on this bank.

    pseudolife-mcp invite <name> [--tier TIER] [--board | --no-board] [--expires 15m]
                                 [--replace] [--url URL] [--port 8765] [--bank FP] [--json]
    pseudolife-mcp invite --list [--bank FP] [--json]
    pseudolife-mcp invite --revoke <name> [--yes] [--bank FP] [--json]

An operator command on the daemon host. Like ``board-audit`` and ``lease
break`` it talks to Postgres directly: access to the database is the
privilege. ``invite`` creates a principal in the bank's ``principals``
table (schema v53), admits it to the agent board unless ``--no-board``, and
prints a short-lived, single-use pairing code. The other machine redeems it
with ``pseudolife-mcp pair <url> <code>`` (or the installer's option 2),
which mints the bearer token there and registers only its SHA-256: no
token ever leaves that machine, and no daemon restart is needed. Only the
code's SHA-256 is stored.

Where it runs: with ``PSEUDOLIFE_MCP_DATABASE_URL`` set, or the lite
tier's embedded Postgres found (as ``export`` finds it), here. Otherwise, on
a Docker install, inside the daemon container (``docker exec -i
pseudolife-mcp-daemon python -m pseudolife_memory.cli invite ...``), after
checking the image has this command, so the host never handles the
database password and the name checks see the daemon's real environment.

Before an invite writes, the database's bank must be the one the daemon on
``127.0.0.1:<port>`` serves (``/health``'s ``bank`` fingerprint; the bench
Postgres also holds test databases), and that daemon must report
``"auth": true``. ``--revoke`` and ``--list`` need no daemon: revoking only
narrows access, and must work while the daemon is down or degraded.
``--bank <fingerprint>`` makes any mode confirm the database first. With
the daemon container stopped on a Docker install, they run as plain SQL
through ``psql`` in the ``pseudolife-mcp-postgres`` container, over its
local socket as ``ops/restore.sh`` does (no password), and a failure there
prints the SQL to run by hand.

Names: ``[a-z0-9][a-z0-9._-]{0,63}``; never ``default``, ``daemon`` or
``maintainer``, nor a name already in the daemon's ``PSEUDOLIFE_MCP_TOKENS``
or ``PSEUDOLIFE_MCP_TIER_MAP``. A paired name needs ``--replace`` (its old
token works until the new code is redeemed); a revoked name may be invited
again. ``--revoke`` takes effect at the daemon's next refresh (within 10 s).

Exit codes: 0 done; 1 the database failed; 2 usage, or not confirmed; 4
refused before any change.

Standard library only until a path is chosen: on a Docker host
``pseudolife-mcp`` is a shim runtime without the storage layer.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from pseudolife_memory import expose_cli
from pseudolife_memory.principals import (
    RESERVED_PRINCIPALS, format_pairing_code, valid_principal_name,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 4

CONTAINER = "pseudolife-mcp-daemon"
POSTGRES_CONTAINER = "pseudolife-mcp-postgres"
# psql inside the Postgres container, as ops/restore.sh runs it: the local
# socket, the container's own user and database, no password.
_PSQL = ["docker", "exec", "-i", POSTGRES_CONTAINER, "sh", "-c",
         'psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
_NOW_SQL = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"
LIST_SQL = (
    "SELECT coalesce(json_agg(json_build_array(principal, tier, board, token_hash IS NOT NULL, "
    f"code_hash IS NOT NULL, code_expires_at, created_at, paired_at, revoked_at, {_NOW_SQL}) "
    "ORDER BY principal), '[]'::json) FROM public.principals;")
BANK_SQL = "SELECT value #>> '{}' FROM public.meta WHERE key = 'coordination_bank_id';"
DEFAULT_PORT = expose_cli.DEFAULT_PORT
# Stored principals arrive with schema v53.
MIN_SCHEMA = 53
TIERS = ("minimal", "core", "full")
URL_PLACEHOLDER = "<daemon-url>"
# The first line `invite --version-check` prints: an image that answers it
# carries this command.
VERSION_LINE = "pseudolife-mcp invite 1"
_EXPIRES = re.compile(r"(\d+)\s*([smhd]?)")
_UNIT = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}
DOCKER_TIMEOUT_S = 60


# ── seams (the tests replace these) ──────────────────────────────────────────

def probe_health(url: str) -> dict | None:
    return expose_cli.probe_health(url, expose_cli.LOCAL_HEALTH_TIMEOUT_S)


def run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command; output is captured and never contains a secret: the
    code is drawn inside the process that prints it. ``input`` is stdin."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S,
                              input=input, stdin=None if input is not None else subprocess.DEVNULL,
                              errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", type(exc).__name__)


def docker_available() -> bool:
    return shutil.which("docker") is not None


def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(question: str) -> bool:
    print(question, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def exposed_url(port: int) -> str | None:
    """The client URL ``expose status`` reports, or ``None``."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            expose_cli.main(["status", "--port", str(port), "--json"])
        report = json.loads(out.getvalue())
    except Exception:  # noqa: BLE001 - no URL is a placeholder, not a failure
        return None
    return report.get("url") if report.get("state") == "exposed" else None


def local_database() -> tuple[str | None, object | None]:
    """``(dsn, own_instance)``: the explicit DSN, else the lite tier's
    embedded Postgres (attached, or started for the duration), else
    ``(None, None)``. Imports the storage layer only when one is there."""
    dsn = os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
    if dsn:
        return dsn, None
    try:
        from pseudolife_memory.storage import embedded_pg
        if not embedded_pg.available():
            return None, None
        from pseudolife_memory.backup_cli import _default_data_dir
        from pseudolife_memory.transfer_cli import _resolve_dsn
    except ImportError:
        return None, None
    return _resolve_dsn(_default_data_dir(os.environ))


# ── report ───────────────────────────────────────────────────────────────────

class _Report:
    def __init__(self, as_json: bool):
        self.as_json = as_json
        self.data: dict = {"error": None, "notes": [], "exit": None}

    def say(self, line: str = "") -> None:
        if not self.as_json:
            print(line)

    def note(self, line: str) -> None:
        self.data["notes"].append(line)
        self.say(line)

    def fail(self, code: int, line: str) -> int:
        self.data["error"] = line
        if not self.as_json:
            print(f"invite: {line}", file=sys.stderr)
        return self.finish(code)

    def finish(self, code: int) -> int:
        self.data["exit"] = code
        if self.as_json:
            print(json.dumps(self.data, indent=2, default=str))
        return code


# ── checks shared by both paths ─────────────────────────────────────────────

def _expires(value: str) -> int:
    match = _EXPIRES.fullmatch(value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {value!r} (e.g. 15m, 2h, 1d)")
    seconds = int(match.group(1)) * _UNIT[match.group(2)]
    if not 60 <= seconds <= 24 * 3600:
        raise argparse.ArgumentTypeError("--expires must be between 1 minute and 24 hours")
    return seconds


def _env_names() -> tuple[set[str], set[str]]:
    from pseudolife_memory.principals import parse_token_map
    from pseudolife_memory.toolset_tiers import parse_tier_map
    tokens = set(parse_token_map(os.environ.get("PSEUDOLIFE_MCP_TOKENS")).values())
    tiers = set(parse_tier_map(os.environ.get("PSEUDOLIFE_MCP_TIER_MAP")))
    return tokens, tiers


def _name_refusal(name: str) -> str | None:
    if name in RESERVED_PRINCIPALS:
        return f"{name!r} is reserved; choose another name"
    tokens, tiers = _env_names()
    if name in tokens:
        return (f"{name} already has a token in PSEUDOLIFE_MCP_TOKENS; an environment principal "
                "keeps working as it is, and a stored one of the same name would be shadowed by it")
    if name in tiers:
        return (f"{name} is named in PSEUDOLIFE_MCP_TIER_MAP; remove it there first, or choose "
                "another name")
    return None


def _daemon_check(port: int, report: _Report, *, need_auth: bool) -> tuple[dict | None, str | None]:
    """``(health, refusal)`` for the daemon on 127.0.0.1:<port>."""
    local = f"http://127.0.0.1:{port}"
    health = probe_health(local)
    if not isinstance(health, dict) or health.get("status") != "ok":
        return None, (f"no healthy daemon answers at {local}/health: start it (or pass --port), then "
                      "re-run")
    report.data["daemon"] = {"auth": health.get("auth"), "schema": health.get("schema"),
                             "bank": health.get("bank")}
    if need_auth and health.get("auth") is not True:
        return None, (f'the daemon at {local} does not report "auth": true. Stored principals do not '
                      "turn authentication on: give the daemon a token first "
                      "(docs/guide/remote-bank.md)")
    schema = health.get("schema")
    if not isinstance(schema, int) or schema < MIN_SCHEMA:
        return None, (f"the daemon runs schema {schema}, older than invite (v{MIN_SCHEMA}): update the "
                      "daemon first")
    if not health.get("bank"):
        return None, ("the daemon has not reported which bank it serves yet (/health bank is null until "
                      "its storage has started and the agent board has been used once): start a "
                      "session against it, then re-run")
    return health, None


def revoke_sql(name: str) -> str:
    """The revocation as plain SQL, for psql. ``name`` must be a principal
    name ([a-z0-9._-] only), so it can stand as a literal."""
    if not valid_principal_name(name):
        raise ValueError("not a principal name")
    return ("UPDATE public.principals SET revoked_at = COALESCE(revoked_at, "
            f"{_NOW_SQL}), code_hash = NULL, code_expires_at = NULL, paired_code_hash = NULL "
            f"WHERE principal = '{name}' RETURNING principal;")


def _bank_refusal(bank: str | None, expected: str | None, what: str) -> str | None:
    if bank == expected:
        return None
    return (f"this database holds bank {bank or '(none)'}, but {what} is bank {expected}: it is "
            "another database. Nothing was changed")


# ── the database path ─────────────────────────────────────────────────────────

def _bank_of(conn) -> str | None:
    from pseudolife_memory.principal_store import bank_fingerprint
    from pseudolife_memory.storage.coordination import BANK_ID_META_KEY
    row = conn.execute("SELECT value FROM public.meta WHERE key = %s", (BANK_ID_META_KEY,)).fetchone()
    return bank_fingerprint(row[0] if row else None)


def _listed_note(name: str) -> str | None:
    try:
        from pseudolife_memory.backup_cli import _default_data_dir
        from pseudolife_memory.utils.config import load_config
        config = load_config(_default_data_dir(os.environ) / "config.yaml")
    except Exception:  # noqa: BLE001 - the note is a courtesy
        return None
    if name in config.coordination.allowed_principals:
        return (f"note: {name} is already listed in coordination.allowed_principals; the invite gives "
                "it a token")
    return None


def _when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else "-"


def _local(args, report: _Report, dsn: str) -> int:
    import psycopg

    from pseudolife_memory import principal_store as store

    inviting = bool(args.name)
    health = None
    if inviting:
        health, refusal = _daemon_check(args.port, report, need_auth=True)
        if refusal:
            return report.fail(EXIT_REFUSED, refusal)
    try:
        conn = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - the DSN can be in the message
        return report.fail(EXIT_FAILED, f"could not connect to the bank's database ({type(exc).__name__})")
    with conn:
        conn.execute("SET search_path TO public")
        conn.execute("SET statement_timeout = '10s'")
        try:
            if inviting or args.bank:
                bank = _bank_of(conn)
                refusal = (_bank_refusal(bank, args.bank, "--bank") if args.bank else None) or (
                    _bank_refusal(bank, health.get("bank"),
                                  f"the daemon on port {args.port} serves") if inviting else None)
                if refusal:
                    return report.fail(EXIT_REFUSED, refusal)
            if args.list:
                return _list(report, store.list_principals(conn))
            if args.revoke:
                return _revoke(args, report, store, conn)
            return _invite(args, report, store, conn)
        except psycopg.errors.UndefinedTable:
            return report.fail(EXIT_REFUSED, ("this bank has no principals table yet (schema v53): "
                                              "update the daemon and let it start, then re-run"))
        except store.InviteRefused as exc:
            return report.fail(EXIT_REFUSED, f"{exc}. Nothing was changed")
        except psycopg.Error as exc:
            return report.fail(EXIT_FAILED, f"the database refused the change ({type(exc).__name__})")


def _invite(args, report: _Report, store, conn) -> int:
    note = _listed_note(args.name)
    created = store.create_invite(conn, args.name,
                                  tier=store.KEEP if args.tier is None else args.tier,
                                  board=store.KEEP if args.board is None else args.board,
                                  ttl_seconds=args.expires, replace=args.replace)
    code = format_pairing_code(created["code"])
    url = args.url or exposed_url(args.port) or URL_PLACEHOLDER
    command = f"pseudolife-mcp pair {url} {code}"
    minutes = round(args.expires / 60)
    report.data.update(principal=args.name, state=created["state"], code=code,
                       expires_at=created["expires_at"], tier=created["tier"],
                       board=created["board"], url=url, pair_command=command)
    what = {"new": "invited", "pending": "re-invited (the earlier code no longer works)",
            "revoked": "re-invited after its revocation",
            "paired": "given a replacement code (its current token works until the code is redeemed)"}
    report.say(f"{args.name}: {what[created['state']]}; tier {created['tier'] or 'the daemon default'}, "
               f"agent board {'on' if created['board'] else 'off'}")
    report.say(f"  code     {code}   single use, expires in {minutes} min ({_when(created['expires_at'])})")
    report.say(f"  on the new machine:  {command}")
    report.say("  or run the installer there and answer 2, giving this code where it asks for the token")
    if url == URL_PLACEHOLDER:
        report.note("the daemon is not exposed on the tailnet here; put its URL in place of "
                    "<daemon-url> (`pseudolife-mcp expose tailscale` exposes it), or pass --url")
    if note:
        report.note(note)
    report.note("an invited machine can read and write the whole bank and use the agent board, "
                "but not change the daemon's configuration")
    return report.finish(EXIT_OK)


def _revoke(args, report: _Report, store, conn) -> int:
    if not store.revoke(conn, args.revoke):
        return report.fail(EXIT_REFUSED, f"no stored principal is named {args.revoke}")
    report.data.update(principal=args.revoke, state="revoked")
    report.say(f"{args.revoke}: revoked; the daemon stops accepting its token within 10 seconds")
    return report.finish(EXIT_OK)


def _list(report: _Report, rows: list[dict]) -> int:
    report.data["principals"] = rows
    if not rows:
        report.say("no stored principals")
        return report.finish(EXIT_OK)
    report.say(f"  {'name':<24} {'state':<9} {'tier':<8} {'board':<6} {'created':<21} {'paired':<21} revoked")
    for row in rows:
        state = row["state"] + (f" until {_when(row['code_expires_at'])}" if row["state"] == "pending" else "")
        report.say(f"  {row['principal']:<24} {state:<9} {row['tier'] or 'default':<8} "
                   f"{'on' if row['board'] else 'off':<6} {_when(row['created_at']):<21} "
                   f"{_when(row['paired_at']):<21} {_when(row['revoked_at'])}")
    return report.finish(EXIT_OK)


# ── the Docker path ───────────────────────────────────────────────────────────

def _forward(args) -> list[str]:
    """The same request for the container, with the URL and the
    confirmation resolved here."""
    out = []
    if args.list:
        out.append("--list")
    elif args.revoke:
        out += ["--revoke", args.revoke, "--yes"]
    else:
        out.append(args.name)
        if args.tier:
            out += ["--tier", args.tier]
        if args.board is not None:
            out.append("--board" if args.board else "--no-board")
        out += ["--expires", f"{args.expires}s"]
        if args.replace:
            out.append("--replace")
        out += ["--url", args.url or exposed_url(args.port) or URL_PLACEHOLDER]
    if args.bank:
        out += ["--bank", args.bank]
    if args.json:
        out.append("--json")
    return out


def _daemon_running() -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER])
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _psql(sql: str) -> subprocess.CompletedProcess:
    return run(_PSQL, input=sql + "\n")


def _through_postgres(args, report: _Report) -> int:
    """``--list`` / ``--revoke`` with the daemon container stopped: plain
    SQL through psql in the Postgres container."""
    from pseudolife_memory.principal_store import bank_fingerprint, describe_rows
    sql = LIST_SQL if args.list else revoke_sql(args.revoke)

    def by_hand(why: str) -> int:
        return report.fail(EXIT_FAILED, (
            f"{why}. With the Postgres container running, the same change by hand is: docker exec -i "
            f"{POSTGRES_CONTAINER} psql -U pseudolife -d pseudolife_memory -c \"{sql}\""))

    if args.bank:
        found = _psql(BANK_SQL)
        if found.returncode != 0:
            return by_hand(f"psql in {POSTGRES_CONTAINER} failed (exit {found.returncode})")
        refusal = _bank_refusal(bank_fingerprint((found.stdout or "").strip() or None), args.bank,
                                "--bank")
        if refusal:
            return report.fail(EXIT_REFUSED, refusal)
    proc = _psql(sql)
    if proc.returncode != 0:
        return by_hand(f"psql in {POSTGRES_CONTAINER} failed (exit {proc.returncode})")
    output = (proc.stdout or "").strip()
    if args.list:
        try:
            rows = json.loads(output.splitlines()[-1]) if output else []
        except (ValueError, IndexError):
            return by_hand("psql answered something that is not the list")
        return _list(report, describe_rows(rows))
    if args.revoke not in output.splitlines():
        return report.fail(EXIT_REFUSED, f"no stored principal is named {args.revoke}")
    report.data.update(principal=args.revoke, state="revoked")
    report.say(f"{args.revoke}: revoked; the daemon stops accepting its token within 10 seconds of "
               "starting again")
    return report.finish(EXIT_OK)


def _in_container(args, report: _Report) -> int:
    if not _daemon_running():
        if args.list or args.revoke:
            return _through_postgres(args, report)
        return report.fail(EXIT_REFUSED, (
            f"the {CONTAINER} container is not running: start the daemon (an invite needs it to "
            "report its bank), then re-run"))
    base = ["docker", "exec", CONTAINER, "python", "-m", "pseudolife_memory.cli", "invite"]
    check = run([*base, "--version-check"])
    if check.returncode != 0 or not (check.stdout or "").startswith(VERSION_LINE):
        if check.returncode == 127 or "No such container" in (check.stderr or ""):
            return report.fail(EXIT_REFUSED, (
                f"no database here (PSEUDOLIFE_MCP_DATABASE_URL is unset and no lite bank was found) and "
                f"no {CONTAINER} container: run this on the daemon's host"))
        return report.fail(EXIT_REFUSED, ("the daemon's image predates `invite`: update the daemon "
                                          "first (pseudolife-mcp update), then re-run"))
    proc = run(["docker", "exec", "-i", CONTAINER, "python", "-m", "pseudolife_memory.cli", "invite",
                *_forward(args)])
    # The container's own report: relayed as it is (it carries the code).
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    return proc.returncode if proc.returncode in (EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED) else EXIT_FAILED


# ── the command ─────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp invite",
        description="Give another machine its own principal on this bank: prints a short-lived, "
                    "single-use pairing code for `pseudolife-mcp pair`. Exit codes: 0 done, 1 the "
                    "database failed, 2 usage or not confirmed, 4 refused before any change.")
    parser.add_argument("name", nargs="?", help="the new machine's principal, e.g. laptop")
    parser.add_argument("--tier", choices=TIERS, help="its default toolset tier (default: the daemon's)")
    board = parser.add_mutually_exclusive_group()
    board.add_argument("--board", dest="board", action="store_const", const=True, default=None,
                       help="admit it to the agent board (a new principal's default)")
    board.add_argument("--no-board", dest="board", action="store_const", const=False,
                       help="do not admit it to the agent board")
    parser.add_argument("--expires", type=_expires, default=15 * 60,
                        help="how long the code lives: 15m (default), at most 24h")
    parser.add_argument("--replace", action="store_true",
                        help="give a paired principal a new code (its token works until redeemed)")
    parser.add_argument("--url", help="the daemon URL to print (default: expose status)")
    parser.add_argument("--port", type=expose_cli._port,
                        default=int(os.environ.get("PSEUDOLIFE_MCP_PORT") or DEFAULT_PORT),
                        help=f"the local daemon's port (default: PSEUDOLIFE_MCP_PORT, else {DEFAULT_PORT})")
    parser.add_argument("--bank", metavar="FINGERPRINT",
                        help="refuse unless the database holds this bank (/health's bank)")
    parser.add_argument("--list", action="store_true", help="list stored principals")
    parser.add_argument("--revoke", metavar="NAME", help="revoke a stored principal")
    parser.add_argument("--yes", action="store_true", help="revoke without asking")
    parser.add_argument("--json", action="store_true", help="one JSON report on stdout")
    parser.add_argument("--version-check", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[2:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.version_check:
        print(VERSION_LINE)
        return EXIT_OK
    report = _Report(args.json)
    modes = [bool(args.name), args.list, bool(args.revoke)]
    if sum(modes) != 1:
        return report.fail(EXIT_USAGE, "give one of: a name to invite, --list, or --revoke NAME")
    target = args.name or args.revoke
    if target is not None:
        if not valid_principal_name(target):
            return report.fail(EXIT_USAGE, (f"{target!r} is not a principal name: lowercase letters, "
                                            "digits, '.', '_' and '-', starting with a letter or digit, "
                                            "at most 64 characters"))
        if args.name:
            refusal = _name_refusal(args.name)
            if refusal:
                return report.fail(EXIT_REFUSED, refusal)
    if args.revoke and not args.yes:
        if not interactive():
            return report.fail(EXIT_USAGE, "not interactive: re-run with --yes to revoke")
        if not _ask(f"revoke {args.revoke}? Its token stops working within 10 seconds. [y/N] "):
            return report.fail(EXIT_USAGE, "not confirmed; nothing was changed")
        args.yes = True
    dsn, instance = local_database()
    if dsn:
        try:
            return _local(args, report, dsn)
        finally:
            if instance is not None:
                instance.stop()
    if docker_available():
        return _in_container(args, report)
    return report.fail(EXIT_REFUSED, ("no database found: set PSEUDOLIFE_MCP_DATABASE_URL, or run this on "
                                      "the daemon's host (a Docker install runs it in the daemon "
                                      "container)"))


if __name__ == "__main__":
    sys.exit(main())
