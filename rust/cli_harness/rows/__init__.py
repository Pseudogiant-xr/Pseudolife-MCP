"""One module per PARITY row; each exposes ``cases()``."""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

# Each row declares its PARITY ID (and its CI data, rust/ci/row_ci.py) in its
# own rows/<row>.json, so ports adding different rows touch no shared list.
ROWS = {path.stem: json.loads(path.read_text(encoding="utf-8"))["parity"]
        for path in sorted(Path(__file__).resolve().parent.glob("*.json"))}


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
