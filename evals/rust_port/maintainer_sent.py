"""Additive exact-byte controls for the complete maintainer/sent HTTP port.

These are serializer fixtures, not evidence that SQL or a native HTTP process
ran. Candidate responses must be captured independently; the oracle bytes must
never be supplied to a candidate as its response. No application is imported.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import math
import struct
import urllib.error
import urllib.request
from uuid import UUID

CORPUS = "phase3-sent-http-v1"
ORACLE_COMMIT = "0b46bb8e2010cf0e428dd650b98cedb165ad3d75"
OWNED_HEADERS = ("content-type", "content-length", "cache-control",
                 "x-content-type-options")


def fixture_value(value):
    """Describe a typed producer without losing float bits or object order."""
    if value is None:
        return {"kind": "null"}
    if type(value) is bool:
        return {"kind": "bool", "value": value}
    if type(value) is int:
        return {"kind": "integer", "decimal": str(value)}
    if type(value) is float:
        return {"kind": "float64", "bits_be": struct.pack(">d", value).hex()}
    if type(value) is str:
        return {"kind": "string", "value": value}
    if type(value) is list:
        return {"kind": "array", "items": [fixture_value(v) for v in value]}
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("fixture object keys must be strings")
        return {"kind": "object", "members": [
            [key, fixture_value(item)] for key, item in value.items()]}
    if type(value) is UUID:
        return {"kind": "fixture-uuid", "hex": value.hex}
    if type(value) is datetime:
        if value.tzinfo not in (None, timezone.utc):
            raise TypeError("unsupported fixture timezone")
        return {"kind": "fixture-datetime", "year": value.year,
                "month": value.month, "day": value.day, "hour": value.hour,
                "minute": value.minute, "second": value.second,
                "microsecond": value.microsecond, "utc": value.tzinfo is timezone.utc}
    raise TypeError("unsupported fixture producer")


def oracle_response(payload, status=200):
    """Capture the pinned _send_json contract, including default=str."""
    body = json.dumps(payload, default=str).encode("utf-8")
    return {"status": status, "headers": {
        "content-type": "application/json; charset=utf-8",
        "content-length": str(len(body)), "cache-control": "no-store",
        "x-content-type-options": "nosniff"},
        "body_b64": base64.b64encode(body).decode("ascii")}


def serialization_controls():
    """Mandatory supplemental controls; whole SQL endpoint cases remain required."""
    controls = [
        ("empty-page", {"messages": []}, "json-compatible"),
        ("escaping-and-separators", {"text": 'é雪😀"\\\b\f\n\r\t\x00\x1f',
                                     "literal": r"\u00e9\n", "empty": ""},
         "json-compatible"),
        ("finite-float-spelling", {"numbers": [1e16, 1e15, 1e-4, 1e-5,
            1e-6, 1e-7, 1.25, -0.0, 0.0, 0, 1, 1.0,
            math.nextafter(1e16, 0), math.nextafter(1e-4, 0)]}, "float64"),
        ("large-integers", {"numbers": [9007199254740993, 10**40, -(10**40)]},
         "integer"),
        ("jsonb-order-discriminator", {"wake": {"a": 1, "b": 2, "aa": 3,
            "zz": {"x": [], "yy": {}, "zzz": [2, 1, 2]}}},
         "supplemental-ordered-object-not-database-proof"),
        ("default-str-uuid", {"value": UUID("12345678-1234-5678-1234-567812345678")},
         "fixture-only-uuid-not-sent-sql-reachability"),
        ("default-str-timestamp-naive", {"value": datetime(2026, 1, 2, 3, 4, 5, 123456)},
         "fixture-only-datetime-not-sent-sql-reachability"),
        ("default-str-timestamp-utc", {"value": datetime(2026, 1, 2, 3, 4, 5,
                                                        tzinfo=timezone.utc)},
         "fixture-only-datetime-not-sent-sql-reachability"),
        ("nonfinite-compatibility", {"numbers": [float("nan"), float("inf"),
                                                 float("-inf")]},
         "separate-nonfinite-compatibility-control"),
    ]
    return [{"id": name, "producer": producer, "input": fixture_value(payload),
             "expected": oracle_response(payload), "required": True,
             "evidence_scope": "supplemental-serializer-only"}
            for name, payload, producer in controls]


def proof_controls():
    """Inputs for real SQL fixture replay, preserving Python truthiness.

    This table does not run the projection, gate, SQL or HTTP handler. Expected
    labels/statuses are contract inputs to independent endpoint captures.
    """
    values = [("null", None, None), ("false", False, None), ("zero", 0, None),
              ("empty-string", "", None), ("empty-object", {}, None),
              ("empty-array", [], None), ("absent-label", {"other": 1}, None),
              ("label-null", {"label": None}, None),
              ("label-object", {"label": {"a": 1, "bb": [2, 1]}}, {"a": 1, "bb": [2, 1]})]
    out = [{"id": "proof-" + name, "proof": fixture_value(value),
            "expected_label": fixture_value(label), "expected_status": 200,
            "required": True} for name, value, label in values]
    for name, value in [("nonempty-array", [1]), ("nonempty-string", "x"),
                        ("one", 1), ("true", True)]:
        out.append({"id": "proof-" + name, "proof": fixture_value(value),
                    "expected_status": 503,
                    "expected_error": "coordination_unavailable", "required": True})
    return out


def compare_response(expected, actual):
    """No semantic JSON, sorting, rounding, tolerance or body normalization."""
    if not isinstance(actual, dict):
        return ["response_capture"]
    differences = []
    if type(actual.get("status")) is not int or actual["status"] != expected["status"]:
        differences.append("status")
    headers = actual.get("headers")
    if not isinstance(headers, dict):
        differences.append("headers")
    else:
        if set(headers) != set(OWNED_HEADERS):
            differences.append("headers/members")
        for name in OWNED_HEADERS:
            if headers.get(name) != expected["headers"][name]:
                differences.append("headers/" + name)
    try:
        body = base64.b64decode(actual["body_b64"], validate=True)
        wanted = base64.b64decode(expected["body_b64"], validate=True)
        if body != wanted:
            differences.append("body_bytes")
        if not isinstance(headers, dict) or headers.get("content-length") != str(len(body)):
            differences.append("body_length")
    except (KeyError, TypeError, ValueError):
        differences.append("body_capture")
    return differences


def observe_http(client, *, method="GET", query="", headers=None, body=None):
    """Capture independent HTTP bytes using the existing isolated HttpClient.

    The caller owns the candidate process and disposable bank. This function
    neither discovers a daemon nor starts one, and runs no Python handler.
    Its client must use harness.HttpClient's loopback/no-proxy/no-redirect policy.
    """
    from evals.rust_port.harness import HttpClient
    if type(client) is not HttpClient:
        raise TypeError("sent capture requires the isolated harness HttpClient")
    path = "/api/maintainer/sent" + ("?" + query if query else "")
    request = urllib.request.Request(client.base_url + path, data=body,
                                     headers=headers or {}, method=method)
    try:
        response = client.opener.open(request, timeout=client.timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        # Incidental server/framing headers are outside the application contract.
        selected = {}
        for name in OWNED_HEADERS:
            values = response.headers.get_all(name, [])
            if len(values) > 1:
                raise ValueError("duplicate application response header")
            if values:
                selected[name] = values[0]
        return {"status": response.status, "headers": selected,
                "body_b64": base64.b64encode(raw).decode("ascii")}
