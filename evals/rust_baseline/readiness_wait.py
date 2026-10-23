"""Startup wait for the evals' launched daemons, on the daemons' readiness
contract (pseudolife_memory/readiness.py, /health ``ready`` and ``pending``).

These instruments also run against an immutable oracle checkout:
``phase1_oracle.prepare`` overlays only ``evals/`` onto the selected commit,
which may predate the contract. So this module cannot import the package's
helper, and a daemon whose /health has no ``ready`` key falls back to its own
startup protocol: ``status`` ok, then a bearer no token matches is no longer
refused 503 ``principals_unavailable`` (the stored-principal view's first
read, which such a daemon's /health does not report). Remove the fallback
once every selectable oracle carries the contract.
"""
from __future__ import annotations

import time

# A bearer no daemon token matches: refused before any handler on a
# token-mode daemon, so the probe writes nothing.
PRINCIPAL_PROBE = "Bearer pl-harness-principal-probe"


class StartupFailed(RuntimeError):
    def __init__(self, message: str, *, exited: bool):
        super().__init__(message)
        self.exited = exited


def _get(url, path, deadline, headers=None):
    import httpx
    return httpx.get(url + path, headers=headers or {}, trust_env=False, follow_redirects=False,
                     timeout=max(0.001, min(2, deadline - time.monotonic())))


def _json(response):
    try:
        return response.json()
    except ValueError:
        return None


def wait_started(url, deadline, *, alive, check, poll=0.1):
    """Wait until the daemon at ``url`` can serve; return ``(payload,
    evidence)``. ``check(status, payload)`` sees every /health answer first
    and may raise (an identity refusal). Raises :class:`StartupFailed` when
    the daemon exits or ``deadline`` (a ``time.monotonic`` value) passes."""
    import httpx
    answers, legacy = 0, None
    last = "no HTTP answer"
    while True:
        if not alive():
            raise StartupFailed(f"daemon exited during startup; last answer: {last}", exited=True)
        try:
            response = _get(url, "/health", deadline)
        except httpx.HTTPError:
            response = None
        if response is not None:
            payload = _json(response)
            check(response.status_code, payload)
            last = f"HTTP {response.status_code}: " + (
                f"status {payload.get('status')!r}, ready {payload.get('ready')!r}, "
                f"pending {payload.get('pending')!r}" if isinstance(payload, dict) else "not a JSON object")
            if response.status_code == 200 and isinstance(payload, dict):
                if "ready" not in payload and payload.get("status") == "ok":
                    legacy = payload
                    break
                if payload.get("ready") is True:
                    return payload, {"health_answers_before_ready": answers}
            answers += 1
        if time.monotonic() >= deadline:
            raise StartupFailed(f"startup deadline exceeded; last answer: {last}", exited=False)
        time.sleep(poll)
    # An oracle older than the contract: wait out its stored-principal load.
    unavailable = 0
    while True:
        if not alive():
            raise StartupFailed("daemon exited while its stored principals loaded", exited=True)
        try:
            response = _get(url, "/api/search?q=", deadline, {"Authorization": PRINCIPAL_PROBE})
            body = _json(response) if response.status_code == 503 else None
            error = body.get("error") if isinstance(body, dict) else None
            if not (response.status_code == 503 and error == "principals_unavailable"):
                return legacy, {"health_answers_before_ready": answers, "legacy_startup": True,
                                "principals_unavailable_before_ready": unavailable}
            unavailable += 1
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise StartupFailed("startup deadline exceeded: the stored-principal view did not load "
                                f"({unavailable} principals_unavailable answers)", exited=False)
        time.sleep(poll)
