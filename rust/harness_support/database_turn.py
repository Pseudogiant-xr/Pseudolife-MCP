"""Opt-in database turns for machines whose harness manages PostgreSQL.

Unconfigured machines do only the marker existence check. Keep the process,
locking and containment implementation behind that check.
"""
from pathlib import Path

MARKER = "test-postgres.turn-enabled"
LOCK = "test-postgres.lock"


def lock_folder() -> Path:
    return Path.home() / ".pseudolife-mcp" / "locks"


def ensure_turn(*, holder: str = "database harness", wait_seconds: float = 0,
                contain: bool = False):
    folder = lock_folder()
    if not (folder / MARKER).exists():
        return None
    from . import _database_turn

    return _database_turn.acquire(folder / LOCK, holder=holder,
                                  wait_seconds=wait_seconds, contain=contain)


def require_turn(*, holder: str = "database harness", wait_seconds: float = 0,
                 contain: bool = False):
    """Give standalone callers a short refusal instead of a traceback."""
    try:
        return ensure_turn(holder=holder, wait_seconds=wait_seconds, contain=contain)
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
