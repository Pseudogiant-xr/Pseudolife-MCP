"""The dream-extractor stub and its harness helpers, without daemons or a database."""
import hashlib
import http.client
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stub_extractor import StubExtractor  # noqa: E402

CLAIMS_SYSTEM = "You consolidate numbered notes into claims. Answer JSON."
EVENTS_SYSTEM = "You extract EVENTS from the notes."


def chat_body(system: str, user: str, model: str = "stub-model") -> bytes:
    return json.dumps({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0}).encode()


def request(stub, method, path, body=None):
    host, port = stub.origin.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


@pytest.fixture
def stub_factory():
    started = []

    def make(spec=None, name="test"):
        s = StubExtractor(spec, name).start()
        started.append(s)
        return s

    yield make
    for s in started:
        s.close()


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import pgdisposable as pg
    from rust.cli_harness.run_names import RunNames
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", pg.DISPOSABLE_NAME)
    h = importlib.import_module("w2e")
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", re.compile(r"pl_cf_w2e_[a-z0-9_]{1,40}"))
    monkeypatch.setattr(pg, "NAMES", RunNames())
    return h


def test_urls(stub_factory):
    s = stub_factory()
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+", s.origin)
    assert s.base_url == s.origin + "/v1"
    assert s.fallback_base_url == s.origin + "/fallback/v1"


def test_health_and_models_routes(stub_factory):
    s = stub_factory({"served_model": "served-a", "fallback_served_model": "served-b"})
    for path in ("/health", "/fallback/health"):
        status, raw = request(s, "GET", path)
        assert (status, json.loads(raw)) == (200, {"status": "ok"})
    status, raw = request(s, "GET", "/v1/models")
    assert (status, json.loads(raw)) == (200, {"object": "list", "data": [{"id": "served-a"}]})
    status, raw = request(s, "GET", "/fallback/v1/models")
    assert json.loads(raw)["data"][0]["id"] == "served-b"
    status, raw = request(s, "GET", "/v1/health")
    assert status == 404 and "error" in json.loads(raw)


def test_served_model_defaults(stub_factory):
    s = stub_factory()
    assert json.loads(request(s, "GET", "/v1/models")[1])["data"][0]["id"] == "stub-served-model"
    assert json.loads(request(s, "GET", "/fallback/v1/models")[1])["data"][0]["id"] \
        == "stub-served-model"


def test_health_override(stub_factory):
    s = stub_factory({"health": 503})
    assert request(s, "GET", "/health")[0] == 503
    assert request(s, "GET", "/fallback/health")[0] == 200
    s2 = stub_factory({"fallback_health": 500})
    assert request(s2, "GET", "/health")[0] == 200
    assert request(s2, "GET", "/fallback/health")[0] == 500


def test_sha256_keyed_reply_wins(stub_factory):
    body = chat_body(CLAIMS_SYSTEM, "1. note")
    s = stub_factory({"reply_model": "rm", "replies": [
        {"kind": "claims", "user": "1. note", "content": "by-kind"},
        {"sha256": hashlib.sha256(body).hexdigest(), "content": "by-sha", "model": "m-sha"},
    ]})
    status, raw = request(s, "POST", "/v1/chat/completions", body)
    assert status == 200
    assert json.loads(raw) == {"choices": [{"message": {"content": "by-sha"}}], "model": "m-sha"}


def test_kind_user_keyed_reply_envelope(stub_factory):
    s = stub_factory({"reply_model": "rm", "replies": [
        {"kind": "claims", "user": "1. note", "content": "claims-reply"},
        {"kind": "events", "user": "1. note", "content": "events-reply"},
    ]})
    status, raw = request(s, "POST", "/v1/chat/completions", chat_body(EVENTS_SYSTEM, "1. note"))
    assert status == 200
    assert json.loads(raw) == {"choices": [{"message": {"content": "events-reply"}}],
                               "model": "rm"}
    status, raw = request(s, "POST", "/fallback/v1/chat/completions",
                          chat_body(CLAIMS_SYSTEM, "1. note"))
    assert json.loads(raw)["choices"][0]["message"]["content"] == "claims-reply"
    assert all(r["answered"] for r in s.drain())


def test_default_reply_model_and_raw_error_entry(stub_factory):
    s = stub_factory({"replies": [
        {"kind": "other", "user": "x", "content": "ok"},
        {"kind": "other", "user": "garbage", "status": 502, "body": "<html>bad gateway"},
    ]})
    _, raw = request(s, "POST", "/v1/chat/completions", chat_body("Something else", "x"))
    assert json.loads(raw)["model"] == "stub-model"
    status, raw = request(s, "POST", "/v1/chat/completions", chat_body("Something", "garbage"))
    assert (status, raw) == (502, b"<html>bad gateway")


def test_unanswered_request_is_flagged(stub_factory):
    s = stub_factory({"replies": [{"kind": "claims", "user": "known", "content": "c"}]})
    body = chat_body(CLAIMS_SYSTEM, "unknown")
    status, raw = request(s, "POST", "/v1/chat/completions", body)
    assert status == 500
    assert json.loads(raw) == {"error": "stub_extractor: no table entry"}
    (rec,) = s.drain()
    assert rec == {"method": "POST", "path": "/v1/chat/completions",
                   "body": body.decode("latin-1"),
                   "sha256": hashlib.sha256(body).hexdigest(), "answered": False}


def test_drain_records_and_clears(stub_factory):
    s = stub_factory()
    assert s.drain() == []
    request(s, "GET", "/health")
    request(s, "GET", "/v1/models")
    request(s, "POST", "/nowhere", b"\xff\x00")
    recs = s.drain()
    assert [(r["method"], r["path"]) for r in recs] == [
        ("GET", "/health"), ("GET", "/v1/models"), ("POST", "/nowhere")]
    assert recs[0]["body"] is None and recs[0]["sha256"] is None
    assert recs[2]["body"] == "\xff\x00"
    assert recs[2]["sha256"] == hashlib.sha256(b"\xff\x00").hexdigest()
    assert s.drain() == []
    request(s, "GET", "/health")
    assert len(s.drain()) == 1


def test_replace_origin(harness):
    origin = "http://127.0.0.1:54321"
    value = {"isError": False, "value": {
        "endpoint": f"{origin}/v1", "list": [f"at {origin}/fallback/v1", 3, None],
        f"{origin}/k": True, "other": "http://127.0.0.1:5432/v1"}}
    assert harness.replace_origin(value, origin) == {"isError": False, "value": {
        "endpoint": "http://stub/v1", "list": ["at http://stub/fallback/v1", 3, None],
        "http://stub/k": True, "other": "http://127.0.0.1:5432/v1"}}
    assert harness.replace_origin(None, origin) is None


def test_extractor_env(harness, stub_factory):
    s = stub_factory()
    assert harness.extractor_env({"extractor": {}}, None) == {}
    assert harness.extractor_env({"extractor": {"model": "m"}}, s) == {
        "PSEUDOLIFE_DREAM_BASE_URL": s.base_url, "PSEUDOLIFE_DREAM_MODEL": "m"}
    env = harness.extractor_env({"extractor": {}, "extractor_fallback": True}, s)
    assert env == {"PSEUDOLIFE_DREAM_BASE_URL": s.base_url,
                   "PSEUDOLIFE_DREAM_MODEL": "stub-model",
                   "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL": s.fallback_base_url,
                   "PSEUDOLIFE_DREAM_FALLBACK_MODEL": "stub-fallback-model"}
    assert harness.start_stubs({}, ("python",)) == {}


def test_extractor_diffs(harness):
    def rec(body, answered=True, method="POST", path="/v1/chat/completions"):
        raw = body.encode("latin-1") if body is not None else None
        return {"method": method, "path": path, "body": body, "answered": answered,
                "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None}

    same = [rec(None, method="GET", path="/health"), rec('{"a": 1, "b": "x"}')]
    assert harness.extractor_diffs(same, list(same)) == []
    diffs = harness.extractor_diffs([rec('{"a": 1, "b": "x"}', answered=False)],
                                    [rec('{"a": 1, "b": "y"}'), rec("{}")])
    assert diffs[0].startswith("extractor python: unanswered POST /v1/chat/completions sha256=")
    assert diffs[1] == "extractor requests: python sent 1, rust sent 2"
    assert diffs[2].startswith("extractor request 0: python POST /v1/chat/completions sha256=")
    assert '- "b": "x"}' in diffs[2] and '+ "b": "y"}' in diffs[2]
    assert "first difference at byte 15" in diffs[2]
    assert diffs[3].startswith("extractor request 1: python <none> vs rust POST")
    assert len(diffs) == 4
    long = harness.extractor_diffs([rec("a," * 2000)], [rec("b," * 2000)])
    assert len(long[0]) < 800
