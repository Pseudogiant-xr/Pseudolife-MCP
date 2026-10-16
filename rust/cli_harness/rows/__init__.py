"""One module per PARITY row; each exposes ``cases()``."""

from __future__ import annotations

import importlib
import importlib.util

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


def fixture_login_secrets(row: str) -> tuple[str, ...]:
    """The fixed login secrets a row's goldens may carry as written (its
    ``FIXTURE_LOGIN_SECRETS``); a drawn one never reaches a golden. A row
    with no module of its own declares none."""
    if importlib.util.find_spec(f"{__name__}.{row}") is None:
        return ()
    return tuple(getattr(importlib.import_module(f"{__name__}.{row}"),
                         "FIXTURE_LOGIN_SECRETS", ()))


def mutants(row: str) -> list:
    """The row module's own ``MUTANTS`` list (empty when it has none)."""
    return list(getattr(importlib.import_module(f"{__name__}.{row}"), "MUTANTS", []))
