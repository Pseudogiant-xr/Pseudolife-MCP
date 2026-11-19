"""Load captured CLI payloads and seed them through the private writer."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_SOURCE: Path | None = None
_PAYLOADS = json.loads(Path(__file__).with_name("producer_payloads.json").read_text(encoding="utf-8"))


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


def wait_mail_digest() -> str:
    return _PAYLOADS["wait_mail_digest"]


def doorbell_record(kind: str) -> str:
    return _PAYLOADS["doorbell_records"][kind]


def write_digest(path: Path, watermark: int, text: str) -> None:
    # ``CoordinationAdapter._write_digest``'s body.
    write_private(path, f"{watermark}\n" + (text + "\n" if text else ""))


def write_ring(path: Path, watermark: int, decision: str, reason: str) -> None:
    # ``CoordinationAdapter._write_ring``'s body.
    write_private(path, f"{watermark}\n{decision} {reason}\n")


def write_seen(path: Path, watermark: int) -> None:
    # ``CoordinationAdapter._mark_seen``'s body.
    write_private(path, f"{watermark}\n")
