"""Timing medians exclude failed and pending work without hiding its inventory."""

import copy
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("ci_timings", Path(__file__).with_name("timings.py"))
timings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timings)


def test_failed_runs_and_skipped_steps_do_not_reduce_the_median():
    run = {"conclusion": "success", "attempt": 1,
           "createdAt": "2026-10-10T00:00:00Z", "startedAt": "2026-10-10T00:00:00Z", "jobs": [
        {"name": "Rust / ubuntu-latest", "conclusion": "success",
         "startedAt": "2026-10-10T00:00:10Z", "completedAt": "2026-10-10T00:01:10Z",
         "steps": [{"name": "Nextest", "conclusion": "success",
                    "startedAt": "2026-10-10T00:00:20Z", "completedAt": "2026-10-10T00:00:50Z"},
                   {"name": "Dispatch capture", "conclusion": "skipped", "startedAt": "", "completedAt": ""}]},
        {"name": "Capture", "conclusion": "skipped", "startedAt": "", "completedAt": "", "steps": []}]}
    second = copy.deepcopy(run)
    second["jobs"][0]["completedAt"] = "2026-10-10T00:03:10Z"
    failed = {"conclusion": "failure", "jobs": []}
    result = timings.summarize({"runs": [run, second, failed]})
    assert result["completed_runs"] == 3
    assert result["successful_runs"] == 2
    assert result["wall_median_seconds"] == 130
    assert result["jobs"][0]["median_seconds"] == 120
    assert result["steps"] == [{"name": "Rust / ubuntu-latest / Nextest", "samples": 2, "median_seconds": 30}]


def test_no_success_is_not_a_zero_time_speedup():
    with pytest.raises(ValueError, match="no successful runs"):
        timings.summarize({"runs": [{"conclusion": "failure"}]})


def test_retry_wall_does_not_include_the_previous_day():
    artifact = {"runs": [{"conclusion": "success", "attempt": 2,
                         "createdAt": "2026-10-09T00:00:00Z",
                         "startedAt": "2026-10-10T00:00:00Z", "jobs": [
        {"name": "Rust / ubuntu-latest", "conclusion": "success",
         "startedAt": "2026-10-10T00:00:10Z", "completedAt": "2026-10-10T00:05:00Z",
         "steps": []}]}]}
    assert timings.summarize(artifact)["wall_median_seconds"] == 300


def test_retry_without_an_attempt_start_is_refused():
    artifact = {"runs": [{"conclusion": "success", "attempt": 2,
                         "createdAt": "2026-10-09T00:00:00Z",
                         "startedAt": "2026-10-09T00:00:00Z", "jobs": [
        {"name": "Rust", "conclusion": "success", "startedAt": "2026-10-10T00:00:10Z",
         "completedAt": "2026-10-10T00:05:00Z", "steps": []}]}]}
    with pytest.raises(ValueError, match="distinct attempt start"):
        timings.summarize(artifact)
