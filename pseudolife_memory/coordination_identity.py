"""Authenticate a bank binding before disclosing a saved mailbox credential."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import uuid

import httpx


def bound_state_path(root: Path, url: str, thread_id: str) -> Path:
    # Authority is pinned inside the record, rather than selecting a new file
    # when the caller changes principal or the endpoint serves another bank.
    namespace = hashlib.sha256(b"coordination-v2\0" + url.rstrip("/").encode()).hexdigest()[:32]
    return root / namespace / (hashlib.sha256(thread_id.encode("ascii")).hexdigest() + ".json")


def read_legacy(path: Path, url: str) -> dict:
    from .coordination_adapter import AdapterError, _open_state

    current = path.absolute()
    while True:
        if current.is_symlink():
            raise AdapterError("legacy adapter state must not traverse links")
        if current == current.parent:
            break
        current = current.parent
    try:
        fd = _open_state(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError
        state = json.loads(raw)
        if (not isinstance(state, dict) or state.get("bank_url") != url
                or not all(isinstance(state.get(key), str) and state[key]
                           for key in ("agent_id", "credential"))):
            raise ValueError
        return state
    except (OSError, ValueError):
        raise AdapterError("legacy adapter state is unavailable or invalid; state was preserved") from None


async def fetch_context(client, url: str, snapshot, *, identity=None) -> dict:
    from .coordination_adapter import AdapterError

    body = {}
    if identity is not None:
        body = {"agent_id": identity["agent_id"], "nonce": uuid.uuid4().hex}
    try:
        response = await client.post(url + "/api/coordination/context", json=body,
            headers={"Authorization": "Bearer " + snapshot.token}, timeout=5,
            follow_redirects=False)
    except httpx.TransportError:
        raise AdapterError("coordination bank context is unavailable") from None
    if response.status_code != 200:
        # No response body is trusted as a diagnostic or recovery instruction.
        code = "unauthorized" if response.status_code == 401 else "context_unavailable"
        raise AdapterError("coordination bank context was refused", status=response.status_code, code=code)
    try:
        value = response.json()
        bank_id, principal = value["bank_id"], value["principal"]
        if (not isinstance(bank_id, str) or str(uuid.UUID(bank_id)) != bank_id
                or not isinstance(principal, str) or not 1 <= len(principal) <= 256
                or any(ord(char) < 32 for char in principal)):
            raise ValueError
        context = {"bank_id": bank_id, "principal": principal}
        if identity is not None:
            message = json.dumps(["pseudolife-context-v1", bank_id, principal,
                identity["agent_id"], body["nonce"]], separators=(",", ":"), ensure_ascii=True).encode("ascii")
            expected = hmac.new(hashlib.sha256(identity["credential"].encode()).digest(),
                                message, hashlib.sha256).hexdigest()
            proof = value.get("proof")
            if not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
                raise ValueError
        return context
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AdapterError("coordination bank identity could not be verified", code="bank_identity_mismatch") from None


def check_binding(state: dict, context: dict) -> None:
    from .coordination_adapter import AdapterError

    if state.get("version") != 2 or any(state.get(key) != value for key, value in context.items()):
        raise AdapterError("saved mailbox belongs to a different bank or principal; state was preserved",
                           code="bank_identity_mismatch")
