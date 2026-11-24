"""Controls for the lease row's bounded clock comparison."""

import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import compare
from .rows.lease import T0  # registers the row's named rules


def test_lease_cases_load_without_the_frozen_package():
    # Collection already imports the lease module to register its comparison
    # rules. Start a fresh interpreter to exercise its top-level imports too.
    script = """\
import sys
sys.path.insert(0, sys.argv[1])
from rust.harness_support.import_blocker import cold_harness_imports
with cold_harness_imports():
    from rust.cli_harness.rows import load
    assert any(case.id == "check-held" for case in load("lease"))
    assert any(case.id == "break-held" for case in load("lease_operator"))
"""
    repo = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(repo)],
                           capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr


def test_local_lease_board_uses_the_closed_endpoint_without_the_frozen_package():
    from rust.harness_support.import_blocker import cold_harness_imports
    from . import core
    from .rows.lease import Board

    with cold_harness_imports():
        board = Board(bank=False)
        assert board.url == core.DEAD_DAEMON_URL
        board.close()


def test_local_lease_pid_normalizes_without_the_frozen_package():
    from rust.harness_support.import_blocker import cold_harness_imports
    from .rows.lease_operator import fixture_pid

    obs = {"fixture_pid": 2468, "stdout": base64.b64encode(b"watch pid 2468\n").decode(), "stderr": ""}
    with cold_harness_imports():
        fixture_pid(obs)
    assert base64.b64decode(obs["stdout"]) == b"watch pid <PID>\n"


@pytest.mark.parametrize("state,held", [("absent", None), ("free", False), ("held", True)])
def test_local_lease_fixture_without_the_frozen_package(tmp_path, monkeypatch, state, held):
    from rust.harness_support.import_blocker import cold_harness_imports
    from . import producers
    from .rows import lease

    # The old holder child loaded the supplied oracle checkout as PYTHONPATH.
    oracle = tmp_path / "oracle"
    package = oracle / "pseudolife_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('raise AssertionError("child imported the oracle")\n')
    monkeypatch.setattr(producers, "_SOURCE", oracle)
    arm = SimpleNamespace(home=tmp_path, state={}, daemon=SimpleNamespace(locals=[]))
    path = tmp_path / "locks" / "lease-claim_a_b-1881fa03.lock"
    try:
        with cold_harness_imports():
            lease._local(state, name="claim:a/b")(arm)
            from .lease_fixture import probe

            assert probe(path) is held
            assert path.exists() is (state != "absent")
    finally:
        for holder in arm.daemon.locals:
            holder.stop()
    if state == "held":
        assert probe(path) is False


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


@pytest.mark.parametrize("seconds,age", [(-30, "0s"), (31, "31s"), (61, "1m"),
                                         (3661, "1h01m"), (90001, "1d01h")])
def test_holder_age_normalizes_without_the_frozen_package(seconds, age):
    from rust.harness_support.import_blocker import cold_harness_imports
    from . import normalize

    with cold_harness_imports():
        result = normalize.apply(observation(seconds / 3600, "stdout", age), ("lease-seeded-clock",), None)
    assert base64.b64decode(result["stdout"]) == b'queued behind codex for <held-age>, purpose "peer work"\n'


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


def test_recorded_board_answers_as_recorded_and_refuses_an_unrecorded_request():
    import http.client
    from rust.harness_support.import_blocker import cold_harness_imports
    from .rows import lease

    with cold_harness_imports():
        board = lease.Board("free", bank=True)
    try:
        first = lease._recorded("free")["transcripts"][0][0]
        action, body, agent, key = first["key"]
        host, port = board.url.rsplit("//", 1)[1].split(":")

        def post(payload, headers):
            connection = http.client.HTTPConnection(host, int(port), timeout=10)
            data = json.dumps(payload).encode()
            connection.request("POST", f"/api/coordination/{action}", body=data, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {lease.TOKEN}",
                **headers})
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        headers = {k: v for k, v in (("X-PL-Agent", agent), ("X-PL-Agent-Key", key)) if v}
        assert post(body, headers) == (first["status"], first["reply"])
        assert board.states == first["states"]
        # A request the frozen store was never asked, at this point of the
        # history, is answered 500 and fails the case in _after.
        status, reply = post({**body, "w107-unrecorded": True}, headers)
        assert status == 500 and "not in the recording" in reply["error"]
        arm = SimpleNamespace(daemon=board, name="rust", state={})
        with pytest.raises(AssertionError, match="never answered"):
            lease._after(arm, {})
    finally:
        board.close()


def test_board_recording_covers_both_platforms_and_every_board_scenario():
    from .rows import lease

    recording = json.loads(lease.RECORDING.read_text(encoding="utf-8"))["platforms"]
    assert sorted(recording) == ["linux", "windows"]
    scenarios = {"claim", "free", "held", "malformed", "other-mirror", "queue", "refused",
                 "same-mirror", "transient"}
    for platform, recorded in recording.items():
        assert set(recorded) == scenarios, platform
        assert all(recorded[name]["transcripts"] for name in ("free", "claim", "queue")), platform


def test_board_recording_keeps_the_stores_holder_field_order():
    # The CLI prints a board holder's fields in the order it receives them, so
    # a recording written with sorted keys replays a different --json output.
    from .rows import lease

    store_order = ("agent_id", "label", "name", "principal", "purpose",
                   "acquired_at", "expires_at", "expected_end")

    def holders(value):
        if isinstance(value, dict):
            if "agent_id" in value and "acquired_at" in value:
                yield list(value)
            for item in value.values():
                yield from holders(item)
        elif isinstance(value, list):
            for item in value:
                yield from holders(item)

    recording = json.loads(lease.RECORDING.read_text(encoding="utf-8"))["platforms"]
    for platform, recorded in recording.items():
        found = list(holders(recorded))
        assert found, platform
        for keys in found:
            assert keys == [key for key in store_order if key in keys], (platform, keys)
