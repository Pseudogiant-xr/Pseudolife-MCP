"""The plugin hook scripts and their digest, as the frozen daemon reports it
on /health (rust/daemon/src/health.rs keeps the same list and rule;
tests/test_hooks_digest.py pins the implementations to each other).

Copied verbatim, by syntax tree, from pseudolife_memory/plugin_hooks.py at 6ca817d70:
HOOK_SCRIPTS :30-32, hooks_digest :35-46.

The source is frozen, so the copy cannot drift from it; never edit it by hand.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


HOOK_SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh",
                "coordination-start.sh", "coordination-prompt.sh", "session-end.sh",
                "stop-wake.sh", "subagent-board-guard.sh", "subagent-board.sh")


def hooks_digest(directory: Path | str) -> str | None:
    """SHA-256 hex of the hook scripts under ``directory``, or ``None``
    when any is missing (a directory that is not a hooks directory)."""
    directory = Path(directory)
    digest = hashlib.sha256()
    for name in HOOK_SCRIPTS:
        try:
            body = (directory / name).read_bytes()
        except OSError:
            return None
        digest.update(name.encode() + b"\0" + body.replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()
