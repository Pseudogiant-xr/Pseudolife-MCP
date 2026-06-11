"""Crash-safe torch persistence: tmp + os.replace + single .bak rotation.

The v0.1 silent-wipe hazard was ``torch.save`` writing the state file in
place — a crash mid-write corrupted the only copy, the tolerant loader
started empty, and the autosave overwrote the corpse. These helpers make
that sequence structurally impossible for the weights file (entries live
in Postgres from v0.2 on).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import torch


class WeightsCorrupt(Exception):
    """Both the primary file and its .bak failed to load."""


def _bak(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


def atomic_torch_save(obj: Any, path: str | Path) -> None:
    """Write via a temp file, rotating the previous file to ``.bak``.

    Crash windows: before ``os.replace(tmp, path)`` the old file (or its
    rotation to .bak) is intact; after, the new file is complete. There
    is no moment where the only copy is half-written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(obj, tmp)
    if path.exists():
        os.replace(path, _bak(path))
    os.replace(tmp, path)


def _load_one(path: Path) -> Any:
    """``weights_only=True`` first (no arbitrary pickle execution); fall
    back to a full unpickle for legacy saves containing non-tensor types."""
    try:
        return torch.load(path, map_location="cpu", weights_only=True)
    except Exception:  # noqa: BLE001
        return torch.load(path, map_location="cpu", weights_only=False)


def load_with_backup(path: str | Path) -> tuple[Any, bool]:
    """Load ``path``, falling back to its ``.bak``.

    Returns ``(obj, used_backup)``. Raises :class:`WeightsCorrupt` when
    neither loads (or neither exists).
    """
    path = Path(path)
    primary_exc: Exception | None = None
    if path.exists():
        try:
            return _load_one(path), False
        except Exception as exc:  # noqa: BLE001
            primary_exc = exc
    bak = _bak(path)
    if bak.exists():
        try:
            return _load_one(bak), True
        except Exception as exc:  # noqa: BLE001
            raise WeightsCorrupt(
                f"{path} and {bak} both unreadable: {primary_exc!r} / {exc!r}"
            ) from exc
    raise WeightsCorrupt(f"{path} unreadable and no backup: {primary_exc!r}")
