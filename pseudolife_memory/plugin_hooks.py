"""The plugin's hook scripts, digested the same way on every side.

The Claude Code plugin's version is pinned to the package version, so a
change to ``plugin/hooks`` without a release leaves the version string
alone: on 2026-09-21 ``/plugin update`` answered "already at the latest
version" while the cached scripts differed from master, and the version
handshake saw two equal strings. The daemon therefore also publishes a
digest of the scripts it was built with (``/health`` ``hooks_digest``), the
SessionStart hooks send a digest of the scripts beside them, and the
briefing says when the two differ.

The digest must come out identical from Python, bash (``sha256sum`` /
``shasum``) and PowerShell, over a checkout, a marketplace cache or Codex's
content-addressed copy, on any line-ending convention: SHA-256 over
``name NUL bytes NUL`` for the four scripts in :data:`HOOK_SCRIPTS` order,
with CRLF normalised to LF. ``tests/test_hooks_digest.py`` pins the three
implementations against each other.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# In this order on every side. ``hooks.json`` is deliberately absent: Codex's
# copy of the scripts carries no manifest, and must digest the same.
HOOK_SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh", "session-end.sh")


def hooks_digest(directory: Path | str) -> str | None:
    """SHA-256 hex of the four hook scripts under ``directory``, or ``None``
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


def plugin_dir() -> Path | None:
    """Where this daemon's copy of the plugin lives: ``PSEUDOLIFE_PLUGIN_DIR``
    (the image sets it), else ``plugin/`` beside the package in a checkout.
    ``None`` when neither holds the hook scripts."""
    configured = os.environ.get("PSEUDOLIFE_PLUGIN_DIR")
    candidates = [Path(configured)] if configured else [Path(__file__).resolve().parents[1] / "plugin"]
    for candidate in candidates:
        if (candidate / "hooks" / HOOK_SCRIPTS[0]).is_file():
            return candidate
    return None


def daemon_hooks_digest() -> str | None:
    """The digest of the hook scripts this daemon ships, or ``None`` when it
    has no plugin tree (a bare pip install of the package)."""
    directory = plugin_dir()
    return hooks_digest(directory / "hooks") if directory else None
