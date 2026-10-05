"""Consolidation trigger.

A dream is bank maintenance, not part of any episode, so it runs under its own
short-lived maintenance session rather than an episode's — writes it makes must
never be attributed to a graded run, and its session must never be a candidate
for ``used_ids`` credit.

The plugin only offers the per-episode trigger (``PL_DREAM=episode``, fired by
the batch hook after each simulation). Trial-level or run-level scheduling is the
orchestrator's business: it knows the run shape, and a dream between trials is a
decision about the experiment, not about one simulation.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from .client import PLMemoryClient

logger = logging.getLogger(__name__)

#: The same truth words ``agent._flag`` / ``memory_env._flag`` accept. Read
#: here rather than passed in because the trigger's only caller is the
#: harness's batch hook, which knows the cadence but not the arm.
_TRUTHY = ("1", "true", "yes", "on")


def dream_run(url: str, token: Optional[str] = None) -> dict:
    """Run one consolidation pass. Never raises: the client returns errors.

    Refuses outright on a frozen store: consolidation writes facts, so a
    transfer arm reading a read-only bank must not dream into it.
    """
    if (os.environ.get("PL_READ_ONLY") or "").strip().lower() in _TRUTHY:
        return {"skipped": "read_only"}
    client = PLMemoryClient(
        url=url,
        session_uid=f"maint-{uuid.uuid4().hex}",
        writer="taubench",
        token=token,
    )
    result = client.call_tool("memory_dream", {"action": "run"}) or {}
    # ``call_tool`` turns every failure into ``{"error": ...}`` rather than
    # raising, so the caller's try/except never fires: an unreachable daemon
    # would consolidate nothing, silently, for a whole run. Say so, and hand
    # the error back so a caller that can stop, stops.
    if isinstance(result, dict) and result.get("error"):
        logger.warning("memory_dream failed: %s", result["error"])
    return result
