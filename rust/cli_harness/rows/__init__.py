"""One module per PARITY row; each exposes ``cases()`` and may define
``MUTANTS`` (a list of ``mutants.Mutant``) for its own mutant control."""

from __future__ import annotations

import importlib

ROWS: dict[str, str] = {
    "transfer": "CLI-TRANSFER-DSN",
}


def load(row: str):
    return importlib.import_module(f"{__name__}.{row}").cases()


def mutants(row: str) -> list:
    return list(getattr(importlib.import_module(f"{__name__}.{row}"), "MUTANTS", []))
