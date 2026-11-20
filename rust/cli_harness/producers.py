"""Load captured CLI payloads and seed them through the private writer."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Direct and isolated CLI launches put only <repo>/rust on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from rust.harness_support.private_fixture import write_private
finally:
    sys.path.pop(0)

_SOURCE: Path | None = None
_PAYLOADS = json.loads(Path(__file__).with_name("producer_payloads.json").read_text(encoding="utf-8"))


def use_oracle(source: Path) -> None:
    global _SOURCE
    _SOURCE = source
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


def digest_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def digest_dir(home: Path) -> Path:
    return home / ".pseudolife-mcp" / "digests"


def wait_mail_digest() -> str:
    return _PAYLOADS["wait_mail_digest"]


def doorbell_record(kind: str) -> str:
    return _PAYLOADS["doorbell_records"][kind]


def write_digest(path: Path, watermark: int, text: str, *, root: Path) -> None:
    # ``CoordinationAdapter._write_digest``'s body.
    write_private(path, f"{watermark}\n" + (text + "\n" if text else ""), root=root)


def write_ring(path: Path, watermark: int, decision: str, reason: str, *, root: Path) -> None:
    # ``CoordinationAdapter._write_ring``'s body.
    write_private(path, f"{watermark}\n{decision} {reason}\n", root=root)


def write_seen(path: Path, watermark: int, *, root: Path) -> None:
    # ``CoordinationAdapter._mark_seen``'s body.
    write_private(path, f"{watermark}\n", root=root)
