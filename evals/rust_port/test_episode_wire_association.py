"""Additive controls for the delegated distinct-field ordering disposition."""
import copy

import pytest

from .episode_wire import ACCEPT_CASE_IDS, associated_wire_observations_match


def observation(headers):
    return {"response": {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""},
            "wire": [{"method": "GET", "path": "/health", "headers": headers,
                      "request_line_b64": "R0VUIC9oZWFsdGggSFRUUC8xLjENCg==",
                      "header_terminator_b64": "DQo=", "body_read_b64": ""}]}


@pytest.mark.parametrize("case_id", sorted(ACCEPT_CASE_IDS) + ["outside-recorded-57"])
def test_distinct_name_order_is_free_with_exact_presence_and_values(case_id):
    left = observation([["Host", "fixture"], ["User-Agent", "fixture-agent"]])
    right = observation([["user-agent", "fixture-agent"], ["host", "fixture"]])
    originals = copy.deepcopy((left, right))
    assert associated_wire_observations_match(case_id, left, right)
    assert (left, right) == originals


@pytest.mark.parametrize("case_id", sorted(ACCEPT_CASE_IDS))
def test_order_association_preserves_exact_recorded_accept_admission(case_id):
    left = observation([["Host", "fixture"], ["User-Agent", "fixture-agent"]])
    right = observation([["user-agent", "fixture-agent"], ["Accept", "*/*"], ["host", "fixture"]])
    assert associated_wire_observations_match(case_id, left, right)


@pytest.mark.parametrize("headers", [
    [["Host", "fixture"], ["Host", "fixture"]],
    [["Host", "fixture"], ["host", "fixture"]],
    [["Authorization", "Bearer one"], ["authorization", "Bearer two"]],
    [["Accept", "*/*"], ["accept", "*/*"]]])
def test_same_name_multiplicity_and_case_collisions_fail_before_association(headers):
    with pytest.raises(ValueError):
        associated_wire_observations_match("health-0", observation([]), observation(headers))


@pytest.mark.parametrize("field", ["request_line_b64", "header_terminator_b64", "body_read_b64", "method", "path"])
def test_order_disposition_does_not_change_other_framing_or_routing(field):
    left = observation([["Host", "fixture"], ["User-Agent", "fixture-agent"]])
    right = observation([["user-agent", "fixture-agent"], ["host", "fixture"]])
    right["wire"][0][field] = "different"
    assert not associated_wire_observations_match("health-0", left, right)


@pytest.mark.parametrize("name,value", [("Accept", "*/*"), ("Accept", "text/html"), ("Referer", "fixture"), ("X-Extra", "fixture")])
def test_order_disposition_adds_no_header_presence_or_value_permission(name, value):
    assert not associated_wire_observations_match("outside-recorded-57", observation([]), observation([[name, value]]))


def test_order_disposition_preserves_auth_values_and_exact_process_types():
    left = observation([["Authorization", "Bearer one"]])
    right = observation([["authorization", "Bearer two"]])
    assert not associated_wire_observations_match("health-0", left, right)
    right = copy.deepcopy(left)
    right["response"]["exit_code"] = False
    assert not associated_wire_observations_match("health-0", left, right)
