"""Only trusted batch events and fresh spare capacity may use the Linux pool."""
import json

import pytest

import linux_pool as pool


def decision(**changes):
    values = dict(event="pull_request", ref="refs/pull/123/merge", head_ref="integrate/example",
                  head_repository="owner/repo", repository="owner/repo", pool="self-hosted",
                  health=json.dumps(dict(schema=1, observed_at=1000, online=3, idle=3)), now=1000)
    values.update(changes)
    return pool.select(**values)


@pytest.mark.parametrize("changes", [
    {}, {"event": "push", "ref": "refs/heads/master"},
])
def test_only_the_measured_linux_shard_is_offloaded(changes):
    assert decision(**changes)["selfhosted_suites"] == [pool.SUITE]


@pytest.mark.parametrize("changes", [
    {"head_repository": "fork/repo"}, {"head_repository": ""}, {"head_repository": None},
    {"head_ref": "feature/example"}, {"head_ref": "Integrate/example"},
    {"event": "pull_request_target"}, {"event": "merge_group"},
    {"event": "push", "ref": "refs/heads/feature"},
    {"event": "push", "ref": "refs/tags/master"},
    {"event": "workflow_dispatch"}, {"event": "schedule"},
])
def test_forks_and_plain_pull_requests_never_enter_the_batch_pool(changes):
    assert decision(**changes)["selfhosted_suites"] == []


@pytest.mark.parametrize("value", ["", "hosted", "false", "SELF-HOSTED", "anything"])
def test_one_kill_switch_returns_the_shard_to_hosted(value):
    assert decision(pool=value)["selfhosted_suites"] == []


@pytest.mark.parametrize("changes", [
    {"observed_at": 819}, {"observed_at": 1031}, {"observed_at": 0},
    {"observed_at": True}, {"observed_at": "1000"}, {"observed_at": float("nan")},
    {"observed_at": float("inf")}, {"online": 2, "idle": 2}, {"online": 3, "idle": 2},
    {"idle": 4}, {"idle": -1}, {"idle": True}, {"schema": 2}, {"schema": True},
])
def test_stale_unhealthy_or_ambiguous_availability_falls_back(changes):
    frame = dict(schema=1, observed_at=1000, online=3, idle=3)
    frame.update(changes)
    assert decision(health=json.dumps(frame))["selfhosted_suites"] == []


@pytest.mark.parametrize("health", ["", "null", "[]", "{}", "bad json"])
def test_missing_availability_is_a_hosted_fallback(health):
    assert decision(health=health)["selfhosted_suites"] == []


@pytest.mark.parametrize("stamp", [820, 1030])
def test_heartbeat_boundary_is_explicit(stamp):
    frame = dict(schema=1, observed_at=stamp, online=3, idle=3)
    assert decision(health=json.dumps(frame))["selfhosted_suites"] == [pool.SUITE]
