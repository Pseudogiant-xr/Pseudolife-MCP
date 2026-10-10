"""The handshake shim's cache file name, shared by the rows whose CLI runs a
shim handshake against a fixture daemon (doctor, connect)."""

from __future__ import annotations

import hashlib
import re

from .. import normalize

_CACHE = re.compile(r"^(\.pseudolife-mcp/handshake-cache/)([0-9a-f]{16})(\.json)$")
NAME = "<fixture-url-hash>"


@normalize.rule("shim-handshake-cache-name")
def handshake_cache_name(obs: dict) -> None:
    """The shim names its handshake cache ``sha256(daemon url)[:16]``
    (shim.py ``_handshake_cache_path``, ``cache.rs``), and the fixture's port
    differs per run, so a golden recorded on one port and a replay on another
    would name the same file apart. Only a cache file whose name is that hash
    of this observation's own fixture URL (``fixture_url``, recorded by the
    row's ``after`` hook) is renamed ``<fixture-url-hash>.json``, in the
    files and in every record keyed by them; a cache for any other URL keeps
    its name, and its content is compared as before."""
    url = obs.get("fixture_url")
    if not url:
        return
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    renamed: dict[str, str] = {}
    for rel in obs.get("files", {}):
        match = _CACHE.match(rel)
        if match and match.group(2) == digest:
            renamed[rel] = match.group(1) + NAME + match.group(3)
    if not renamed:
        return
    obs["files"] = {renamed.get(rel, rel): value for rel, value in obs["files"].items()}
    db = obs.get("db")
    for record in (obs.get("modes"), db.get("windows_acl") if isinstance(db, dict) else None):
        if isinstance(record, dict):
            moved = {renamed.get(rel, rel): value for rel, value in record.items()}
            record.clear()
            record.update(moved)
