"""Seed state through the oracle's own writers, not hand-built bytes.

The harness imports the oracle checkout's modules (``use_oracle`` puts its
source first on ``sys.path``) and calls the functions that write these files
in production: the coordination adapter's private atomic writer and its
digest renderer.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_SOURCE: Path | None = None


def use_oracle(source: Path) -> None:
    global _SOURCE
    _SOURCE = source
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


def _adapter_module():
    import pseudolife_memory.coordination_adapter as module  # noqa: PLC0415
    return module


def digest_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def digest_dir(home: Path) -> Path:
    return home / ".pseudolife-mcp" / "digests"


def _writer():
    module = _adapter_module()
    adapter = module.CoordinationAdapter.__new__(module.CoordinationAdapter)
    adapter._digest_write_reported = False
    return adapter


def write_private(path: Path, body: str) -> None:
    """``CoordinationAdapter._write_private``: private dir, temp, replace."""
    if not _writer()._write_private(path, body):
        raise RuntimeError(f"producer could not write {path}")


def render(count: int, preview: list[dict], maintainer: int = 0) -> str:
    return _adapter_module().render_digest(count, preview, maintainer)


def write_digest(path: Path, watermark: int, text: str) -> None:
    # ``CoordinationAdapter._write_digest``'s body.
    write_private(path, f"{watermark}\n" + (text + "\n" if text else ""))


def write_ring(path: Path, watermark: int, decision: str, reason: str) -> None:
    # ``CoordinationAdapter._write_ring``'s body.
    write_private(path, f"{watermark}\n{decision} {reason}\n")


def write_seen(path: Path, watermark: int) -> None:
    # ``CoordinationAdapter._mark_seen``'s body.
    write_private(path, f"{watermark}\n")


def preview(message_id: str, sender: str, excerpt: str, created_at: float = 1_791_500_000.0,
            agent: str = "0123456789abcdef0123456789abcdef") -> dict:
    return {"message_id": message_id, "sender_label": sender, "created_at": created_at,
            "sender_agent_id": agent, "excerpt": excerpt}
