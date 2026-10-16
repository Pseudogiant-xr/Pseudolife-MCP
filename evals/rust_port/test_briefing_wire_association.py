"""Narrow controls for the recorded hook HTTP dispositions."""
import copy
from pathlib import Path

import pytest

from evals.rust_port.briefing_wire import ACCEPT_CASE_IDS, associated_headers, associated_wire_observations_match
from evals.rust_port.cli_briefing_hook import cases


def observation(headers):
    return {"exit_code": 0, "stdout_b64": "", "stderr_b64": "", "wire": [{
        "method": "GET", "target": "/health", "body_b64": "", "headers": headers,
        "framing": "fixed", "content_length": 0,
    }]}


def test_accept_ids_are_exactly_the_recorded_hook_ledger():
    assert len(ACCEPT_CASE_IDS) == 53
    assert ACCEPT_CASE_IDS == {case["id"] for case in cases()}
    register = (Path(__file__).resolve().parents[2] / "rust/PARITY.md").read_text(encoding="utf-8")
    for name in ACCEPT_CASE_IDS:
        assert f"`{name}`" in register


@pytest.mark.parametrize("case_id", sorted(ACCEPT_CASE_IDS))
def test_accept_allowance_is_only_absent_oracle_and_single_native_wildcard(case_id):
    left = observation([["Host", "fixture"], ["User-Agent", "Python-urllib/3.11"]])
    right = observation([["user-agent", "Python-urllib/3.11"], ["Accept", "*/*"], ["host", "fixture"]])
    before = copy.deepcopy((left, right))
    assert associated_wire_observations_match(case_id, left, right)
    assert (left, right) == before
    for value in ["", " */*", "application/json", "*/* "]:
        changed = copy.deepcopy(right)
        changed["wire"][0]["headers"][1][1] = value
        assert not associated_wire_observations_match(case_id, left, changed)
    duplicate = copy.deepcopy(right)
    duplicate["wire"][0]["headers"].append(["accept", "*/*"])
    assert not associated_wire_observations_match(case_id, left, duplicate)
    assert not associated_wire_observations_match("unrecorded-hook-case", left, right)
    assert not associated_wire_observations_match(case_id, right, left)


def test_distinct_names_are_associated_but_repeated_values_and_presence_are_exact():
    left = observation([["Host", "fixture"], ["X-Tag", "first"], ["x-tag", "second"]])
    right = observation([["X-Tag", "first"], ["host", "fixture"], ["X-TAG", "second"]])
    assert associated_wire_observations_match("unrecorded-hook-case", left, right)
    for headers in [
        [["Host", "fixture"], ["x-tag", "second"], ["x-tag", "first"]],
        [["Host", "fixture"], ["x-tag", "first"]],
        [["Host", "fixture"], ["x-tag", "first, second"]],
        [["Host", "fixture"], ["x-tag", "first"], ["x-tag", "second"], ["Referer", "fixture"]],
    ]:
        assert not associated_wire_observations_match("briefing-plain", left, observation(headers))


@pytest.mark.parametrize("field,value", [
    ("method", "POST"), ("target", "/ready"), ("body_b64", "eA=="),
    ("framing", "chunked"), ("content_length", 1),
])
def test_route_body_and_framing_are_not_normalized(field, value):
    left = observation([["authorization", "Bearer synthetic"]])
    right = copy.deepcopy(left)
    right["wire"][0][field] = value
    assert not associated_wire_observations_match("briefing-plain", left, right)


@pytest.mark.parametrize("name", ["authorization", "referer", "accept-encoding", "connection", "user-agent"])
def test_other_header_values_are_exact(name):
    left = observation([[name, "exact"]])
    assert not associated_wire_observations_match("briefing-plain", left, observation([[name, "changed"]]))
    assert not associated_wire_observations_match("briefing-plain", left, observation([]))


@pytest.mark.parametrize("headers", [[["bad name", "x"]], [["Ä", "x"]], [["host", 1]], [["host"]]])
def test_header_association_rejects_lossy_or_invalid_inputs(headers):
    with pytest.raises(ValueError):
        associated_headers(headers)
