"""One module per PARITY row; each exposes ``cases()``."""

from __future__ import annotations

import importlib

ROWS = {
    "mail": "CLI-MAIL",
    "audit": "CLI-AUDIT",
    "backup": "CLI-BACKUP-DSN",
    "hook": "CLI-HOOK",
    "episode": "CLI-EPISODE",
    "lease": "CLI-LEASE-core",
    "transfer": "CLI-TRANSFER-DSN",
    "test_login": "CLI-TEST-LOGIN",
    "doctor": "CLI-DOCTOR",
    "connect": "CLI-CONNECT",
    "maintainer": "CLI-MAINTAINER",
    "invite": "CLI-PAIRING",
    "pair": "CLI-PAIRING",
    "move": "CLI-MOVE-REFUSALS",
    "tunnel": "CLI-TUNNEL-STATUS",
}


def load(row: str):
    return importlib.import_module(f"{__name__}.{row}").cases()


def mutants(row: str) -> list:
    """The row module's own ``MUTANTS`` list (empty when it has none)."""
    return list(getattr(importlib.import_module(f"{__name__}.{row}"), "MUTANTS", []))
