"""Operator-only access to the agent board's audit log (schema v42).

``export`` streams ``coordination_events`` as JSON lines, one event per line,
in chain order; ``verify`` walks the hash chain and prints one JSON report;
``redact`` (schema v46) removes one message body and records why; ``stats``
computes coordination telemetry (mail latency, wake precision, park
outcomes, sends per session-hour, suite lock waits) for a time window from
the log, the v49 ``coordination_wakes`` table and the suite lock's durations
file, as one JSON object that carries counts, seconds and names only.

All four reach the bank directly, through ``PSEUDOLIFE_MCP_DATABASE_URL`` or
the lite tier's embedded instance, so they need the database owner's
credentials, never a bearer token. With neither, they re-run inside the
Docker tier's daemon container when it runs here (daemon_exec.py); files
they name stay on this host. ``export``, ``verify`` and ``stats`` read
one read-only snapshot; ``redact`` writes one transaction under the same
locks as the daemon's own writes. All are safe to run beside a live daemon.
There is deliberately no MCP tool and no REST route: the log holds message
bodies verbatim, and bodies carry machine paths and usernames. Treat the
output of ``export`` as private: keep it out of repositories and anywhere
public. ``stats`` copies none of that into its report.

Exit status: 0 success, 1 the chain failed verification, an expected head
could not be confirmed, or a redaction was refused (the JSON result says why),
2 nothing could be checked or changed (bad arguments, no bank, no audit log, a
log that predates v46 or a busy board for ``redact``, an unreadable file,
output closed early).
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from pseudolife_memory.board_audit_stats import PARK_LOOKBACK, compute_stats
from pseudolife_memory.storage.coordination import (
    AUDIT_COLUMNS, CoordinationError, CoordinationStore, audit_events, audit_has_body_column, resolve_agent_id,
    verify_audit_chain,
)
from pseudolife_memory.daemon_exec import NoBank, no_bank_message, run_in_daemon

EXIT_OK, EXIT_BROKEN, EXIT_ERROR = 0, 1, 2
# The suite lock's durations file (tests/suite_lock.py writes it); the lock
# directory is the same one the suite reads, an override or the home default.
LOCK_DIR_ENV = "PSEUDOLIFE_SUITE_LOCK_DIR"
DURATIONS_FILE = "full-suite.durations.jsonl"
DEFAULT_WINDOW = 24 * 3600

# What each redaction refusal means, for the operator's terminal. None of
# them repeats the reason or the body.
_REDACT_REFUSALS = {
    "invalid_message_id": "--message-id must be one message id as the audit log shows it",
    "invalid_reason": "--reason must be 1-240 characters on one line, with no control, "
                      "format or separator characters",
    "secret_like_body": "the reason looks like it holds a credential, and the reason is "
                        "kept in the log for good; describe the mistake without repeating it",
    "message_not_found": "no record of this message id: an unknown id, or one whose send "
                         "event audit retention removed and whose live copy is gone too",
    "body_in_hashed_payload": "this message was sent before schema v46, when the body was "
                              "part of the hashed payload; removing it would break the "
                              "chain, so it stays until audit retention removes the event, "
                              "and its live copy is already gone",
    "already_redacted": "this message's body is already redacted",
}


class AuditCliError(ValueError):
    """Operator-safe diagnostic: never a connection string, credential or body."""


def _time(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        try:
            # An ISO time without an offset is the local time of this machine.
            seconds = datetime.fromisoformat(value).timestamp()
        except (ValueError, OSError, OverflowError):  # OSError: pre-1970 on Windows
            seconds = math.nan
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError("expected epoch seconds or an ISO 8601 date/time")
    return seconds


def _head(value: str) -> tuple[int, str]:
    seq, sep, digest = value.partition(":")
    if (not sep or not seq.isdigit() or int(seq) < 1 or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise argparse.ArgumentTypeError("expected SEQ:HASH as verify prints them")
    return int(seq), digest


class _ReaderClosed(Exception):
    """Whoever read stdout stopped early (``export | head``)."""


@contextmanager
def _bank(*, write=False):
    """A connection to the bank, found the way ``export`` finds it:
    PSEUDOLIFE_MCP_DATABASE_URL, else the lite tier's embedded instance
    (attached, or started for the duration and stopped afterwards).
    Read-only unless ``write``, which ``redact`` alone asks for."""
    from pseudolife_memory.backup_cli import _default_data_dir
    from pseudolife_memory.transfer_cli import _resolve_dsn
    dsn, own_instance = _resolve_dsn(_default_data_dir(os.environ))
    try:
        if not dsn:
            raise NoBank
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=5, autocommit=write)
        try:
            if write:
                # The daemon's own lock timeout: a redaction waits on the
                # chain lock like any board write, never indefinitely.
                conn.execute("SET lock_timeout = '5s'")
            else:
                # One read-only, repeatable-read snapshot: a daemon appending
                # while this runs cannot tear the chain being read.
                conn.read_only = True
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.execute("SET search_path TO public")
            present = conn.execute(
                "SELECT to_regclass('public.coordination_events') IS NOT NULL").fetchone()[0]
            if not write:
                conn.commit()
            if not present:
                raise AuditCliError("this bank has no audit log yet (schema older than v42); "
                                    "start a v42 daemon once to create it")
            if write and not audit_has_body_column(conn):
                raise AuditCliError("this bank's audit log predates schema v46, so no body in it "
                                    "can be redacted: every send body is part of the hashed "
                                    "payload. Start a v46 daemon once to add the body column; "
                                    "bodies sent before it stay unredactable")
            yield conn
        finally:
            conn.close()
    finally:
        if own_instance is not None:
            own_instance.stop()


class _Storage:
    """What ``CoordinationStore`` needs to write: the connection, and a
    transaction that never reports a commit the server did not confirm."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction() as transaction:
            yield
        if transaction.status is not transaction.Status.COMMITTED:
            raise AuditCliError("the database did not confirm the commit; run verify to see "
                                "whether the redaction landed before retrying")


def _write_error(target, exc):
    """Stdout failing is a reader that went away (EPIPE on POSIX, EINVAL on
    Windows); a file failing is the file's problem, never the bank's."""
    if target is None:
        return _ReaderClosed()
    return AuditCliError(f"cannot write {target}: {exc.strerror}")


def _export(args) -> int:
    target = Path(args.out) if args.out else None
    if target is not None and target.exists():
        raise AuditCliError(f"{target} exists; export never replaces a file")
    with _bank() as conn:
        try:
            out = (target.open("x", encoding="utf-8", newline="\n") if target is not None
                   else nullcontext(sys.stdout))
        except OSError as exc:
            raise AuditCliError(f"cannot create {target}: {exc.strerror}") from None
        with out as stream:
            agent = args.agent
            if agent is not None:
                try:
                    agent = resolve_agent_id(conn, agent)
                except CoordinationError as exc:
                    if exc.code == "ambiguous_agent":
                        raise AuditCliError(f"--agent {agent} matches several ids: "
                                            f"{exc.detail}; give a longer prefix") from None
                    raise AuditCliError(f"no agent id starts with {agent}") from None
            rows = audit_events(conn, project=args.project, task=args.task,
                                agent_id=agent, since=args.since, until=args.until)
            try:
                for row in rows:
                    row = dict(row)
                    row["payload"] = json.loads(row["payload"])
                    line = json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
                    try:
                        stream.write(line)
                    except OSError as exc:
                        raise _write_error(target, exc) from None
                try:
                    stream.flush()
                except OSError as exc:
                    raise _write_error(target, exc) from None
            except BaseException:
                rows.close()
                if target is not None:
                    try:
                        stream.close()  # re-raises a failed flush, but closes
                    except OSError:
                        pass
                    target.unlink(missing_ok=True)  # this run created it exclusively
                raise
    return EXIT_OK


class _DuplicateKey(ValueError):
    """A JSON object in an export repeats a key."""


def _unique_keys(pairs):
    """``json`` keeps the last of a repeated key; an export has none, and a
    line with one could show a person one body and verify another."""
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKey()
    return dict(pairs)


def _read_export(path: Path):
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        raise AuditCliError(f"cannot read {path}") from None
    with handle:
        lines = enumerate(handle, 1)
        while True:
            try:
                number, line = next(lines)
            except StopIteration:
                return
            except UnicodeDecodeError:
                raise AuditCliError(f"{path} is not UTF-8 JSON lines (write exports with "
                                    "--out rather than a shell redirect)") from None
            if not line.strip():
                continue
            try:
                row = json.loads(line, object_pairs_hook=_unique_keys)
            except _DuplicateKey:
                raise AuditCliError(f"{path} line {number} has a duplicate key, so a reader "
                                    "and verify could see different values; it cannot be "
                                    "checked") from None
            except ValueError:
                raise AuditCliError(f"{path} line {number} is not JSON") from None
            # ``body`` is absent from exports written before v46, whose send
            # bodies are inside the hashed payload. Left absent, so verify can
            # tell a file without body fields from a body that was removed.
            if (not isinstance(row, dict) or set(AUDIT_COLUMNS) - set(row)
                    or type(row["seq"]) is not int
                    or type(row["created_at"]) not in (int, float)
                    or not all(isinstance(row[k], str) for k in (
                        "event", "actor", "principal", "agent_id", "project", "task",
                        "hlc", "prev_hash", "hash"))
                    or not isinstance(row.get("body"), (str, type(None)))
                    or not isinstance(row.get("body_salt"), (str, type(None)))):
                raise AuditCliError(f"{path} line {number} is not an exported audit event")
            yield row


def _verify(args) -> int:
    if args.input:
        report = verify_audit_chain(_read_export(Path(args.input)), expect_head=args.expect_head)
    else:
        with _bank() as conn, closing(audit_events(conn)) as rows:
            report = verify_audit_chain(rows, expect_head=args.expect_head)
    print(json.dumps(report))
    return EXIT_OK if report["ok"] else EXIT_BROKEN


def _vacuum(conn):
    """Free the old row versions a redaction leaves in the table files (the
    body stays readable there until a vacuum lets the space be reused), and
    rebuild the planner statistics: ANALYZE copies sampled column values under
    1 kB (a body, its salt, the live text, its request fingerprint) word for
    word into pg_statistic, where they would stay until the next automatic
    analyze. Then vacuum pg_statistic, so the superseded statistics row is
    freed as well. It runs after the commit, outside any transaction, as
    VACUUM must, and does not reach WAL, WAL archives or backups.

    Returns the warnings Postgres raised on the way. A role that may not
    vacuum or analyze a table gets a WARNING and a skip, not an error, and
    psycopg drops notices nobody handles, so without this a skipped step
    would read as done."""
    warnings = []

    def note(diag):
        if diag.severity_nonlocalized == "WARNING":
            warnings.append(diag.message_primary or "")

    # A role, database or connection option can set client_min_messages to
    # ERROR, and the server then never sends a skip's warning at all: ask for
    # warnings for these two statements, and put the setting back after.
    previous = conn.execute("SHOW client_min_messages").fetchone()[0]
    conn.add_notice_handler(note)
    try:
        conn.execute("SET client_min_messages TO warning")
        conn.execute("VACUUM (ANALYZE) coordination_events, coordination_messages")
        conn.execute("VACUUM pg_catalog.pg_statistic")
    finally:
        conn.remove_notice_handler(note)
        conn.execute("SELECT set_config('client_min_messages', %s, false)", (previous,))
    return warnings


def _redact(args) -> int:
    import psycopg
    with _bank(write=True) as conn:
        try:
            result = CoordinationStore(_Storage(conn)).redact(args.message_id, args.reason)
        except CoordinationError as exc:
            print(json.dumps({"ok": False, "message_id": args.message_id, "reason": exc.code}))
            print(f"board-audit: {_REDACT_REFUSALS.get(exc.code, exc.code)}"
                  + (f": {exc.detail}" if exc.detail else ""), file=sys.stderr)
            return EXIT_BROKEN
        except psycopg.errors.LockNotAvailable:
            raise AuditCliError("the board is busy: a row or the audit chain stayed locked for "
                                "more than 5 s. Nothing was changed; retry") from None
        skipped = []
        try:
            skipped = _vacuum(conn)
            vacuumed = not skipped
        except psycopg.Error:
            vacuumed = False
    print(json.dumps({"ok": True, **result, "vacuumed": vacuumed}))
    if result["audit_copy"] == "kept":
        print("board-audit: the live copy is blanked and out of delivery, but this message "
              "was sent before schema v46: the audit log keeps its body until audit "
              "retention removes the send event", file=sys.stderr)
    if result.get("other_copies"):
        print(f"board-audit: this message was one of {len(result['other_copies']) + 1} copies "
              "of one send to a project or the whole board; each keeps its own copy of the "
              "body until it is redacted too: " + " ".join(result["other_copies"]),
              file=sys.stderr)
    if result["audit_copy"] == "gone":
        print("board-audit: audit retention had already removed this message's send event, "
              "so the audit log held no copy of it; its live request fingerprint (and any "
              "live text) is blanked", file=sys.stderr)
    if skipped:
        print("board-audit: the redaction is committed, but Postgres skipped part of the "
              "clean-up (" + "; ".join(skipped) + "), so the old row versions or the "
              "statistics may still hold the body; run `VACUUM (ANALYZE) coordination_events, "
              "coordination_messages` and `VACUUM pg_statistic` as the tables' owner or a "
              "superuser", file=sys.stderr)
    elif not vacuumed:
        print("board-audit: the redaction is committed, but the VACUUM that frees the old "
              "row versions and rebuilds the statistics failed (the board may be busy); run "
              "`VACUUM (ANALYZE) coordination_events, coordination_messages` and `VACUUM "
              "pg_statistic` later", file=sys.stderr)
    print("board-audit: record this head outside the bank; `pseudolife-mcp board-audit "
          f"verify --expect-head {result['expect_head']}` later shows the redaction record "
          "is still there", file=sys.stderr)
    return EXIT_OK


def _window(since, until, now):
    """The stats window: ``[since, until)``, the last DEFAULT_WINDOW seconds
    up to now when neither is given."""
    until = now if until is None else until
    since = until - DEFAULT_WINDOW if since is None else since
    if since >= until:
        raise AuditCliError("--since must be before --until")
    return since, until


def _durations_path(environ) -> Path:
    override = environ.get(LOCK_DIR_ENV)
    directory = Path(override) if override else Path.home() / ".pseudolife-mcp" / "locks"
    return directory / DURATIONS_FILE


def _read_durations(path: Path):
    """The lock's JSON lines as dicts, a line that is not one skipped; and
    whether the file was there at all."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return [], False
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, True


def _wake_rows(conn, since, until):
    """The ``coordination_wakes`` rows decided in the window, or ``None`` on
    a bank whose schema predates them (v49)."""
    present = conn.execute("SELECT to_regclass('public.coordination_wakes') IS NOT NULL").fetchone()[0]
    if not present:
        return None
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT recipient_agent_id,sender_agent_id,message_id,decision,reason,urgent,"
                    "ring_at,created_at,served_at FROM coordination_wakes "
                    "WHERE created_at>=%s AND created_at<%s ORDER BY created_at,wake_id",
                    (since, until))
        return cur.fetchall()


def _stats(args) -> int:
    from pseudolife_memory import __version__
    now = time.time()
    since, until = _window(args.since, args.until, now)
    target = Path(args.out) if args.out else None
    if target is not None and target.exists():
        raise AuditCliError(f"{target} exists; stats never replaces a file (--append adds a line "
                            "to a running log instead)")
    durations, found = _read_durations(Path(args.durations) if args.durations
                                       else _durations_path(os.environ))
    sources = {"durations": "file" if found else "absent"}
    # The span the bank is read for, and an export clipped to: the window,
    # plus the park lookback before it.
    first = since - PARK_LOOKBACK
    if args.input:
        # An export already has the payload parsed; wakes are not exported.
        events = [row for row in _read_export(Path(args.input))
                  if first <= row["created_at"] < until]
        wakes = []
        sources.update(events="export", wakes="none")
    else:
        with _bank() as conn:
            with closing(audit_events(conn, since=first, until=until)) as rows:
                events = []
                for row in rows:
                    row = dict(row)
                    row["payload"] = json.loads(row["payload"])
                    events.append(row)
            wakes = _wake_rows(conn, since, until)
        sources.update(events="bank", wakes="bank" if wakes is not None else "absent")
    report = compute_stats(events, wakes or [], durations, since=since, until=until, now=now,
                           version=__version__, sources=sources)
    _emit(args, json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n")
    return EXIT_OK


def _emit(args, line: str) -> None:
    """Where ``stats`` puts its one line: a new ``--out`` file, an
    ``--append`` log, else stdout."""
    target = Path(args.out) if args.out else None
    if target is not None:
        try:
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
        except OSError as exc:
            raise AuditCliError(f"cannot write {target}: {exc.strerror}") from None
    if args.append:
        try:
            with Path(args.append).open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
        except OSError as exc:
            raise AuditCliError(f"cannot append to {args.append}: {exc.strerror}") from None
    if target is None and not args.append:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except OSError as exc:
            raise _write_error(None, exc) from None


# --- no bank here: the daemon container ------------------------------------------

def _forward(args) -> list[str]:
    """The arguments the container needs. Files the command names (--out,
    --append, --durations) are this host's: they are read and written here."""
    out = [args.action]

    def add(flag, value):
        if value is not None:
            out.extend([flag, str(value)])
    if args.action == "export":
        add("--project", args.project)
        add("--task", args.task)
        add("--agent", args.agent)
    if args.action in ("export", "stats"):
        add("--since", args.since)
        add("--until", args.until)
    if args.action == "verify" and args.expect_head is not None:
        add("--expect-head", "%d:%s" % args.expect_head)
    if args.action == "redact":
        add("--message-id", args.message_id)
        add("--reason", args.reason)
    return out


def _in_daemon(args) -> int:
    """No bank in this shell: run the action in the daemon container when
    it runs here (pseudolife_memory/daemon_exec.py); the container only
    reads the bank."""
    argv = _forward(args)
    if args.action == "stats":
        return _stats_in_daemon(args, argv)
    if args.action == "export" and args.out:
        target = Path(args.out)
        try:
            stream = target.open("xb")
        except OSError as exc:
            raise AuditCliError(f"cannot create {target}: {exc.strerror}") from None
        with stream:
            ran = run_in_daemon("board-audit", argv, stdout=stream)
        if ran is None or (ran.returncode != 0 and target.stat().st_size == 0):
            target.unlink()
    else:
        ran = run_in_daemon("board-audit", argv)
    if ran is None:
        raise AuditCliError(no_bank_message("board-audit"))
    return ran.returncode


def _stats_in_daemon(args, argv) -> int:
    """This host's suite-lock durations go in on stdin; the report comes
    back on stdout and is written here, as ``_stats`` writes it."""
    path = Path(args.durations) if args.durations else _durations_path(os.environ)
    try:
        durations = path.open("rb")
    except OSError:
        durations = None    # the container then finds none either: "absent", as here
    else:
        argv = [*argv, "--durations", "/dev/stdin"]
    try:
        ran = run_in_daemon("board-audit", argv, stdin=durations, stdout=subprocess.PIPE)
    finally:
        if durations is not None:
            durations.close()
    if ran is None:
        raise AuditCliError(no_bank_message("board-audit"))
    if ran.returncode != 0:
        return ran.returncode
    _emit(args, ran.stdout.decode("utf-8", errors="replace"))
    return EXIT_OK


def main(argv=None) -> int:
    """CLI entry point; accepts the arguments after ``board-audit``."""
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp board-audit",
        description="Export, verify, redact or compute stats on the agent board's audit log "
                    "(operator-only). Reads PSEUDOLIFE_MCP_DATABASE_URL, or the lite tier's "
                    "bank, else runs inside the pseudolife-mcp-daemon container when it "
                    "runs here. An export holds message bodies: keep it private.")
    actions = parser.add_subparsers(dest="action", required=True)
    export = actions.add_parser("export", help="write events as JSON lines, oldest first")
    export.add_argument("--project", help="only events in this project")
    export.add_argument("--task", help="only events in this task")
    export.add_argument("--agent", help="only events by this agent or to its mailbox "
                                        "(a full id, or a unique prefix of 8 or more characters)")
    export.add_argument("--since", type=_time, help="epoch seconds or ISO 8601, inclusive")
    export.add_argument("--until", type=_time, help="epoch seconds or ISO 8601, exclusive")
    export.add_argument("--out", help="a NEW file to write instead of stdout")
    verify = actions.add_parser("verify", help="check the hash chain and print its head")
    verify.add_argument("--expect-head", type=_head, metavar="SEQ:HASH",
                        help="a head recorded earlier; fails if it was dropped or rewritten")
    verify.add_argument("--input", help="verify an unfiltered export file instead of the bank")
    redact = actions.add_parser(
        "redact", help="remove one message body (sent from schema v46 on) and log why")
    redact.add_argument("--message-id", required=True,
                        help="the message whose body to remove (a full id, or a unique "
                             "prefix of 8 or more characters)")
    redact.add_argument("--reason", required=True,
                        help="why, kept in the log for good (at most 240 characters; "
                             "never repeat the secret)")
    stats = actions.add_parser(
        "stats", help="one JSON object of coordination telemetry for a window (default the "
                      "last 24 h): mail latency, wake precision, park outcomes, sends per "
                      "session-hour, suite lock waits; counts, seconds and names only")
    stats.add_argument("--since", type=_time, help="epoch seconds or ISO 8601, inclusive "
                                                   "(default: 24 h before --until)")
    stats.add_argument("--until", type=_time, help="epoch seconds or ISO 8601, exclusive "
                                                   "(default: now)")
    stats.add_argument("--input", help="read events from an export file instead of the bank "
                                       "(wake rows are not exported, so wake precision is empty)")
    stats.add_argument("--durations", help="the suite lock's durations file (default: "
                                           f"{DURATIONS_FILE} in the lock directory)")
    stats.add_argument("--out", help="a NEW file to write the object to instead of stdout")
    stats.add_argument("--append", help="a JSON lines file to append the object to (created "
                                        "if missing)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_OK
    try:
        return {"export": _export, "verify": _verify, "redact": _redact,
                "stats": _stats}[args.action](args)
    except NoBank:
        try:
            return _in_daemon(args)
        except AuditCliError as exc:
            print(f"board-audit: {exc}", file=sys.stderr)
    except AuditCliError as exc:
        print(f"board-audit: {exc}", file=sys.stderr)
    except _ReaderClosed:
        # Nothing is wrong with the bank. Point stdout at the null device so
        # the interpreter's exit-time flush cannot complain either.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        print("board-audit: the output was closed before the export finished",
              file=sys.stderr)
    except Exception:
        # Database/client exceptions can include the connection string.
        print("board-audit failed; the database error is not shown because it can "
              "include the connection string. Check PSEUDOLIFE_MCP_DATABASE_URL.",
              file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
