"""Authenticate a bank binding before disclosing a saved mailbox credential."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import uuid

import httpx


def default_digest_dir(env=None) -> Path:
    """Where per-session turn digests live. The prompt hooks compute the same
    default from their own environment, so a host's MCP env block and its
    hook env must agree if ``PSEUDOLIFE_DIGEST_DIR`` overrides it."""
    configured = (os.environ if env is None else env).get("PSEUDOLIFE_DIGEST_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".pseudolife-mcp" / "digests"


def digest_path_for(session_id: str, root: Path | None = None) -> Path | None:
    """The digest file for a host session id, or None without one. The file
    name is the id's SHA-256 so the id itself never lands on disk."""
    if not session_id:
        return None
    root = default_digest_dir() if root is None else Path(root)
    return root / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".txt")


_HOST_KEY = re.compile(rb"[0-9a-f]{64}")


def resolve_digest_path(session_id: str, root: Path | None = None, *,
                        env=None) -> Path | None:
    """The digest file a Claude Code hook or Bash command should read for
    ``session_id``, its host's current session id.

    The shim keys its file by the id it was spawned with. ``/clear`` gives
    the session a new id, which the hooks and the Bash tool see but the shim
    never does. A ``claude-<CLAUDE_PID>.host`` record in the digest directory
    maps a Claude process back to the shim's spawn-time key, for hooks that
    handle ``/clear`` to write and read under this same rule. The record
    counts only when ``CLAUDE_PID`` is all ASCII digits,
    ``session_id`` is this process's current ``CLAUDE_CODE_SESSION_ID``, and
    the record is a regular file (not a link) whose first line is exactly 64
    lowercase hex digits (the shim's key) and whose second line is exactly
    the SHA-256 of ``session_id`` (the session the record was confirmed
    for). Otherwise the id keys the file directly."""
    env = os.environ if env is None else env
    path = digest_path_for(session_id, default_digest_dir(env) if root is None else root)
    pid = env.get("CLAUDE_PID", "")
    if (path is None or not (pid.isascii() and pid.isdigit())
            or env.get("CLAUDE_CODE_SESSION_ID") != session_id):
        return path
    record = path.parent / f"claude-{pid}.host"
    try:
        # Never follow a link, never block on a FIFO; judge what was opened.
        fd = os.open(record, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return path
    try:
        if os.path.islink(record) or not stat.S_ISREG(os.fstat(fd).st_mode):
            return path
        # Two 64-hex lines fit in 130 bytes; anything longer fails the match.
        lines = os.read(fd, 256).split(b"\n")
    except OSError:
        return path
    finally:
        os.close(fd)
    # Line 2 confirms the session the record belongs to, so a record left by
    # another session (or in the retired one-line format) is never followed.
    if (len(lines) < 2 or _HOST_KEY.fullmatch(lines[0]) is None
            or lines[1] != path.stem.encode("ascii")):
        return path
    return path.parent / (lines[0].decode("ascii") + ".txt")


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
