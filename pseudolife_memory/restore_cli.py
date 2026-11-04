"""Restore a Docker bank without a source checkout."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pseudolife_memory import update_cli as up
from pseudolife_memory.docker_backup_restore import DockerBank, validate_state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pseudolife-mcp restore", description="Replace a Docker bank from its backup.")
    parser.add_argument("--backup-file", required=True, type=Path, help="complete plain-format .sql.gz database dump")
    parser.add_argument("--state-archive", type=Path, help="matching state-volume .tgz backup")
    parser.add_argument("--yes", action="store_true", help="confirm replacing the existing bank without a prompt")
    parser.add_argument("--leave-stopped", action="store_true", help="leave the daemon stopped after restoring")
    parser.add_argument("--container", help="PostgreSQL container (default: pseudolife-mcp-postgres)")
    parser.add_argument("--daemon-container", help="daemon container (default: pseudolife-mcp-daemon)")
    parser.add_argument("--db", help="database name (default: the running daemon's configuration)")
    parser.add_argument("--user", help="database role (default: the running daemon's configuration)")
    parser.add_argument("--daemon-url", help="health endpoint (default: the daemon's published port)")
    args = parser.parse_args(argv)
    try:
        if not up._dump_complete(args.backup_file):
            raise up.UpdateError("the backup is unreadable or incomplete; use a complete .sql.gz dump. Nothing was restored", 2)
        if args.state_archive:
            validate_state(args.state_archive)
        if not args.yes:
            if not up.interactive():
                raise up.UpdateError("restore replaces the existing bank; rerun with --yes to confirm. Nothing was restored", 2)
            if up.ask("Restore replaces the existing bank after taking a safety backup. Continue? [y/N] ") not in ("y", "yes"):
                print("Restore cancelled; nothing was changed.")
                return 0
        with up.UpdateLock():
            bank = DockerBank(container=args.container, daemon_container=args.daemon_container, db=args.db, user=args.user,
                              daemon_url=args.daemon_url)
            bank.restore(args.backup_file, state_archive=args.state_archive, leave_stopped=args.leave_stopped)
        return 0
    except (up.UpdateError, OSError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, up.UpdateError) else 1


if __name__ == "__main__":
    sys.exit(main())
