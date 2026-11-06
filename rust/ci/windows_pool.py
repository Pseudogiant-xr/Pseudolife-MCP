"""Select Windows parity shards only when a self-hosted worker can take them now."""
from __future__ import annotations

import json
import math
import time

import linux_pool

POOL_VARIABLE = "PARITY_BATCH_WINDOWS_POOL"
HEALTH_VARIABLE = "PARITY_WINDOWS_HEALTH"
# The controller's orchestrator writes the frame every 30-second demand poll
# (every 300 s while GitHub's rate limit is low, which this treats as stale).
# 120 s allows two missed polls and a slow API request.
MAX_AGE_SECONDS = 120
MAX_AHEAD_SECONDS = 30
MAX_WORKERS = 16
# The hypervisor's memory guard refuses a Windows worker's start while host
# MemAvailable is under 12288 MB (ci-worker-hook.sh, maintainer 2026-10-31).
# A booted Windows worker then holds about 10.2 GB of host RAM (idle Server
# Core worker, measured 2026-10-30), so each further start needs that much
# more. The margin covers memory that other guests take between the frame and
# the start.
GUARD_MB = 12288
MARGIN_MB = 2048
WORKER_MB = 10240
# w1c-connection ran at 0.67x hosted windows-latest's time on the workers over
# the five 2026-11-06 soak runs and touches none of the contention-sensitive
# limits (cli-live's security step, the judges' process tests). It moves
# alone until routed batches have been observed. eval (0.84x) is the planned
# second shard; it also needs rust/ci/ci_budget.py's cache check to accept a
# pool route that keeps the runner's OS, since its rust-cache step saves on
# hosted runners only.
SUITES = ("w1c-connection",)
MAX_SUITES = 1


def _count(value, upper=None) -> bool:
    return type(value) is int and value >= 0 and (upper is None or value <= upper)


def startable_now(stopped_clean: int, memory_mb) -> int:
    """Stopped, rolled-back workers the memory guard would let start one
    after another, or none while host memory is unknown."""
    if memory_mb is None:
        return 0
    started = 0
    while started < stopped_clean and memory_mb - started * WORKER_MB >= GUARD_MB + MARGIN_MB:
        started += 1
    return started


def select(*, event: str, ref: str, head_ref: str, head_repository: str,
           repository: str, pool: str, health: str, now: float) -> dict:
    reason = "pool disabled"
    if pool == "self-hosted":
        reason = "event is not a same-repository batch or master push"
        if linux_pool.eligible(event, ref, head_ref, head_repository, repository):
            reason = "availability is missing or invalid"
            try:
                frame = json.loads(health)
                observed = frame["observed_at"]
                workers, idle, stopped = frame["workers"], frame["idle_running"], frame["stopped_clean"]
                queued, memory = frame["queued"], frame["memory_available_mb"]
                valid = (type(frame.get("schema")) is int and frame["schema"] == 1
                         and type(observed) in (int, float) and math.isfinite(observed) and observed > 0
                         and _count(workers, MAX_WORKERS) and _count(idle) and _count(stopped)
                         and idle + stopped <= workers and _count(queued)
                         and (memory is None or _count(memory)))
                if valid:
                    age = now - observed
                    reason = "availability heartbeat is stale or ahead"
                    if -MAX_AHEAD_SECONDS <= age <= MAX_AGE_SECONDS:
                        # Any other queued Windows job could take the free
                        # worker first and leave a routed shard waiting.
                        reason = "other Windows jobs are queued"
                        if queued == 0:
                            reason = "no Windows worker is free and startable now"
                            free = idle + startable_now(stopped, memory)
                            routed = SUITES[:max(0, min(MAX_SUITES, free))]
                            if routed:
                                return {"windows_suites": list(routed),
                                        "reason": "a Windows worker is free and can start now"}
            except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
                pass
    return {"windows_suites": [], "reason": reason}


def live_choice(context: dict, token: str) -> dict:
    fallback = lambda reason: {"windows_suites": [], "reason": reason}
    if not linux_pool.eligible(**context):
        return fallback("event is not a same-repository batch or master push")
    if not token:
        return fallback("PARITY_VARS_READ_TOKEN is missing; using hosted")
    try:
        value = linux_pool.fetch_variable(context["repository"], POOL_VARIABLE, token)
        health = (linux_pool.fetch_variable(context["repository"], HEALTH_VARIABLE, token)
                  if value == "self-hosted" else "")
        return select(**context, pool=value or "hosted", health=health or "", now=time.time())
    except linux_pool.ReadError as error:
        return fallback(str(error) + "; using hosted")
