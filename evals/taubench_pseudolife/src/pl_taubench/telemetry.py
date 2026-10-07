"""JSONL telemetry for memory calls and episode outcomes.

Every memory operation flows through ``client.PLMemoryClient.call_tool``, so that
is the single instrumentation point for calls; the task-end reflection adds one
episode record carrying the verdict, the window check, and the daemon's full
``used_ids_*`` accounting (which ids were credited, which went unmatched, which
were served under a different session, and why).

Telemetry must never break a run: every entry point swallows its own errors.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .context import get_context

DEFAULT_LOG_PATH = Path("local") / "data" / "taubench_memory_events.jsonl"

_write_lock = threading.Lock()
_cached: Optional[tuple[str, Path]] = None


def reset_log_path() -> None:
    """Drop the resolved-path cache (tests, and any in-process env change)."""
    global _cached
    _cached = None


def log_path() -> Path:
    """Where records land: ``PL_TAUBENCH_LOG``, else the default under ``local/``.

    The default is relative to the working directory because runs are launched
    from the repo root; ``local/`` is gitignored scratch, so a stray call that
    forgets the env var cannot land in tracked data.
    """
    global _cached
    env = os.environ.get("PL_TAUBENCH_LOG") or ""
    if _cached is not None and _cached[0] == env:
        return _cached[1]
    path = Path(env) if env else DEFAULT_LOG_PATH
    _cached = (env, path)
    return path


def _append(record: dict) -> None:
    try:
        path = log_path()
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str)
        with _write_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - telemetry never breaks a run
        pass


def record_call(
    *,
    tool: str,
    args_digest: Optional[dict],
    ok: bool,
    latency_ms: float,
    result_len: int,
    served_ids: Optional[list] = None,
    extra: Optional[dict] = None,
) -> None:
    """One memory tool call. ``args_digest`` is a small, low-cardinality summary."""
    try:
        record = {
            "ts": time.time(),
            "kind": "call",
            **get_context(),
            "tool": tool,
            "ok": bool(ok),
            "latency_ms": round(float(latency_ms), 1),
            "result_len": int(result_len or 0),
            "args": args_digest or {},
        }
        if served_ids is not None:
            record["served_ids"] = list(served_ids)
            record["n_served"] = len(served_ids)
        if extra:
            record.update(extra)
    except Exception:  # noqa: BLE001
        return
    _append(record)


def record_episode(
    episode: Any,
    *,
    verdict: Optional[dict],
    window_ok: bool,
    outcome_result: Optional[dict],
    extra: Optional[dict] = None,
) -> None:
    """The episode's task-end summary, including the daemon's used-id accounting."""
    try:
        record = {
            "ts": time.time(),
            "kind": "episode",
            **get_context(),
            "session_uid": getattr(episode, "session_uid", None),
            "n_served": len(getattr(episode, "served_ids", []) or []),
            "first_search_ts": getattr(episode, "first_search_ts", None),
            "window_seconds": getattr(episode, "window_seconds", None),
            "window_ok": bool(window_ok),
            "verdict": verdict or None,
        }
        # Carry the daemon's used-id accounting verbatim: a signal whose ids all
        # land in used_ids_unmatched taught nothing, and only these keys say so.
        for key, value in (outcome_result or {}).items():
            if key.startswith("used_ids") or key in ("recorded", "signal_id"):
                record[key] = value
        if extra:
            record.update(extra)
    except Exception:  # noqa: BLE001
        return
    _append(record)
