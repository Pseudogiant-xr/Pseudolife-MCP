"""Supplemental instrument controls; these do not claim native HTTP parity."""
import base64
from copy import deepcopy

import pytest

from evals.rust_port.maintainer_sent import (
    compare_response, oracle_response, proof_controls, serialization_controls)


def bodies():
    return {case["id"]: base64.b64decode(case["expected"]["body_b64"])
            for case in serialization_controls()}


def test_required_controls_preserve_oracle_float_and_escaping_bytes():
    captured = bodies()
    assert b"1e+16" in captured["finite-float-spelling"]
    assert b"1e-05" in captured["finite-float-spelling"]
    assert b"-0.0, 0.0, 0, 1, 1.0" in captured["finite-float-spelling"]
    assert b"\\u00e9\\u96ea\\ud83d\\ude00" in captured["escaping-and-separators"]
    assert b'"literal": "\\\\u00e9\\\\n"' in captured["escaping-and-separators"]
    assert captured["default-str-timestamp-utc"] == b'{"value": "2026-01-02 03:04:05+00:00"}'
    assert captured["nonfinite-compatibility"] == b'{"numbers": [NaN, Infinity, -Infinity]}'
    assert all(case["required"] for case in serialization_controls())


@pytest.mark.parametrize("mutation", ["compact", "reorder", "round", "escape", "float-type"])
def test_exact_comparator_rejects_semantically_close_response(mutation):
    expected = oracle_response({"a": 1.0, "bb": "é", "score": 0.125})
    actual = deepcopy(expected)
    changes = {"compact": b'{"a":1.0,"bb":"\\u00e9","score":0.125}',
               "reorder": b'{"bb": "\\u00e9", "a": 1.0, "score": 0.125}',
               "round": b'{"a": 1.0, "bb": "\\u00e9", "score": 0.13}',
               "escape": '{"a": 1.0, "bb": "é", "score": 0.125}'.encode(),
               "float-type": b'{"a": 1, "bb": "\\u00e9", "score": 0.125}'}
    actual["body_b64"] = base64.b64encode(changes[mutation]).decode()
    actual["headers"]["content-length"] = str(len(changes[mutation]))
    assert "body_bytes" in compare_response(expected, actual)


def test_exact_comparator_requires_owned_headers_status_and_valid_length():
    expected = oracle_response({"messages": []})
    assert compare_response(expected, deepcopy(expected)) == []
    actual = deepcopy(expected)
    actual["status"] = True
    del actual["headers"]["x-content-type-options"]
    actual["headers"]["content-length"] = "0"
    assert compare_response(expected, actual) == ["status", "headers/content-length",
                                                 "headers/x-content-type-options", "body_length"]


def test_proof_controls_distinguish_falsy_and_truthy_nonobjects():
    cases = {case["id"]: case for case in proof_controls()}
    for name in ("null", "false", "zero", "empty-string", "empty-object", "empty-array"):
        assert cases["proof-" + name]["expected_label"] == {"kind": "null"}
        assert cases["proof-" + name]["expected_status"] == 200
    for name in ("nonempty-array", "nonempty-string", "one", "true"):
        assert cases["proof-" + name]["expected_status"] == 503
        assert cases["proof-" + name]["expected_error"] == "coordination_unavailable"


def test_capture_retains_raw_nonfinite_bytes_and_nosniff_without_json_parsing():
    from email.message import Message
    from evals.rust_port.harness import HttpClient
    from evals.rust_port.maintainer_sent import observe_http
    expected = oracle_response({"value": float("nan")})

    class Response:
        status = 200
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return base64.b64decode(expected["body_b64"])

    for key, value in expected["headers"].items():
        Response.headers[key] = value
    Response.headers["Date"] = "incidental"

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:19000/api/maintainer/sent?limit=1"
            return Response()

    client = HttpClient("http://127.0.0.1:19000")
    client.opener = Opener()
    assert observe_http(client, query="limit=1") == expected
