"""Exact JSON number tokens must survive portable golden recording."""
import json

import pytest

import run


def response(token):
    raw = '{"entries": [{"timestamp": ' + token + '}]}'
    return {"status": 200, "headers": {}, "json": json.loads(raw), "raw": raw}


@pytest.mark.parametrize("expected,mutant", [
    ("-847044394961070.2", "-847044394961070.3"),
    ("93026504287663.12", "93026504287663.13"),
    ("1000000000000000.2", "1000000000000000.3"),
])
def test_golden_float_token_mutant(tmp_path, monkeypatch, expected, mutant):
    c = run.case("json-float-shortest-ties", "GET", "/api/search?q=tie")
    oracle, candidate = response(expected), response(mutant)
    assert oracle["json"] == candidate["json"]
    assert run.compare_case(c, oracle, candidate)["diffs"]
    monkeypatch.setattr(run, "GOLDENS", tmp_path)
    run.save_golden("float-tie", [{"_python": oracle}], None)
    recorded = run.load_golden("float-tie")["responses"][0]
    assert "raw" not in recorded
    assert run.compare_case(c, recorded, run.golden_scrub(oracle))["diffs"] == []
    assert run.compare_case(c, recorded, run.golden_scrub(candidate))["diffs"]


def test_recording_is_portable_idempotent_and_keeps_only_exact_tokens(monkeypatch):
    monkeypatch.setattr(run, "_machine_paths", lambda: ["/private/home"])
    raw = '{"path": "/private/home/data", "free": "<free text>", "a.b": [1.00, 9007199254740993]}'
    oracle = {"status": 200, "headers": {}, "json": json.loads(raw), "raw": raw}
    recorded = run.golden_scrub(oracle)
    assert "/private/home" not in json.dumps(recorded)
    assert recorded["number_tokens"] == [
        {"path": ["a.b", 0], "token": "1.00"},
        {"path": ["a.b", 1], "token": "9007199254740993"},
    ]
    assert run.golden_scrub(recorded) == recorded


def test_search_free_scores_and_replay_seed_clocks_remain_free():
    c = run.case("seeded", "GET", "/api/search?q=memory")
    def answer(ts, score):
        raw = '{"entries": [{"timestamp": ' + ts + ', "score": ' + score + ', "access_count": 1}]}'
        return {"status": 200, "headers": {}, "json": json.loads(raw), "raw": raw}
    oracle = answer("1.10", "0.3")
    candidate = answer("2.20", "0.4")
    recorded = run.golden_scrub(run.normalize_response(oracle, c["path"], []))
    actual = run.golden_scrub(candidate)
    assert run.compare_case(c, recorded, actual)["diffs"]
    assert run.compare_case(c, run.seed_clock_scrub(recorded), run.seed_clock_scrub(actual))["diffs"] == []


@pytest.mark.parametrize("token", ["-0.0", "1e0", "9007199254740993"])
def test_missing_token_evidence_cannot_pass_new_recording(token):
    c = run.case("missing evidence", "GET", "/api/search?q=memory")
    oracle = run.golden_scrub(response(token))
    candidate = {k: v for k, v in oracle.items() if k != "number_tokens"}
    assert run.compare_case(c, oracle, candidate)["diffs"]


def test_explicit_fixture_artifacts_override_callers_real_model(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_DAEMON_ONNX_DIR", "/real-model")
    monkeypatch.setenv("ORT_DYLIB_PATH", "/real-runtime")
    fixture = {"PSEUDOLIFE_DAEMON_ONNX_DIR": "/fixture-model", "ORT_DYLIB_PATH": "/fixture-runtime"}
    assert run.rust_env(fixture) == {**fixture, "PSEUDOLIFE_DAEMON_STATIC_DIR": str(run.REPO / "pseudolife_memory/web/static")}
