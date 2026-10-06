"""Static adversarial checks; these do not exercise the native executable."""
import base64
import copy

import pytest

from evals.rust_port.lease_policy_preparation import (
    forbidden_header, refusal_response, successful_child_clock, traceback_response,
)


def encoded(raw):
    return base64.b64encode(raw).decode()


def response(stderr=b"", stamp=b"1000.25"):
    return {"exit_code": 1, "stdout_b64": "", "stderr_b64": encoded(stderr),
            "post_files_b64": {"ran.json": encoded(
                b'{"held": "suite,gpu", "t": ' + stamp + b', "credential": false}')}}


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_traceback_keeps_terminal_newline_and_other_diagnostics(newline):
    terminal = b"OverflowError: signed integer is greater than maximum" + newline
    prefix, suffix = b"before" + newline, b"after" + newline
    oracle = response(prefix + b"Traceback (most recent call last):" + newline
                      + b'  File "fixture.py", line 1, in main' + newline
                      + b"    fail()" + newline + b"    ^^^^^^" + newline + terminal + suffix)
    candidate = response(prefix + terminal + suffix)
    assert traceback_response(oracle, candidate, terminal)
    for key, value in (("exit_code", 0), ("stdout_b64", encoded(b"extra")),
                       ("stderr_b64", encoded(terminal)), ("post_files_b64", {})):
        changed = {**candidate, key: value}
        assert not traceback_response(oracle, changed, terminal)
    assert not traceback_response(oracle, response(prefix + terminal.replace(b"maximum", b"minimum") + suffix), terminal)
    assert not traceback_response(oracle, response(prefix + terminal.replace(newline, b"") + suffix), terminal)


def test_unrelated_stderr_is_not_discarded_as_a_frame():
    terminal = b"ValueError: bad\n"
    oracle = response(b"Traceback (most recent call last):\nwarning\n" + terminal)
    with pytest.raises(ValueError, match="unrecognized stderr"):
        traceback_response(oracle, response(terminal), terminal)


@pytest.mark.parametrize("field", ["agent_id", "credential", "bearer"])
def test_header_policy_is_bounded_to_named_fields_and_forbidden_bytes(field):
    for value in [chr(code) for code in range(32)] + ["\x7f", "a\r\n b"]:
        assert forbidden_header(field, value)
    assert not forbidden_header(field, "ordinary")
    assert not forbidden_header(field, "é")
    expected = refusal_response(field, {"instance.id": "exact"}, b"\r\n")
    assert expected["exit_code"] == 1 and expected["stdout_b64"] == ""
    assert expected["post_files_b64"] == {"instance.id": "exact"}
    assert base64.b64decode(expected["stderr_b64"]) == (
        f"lease: HTTP_FORBIDDEN_INPUT_REFUSED: invalid {field} header\r\n".encode())
    with pytest.raises(ValueError, match="outside"):
        forbidden_header("purpose", "\x7f")


def test_clock_requires_retained_arm_windows_and_preserves_all_other_bytes():
    oracle, candidate = response(stamp=b"1000.25"), response(stamp=b"1001.50")
    repeated = response(stamp=b"1002.25")
    for item in (oracle, candidate, repeated):
        item["exit_code"] = 0
    original = copy.deepcopy([oracle, candidate])
    windows = {"oracle": [1000, 1000.5], "candidate": [1001, 1002], "oracle_repeat": [1002, 1003]}
    assert successful_child_clock(oracle, candidate, windows, repeated) == "matched"
    assert [oracle, candidate] == original
    assert successful_child_clock(oracle, candidate, None) == "incomplete"
    for invalid in (None, [1], [True, 2], [2, 1], [float("nan"), 2]):
        assert successful_child_clock(oracle, candidate, {**windows, "oracle": invalid}, repeated) == "incomplete"
    assert successful_child_clock(oracle, candidate, windows) == "incomplete"
    assert successful_child_clock(oracle, candidate, {**windows, "oracle_repeat": windows["oracle"]}, oracle) == "incomplete"
    assert successful_child_clock(oracle, candidate, {**windows, "candidate": [1000, 1001]}, repeated) == "failed"
    for key, value in (("exit_code", 1), ("stdout_b64", encoded(b"extra")),
                       ("stderr_b64", encoded(b"extra")), ("post_files_b64", {"ran.json": encoded(
                           b'{"held": "suite,gpu", "t": 1001.50, "credential": true}')})):
        assert successful_child_clock(oracle, {**candidate, key: value}, windows, repeated) == "failed"
    assert successful_child_clock(oracle, {**candidate, "post_files_b64": {}}, windows, repeated) == "failed"
