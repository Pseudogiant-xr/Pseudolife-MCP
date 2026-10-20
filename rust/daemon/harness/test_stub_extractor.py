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


def test_stage_prompts_have_their_own_kinds():
    """The dream stages' real prompts key their own reply tables."""
    import stub_extractor
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pseudolife_memory.memory import dream as d
    assert stub_extractor.request_kind(d._OUTCOME_INFER_SYSTEM_PROMPT) == "outcomes"
    assert stub_extractor.request_kind(d._LESSON_SYSTEM_PROMPT) == "lessons"
    assert stub_extractor.request_kind(d._RULE_LESSON_SYSTEM_PROMPT) == "rules"
    assert stub_extractor.request_kind(
        d._DIGEST_SYSTEM_PROMPT.format(target_chars=10)) == "digest"
    assert stub_extractor.request_kind(d._SYSTEM_PROMPT) == "claims"


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
                   "sha256": hashlib.sha256(body).hexdigest(), "authorization": None,
                   "answered": False}


def test_authorization_is_recorded_as_a_digest(stub_factory):
    from stub_extractor import auth_digest
    s = stub_factory({"replies": [{"kind": "claims", "user": "n", "content": "c"}]})
    host, port = s.origin.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("POST", "/fallback/v1/chat/completions", body=chat_body(CLAIMS_SYSTEM, "n"),
              headers={"Content-Type": "application/json", "Authorization": "Bearer sk-secret"})
    c.getresponse().read()
    c.close()
    (rec,) = s.drain()
    digest = "sha256:" + hashlib.sha256(b"Bearer sk-secret").hexdigest()[:16]
    assert rec["authorization"] == digest == auth_digest("Bearer sk-secret")
    assert "sk-secret" not in json.dumps(rec)


def test_reason_phrase_and_extra_headers(stub_factory):
    s = stub_factory({"replies": [
        {"kind": "claims", "user": "r", "status": 500, "reason": "Login Required", "body": "{}"},
        {"kind": "claims", "user": "h", "status": 302, "body": "",
         "headers": {"Location": "http://elsewhere.example/oauth/authorize"}},
    ]})
    host, port = s.origin.removeprefix("http://").split(":")
    for user, want in (("r", (500, "Login Required", None)),
                       ("h", (302, "Found", "http://elsewhere.example/oauth/authorize"))):
        c = http.client.HTTPConnection(host, int(port), timeout=10)
        c.request("POST", "/v1/chat/completions", body=chat_body(CLAIMS_SYSTEM, user),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        assert (r.status, r.reason, r.getheader("Location")) == want


def test_health_sequence(stub_factory):
    s = stub_factory({"health": [503, 503, 200]})
    assert [request(s, "GET", "/health")[0] for _ in range(5)] == [503, 503, 200, 200, 200]
    assert request(s, "GET", "/fallback/health")[0] == 200


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


JUDGE_SYSTEM = "You judge MERGE PROPOSALS for a knowledge-graph deduper."


def keyed_request(stub, path, body, key=None):
    host, port = stub.origin.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    c.request("POST", path, body=body, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


def judge_user(*pairs):
    return "\n\n".join(
        f"[{i}] FROM '{a}' (degree 1, scopes []) evidence: none\n"
        f"    INTO '{b}' (degree 1, scopes []) evidence: none\n    detector: r"
        for i, (a, b) in enumerate(pairs, 1))


def test_judge_prompt_has_its_own_kind():
    import stub_extractor
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pseudolife_memory.memory import dream as d
    assert stub_extractor.request_kind(d._JUDGE_SYSTEM_PROMPT) == "judge_merges"


def test_judge_verdicts_answer_by_pair_on_each_endpoint(stub_factory):
    s = stub_factory({"reply_model": "one", "second_reply_model": "two",
                      "third_reply_model": None, "replies": [
        {"kind": "judge_merges", "verdicts": {"A|B": {"verdict": "accept", "confidence": 0.9}}},
        {"kind": "judge_merges", "endpoint": "second",
         "verdicts": {"C|D": {"verdict": "reject", "confidence": 0.8, "relation": "uses"}}},
        {"kind": "judge_merges", "endpoint": "third", "model": "three-x",
         "verdicts": {"A|B": {"verdict": "leave", "confidence": 0.5}}},
    ]})
    body = chat_body(JUDGE_SYSTEM, judge_user(("B", "A"), ("D", "C")))
    _, raw = keyed_request(s, "/v1/chat/completions", body)
    reply = json.loads(raw)
    assert reply["model"] == "one"
    assert json.loads(reply["choices"][0]["message"]["content"]) == {
        "verdicts": [{"id": 1, "verdict": "accept", "confidence": 0.9}]}
    _, raw = keyed_request(s, "/second/v1/chat/completions", body)
    reply = json.loads(raw)
    assert reply["model"] == "two"
    assert json.loads(reply["choices"][0]["message"]["content"]) == {
        "verdicts": [{"id": 2, "verdict": "reject", "confidence": 0.8, "relation": "uses"}]}
    _, raw = keyed_request(s, "/third/v1/chat/completions", body)
    assert json.loads(raw)["model"] == "three-x"
    status, raw = request(s, "GET", "/second/v1/models")
    assert json.loads(raw)["data"][0]["id"] == "two"
    # An endpoint-less entry never answers the judge endpoints, and an
    # endpoint entry never answers the primary base.
    s2 = stub_factory({"replies": [{"kind": "judge_merges", "endpoint": "second",
                                    "verdicts": {}}]})
    assert keyed_request(s2, "/v1/chat/completions", body)[0] == 500
    assert keyed_request(s2, "/third/v1/chat/completions", body)[0] == 500


def test_times_entries_answer_per_caller_then_yield(stub_factory):
    s = stub_factory({"replies": [
        {"kind": "judge_merges", "endpoint": "second", "times": 1, "status": 500, "body": "{}"},
        {"kind": "judge_merges", "endpoint": "second", "verdicts": {}},
    ]})
    body = chat_body(JUDGE_SYSTEM, judge_user(("A", "B")))
    path = "/second/v1/chat/completions"
    assert [keyed_request(s, path, body, "side-python")[0] for _ in range(2)] == [500, 200]
    assert [keyed_request(s, path, body, "side-rust")[0] for _ in range(2)] == [500, 200]


def test_judge_verdict_lists_repeat_an_id_and_models_move(stub_factory):
    s = stub_factory({"reply_model": "one", "second_reply_model": "two", "replies": [
        {"kind": "judge_merges", "endpoint": "second", "times": 1, "model": "two-a",
         "verdicts": {"A|B": [{"verdict": "reject", "confidence": 0.8},
                              {"verdict": "leave", "confidence": 0.4}]}},
        {"kind": "judge_merges", "endpoint": "second", "model": "two-b",
         "verdicts": {"A|B": {"verdict": "accept", "confidence": 0.7}}},
    ]})
    body = chat_body(JUDGE_SYSTEM, judge_user(("A", "B")))
    path = "/second/v1/chat/completions"
    first = json.loads(keyed_request(s, path, body, "side-python")[1])
    assert first["model"] == "two-a"
    assert json.loads(first["choices"][0]["message"]["content"]) == {"verdicts": [
        {"id": 1, "verdict": "reject", "confidence": 0.8},
        {"id": 1, "verdict": "leave", "confidence": 0.4}]}
    later = json.loads(keyed_request(s, path, body, "side-python")[1])
    assert later["model"] == "two-b"
    assert json.loads(keyed_request(s, path, body, "side-rust")[1])["model"] == "two-a"


def test_shared_stub_records_read_per_caller(stub_factory):
    from stub_extractor import auth_digest
    s = stub_factory({"replies": [{"kind": "judge_merges", "verdicts": {}}]})
    body = chat_body(JUDGE_SYSTEM, judge_user(("A", "B")))
    keyed_request(s, "/v1/chat/completions", body, "side-python")
    keyed_request(s, "/v1/chat/completions", body, "side-rust")
    keyed_request(s, "/v1/chat/completions", body, "side-python")
    py, rs = auth_digest("Bearer side-python"), auth_digest("Bearer side-rust")
    assert (s.count(py), s.count(rs), s.count()) == (2, 1, 3)
    assert len(s.drain(auth=py)) == 2
    assert s.count(py) == 0 and s.count(rs) == 1
    assert [r["authorization"] for r in s.drain()] == [rs]


def test_shared_stub_env_and_config(harness, stub_factory):
    s = stub_factory()
    scn = {"extractor": {"model": "judge-one"}, "shared_stub": True,
           "config": "judge_second_url: \"@STUB@/second/v1\"\n"}
    env = harness.extractor_env(scn, s, "rust")
    assert env["PSEUDOLIFE_DREAM_API_KEY"] == env["PSEUDOLIFE_JUDGE_SECOND_API_KEY"] \
        == env["PSEUDOLIFE_JUDGE_THIRD_API_KEY"] == "side-rust"
    assert harness.scenario_config(scn, s) == f"judge_second_url: \"{s.origin}/second/v1\"\n"
    stubs = harness.start_stubs(scn, ("seed", "python", "rust"))
    try:
        assert stubs["python"] is stubs["rust"] and stubs["seed"] is not stubs["python"]
    finally:
        harness.close_stubs(stubs)
