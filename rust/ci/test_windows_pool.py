"""Windows shards move only to a free, startable self-hosted worker; any doubt stays hosted."""
import json

import pytest

import linux_pool
import windows_pool as pool

NOW = 2_000_000_000
REPO = "Pseudogiant-xr/Pseudolife-MCP"
MASTER = dict(event="push", ref="refs/heads/master", head_ref="", head_repository="", repository=REPO)
BATCH = dict(event="pull_request", ref="refs/pull/1/merge", head_ref="integrate/2026-11-07-batch-1",
             head_repository=REPO, repository=REPO)
# Enough host memory for one start: guard 12288 + margin 2048.
ONE_START = pool.GUARD_MB + pool.MARGIN_MB


def frame(**changes):
    value = {"schema": 1, "observed_at": NOW - 30, "workers": 2, "idle_running": 0,
             "stopped_clean": 2, "queued": 0, "memory_available_mb": ONE_START + pool.WORKER_MB}
    value.update(changes)
    return json.dumps(value)


def choose(health=None, pool_value="self-hosted", context=MASTER, now=NOW):
    return pool.select(**context, pool=pool_value, health=frame() if health is None else health, now=now)


@pytest.mark.parametrize("context", [MASTER, BATCH])
def test_a_startable_stopped_worker_moves_only_the_first_listed_shard(context):
    result = choose(context=context)
    assert result["windows_suites"] == ["w1c-connection"]
    assert pool.SUITES[0] == "w1c-connection" and pool.MAX_SUITES == 1


def test_an_idle_running_worker_counts_even_while_memory_is_unknown():
    assert choose(frame(idle_running=1, stopped_clean=0, memory_available_mb=None))["windows_suites"] == [
        "w1c-connection"]


@pytest.mark.parametrize("health", [
    frame(memory_available_mb=None),                              # no hypervisor memory note yet
    frame(memory_available_mb=ONE_START - 1),                     # one MB under guard + margin
    frame(memory_available_mb=0),
    frame(stopped_clean=0),                                       # both workers busy or rolling back
    frame(stopped_clean=0, idle_running=0, memory_available_mb=10 ** 6),
])
def test_no_free_and_startable_worker_keeps_hosted(health):
    result = choose(health)
    assert result["windows_suites"] == []
    assert result["reason"] == "no Windows worker is free and startable now"


@pytest.mark.parametrize("queued", [1, 5])
def test_any_other_queued_windows_job_keeps_hosted_even_with_free_workers(queued):
    result = choose(frame(queued=queued, idle_running=2, stopped_clean=0))
    assert result == {"windows_suites": [], "reason": "other Windows jobs are queued"}


def test_memory_math_is_sequential():
    assert pool.startable_now(2, None) == 0
    assert pool.startable_now(2, ONE_START - 1) == 0
    assert pool.startable_now(2, ONE_START) == 1
    assert pool.startable_now(2, ONE_START + pool.WORKER_MB - 1) == 1
    assert pool.startable_now(2, ONE_START + pool.WORKER_MB) == 2
    assert pool.startable_now(1, 10 ** 6) == 1
    assert pool.startable_now(0, 10 ** 6) == 0


@pytest.mark.parametrize("stamp,routed", [
    (NOW - pool.MAX_AGE_SECONDS, True), (NOW - pool.MAX_AGE_SECONDS - 1, False),
    (NOW + pool.MAX_AHEAD_SECONDS, True), (NOW + pool.MAX_AHEAD_SECONDS + 1, False),
])
def test_heartbeat_age_boundary_is_explicit(stamp, routed):
    result = choose(frame(observed_at=stamp))
    assert bool(result["windows_suites"]) is routed
    if not routed:
        assert result["reason"] == "availability heartbeat is stale or ahead"


@pytest.mark.parametrize("value", ["hosted", "", "Self-Hosted", "self-hosted ", "linux"])
def test_only_the_exact_kill_switch_value_enables_routing(value):
    assert choose(pool_value=value) == {"windows_suites": [], "reason": "pool disabled"}


@pytest.mark.parametrize("context", [
    {**BATCH, "head_repository": "fork/Pseudolife-MCP"},          # fork-origin batch name
    {**BATCH, "head_ref": "feature/x"},                            # ordinary same-repo PR
    {**MASTER, "ref": "refs/heads/integrate/x"},                   # push to another branch
    {**MASTER, "repository": "fork/Pseudolife-MCP"},
    {**MASTER, "event": "schedule"},
    {**MASTER, "event": "workflow_dispatch"},
    {**BATCH, "event": "pull_request_target"},
])
def test_forks_and_other_events_never_enter_the_windows_pool(context):
    assert choose(context=context)["windows_suites"] == []


@pytest.mark.parametrize("health", [
    "", "null", "[]", "{}", "not json", '[' * 5000 + ']' * 5000,
    frame(schema=2), frame(schema=True), frame(schema="1"),
    frame(observed_at=None), frame(observed_at=0), frame(observed_at="2000000000"),
    '{"schema":1,"observed_at":NaN,"workers":2,"idle_running":1,"stopped_clean":0,"queued":0,"memory_available_mb":null}',
    frame(workers=-1), frame(workers=17), frame(workers=True),
    frame(idle_running=3, stopped_clean=0),                        # more than the workers
    frame(idle_running=1, stopped_clean=2),
    frame(stopped_clean=-1), frame(queued=-1), frame(queued=None), frame(queued=0.0),
    frame(memory_available_mb=-1), frame(memory_available_mb=1.5), frame(memory_available_mb="20000"),
    json.dumps({k: v for k, v in json.loads(frame()).items() if k != "queued"}),
    json.dumps({k: v for k, v in json.loads(frame()).items() if k != "memory_available_mb"}),
    '{"schema":1,"observed_at":' + '9' * 400 + ',"workers":2}',
])
def test_invalid_or_ambiguous_frames_keep_hosted(health):
    assert choose(health)["windows_suites"] == []


def test_live_choice_reads_only_the_windows_variables_and_skips_health_when_disabled(monkeypatch):
    calls = []

    def read(repository, name, token):
        calls.append(name)
        return {"PARITY_BATCH_WINDOWS_POOL": "hosted"}.get(name)

    monkeypatch.setattr(linux_pool, "fetch_variable", read)
    assert pool.live_choice(MASTER, "fixture-token")["windows_suites"] == []
    assert calls == ["PARITY_BATCH_WINDOWS_POOL"]


def test_live_choice_without_a_token_or_on_a_fork_never_reads(monkeypatch):
    monkeypatch.setattr(linux_pool, "fetch_variable", lambda *a: pytest.fail("must not read"))
    assert pool.live_choice(MASTER, "")["reason"] == "PARITY_VARS_READ_TOKEN is missing; using hosted"
    assert pool.live_choice({**BATCH, "head_repository": "fork/x"}, "fixture-token")["windows_suites"] == []


def test_read_errors_fall_back_without_echoing_the_token(monkeypatch):
    def read(repository, name, token):
        raise linux_pool.ReadError("availability read failed (HTTP 403)")

    monkeypatch.setattr(linux_pool, "fetch_variable", read)
    result = pool.live_choice(MASTER, "fixture-secret-token")
    assert result["windows_suites"] == [] and "fixture-secret-token" not in json.dumps(result)


def test_reader_may_fetch_the_windows_variables_and_nothing_else():
    assert {"PARITY_BATCH_WINDOWS_POOL", "PARITY_WINDOWS_HEALTH"} <= linux_pool.READABLE
    with pytest.raises(linux_pool.ReadError):
        linux_pool.fetch_variable(REPO, "PARITY_HEARTBEAT_WINDOWS", "fixture-token")
