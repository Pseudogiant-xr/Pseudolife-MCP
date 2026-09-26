"""Operator-only access to the agent board's audit log (schema v42).

``export`` streams ``coordination_events`` as JSON lines, one event per line,
in chain order; ``verify`` walks the hash chain and prints one JSON report;
``redact`` (schema v46) removes one message body and records why.

All three reach the bank directly, through ``PSEUDOLIFE_MCP_DATABASE_URL`` or
the lite tier's embedded instance, so they need the database owner's
credentials, never a bearer token. ``export`` and ``verify`` read one
read-only snapshot; ``redact`` writes one transaction under the same locks as
the daemon's own writes. All are safe to run beside a live daemon. There is
deliberately no MCP tool and no REST route: the log holds message bodies
verbatim, and bodies carry machine paths and usernames. Treat the output as
private: keep it out of repositories and anywhere public.

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
import sys

from pseudolife_memory.storage.coordination import (
    AUDIT_COLUMNS, CoordinationError, CoordinationStore, audit_events, audit_has_body_column,
    verify_audit_chain,
)

EXIT_OK, EXIT_BROKEN, EXIT_ERROR = 0, 1, 2

# What each redaction refusal means, for the operator's terminal. None of
# them repeats the reason or the body.
_REDACT_REFUSALS = {
    "invalid_message_id": "--message-id must be one message id as the audit log shows it",
    "invalid_reason": "--reason must be 1-240 characters on one line, with no control, "
                      "format or separator characters",
    "secret_like_body": "the reason looks like it holds a credential, and the reason is "
                        "kept in the log for good; describe the mistake without repeating it",
    "message_not_found": "no send event for this message id in the audit log: an unknown "
                         "id, or one audit retention already removed",
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
            raise AuditCliError("no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's "
                                "database URL, or run where the lite tier's data dir holds one")
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
            rows = audit_events(conn, project=args.project, task=args.task,
                                agent_id=args.agent, since=args.since, until=args.until)
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
    body stays readable there until a vacuum lets the space be reused). It
    runs after the commit, outside any transaction, as VACUUM must, and does
    not reach WAL, WAL archives or backups."""
    conn.execute("VACUUM coordination_events, coordination_messages")


def _redact(args) -> int:
    import psycopg
    with _bank(write=True) as conn:
        try:
            result = CoordinationStore(_Storage(conn)).redact(args.message_id, args.reason)
        except CoordinationError as exc:
            print(json.dumps({"ok": False, "message_id": args.message_id, "reason": exc.code}))
            print(f"board-audit: {_REDACT_REFUSALS.get(exc.code, exc.code)}", file=sys.stderr)
            return EXIT_BROKEN
        except psycopg.errors.LockNotAvailable:
            raise AuditCliError("the board is busy: a row or the audit chain stayed locked for "
                                "more than 5 s. Nothing was changed; retry") from None
        try:
            _vacuum(conn)
            vacuumed = True
        except psycopg.Error:
            vacuumed = False
    print(json.dumps({"ok": True, **result, "vacuumed": vacuumed}))
    if result["audit_copy"] == "kept":
        print("board-audit: the live copy is blanked and out of delivery, but this message "
              "was sent before schema v46: the audit log keeps its body until audit "
              "retention removes the send event", file=sys.stderr)
    if not vacuumed:
        print("board-audit: the redaction is committed, but the VACUUM that frees the old "
              "row versions failed (the board may be busy); run `VACUUM coordination_events, "
              "coordination_messages` later", file=sys.stderr)
    print("board-audit: record this head outside the bank; `pseudolife-mcp board-audit "
          f"verify --expect-head {result['expect_head']}` later shows the redaction record "
          "is still there", file=sys.stderr)
    return EXIT_OK


def main(argv=None) -> int:
    """CLI entry point; accepts the arguments after ``board-audit``."""
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp board-audit",
        description="Export, verify or redact the agent board's audit log (operator-only). "
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
    redact = actions.add_parser(
        "redact", help="remove one message body (sent from schema v46 on) and log why")
    redact.add_argument("--message-id", required=True, help="the message whose body to remove")
    redact.add_argument("--reason", required=True,
                        help="why, kept in the log for good (at most 240 characters; "
                             "never repeat the secret)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_OK
    try:
        return {"export": _export, "verify": _verify, "redact": _redact}[args.action](args)
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
