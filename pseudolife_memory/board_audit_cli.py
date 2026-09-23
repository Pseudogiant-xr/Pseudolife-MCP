"""Operator-only, read-only access to the agent board's audit log (schema v42).

``export`` streams ``coordination_events`` as JSON lines, one event per line,
in chain order; ``verify`` walks the hash chain and prints one JSON report.

Both read the bank directly, through ``PSEUDOLIFE_MCP_DATABASE_URL`` or the
lite tier's embedded instance, in a read-only snapshot, so they need the
database owner's credentials, never a bearer token, and are safe to run beside
a live daemon. There is deliberately
no MCP tool and no REST route: the log holds message bodies verbatim, and
bodies carry machine paths and usernames. Treat the output as private: keep it
out of repositories and anywhere public.

Exit status: 0 success, 1 the chain failed verification or an expected head
could not be confirmed, 2 nothing could be checked (bad arguments, no bank, no
audit log, an unreadable file, output closed early).
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys

from pseudolife_memory.storage.coordination import (
    AUDIT_COLUMNS, audit_events, verify_audit_chain,
)

EXIT_OK, EXIT_BROKEN, EXIT_ERROR = 0, 1, 2


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
    if (not sep or not seq.isdigit() or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise argparse.ArgumentTypeError("expected SEQ:HASH as verify prints them")
    return int(seq), digest


class _ReaderClosed(Exception):
    """Whoever read stdout stopped early (``export | head``)."""


@contextmanager
def _bank():
    """A read-only connection to the bank, found the way ``export`` finds it:
    PSEUDOLIFE_MCP_DATABASE_URL, else the lite tier's embedded instance
    (attached, or started for the duration and stopped afterwards)."""
    from pseudolife_memory.backup_cli import _default_data_dir
    from pseudolife_memory.transfer_cli import _resolve_dsn
    dsn, own_instance = _resolve_dsn(_default_data_dir(os.environ))
    try:
        if not dsn:
            raise AuditCliError("no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's "
                                "database URL, or run where the lite tier's data dir holds one")
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=5)
        try:
            # One read-only, repeatable-read snapshot: a daemon appending
            # while this runs cannot tear the chain being read.
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.execute("SET search_path TO public")
            present = conn.execute(
                "SELECT to_regclass('public.coordination_events') IS NOT NULL").fetchone()[0]
            conn.commit()
            if not present:
                raise AuditCliError("this bank has no audit log yet (schema older than v42); "
                                    "start a v42 daemon once to create it")
            yield conn
        finally:
            conn.close()
    finally:
        if own_instance is not None:
            own_instance.stop()


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
            rows = audit_events(conn, project=args.project, task=args.task,
                                agent_id=args.agent, since=args.since, until=args.until)
            try:
                for row in rows:
                    row = dict(row)
                    row["payload"] = json.loads(row["payload"])
                    line = json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
                    try:
                        stream.write(line)
                    except OSError:
                        # EPIPE on POSIX, EINVAL on Windows.
                        if target is None:
                            raise _ReaderClosed() from None
                        raise
                try:
                    stream.flush()
                except OSError:
                    if target is None:
                        raise _ReaderClosed() from None
                    raise
            except BaseException:
                rows.close()
                if target is not None:
                    stream.close()
                    target.unlink(missing_ok=True)  # this run created it exclusively
                raise
    return EXIT_OK


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
                row = json.loads(line)
            except ValueError:
                raise AuditCliError(f"{path} line {number} is not JSON") from None
            if (not isinstance(row, dict) or set(AUDIT_COLUMNS) - set(row)
                    or type(row["seq"]) is not int
                    or type(row["created_at"]) not in (int, float)
                    or not all(isinstance(row[k], str) for k in (
                        "event", "actor", "principal", "agent_id", "project", "task",
                        "hlc", "prev_hash", "hash"))):
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


def main(argv=None) -> int:
    """CLI entry point; accepts the arguments after ``board-audit``."""
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp board-audit",
        description="Export or verify the agent board's audit log (operator-only, read-only). "
                    "Reads PSEUDOLIFE_MCP_DATABASE_URL, or the lite tier's bank. The output "
                    "holds message bodies: "
                    "keep it private.")
    actions = parser.add_subparsers(dest="action", required=True)
    export = actions.add_parser("export", help="write events as JSON lines, oldest first")
    export.add_argument("--project", help="only events in this project")
    export.add_argument("--task", help="only events in this task")
    export.add_argument("--agent", help="only events by this agent or to its mailbox")
    export.add_argument("--since", type=_time, help="epoch seconds or ISO 8601, inclusive")
    export.add_argument("--until", type=_time, help="epoch seconds or ISO 8601, exclusive")
    export.add_argument("--out", help="a NEW file to write instead of stdout")
    verify = actions.add_parser("verify", help="check the hash chain and print its head")
    verify.add_argument("--expect-head", type=_head, metavar="SEQ:HASH",
                        help="a head recorded earlier; fails if it was dropped or rewritten")
    verify.add_argument("--input", help="verify an unfiltered export file instead of the bank")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_OK
    try:
        return _export(args) if args.action == "export" else _verify(args)
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
