"""Wire-size controls for the admission corpus."""
import asyncio
import copy
import http.client
import json
import socket
import struct
import threading
import time
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


def early_refusal_server(respond: bool, reset_first: bool = False):
    """Answer (or not) as soon as the headers arrive, then close with the body unread.

    ``reset_first`` makes the first connection a daemon fault: an abortive
    close that never writes its response, while later ones answer."""
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
                if reset_first and len(seen) == 1:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    continue
                if respond:
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\ncontent-type: application/json\r\n"
                                 b"content-length: 28\r\nconnection: close\r\n\r\n"
                                 b'{"error":"forbidden_origin"}')

    threading.Thread(target=serve, daemon=True).start()
    return listener, seen


TOO_LARGE = (b"HTTP/1.1 413 Payload Too Large\r\ncontent-type: application/json\r\n"
             b"content-length: 29\r\nconnection: close\r\n\r\n" b'{"error":"request_too_large"}')


def budget_server(budget, respond=True):
    """Read the headers and ``budget`` body bytes, then refuse (or not) and close with the rest unread.

    The daemon refuses a chunked body over its limit only after reading the
    chunks up to it, so a headers-only replay of that request waits for a
    body and times out (#830). Wire bytes, chunk framing included, count
    toward ``budget``."""
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
                head, _, rest = data.partition(b"\r\n\r\n")
                got = len(rest)
                while got < budget:
                    chunk = conn.recv(min(65536, budget - got))
                    if not chunk:
                        break
                    got += len(chunk)
                seen.append((head, got))
                if respond:
                    conn.sendall(TOO_LARGE)

    threading.Thread(target=serve, daemon=True).start()
    return listener, seen


class UnbufferedSend(http.client.HTTPConnection):
    """A zero send buffer: the case that makes the reset race deterministic on Windows (#828)."""

    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 0)
        except OSError:  # macOS and the BSDs refuse 0; Linux clamps it up
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1)


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("budget, size, unbuffered, calls", [
    # The body-limits shapes: a 4 MiB limit and one byte over it.
    (4 * 1024 * 1024, 4 * 1024 * 1024 + 1, False, 20),
    # Most of the body unread: the send fails or returns at once.
    (256 * 1024, 32 * 1024 * 1024, True, 5),
])
def test_refusal_after_reading_the_body_is_read(chunked, budget, size, unbuffered, calls, monkeypatch):
    # Hosted Windows run 39155013424 lost a chunked next-byte 413 to
    # WinError 10053, and the headers-only replay then timed out (#830): it
    # cannot recover a refusal decided after reading the body, so none may be
    # lost. Read after the send, 142 of 1000 were lost on a Windows 11 host
    # (2026-10-19); with the receive waiting, 0 of 1000.
    if unbuffered:
        monkeypatch.setattr(http.client, "HTTPConnection", UnbufferedSend)
    listener, seen = budget_server(budget)
    with listener:
        for _ in range(calls):
            actual = run.call(listener.getsockname()[1], "POST", "/api/facts/set",
                              [("Content-Type", "application/json")], b"a" * size,
                              timeout=30, chunked=chunked)
            assert (actual["status"], actual["json"]) == (413, {"error": "request_too_large"})
            assert set(actual) == {"status", "headers", "json", "raw"}
    # One connection per call, each reading the budget: no request was replayed.
    framing = b"Transfer-Encoding: chunked" if chunked else b"Content-Length: %d" % size
    assert [(framing in head, got) for head, got in seen] == [(True, budget)] * calls


@pytest.mark.parametrize("chunked", [False, True])
def test_refusal_decided_on_headers_is_read(chunked):
    # The near miss: the daemon answers before reading any body (an origin
    # refusal). Read after the send, 882 of 1000 were lost to the reset on a
    # Windows 11 host and needed the replay (2026-10-19); with the receive
    # waiting, 0 of 1000 there and 68 of 3000 under full CPU load on the
    # Windows dev VM. The replay still recovers those, so it may appear, but
    # rarely: a return to read-after-send replays nearly every call.
    listener, seen = early_refusal_server(respond=True)
    replays = 0
    with listener:
        port = listener.getsockname()[1]
        for _ in range(40):
            actual = run.call(port, "POST", "/api/nope", [("Origin", "http://evil.example")],
                              b" " * 262145, chunked=chunked)
            assert (actual["status"], actual["json"]) == (403, {"error": "forbidden_origin"})
            assert set(actual) - {"status", "headers", "json", "raw"} <= {"_transport_replay"}
            replays += "_transport_replay" in actual
    assert replays <= 10
    # Every connection, replays included, announced the same request framing.
    framing = b"Transfer-Encoding: chunked" if chunked else b"Content-Length: 262145"
    assert len(seen) == 40 + replays and all(framing in request for request in seen)


def wire_size(size, chunked):
    """Bytes ``call`` puts on the wire for a ``size`` body, chunk framing included."""
    if not chunked:
        return size
    width = max(1, (size + 3) // 4)
    pieces = [min(width, size - i) for i in range(0, size, width)]
    return sum(len(b"%X\r\n" % n) + n + 2 for n in pieces) + len(b"0\r\n\r\n")


@pytest.mark.parametrize("chunked", [False, True])
def test_clean_close_before_any_status_is_the_daemons_own(chunked):
    # The daemon read the whole body and closed without a word: no reset, so
    # nothing was lost to the host, and the case fails as the daemon's close.
    size = 262145
    listener, seen = budget_server(wire_size(size, chunked), respond=False)
    with listener, pytest.raises(http.client.RemoteDisconnected):
        run.call(listener.getsockname()[1], "POST", "/api/nope", body=b" " * size,
                 timeout=10, chunked=chunked)
    assert [got for _, got in seen] == [wire_size(size, chunked)]


@pytest.mark.parametrize("host_discards, budget, chunked", [
    (False, 0, False), (False, 0, True), (False, 4 * 1024 * 1024, False), (False, 4 * 1024 * 1024, True),
    (True, 0, False), (True, 0, True), (True, 4 * 1024 * 1024, True),
])
def test_daemon_that_closes_without_answering_still_fails(monkeypatch, host_discards, budget, chunked):
    # The near miss of the refusal tests: the same reset with nothing sent,
    # after the headers or after reading a 4 MiB budget. Where the host may
    # have discarded a response, the replay gets no answer either.
    monkeypatch.setattr(run, "HOST_DISCARDS_ON_RESET", host_discards)
    listener, seen = budget_server(budget, respond=False)
    with listener, pytest.raises((ConnectionError, RuntimeError)):
        run.call(listener.getsockname()[1], "POST", "/api/nope", body=b" " * (4 * 1024 * 1024 + 1),
                 timeout=10, chunked=chunked)
    if not host_discards:
        assert len(seen) == 1


def test_reset_without_a_response_is_not_replayed_where_the_host_keeps_data(monkeypatch):
    # The near miss: a daemon that drops its response with an abortive close
    # would answer the headers-only replay. Off Windows that reset is the
    # daemon's own, so the case must fail rather than compare the replay.
    monkeypatch.setattr(run, "HOST_DISCARDS_ON_RESET", False)
    listener, seen = early_refusal_server(respond=True, reset_first=True)
    with listener, pytest.raises(ConnectionError):
        run.call(listener.getsockname()[1], "POST", "/api/nope", body=b" " * 262145, timeout=10)
    assert len(seen) == 1


@pytest.mark.parametrize("chunked", [False, True])
def test_daemon_that_never_answers_still_times_out(chunked):
    # On Windows the exchange runs blocking with kernel timeouts; they keep
    # the caller's bound, for the read and for a send the daemon never reads.
    listener = socket.create_server(("127.0.0.1", 0))
    held = []

    def hold():
        while True:
            try:
                held.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=hold, daemon=True).start()
    started = time.monotonic()
    with listener, pytest.raises(TimeoutError):
        run.call(listener.getsockname()[1], "POST", "/api/nope", body=b" " * (8 * 1024 * 1024),
                 timeout=1, chunked=chunked)
    assert time.monotonic() - started < 10
    for conn in held:
        conn.close()


def exchange_that_loses(first, replay=None):
    """An _exchange whose first answer is lost to ``first``; the replay answers ``replay``."""
    calls = []

    def exchange(port, method, path, headers, body, *, headers_only, **options):
        calls.append(dict(method=method, path=path, headers=headers, body=body,
                          headers_only=headers_only, **options))
        if len(calls) == 1:
            raise first
        if isinstance(replay, BaseException):
            raise replay
        return dict(replay)
    return exchange, calls


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("error", [ConnectionAbortedError(10053, "aborted by host"),
                                   ConnectionResetError(10054, "reset by peer")])
def test_lost_early_response_is_replayed_headers_only(error, chunked, capsys, monkeypatch):
    monkeypatch.setattr(run, "HOST_DISCARDS_ON_RESET", True)
    forbidden = {"status": 403, "headers": {"content-type": "application/json"},
                 "json": {"error": "forbidden_origin"}, "raw": '{"error":"forbidden_origin"}'}
    exchange, calls = exchange_that_loses(error, forbidden)
    monkeypatch.setattr(run, "_exchange", exchange)
    actual = run.call(1, "PUT", "/api/nope", [("Origin", "http://evil.example")], b"large", chunked=chunked)
    assert actual == dict(forbidden, _transport_replay=f"{type(error).__name__}: {error}")
    # The same request line, headers and framing, without the body.
    assert calls[1] == dict(calls[0], headers_only=True)
    assert calls[0]["headers_only"] is False and calls[0]["chunked"] is chunked
    assert "headers-only replay answered 403" in capsys.readouterr().err


def test_replay_that_gets_no_final_status_reports_both_failures(monkeypatch):
    monkeypatch.setattr(run, "HOST_DISCARDS_ON_RESET", True)
    exchange, calls = exchange_that_loses(ConnectionResetError(10054, "reset"),
                                          RuntimeError("early refusal did not send a final status"))
    monkeypatch.setattr(run, "_exchange", exchange)
    with pytest.raises(RuntimeError, match="ConnectionResetError.*did not send a final status"):
        run.call(1, "POST", "/api/nope", body=b"large", chunked=True)
    assert len(calls) == 2


@pytest.mark.parametrize("error, body, host_discards", [
    # A clean EOF before any status is the daemon's own close, not the host's.
    (http.client.RemoteDisconnected("closed without response"), b"large", True),
    # Without a body in flight there is no unread data to provoke a reset.
    (ConnectionResetError(10054, "reset"), None, True),
    (ConnectionAbortedError(10053, "aborted"), None, True),
    (ConnectionResetError(10054, "reset"), b"", True),
    # A host that keeps received data never lost a response to the reset.
    (ConnectionResetError(104, "reset"), b"large", False),
    (ConnectionAbortedError(103, "aborted"), b"large", False),
])
def test_closes_the_host_did_not_cause_are_not_replayed(error, body, host_discards, monkeypatch):
    monkeypatch.setattr(run, "HOST_DISCARDS_ON_RESET", host_discards)
    exchange, calls = exchange_that_loses(error)
    monkeypatch.setattr(run, "_exchange", exchange)
    with pytest.raises(type(error)):
        run.call(1, "POST", "/api/nope", body=body)
    assert len(calls) == 1


@pytest.mark.parametrize("side", ["python", "rust"])
def test_replayed_response_is_recorded_in_the_row_not_diffed(side):
    c = run.case("foreign origin", "GET", "/api/nope")
    response = {"status": 403, "headers": {}, "json": {"error": "forbidden_origin"}}
    replayed = dict(response, _transport_replay="ConnectionAbortedError: [WinError 10053]")
    py, rs = (replayed, response) if side == "python" else (response, replayed)
    row = run.compare_case(c, py, rs)
    assert row["diffs"] == []
    assert row["transport_replays"] == {side: "ConnectionAbortedError: [WinError 10053]"}
    assert "_transport_replay" in replayed  # the caller's response is not mutated
    assert "_transport_replay" not in run.normalize_response(replayed, "/api/nope", [])
    assert "transport_replays" not in run.compare_case(c, response, response)


@pytest.mark.parametrize("chunked", [False, True])
def test_send_timeout_after_an_early_answer_still_fails(chunked, monkeypatch):
    # The near miss of a send cut off by a refusal: a daemon that answers
    # early but never reads the body and never closes. Only a send ended by
    # the refusal (broken pipe, reset, abort) may give way to the response.
    monkeypatch.setattr(http.client, "HTTPConnection", UnbufferedSend)
    listener = socket.create_server(("127.0.0.1", 0))
    held = []

    def answer_and_hold():
        conn, _ = listener.accept()
        held.append(conn)
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 2\r\n\r\n{}")

    threading.Thread(target=answer_and_hold, daemon=True).start()
    with listener, pytest.raises(TimeoutError):
        run.call(listener.getsockname()[1], "POST", "/api/nope", body=b" " * (32 * 1024 * 1024),
                 timeout=2, chunked=chunked)
    for conn in held:
        conn.close()


def test_scenario_error_names_the_case_in_flight_and_the_harness_line():
    listener = socket.create_server(("127.0.0.1", 0))

    def close_unanswered():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.close()

    threading.Thread(target=close_unanswered, daemon=True).start()
    with listener, pytest.raises(http.client.RemoteDisconnected) as raised:
        run.call(listener.getsockname()[1], "GET", "/health", timeout=10)
    scn = run.BodyLimits()
    assert run.scenario_error(raised.value, scn).startswith("RemoteDisconnected: ")
    assert run.scenario_error(raised.value, scn).endswith(" in _exchange)")
    scn.case_in_flight = "/api/nope chunked next byte"
    text = run.scenario_error(raised.value, scn)
    assert "(case '/api/nope chunked next byte'; at run.py:" in text and text.endswith(" in _exchange)")
    assert run.scenario_error(RuntimeError("no traceback"), run.BodyLimits()) == "RuntimeError: no traceback"


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
