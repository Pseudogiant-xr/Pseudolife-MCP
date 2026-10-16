"""Reject every header difference outside the delegated episode disposition."""
import copy

import pytest

from .episode_wire import ACCEPT_CASE_IDS, associated_headers, wire_observations_match


def observation(headers):
    return {"response": {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""},
            "post_files_b64": {}, "wire": [{"method": "GET", "path": "/health",
                "headers": headers, "body_b64": "", "framing": "retained"}]}


@pytest.mark.parametrize("case_id", sorted(ACCEPT_CASE_IDS))
def test_exact_recorded_ids_admit_only_absent_oracle_accept_wildcard(case_id):
    oracle = observation({"Host": "fixture", "Authorization": "Bearer fixture"})
    candidate = observation({"host": "fixture", "authorization": "Bearer fixture", "accept": "*/*"})
    originals = copy.deepcopy((oracle, candidate))
    assert wire_observations_match(case_id, oracle, candidate)
    assert (oracle, candidate) == originals


@pytest.mark.parametrize("value", ["text/html", "application/json", "*/* ", " */*", "", "*/*;q=1", "*/*, text/html"])
def test_other_added_accept_values_are_rejected(value):
    assert not wire_observations_match("health-0", observation({}), observation({"Accept": value}))


@pytest.mark.parametrize("name,value", [("Referer", "http://fixture/health"), ("X-Extra", "fixture"),
    ("Authorization", "Bearer fixture"), ("Accept-Encoding", "identity"), ("Content-Length", "0")])
def test_any_other_added_header_is_rejected(name, value):
    assert not wire_observations_match("health-0", observation({}),
                                       observation({"accept": "*/*", name: value}))


def test_accept_disposition_is_directional_and_limited_to_recorded_ids():
    assert not wire_observations_match("not-an-approved-case", observation({}), observation({"accept": "*/*"}))
    assert not wire_observations_match("health-0", observation({"Accept": "*/*"}), observation({}))
    assert not wire_observations_match("health-0", observation({"Accept": "text/html"}), observation({"Accept": "*/*"}))
    assert wire_observations_match("not-an-approved-case", observation({"Accept": "text/html"}),
                                  observation({"accept": "text/html"}))


@pytest.mark.parametrize("headers", [
    {"Host": "fixture", "host": "fixture"},
    [["Host", "fixture"], ["Host", "fixture"]],
    [["Authorization", "Bearer fixture"], ["authorization", "Bearer other"]],
    [["Accept", "*/*"], ["accept", "*/*"]],
    {"bad name": "fixture"}, {"h\u00f6st": "fixture"}, {"Host": ["fixture"]}])
def test_duplicate_case_collision_and_invalid_name_or_value_fail_closed(headers):
    with pytest.raises(ValueError):
        associated_headers(headers)


def test_ordered_pairs_keep_order_and_multiplicity_cannot_disappear():
    oracle = observation([["Host", "fixture"], ["User-Agent", "fixture-agent"]])
    candidate = observation([["host", "fixture"], ["accept", "*/*"], ["user-agent", "fixture-agent"]])
    assert wire_observations_match("health-0", oracle, candidate)
    candidate["wire"][0]["headers"].reverse()
    assert not wire_observations_match("health-0", oracle, candidate)


@pytest.mark.parametrize("field", ["value", "auth", "path", "method", "body", "framing", "exit", "files", "requests"])
def test_every_other_wire_and_process_field_stays_exact(field):
    oracle = observation({"Host": "fixture", "Authorization": "Bearer fixture"})
    candidate = observation({"host": "fixture", "authorization": "Bearer fixture", "accept": "*/*"})
    if field == "value":
        candidate["wire"][0]["headers"]["host"] = "other"
    elif field == "auth":
        candidate["wire"][0]["headers"]["authorization"] = "Bearer other"
    elif field in ("path", "method", "framing"):
        candidate["wire"][0][field] = "other"
    elif field == "body":
        candidate["wire"][0]["body_b64"] = "eA=="
    elif field == "exit":
        candidate["response"]["exit_code"] = 1
    elif field == "files":
        candidate["post_files_b64"] = {"changed": "eA=="}
    else:
        candidate["wire"].append(copy.deepcopy(candidate["wire"][0]))
    assert not wire_observations_match("health-0", oracle, candidate)


def test_process_field_types_remain_exact_after_header_association():
    oracle, candidate = observation({}), observation({"accept": "*/*"})
    candidate["response"]["exit_code"] = False
    assert not wire_observations_match("health-0", oracle, candidate)
