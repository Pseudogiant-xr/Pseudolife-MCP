"""One module per PARITY row; each exposes ``cases()``."""

from __future__ import annotations

import importlib

ROWS = {
    "mail": "CLI-MAIL",
    "audit": "CLI-AUDIT",
    "connect": "CLI-CONNECT",
    "backup": "CLI-BACKUP-DSN",
}


def load(row: str):
    return importlib.import_module(f"{__name__}.{row}").cases()


def mutants(row: str) -> list:
    """The row module's own ``MUTANTS`` list (empty when it has none)."""
    return list(getattr(importlib.import_module(f"{__name__}.{row}"), "MUTANTS", []))
