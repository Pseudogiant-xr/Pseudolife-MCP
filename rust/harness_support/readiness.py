"""The HTTP readiness client used by Rust harnesses.

Only /health response interpretation lives here; the harness owns process
lifecycle, poll intervals and deadlines. No product service is imported.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterable


def is_ready(status: int, body) -> bool:
    """Whether one ``/health`` answer says the daemon is ready."""
    return status == 200 and isinstance(body, dict) and body.get("ready") is True


def stages_done(body, stages: Iterable[str]) -> bool:
    """Whether one ``/health`` answer, of any HTTP status, shows ``stages``
    finished. No stages: any answer will do."""
    stages = tuple(stages)
    if not stages:
        return True
    listed = body.get("pending") if isinstance(body, dict) else None
    return isinstance(listed, list) and not set(stages) & set(listed)


def read_health(base_url: str, timeout: float) -> tuple[int, object]:
    """One ``GET /health``: ``(status, parsed JSON or None)``. Raises on no
    HTTP answer at all."""
    # No proxy: a caller's HTTP_PROXY must never carry a loopback probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url.rstrip("/") + "/health", timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return status, None


def describe(status: int | None, body) -> str:
    """One line on the last answer, for a timeout message."""
    if status is None:
        return "no HTTP answer"
    if not isinstance(body, dict):
        return f"HTTP {status} without a JSON object"
    if "ready" not in body:
        return (f"HTTP {status}, status {body.get('status')!r}, and no 'ready' key "
                "(a daemon older than the readiness contract)")
    reasons = {k: body[k] for k in ("init_refusal", "not_ready", "db") if k in body}
    return (f"HTTP {status}, status {body.get('status')!r}, ready {body.get('ready')!r}, "
            f"pending {body.get('pending')!r}" + (f", {reasons}" if reasons else ""))
