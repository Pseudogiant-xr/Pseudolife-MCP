import base64

import pytest

from evals.rust_port.harness import capture_platform, HttpClient, Policy, compare, execute, isolated_env, replay, run_cli


def test_scores_tolerate_only_named_fields_and_never_order():
    policy = Policy(score_paths=("/entries/*/score",), abs_tol=1e-6,
                    ranking_paths=("/entries",))
    expected = {"entries": [{"id": 1, "score": 0.8}, {"id": 2, "score": 0.7}]}
    close = {"entries": [{"id": 1, "score": 0.8000001}, {"id": 2, "score": 0.7}]}
    assert compare(expected, close, policy) == []
    assert compare(expected, {"entries": list(reversed(close["entries"]))}, policy)
    assert compare({"count": 1.0}, {"count": 1.0000001}, policy)


def test_missing_fields_and_nonfinite_scores_fail():
    policy = Policy(score_paths=("/score",), ignored_values=("/time",))
    assert compare({"time": "a"}, {"time": "b"}, policy) == []
    assert compare({"time": "a"}, {}, policy)
    assert compare({"score": 1.0}, {"score": float("nan")}, policy)
    assert compare({"score": True}, {"score": 1}, policy)
    assert compare({"time": float("nan")}, {"time": float("nan")}, policy)
    assert compare({"time": {"id": 1}}, {"time": {"id": 2}}, policy)


def test_embedded_mcp_text_requires_explicit_json_rule():
    a = {"text": '{"entries":[{"id":1,"score":0.8}]}'}
    b = {"text": '{"entries": [{"id":1,"score":0.8000001}]}'}
    assert compare(a, b, Policy())
    policy = Policy(json_text_paths=("/text",), score_paths=("/text/entries/*/score",))
    assert compare(a, b, policy) == []


def test_cli_exit_stdout_and_stderr_are_independent(tmp_path):
    import sys
    result = run_cli([sys.executable], ["-c", "import sys; print('out'); print('err',file=sys.stderr); sys.exit(2)"],
                     cwd=tmp_path, env=isolated_env(tmp_path), timeout=5)
    assert result["exit_code"] == 2
    expected = b"out\r\n" if sys.platform == "win32" else b"out\n"
    assert base64.b64decode(result["stdout_b64"]) == expected
    assert b"err" in base64.b64decode(result["stderr_b64"])


def test_isolation_drops_credentials_and_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "never-copy-this")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", "never-copy-this")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "never-copy-this")
    env = isolated_env(tmp_path)
    assert not any(value == "never-copy-this" for value in env.values())
    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_tolerance_rejected(value):
    with pytest.raises(ValueError):
        Policy(abs_tol=value)


def test_timeout_is_negative_evidence_and_not_a_valid_oracle(tmp_path):
    import sys
    cases = [{"id": "timeout", "surface": "cli", "request": {"argv": [
        "-c", "import time; time.sleep(5)"]}}]
    options = dict(cli_prefix=[sys.executable], base_url=None, cwd=tmp_path,
                   home=tmp_path / "home", timeout=0.05)
    records = execute(cases, **options)
    assert records[0]["response"] == {"boundary_error": "TimeoutExpired"}
    with pytest.raises(ValueError, match="oracle transcript"):
        replay({"capture_platform": capture_platform(), "records": records}, **options)


def test_failed_candidate_boundary_is_a_diff(tmp_path):
    oracle = {"capture_platform": capture_platform(), "records": [{"id": "missing", "surface": "cli",
                           "request": {"argv": []}, "response": {"exit_code": 0}}]}
    report = replay(oracle, cli_prefix=[str(tmp_path / "absent")], base_url=None,
                    cwd=tmp_path, home=tmp_path / "home")
    assert not report["passed"]


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com", "http://user:pass@localhost:1",
                                  "http://localhost:1/health"])
def test_origin_refuses_remote_and_credentials(url):
    with pytest.raises(ValueError):
        HttpClient(url)


def test_wildcard_cannot_tolerate_nested_or_unlisted_fields():
    a = {"entries": [{"inner": {"score": 1.0}}]}
    b = {"entries": [{"inner": {"score": 1.0000001}}]}
    assert compare(a, b, Policy(score_paths=("/entries/*/score",)))
