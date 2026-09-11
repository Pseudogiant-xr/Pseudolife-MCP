"""Offline, operator-only mailbox recovery after an explicit database restore.

Never starts a daemon, opens a model session, migrates a schema, or prints keys.
Stopping the daemon is an operator precondition, not inferred from a config file.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import sys

import psycopg

from pseudolife_memory.coordination_adapter import (
    CoordinationAdapter, _StateReservation, _open_state,
)
from pseudolife_memory.storage.coordination import CoordinationStore
from pseudolife_memory.utils.config import load_config


class RecoveryError(ValueError):
    """Operator-safe diagnostic containing no database or credential values."""


def _connect():
    dsn = os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
    if not dsn:
        raise RecoveryError("PSEUDOLIFE_MCP_DATABASE_URL is required; no database is started automatically")
    conn = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
    try:
        conn.execute("SET search_path TO public")
        conn.execute("SET lock_timeout = '5s'")
        return conn
    except BaseException:
        conn.close()
        raise


class _RecoveryStorage:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction() as transaction:
            yield
        if transaction.status is not transaction.Status.COMMITTED:
            raise RecoveryError("database commit was not confirmed; keep coordination disabled")


def _perform(args):
    path = Path(args.config)
    if not path.is_file():
        raise RecoveryError("an existing daemon configuration file is required")
    config = load_config(path)
    if config.coordination.enabled:
        raise RecoveryError("coordination must be disabled in the daemon configuration")
    if not args.confirm_daemon_stopped:
        raise RecoveryError("stop the daemon and adapters, then pass --confirm-daemon-stopped")
    if args.action == "recover":
        if not args.confirm_restore:
            raise RecoveryError("restore recovery revokes every instance credential; --confirm-restore is required")
        if any((args.agent, args.principal, args.bank_url, args.state)):
            raise RecoveryError("rebind parameters cannot be used for recover")
        with _connect() as conn:
            result = CoordinationStore(_RecoveryStorage(conn)).recover()
        print(f"Revoked {result['revoked']} mailbox credentials; pending mail retained and wake disabled.")
        return

    if not all((args.agent, args.principal, args.bank_url, args.state)):
        raise RecoveryError("rebind requires --agent, --principal, --bank-url and --state")
    if args.principal not in config.coordination.allowed_principals:
        raise RecoveryError("the requested owner must be in coordination.allowed_principals")
    state_path = Path(args.state).resolve()
    if any((parent / ".git").exists() for parent in state_path.parents):
        raise RecoveryError("private state must be outside a Git repository")
    # Constructing the adapter validates the bank URL; no network client is opened.
    adapter = CoordinationAdapter(args.bank_url, "offline-recovery", state_path=args.state)
    try:
        fd = _open_state(adapter.state_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RecoveryError("state output must be a new private file; existing files are never replaced") from None
    try:
        stat = os.fstat(fd)
        reservation = _StateReservation(stat.st_dev, stat.st_ino)
    finally:
        os.close(fd)
    with _connect() as conn:
        storage = _RecoveryStorage(conn)
        # A failed private-file write must not commit an inaccessible credential.
        # If the process/commit fails after the write, retain the file for explicit
        # operator diagnosis; never infer success or automatically register again.
        with storage._txn():
            result = CoordinationStore(storage).rebind(args.agent, args.principal)
            adapter._identity = {key: result[key] for key in ("agent_id", "credential")}
            adapter._save_new_identity(reservation)
    print("Mailbox rebound; private state written. Wake remains disabled until explicit adapter opt-in.")


def main(argv=None) -> int:
    """CLI entry point; accepts arguments after ``coordination-recovery``."""
    parser = argparse.ArgumentParser(prog="pseudolife-mcp coordination-recovery",
                                     description=__doc__)
    parser.add_argument("action", choices=("recover", "rebind"))
    parser.add_argument("--config", required=True, help="Restored daemon configuration file")
    parser.add_argument("--confirm-daemon-stopped", action="store_true")
    parser.add_argument("--confirm-restore", action="store_true")
    parser.add_argument("--agent")
    parser.add_argument("--principal")
    parser.add_argument("--bank-url")
    parser.add_argument("--state", help="New private adapter state file outside the repository")
    args = parser.parse_args(argv)
    try:
        _perform(args)
        return 0
    except RecoveryError as exc:
        print(f"coordination recovery: {exc}", file=sys.stderr)
    except Exception:
        # Database/client exceptions can include DSNs, keys and private paths.
        print("coordination recovery failed; keep coordination disabled and inspect private state "
              "before retrying. Credentials were not printed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
