"""Wire-size controls for the admission corpus."""
import asyncio
import copy
import json
from unittest.mock import Mock, patch

import pytest

import run
from body_cases import array_body, object_body


MEMORY_READINGS = [
    {"source": "process", "near_limit": None, "rss_bytes": 1024, "rss_peak_bytes": 2048},
    {"source": "cgroup", "current_bytes": 1024, "working_set_bytes": 512,
     "limit_bytes": 2048, "used_fraction": 0.25, "near_limit": False,
     "events": {"max": 2, "oom": 0, "oom_kill": 0}, "anon_bytes": 256,
     "file_bytes": 768, "rss_bytes": 1024, "rss_peak_bytes": 2048},
]


def health_diff(want, got):
    def response(memory):
        return {"status": 200, "headers": {}, "json": {"memory": memory}}
    return run.compare_case(run.case("health-memory-shape-mutants", "GET", "/health"),
                            response(want), response(got))["diffs"]


@pytest.mark.parametrize("reading", MEMORY_READINGS)
@pytest.mark.parametrize("mutation", ["missing-key", "bool-as-count", "string-as-flag"])
def test_health_memory_shape_mutants(reading, mutation):
    broken = copy.deepcopy(reading)
    if mutation == "missing-key":
        del broken["rss_peak_bytes"]
    elif mutation == "bool-as-count":
        broken["rss_bytes"] = True
    else:
        broken["near_limit"] = "broken"
    assert health_diff(reading, broken)


@pytest.mark.parametrize("reading", MEMORY_READINGS)
def test_health_memory_readings_are_free_after_shape_validation(reading):
    changed = copy.deepcopy(reading)
    for key, value in list(changed.items()):
        if type(value) is int:
            changed[key] = value + 1
        elif type(value) is float:
            changed[key] = value + 0.1
        elif type(value) is bool:
            changed[key] = not value
    if "events" in changed:
        changed["events"] = {k: v + 1 for k, v in changed["events"].items()}
    assert not health_diff(reading, changed)


@pytest.mark.parametrize("source", ["process", "cgroup", "unavailable"])
def test_health_memory_source_key_sets_are_not_erased(source):
    reading = next((r for r in MEMORY_READINGS if r["source"] == source), {"source": source})
    for key in reading:
        broken = copy.deepcopy(reading)
        del broken[key]
        assert health_diff(reading, broken), key
    assert health_diff(reading, dict(reading, unexpected=1))


def test_health_memory_partial_process_and_unlimited_cgroup_are_valid():
    process = {"source": "process", "near_limit": None, "rss_bytes": 1}
    assert not health_diff(process, dict(process, rss_bytes=2))
    for limit in (None, 0):
        cgroup = dict(MEMORY_READINGS[1], limit_bytes=limit, used_fraction=None, near_limit=None)
        assert not health_diff(cgroup, dict(cgroup, current_bytes=2048))
        assert health_diff(cgroup, dict(cgroup, near_limit=False))


def test_health_memory_event_keys_and_count_kinds_are_checked():
    reading = MEMORY_READINGS[1]
    for events in ({"max": True, "oom": 0, "oom_kill": 0}, {"oom": 0, "oom_kill": 0},
                   {"max": 2, "oom": 0, "oom_kill": 0, "unexpected": 1}):
        assert health_diff(reading, dict(reading, events=events))


def test_daemon_mutant_observer_failure(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run.py", "mutants", "--rust-bin", "not-executed",
                                    "--only", "body-limits", "--mutants", "text-limit-control",
                                    "--out", str(report)])
    clean = {"scenario": "body-limits", "cases": [{"case": "control", "diffs": []}], "db_diffs": []}
    with patch.object(run.daemons, "scratch_root", return_value=tmp_path), \
            patch.object(run.pg, "existing", return_value=[]), \
            patch.object(run, "run_scenario", side_effect=[clean, RuntimeError("broken observer")]):
        assert run.main() == 1
    result = json.loads(report.read_text())
    assert result["mutants"]["text-limit-control"]["scenario_errors"] == ["body-limits"]
    assert result["survivors"] == ["text-limit-control"]


@pytest.mark.parametrize("size", [32, 1024, 16384, 32768, 262144, 4194304])
@pytest.mark.parametrize("make_body", [array_body, object_body])
def test_boundary_fixture_has_the_named_wire_size(size, make_body):
    assert len(make_body(size)) == size


def test_contract_status_is_checked_even_when_both_arms_agree():
    c = run.case("next byte", "POST", "/api/nope")
    c["expected_status"] = 413
    response = {"status": 400, "headers": {}, "json": {"error": "invalid_json"}}
    assert run.compare_case(c, response, response)["diffs"]


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("send_error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_early_response_is_read_when_request_send_is_refused(chunked, send_error):
    connection = Mock()
    connection.endheaders.side_effect = send_error("server already refused body")
    response = connection.getresponse.return_value
    response.status = 413
    response.read.return_value = b'{"error":"request_too_large"}'
    response.getheaders.return_value = [("Content-Type", "application/json")]
    with patch("http.client.HTTPConnection", return_value=connection):
        actual = run.call(1, "POST", "/api/nope", body=b"large", chunked=chunked)
    assert actual["status"] == 413
    assert actual["json"] == {"error": "request_too_large"}
    connection.getresponse.assert_called_once()
    connection.close.assert_called_once()


def test_scenario_crash_cannot_count_as_a_caught_mutant(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["run.py", "mutants", "--rust-bin", "not-executed",
                                    "--only", "body-limits", "--mutants", "text-limit-control"])
    clean = {"scenario": "body-limits", "cases": [{"case": "control", "diffs": []}], "db_diffs": []}
    failed = {"scenario": "body-limits", "cases": [{"case": "scenario error", "diffs": ["transport error"]}],
              "db_diffs": []}
    with patch.object(run.daemons, "scratch_root", return_value=tmp_path), \
            patch.object(run, "run_scenario", side_effect=[clean, failed]):
        assert run.main() == 1


def test_oracle_stream_stops_after_the_first_over_limit_frame():
    from pseudolife_memory.web.api import _read_body
    chunks = iter([b" " * 200000, b" " * 70000, b" " * 1000])
    seen = []

    async def receive():
        seen.append(1)
        return {"type": "http.request", "body": next(chunks), "more_body": len(seen) < 3}

    with pytest.raises(ValueError, match="request_too_large"):
        asyncio.run(_read_body(receive, max_bytes=262144))
    assert len(seen) == 2


def test_header_only_refusal_sends_no_body_and_preserves_the_response():
    connection = Mock()
    connection.sock.recv.return_value = b"HTTP/1.1 503 Ser"
    response = connection.getresponse.return_value
    response.status = 503
    response.read.return_value = b'{"error":"principals_unavailable"}'
    response.getheaders.return_value = [("Content-Type", "application/json")]
    with patch("http.client.HTTPConnection", return_value=connection):
        result = run.call(1, "POST", "/api/nope", body=b"unsent", headers_only=True)
    assert result["status"] == 503
    connection.endheaders.assert_called_once_with()
    connection.putheader.assert_called_with("Content-Length", "6")
    connection.close.assert_called_once()


def test_expect_refusal_cannot_hide_an_interim_continue():
    connection = Mock()
    connection.sock.recv.return_value = b"HTTP/1.1 100 Con"
    with patch("http.client.HTTPConnection", return_value=connection), \
            pytest.raises(RuntimeError, match="interim 100 Continue"):
        run.call(1, "POST", "/api/nope", [("Expect", "100-continue")],
                 body=b"unsent", headers_only=True)
    connection.endheaders.assert_called_once_with()
    connection.getresponse.assert_not_called()
    connection.close.assert_called_once()
