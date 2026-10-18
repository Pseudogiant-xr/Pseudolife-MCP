"""Wire-size controls for the admission corpus."""
import asyncio
import copy
import http.client
import json
import socket
import threading
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


@pytest.mark.parametrize("reading", MEMORY_READINGS)
def test_health_memory_invalid_values_cannot_impersonate_normalized_tokens(reading):
    broken = run.normalize_health({"memory": reading}, [])["memory"]
    assert health_diff(reading, broken)


@pytest.mark.parametrize("reading", MEMORY_READINGS)
def test_health_memory_golden_replay_validates_only_the_raw_candidate(reading):
    c = run.case("health-memory-shape-mutants", "GET", "/health")
    raw = {"status": 200, "headers": {}, "json": {"memory": reading}}
    golden = run.normalize_response(raw, "/health", [])
    assert not run.compare_case(c, golden, raw, golden=True)["diffs"]
    assert run.compare_case(c, golden, golden, golden=True)["diffs"]


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


def early_refusal_server(respond: bool):
    """Answer (or not) as soon as the headers arrive, then close with the body unread."""
    listener = socket.create_server(("127.0.0.1", 0))
    seen = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                seen.append(data)
                if respond:
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\ncontent-type: application/json\r\n"
                                 b"content-length: 28\r\nconnection: close\r\n\r\n"
                                 b'{"error":"forbidden_origin"}')

    threading.Thread(target=serve, daemon=True).start()
    return listener, seen


def test_early_refusal_survives_the_host_discarding_it_on_reset():
    # Windows drops a received response when the peer's RST lands first; an
    # immediate refusal of a 256 KiB body lost 120 of 200 here (2026-10-18).
    listener, seen = early_refusal_server(respond=True)
    with listener:
        port = listener.getsockname()[1]
        for _ in range(40):
            actual = run.call(port, "POST", "/api/nope", [("Origin", "http://evil.example")],
                              b" " * 262145)
            assert (actual["status"], actual["json"]) == (403, {"error": "forbidden_origin"})
    # Every connection, replays included, announced the same request framing.
    assert all(b"Content-Length: 262145" in request for request in seen)


def test_daemon_that_closes_without_answering_still_fails():
    listener, seen = early_refusal_server(respond=False)
    with listener, pytest.raises((ConnectionError, RuntimeError)):
        run.call(listener.getsockname()[1], "POST", "/api/nope", body=b" " * 262145, timeout=10)


def lost_response_connection(error):
    connection = Mock()
    connection.getresponse.side_effect = error
    return connection


@pytest.mark.parametrize("error", [ConnectionAbortedError(10053, "aborted by host"),
                                   ConnectionResetError(10054, "reset by peer")])
def test_lost_early_response_is_replayed_headers_only(error, capsys):
    replay = Mock()
    replay.sock.recv.return_value = b"HTTP/1.1 403 For"
    response = replay.getresponse.return_value
    response.status = 403
    response.read.return_value = b'{"error":"forbidden_origin"}'
    response.getheaders.return_value = [("Content-Type", "application/json")]
    first = lost_response_connection(error)
    with patch("http.client.HTTPConnection", side_effect=[first, replay]):
        actual = run.call(1, "PUT", "/api/nope", [("Origin", "http://evil.example")], b"large")
    assert actual == {"status": 403, "headers": {"content-type": "application/json"},
                      "json": {"error": "forbidden_origin"},
                      "raw": '{"error":"forbidden_origin"}'}
    first.endheaders.assert_called_once_with(b"large")
    first.close.assert_called_once()
    replay.putrequest.assert_called_once_with("PUT", "/api/nope", skip_host=False,
                                              skip_accept_encoding=True)
    replay.putheader.assert_any_call("Origin", "http://evil.example")
    replay.putheader.assert_called_with("Content-Length", "5")
    replay.endheaders.assert_called_once_with()
    assert "headers-only replay answered 403" in capsys.readouterr().err


def test_replay_that_gets_no_final_status_reports_both_failures():
    replay = Mock()
    replay.sock.recv.return_value = b""
    with patch("http.client.HTTPConnection",
               side_effect=[lost_response_connection(ConnectionResetError(10054, "reset")), replay]), \
            pytest.raises(RuntimeError, match="ConnectionResetError.*did not send a final status"):
        run.call(1, "POST", "/api/nope", body=b"large")
    replay.getresponse.assert_not_called()


@pytest.mark.parametrize("error, body", [
    # A clean EOF before any status is the daemon's own close, not the host's.
    (http.client.RemoteDisconnected("closed without response"), b"large"),
    # Without a body in flight there is no unread data to provoke a reset.
    (ConnectionResetError(10054, "reset"), None),
    (ConnectionAbortedError(10053, "aborted"), None),
])
def test_closes_the_host_did_not_cause_are_not_replayed(error, body):
    first = lost_response_connection(error)
    with patch("http.client.HTTPConnection", side_effect=[first]) as connect, \
            pytest.raises(type(error)):
        run.call(1, "POST", "/api/nope", body=body)
    assert connect.call_count == 1


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


@pytest.mark.parametrize("golden", [False, True])
@pytest.mark.parametrize("side", ["python", "rust"])
@pytest.mark.parametrize("mutation", ["status", "json", "raw", "header"])
def test_invalid_session_end_comparator_rejects_changed_response(side, mutation, golden):
    c = next(c for c in run.BodyLimits().cases() if c.get("session_end_invalid_json"))
    headers = {"content-type": "application/json; charset=utf-8",
               "cache-control": "no-store", "x-content-type-options": "nosniff"}
    py = {"status": 200, "headers": headers, "json": {"ok": True}, "raw": '{"ok": true}'}
    rs = {"status": 400, "headers": headers, "json": {"error": "invalid_json"},
          "raw": '{"error": "invalid_json"}'}
    if golden:
        py, rs = run.golden_scrub(py), run.golden_scrub(rs)
    assert not run.compare_case(c, py, rs, golden=golden)["diffs"]
    changed = copy.deepcopy(py if side == "python" else rs)
    if mutation == "status":
        changed["status"] = 201
    elif mutation == "json":
        changed["json"] = {"unexpected": True}
    elif mutation == "raw":
        changed["raw"] = "{}"
    else:
        changed["headers"].pop("cache-control")
    assert run.compare_case(c, changed if side == "python" else py,
                            changed if side == "rust" else rs, golden=golden)["diffs"]
