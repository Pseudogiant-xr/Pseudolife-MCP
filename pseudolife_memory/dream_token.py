"""Signed, exact membership tokens for manual dream acknowledgement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


_PREFIX = "v1"
MAX_ENTRY_IDS = 4096
MAX_TOKEN_CHARS = 262_144


@dataclass(frozen=True)
class DreamCommitPayload:
    """Verified token contents."""

    entry_ids: tuple[int | str, ...]
    display_timestamp: float


def public_generation(secret: str) -> str:
    """Return a non-secret identifier for one acknowledgement generation."""
    raw = _secret_bytes(secret)
    return hashlib.sha256(raw).hexdigest()


def issue_commit_token(
    *,
    secret: str,
    backend: str,
    generation: str,
    entry_ids: list[int | str],
    display_timestamp: float,
) -> str:
    """Sign an ordered, exact set of entry identities."""
    try:
        _validate_entry_ids(entry_ids, backend)
        timestamp = float(display_timestamp)
        if not _is_finite(timestamp):
            raise ValueError
        payload = {
            "b": backend,
            "g": generation,
            "i": entry_ids,
            "t": timestamp,
        }
        encoded = _b64encode(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
        message = f"{_PREFIX}.{encoded}".encode("ascii")
        signature = _b64encode(hmac.digest(
            _secret_bytes(secret), message, "sha256"))
        token = f"{_PREFIX}.{encoded}.{signature}"
        if len(token) > MAX_TOKEN_CHARS:
            raise ValueError
        return token
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("invalid_dream_commit_payload") from exc


def verify_commit_token(
    token: str,
    *,
    secret: str,
    backend: str,
    generation: str,
) -> DreamCommitPayload:
    """Verify a token and return its exact entry membership."""
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
            raise ValueError
        prefix, encoded, signature = token.split(".")
        if prefix != _PREFIX:
            raise ValueError
        message = f"{prefix}.{encoded}".encode("ascii")
        expected = _b64encode(hmac.digest(_secret_bytes(secret), message, "sha256"))
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload: Any = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("b") != backend or payload.get("g") != generation:
            raise ValueError
        entry_ids = payload.get("i")
        _validate_entry_ids(entry_ids, backend)
        timestamp = float(payload["t"])
        if not _is_finite(timestamp):
            raise ValueError
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError,
            UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_dream_commit_token") from exc
    return DreamCommitPayload(tuple(entry_ids), timestamp)


def _secret_bytes(secret: str) -> bytes:
    try:
        raw = bytes.fromhex(secret)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_dream_ack_secret") from exc
    if len(raw) != 32:
        raise ValueError("invalid_dream_ack_secret")
    return raw


def _validate_entry_ids(entry_ids: Any, backend: str) -> None:
    if backend not in ("file", "postgres"):
        raise ValueError
    if (not isinstance(entry_ids, list) or not entry_ids
            or len(entry_ids) > MAX_ENTRY_IDS):
        raise ValueError
    if backend == "postgres":
        if any(type(item) is not int or item <= 0 for item in entry_ids):
            raise ValueError
    else:
        if any(not _is_uuid_hex(item) for item in entry_ids):
            raise ValueError
    if len(set(entry_ids)) != len(entry_ids):
        raise ValueError


def _is_uuid_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))
