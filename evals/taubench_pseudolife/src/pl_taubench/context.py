"""Per-simulation run context, carried thread-locally.

The harness runs each (task, trial) on its own ``ThreadPoolExecutor`` worker, and
the whole simulation — conversation turns and the task-end reflection — runs on
that thread. So the batch runner stamps the context once per simulation and every
telemetry record reads it back on the same thread.

Mirrors the paper harness's ``memory_metrics.set_context`` so the patch hunk that
calls it is a line-for-line analogue.
"""

from __future__ import annotations

import os
import threading
from typing import Any

_local = threading.local()


def set_context(**ctx: Any) -> None:
    """Set the calling thread's run context. ``None`` values are dropped.

    ``trial`` is shifted by ``PL_TRIAL_OFFSET``. An orchestrator that wants a
    consolidation pass BETWEEN trials cannot express that inside one harness
    run, so it runs the harness once per trial with a single trial each — and
    the harness then reports trial 0 every time. The offset is the real
    column; without it every record of such a run is stamped trial 0, which
    folds four trials of telemetry into one. A missing or unparseable offset
    is 0, never an error: a stamping problem must not end a run.
    """
    ctx = {k: v for k, v in ctx.items() if v is not None}
    trial = ctx.get("trial")
    if isinstance(trial, int) and not isinstance(trial, bool):
        ctx["trial"] = trial + _trial_offset()
    _local.ctx = ctx


def _trial_offset() -> int:
    raw = (os.environ.get("PL_TRIAL_OFFSET") or "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def get_context() -> dict:
    return dict(getattr(_local, "ctx", None) or {})


def clear_context() -> None:
    _local.ctx = {}
