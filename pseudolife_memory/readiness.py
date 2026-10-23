"""The daemon's readiness contract: the ``ready`` and ``pending`` keys of
``/health``.

A daemon answers ``/health`` from its first moment, but it becomes able to
serve in stages, which ``pending`` names while they are unfinished, in this
order:

- ``init``: the background warm-up has connected storage, loaded the
  embedder, hydrated the resident stores and reseeded the clock
  (``MemoryService.coordination_tier_ready``). Until then a request waits on
  the service lock or fails initialization.
- ``principals``: the stored-principal view (``daemon.start_stored_principals``,
  its own thread) has completed its first read. Until then a bearer the
  environment does not match is refused 503 ``principals_unavailable``. A
  daemon without Postgres has no view, so never lists it.

``ready`` is true only when ``status`` is ``"ok"`` and nothing is pending.
Neither key changes ``status`` or the HTTP code: the Docker healthcheck and
``pseudolife-mcp update`` read those, and a daemon still warming up is not a
failed one. The Rust daemon computes both keys the same way
(``rust/daemon/src/health.rs``).

A client that starts a daemon and must not race its startup (the test
helpers, the parity harnesses, the evals' daemon starters) waits with
:func:`wait_ready`. By default it treats every answer but ``ready: true`` as
"not started": a 503, ``ready: false``, or a payload without the key (a
daemon older than this contract). A harness whose daemon cannot finish a
stage by design (no embedding model, an unreachable or leased bank) waits
for the stages it can reach instead (``stages=``). Standard library only, so
any harness can import it.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable

#: Every startup stage, in the order ``pending`` lists them.
STAGES = ("init", "principals")

# Between polls. Startup takes seconds (the embedder load), so this only
# bounds how late after readiness the caller resumes.
POLL_S = 0.2


def service_initialized(svc) -> bool:
    """Whether ``svc`` has finished initializing. Lock-free; never raises."""
    ready = getattr(svc, "coordination_tier_ready", None)
    if not callable(ready):
        return False
    try:
        return bool(ready())
    except Exception:  # noqa: BLE001 - /health must never fail on this
        return False


def principals_loaded() -> bool:
    """Whether the installed stored-principal view has completed a read.
    No installed view (a daemon without Postgres, a test stand-in) has
    nothing to load. Loaded once stays loaded: a view that later goes stale
    is a runtime refusal (503), not an unfinished startup."""
    from pseudolife_memory.principals import installed_store

    store = installed_store()
    if store is None:
        return True
    try:
        return bool(store.loaded())
    except Exception:  # noqa: BLE001 - /health must never fail on this
        return False


def pending(svc) -> list[str]:
    """The unfinished startup stages, in :data:`STAGES` order."""
    done = {"init": service_initialized(svc), "principals": principals_loaded()}
    return [stage for stage in STAGES if not done[stage]]


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


class NotReady(RuntimeError):
    """The daemon did not become ready: it exited first (``exited``) or the
    deadline passed."""

    def __init__(self, message: str, *, exited: bool):
        super().__init__(message)
        self.exited = exited


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


def wait_ready(base_url: str, timeout: float, *, stages: Iterable[str] | None = None,
               alive: Callable[[], bool] | None = None,
               check: Callable[[int, object], None] | None = None,
               poll: float = POLL_S, request_timeout: float = 2.0) -> dict | None:
    """Poll ``base_url``'s ``/health`` until it reports ``ready: true``, or,
    with ``stages``, until none of those stages is pending; return that
    answer's payload.

    ``alive``, when given, is checked before every poll; once it returns
    false this raises :class:`NotReady` at once (the daemon exited).
    ``check``, when given, sees every HTTP answer first and may raise to
    abort the wait (an eval refusing a listener that is not its daemon).
    At the deadline it raises :class:`NotReady` describing the last answer."""
    wanted = None if stages is None else tuple(stages)
    unknown = set(wanted or ()) - set(STAGES)
    if unknown:
        raise ValueError(f"unknown readiness stages {sorted(unknown)}; known: {list(STAGES)}")
    goal = "ready" if wanted is None else f"past {list(wanted)}"
    deadline = time.monotonic() + timeout
    last: tuple[int | None, object] = (None, None)
    while True:
        if alive is not None and not alive():
            raise NotReady(f"daemon at {base_url} exited before it was {goal}; "
                           f"last answer: {describe(*last)}", exited=True)
        remaining = deadline - time.monotonic()
        try:
            last = read_health(base_url, max(0.05, min(request_timeout, remaining)))
        except (OSError, ValueError, http.client.HTTPException):
            pass
        else:
            if check is not None:
                check(*last)
            if is_ready(*last) if wanted is None else stages_done(last[1], wanted):
                return last[1]
        if time.monotonic() >= deadline:
            raise NotReady(f"daemon at {base_url} was not {goal} within {timeout:g}s; "
                           f"last answer: {describe(*last)}", exited=False)
        time.sleep(poll)
