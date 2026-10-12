"""Controls for the lease row's bounded clock comparison."""

import base64
import json

import pytest

from . import compare
from .rows.lease import T0  # registers the row's named rules


@pytest.mark.parametrize("want,got", [(False, 0), (True, 1), (1, 1.0)])
@pytest.mark.parametrize("nested", [False, True])
def test_lease_request_json_scalar_types(want, got, nested):
    def request(value):
        body = json.dumps({"capabilities": {"resumable": [value] if nested else value}})
        return {"exit": 0, "stdout": "", "stderr": "", "files": {}, "requests": [
            {"method": "POST", "target": "/api/register", "body": body,
             "headers": [["Content-Length", str(len(body.encode()))]]}]}
    assert compare.diff(request(want), request(got), ("lease-http-json",))


def test_lease_request_json_checks_byte_length_separately():
    request = {"method": "POST", "target": "/api/register", "body": '{"label":"ü"}',
               "headers": [["Content-Length", str(len('{"label":"ü"}'))]]}
    obs = {"exit": 0, "stdout": "", "stderr": "", "files": {}, "requests": [request]}
    with pytest.raises(ValueError, match="Content-Length"):
        compare.diff(obs, obs, ("lease-http-json",))


def observation(hour, field, age):
    text = f'queued behind codex for {age}, purpose "peer work"\n'
    out = {"exit": 0, "stdout": "", "stderr": "", "files": {},
           "window": [T0 + hour * 3600, T0 + hour * 3600 + 1], "utc_offset": 0,
           "seed_offsets": {str(T0): 0, str(T0 + 60): 0}}
    out[field] = base64.b64encode(text.encode()).decode()
    return out


@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_valid_holder_age_compares_in_each_arms_own_window(field):
    before = observation(1, field, "1h00m")
    later = observation(2, field, "2h00m")
    assert not compare.diff(before, later, ("lease-seeded-clock",))


@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_holder_age_outside_its_own_window_remains_a_difference(field):
    correct = observation(2, field, "2h00m")
    stale = observation(2, field, "1h00m")
    assert compare.diff(correct, stale, ("lease-seeded-clock",))


def test_seed_clock_uses_its_historical_offset():
    from . import normalize
    out = observation(2, "stdout", "2h00m")
    out["utc_offset"] = 3600
    out["stdout"] = base64.b64encode(b"expected end 2023-11-14 22:14\n").decode()
    normalized = normalize.apply(out, ("lease-seeded-clock",), None)
    assert base64.b64decode(normalized["stdout"]) == b"expected end <seed-clock:1700000060>\n"


def test_capture_handshake_preserves_consumed_stderr_bytes(tmp_path):
    import sys
    from .core import Case, Target, run_arm
    def ready(arm, proc):
        prefix = proc.stderr.readline()
        assert prefix == b"READY\x00\xff\n"
        proc.stdin.write(b"go\n")
        proc.stdin.flush()
        return prefix
    case = Case("capture-handshake", [], before_capture=ready)
    code = ("import sys; sys.stderr.buffer.write(b'READY\\x00\\xff\\n'); sys.stderr.flush(); "
            "line=sys.stdin.buffer.readline(); sys.stdout.buffer.write(b'ARMED' if line==b'go\\n' else b'UNARMED'); "
            "sys.stderr.buffer.write(b'DONE\\x00\\xfe\\n')")
    obs = run_arm(case, Target("python", [sys.executable, "-c", code]), tmp_path / "home")
    assert base64.b64decode(obs["stdout"]) == b"ARMED"
    assert base64.b64decode(obs["stderr"]) == b"READY\x00\xff\nDONE\x00\xfe\n"


def test_capture_handshake_retains_already_written_stderr_tail(tmp_path):
    import sys
    from .core import Case, Target, run_arm
    def ready(arm, proc):
        prefix = proc.stderr.readline()
        assert prefix == b"READY\n"
        proc.stdin.write(b"go\n")
        proc.stdin.flush()
        return prefix
    code = ("import sys; sys.stderr.buffer.write(b'READY\\nTAIL\\x00\\xff\\n'); sys.stderr.flush(); "
            "sys.stdin.buffer.readline()")
    obs = run_arm(Case("prefetched-stderr", [], before_capture=ready),
                  Target("python", [sys.executable, "-c", code]), tmp_path / "home")
    assert base64.b64decode(obs["stderr"]) == b"READY\nTAIL\x00\xff\n"


def test_http_defaults_are_narrow_and_custom_headers_stay_visible():
    from .rows.lease import _command_path
    obs = {"requests": [{"body": "{}", "headers": {"content-length": "2", "accept": "*/*",
                       "accept-encoding": "gzip, deflate", "connection": "keep-alive", "origin": "fixture"}}]}
    _command_path(obs)
    headers = obs["requests"][0]["headers"]
    assert headers == {"content-length": "<json-length>", "accept": "*/*", "origin": "fixture"}
    obs = {"requests": [{"body": "{}", "headers": {"content-length": "2",
                       "accept-encoding": "identity", "connection": "close"}}]}
    _command_path(obs)
    assert obs["requests"][0]["headers"]["accept-encoding"] == "identity"
    assert obs["requests"][0]["headers"]["connection"] == "close"
