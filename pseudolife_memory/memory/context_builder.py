"""Relative-age rendering for retrieved memories.

This module used to assemble augmented prompts as well, but the MCP
server returns raw entries and the client builds its own context, so
only the age helper remains — ``service.py`` uses it to stamp cortex
records with a human-readable ``age``.
"""

from __future__ import annotations

import time


def _relative_time(timestamp: float, now: float | None = None) -> str:
    """Render a timestamp as a coarse human-readable offset.

    Buckets: "just now", "N minutes ago", "N hours ago", "N days ago".
    Unknown or zero timestamps return "unknown time".
    """
    if not timestamp:
        return "unknown time"
    now = now if now is not None else time.time()
    age = max(now - timestamp, 0.0)
    if age < 60:
        return "just now"
    if age < 3600:
        minutes = int(age // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if age < 86400:
        hours = int(age // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(age // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
