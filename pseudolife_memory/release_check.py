"""The daemon's knowledge of the newest release.

Until 2026-09-29 a session learned that its plugin or shim was not the
daemon's release, but nothing told anyone that a newer release existed. The
daemon now asks PyPI for
the newest version on a background thread, once per
``updates.check_interval_seconds`` (``config.yaml``; off with
``updates.check_releases: false``), keeps the last good answer, and serves
it on ``/health`` (``updates.latest_release``) and in the session-start
briefing, where the offer names ``pseudolife-mcp update``. The check never
runs on a request path and never raises: a daemon without a route to PyPI
simply offers nothing.

Only a version-shaped value is ever kept: the answer reaches the model's
context through the briefing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("pseudolife-mcp")

PYPI_JSON = "https://pypi.org/pypi/pseudolife-mcp/json"
_VERSION_SHAPE = re.compile(r"[0-9A-Za-z.+-]{1,32}")
_lock = threading.Lock()
_state: dict = {"enabled": False, "latest_release": None, "checked_at": 0.0, "failed_at": 0.0}
_started = False
# Until the first answer, a failed check (the network not up yet at boot)
# is retried after this many seconds rather than the full interval.
RETRY_AFTER_FAILURE_SECONDS = 900.0
# PSEUDOLIFE_RELEASE_CHECK=0 in the daemon's environment makes no request
# whatever config.yaml says: the test daemons set it, and so can any host
# that must not reach PyPI.
OFF_SWITCH = "PSEUDOLIFE_RELEASE_CHECK"


def fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    """The JSON document at ``url``, or ``None`` when it cannot be read."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — a fixed https URL
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None


def version_key(value: str) -> tuple[int, ...] | None:
    """The leading dotted-integer part of a version as a sortable tuple
    (``0.15.0rc1`` -> ``(0, 15, 0)``); ``None`` when there is none."""
    match = re.match(r"^(\d+(?:\.\d+)*)", value or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _fetch_latest_release(timeout: float = 5.0) -> str | None:
    data = fetch_json(PYPI_JSON, timeout=timeout)
    version = ((data or {}).get("info") or {}).get("version")
    if not isinstance(version, str) or not _VERSION_SHAPE.fullmatch(version) or not version_key(version):
        return None
    return version


# The seam the tests replace; the loop and ``check_once`` call it by name.
fetch_latest_release = _fetch_latest_release


def check_once() -> None:
    """One check: record the newest release, or the failure time while
    keeping the last good answer. Never raises."""
    try:
        latest = fetch_latest_release()
    except Exception as exc:  # noqa: BLE001 — a check must never kill the daemon
        logger.debug("release check failed: %s", exc)
        latest = None
    now = time.time()
    with _lock:
        if latest:
            _state["latest_release"], _state["checked_at"] = latest, now
        else:
            _state["failed_at"] = now


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def latest_release() -> str | None:
    return snapshot()["latest_release"]


def reset() -> None:
    """Tests: forget the last answer and allow ``start`` again."""
    global _started
    with _lock:
        _state.update(enabled=False, latest_release=None, checked_at=0.0, failed_at=0.0)
    _started = False


def _loop(interval: float) -> None:
    while True:
        check_once()
        known = snapshot()
        time.sleep(min(interval, RETRY_AFTER_FAILURE_SECONDS) if known["latest_release"] is None else interval)


def start(config, environ=None) -> bool:
    """Idempotent: start the background check when ``config.check_releases``
    is on and the environment does not switch it off. Returns whether a
    thread was started by this call. Daemon-only."""
    global _started
    environ = os.environ if environ is None else environ
    if _started or not getattr(config, "check_releases", False) or environ.get(OFF_SWITCH, "").strip() == "0":
        return False
    _started = True
    with _lock:
        _state["enabled"] = True
    interval = float(getattr(config, "check_interval_seconds", 6 * 3600))
    threading.Thread(target=_loop, args=(interval,), daemon=True, name="pl-release-check").start()
    return True
