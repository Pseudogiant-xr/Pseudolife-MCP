"""Migrate the Docker state volume, then exec the command without root.

Only /data is migrated. Application code and baked models stay root-owned
and readable. Operator tools that need root use Docker's --entrypoint
override, as the backup/restore scripts already do.
"""

import os
import sys
from pathlib import Path

STATE_DIR = Path("/data")
UID = GID = 10001


def _change_owner(name: str, fd: int) -> None:
    if os.stat(name, dir_fd=fd, follow_symlinks=False).st_uid == 0:
        os.chown(name, UID, GID, dir_fd=fd, follow_symlinks=False)


def migrate_state() -> None:
    """Transfer root-owned legacy state without following directory links.

    Descriptor-relative traversal keeps ownership changes inside the open
    state directory even if a directory is renamed during initialization.
    Existing non-root ownership and file contents are preserved.
    """
    fd = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        def refuse(error: OSError) -> None:
            raise error

        for _, dirs, files, directory in os.fwalk(
                ".", topdown=False, follow_symlinks=False, dir_fd=fd, onerror=refuse):
            for name in dirs + files:
                _change_owner(name, directory)
            _change_owner(".", directory)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        raise SystemExit("Pseudolife container: no command supplied")
    try:
        if os.geteuid() == 0:
            migrate_state()
            os.setgroups([])
            os.setgid(GID)
            os.setuid(UID)
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise SystemExit(f"Pseudolife container startup refused: {exc}") from exc


if __name__ == "__main__":
    main()
