"""Candidate-only producer substitutions; immutable Python remains the oracle."""
import base64
import copy
import os
from pathlib import Path
import subprocess

import pytest

from evals.rust_port.test_wait_mail_phase2d_native import native_commands, capture
from evals.rust_port.cli_process import reset_home
from evals.rust_port.cli_wait_mail import cases
from evals.rust_port.wait_mail_policy import delivery_projection


STEM = ".pseudolife-mcp/digests/" + "a" * 64


def delivery_case():
    return copy.deepcopy(next(case for case in cases() if case["id"] == "wait-mail-unicode-delivery"))


@pytest.mark.parametrize("extension,raw", [
    ("txt", b"+12\npeer\n"), ("txt", b"1_2\npeer\n"), ("txt", b"12\r\npeer\n"),
    ("txt", b"12\npeer"), ("txt", b"12"), ("txt", b" 12\npeer\n"),
    ("ring", b"+12\nrung anyone\n"), ("ring", b"12\nrung anyone"),
    ("ring", b"12\nrung anyone\r\n"), ("ring", b"12\nrung anyone\nextra\n"),
    ("seen", b"12"), ("seen", b"12\r\n"), ("seen", b"1_2\n"), ("seen", b"\n"),
])
def test_native_corrupt_writer_record_exits_2_without_delivery_or_marker_change(tmp_path, extension, raw):
    root, commands = native_commands()
    case = delivery_case()
    case["id"] = "wait-mail-reduced-corrupt-" + extension
    case["pre_files_b64"][STEM + "." + extension] = base64.b64encode(raw).decode()
    home = tmp_path / "home"
    try:
        observation = capture(case, commands, "candidate", root, home)[0]
        response = observation["response"]
        assert response["exit_code"] == 2
        assert base64.b64decode(response["stdout_b64"]) == b""
        kind = "digest" if extension == "txt" else extension
        expected = ("wait-mail: corrupt coordination " + kind
                    + " record; expected canonical unsigned ASCII decimal and LF framing; left the mail unshown.\n")
        assert base64.b64decode(response["stderr_b64"]) == expected.replace("\n", os.linesep).encode()
        assert response["post_files_b64"] == observation["pre_files_b64"]
    finally:
        reset_home(home)


@pytest.mark.parametrize("raw", ["NaN", "inf", "٠.٠٢٥", "1_0e-3", " 0.025", "0.025 ", "1e999"])
def test_native_numeric_refusal_keeps_usage_exit_2(tmp_path, raw):
    root, commands = native_commands()
    case = delivery_case()
    case["id"] = "wait-mail-reduced-numeric-refusal"
    case["argv"] = ["wait-mail", "--timeout", raw]
    home = tmp_path / "home"
    try:
        observation = capture(case, commands, "candidate", root, home)[0]
        response = observation["response"]
        assert response["exit_code"] == 2
        assert base64.b64decode(response["stdout_b64"]) == b""
        stderr = base64.b64decode(response["stderr_b64"])
        assert stderr.startswith(b"usage: pseudolife-mcp wait-mail ")
        assert b"pseudolife-mcp wait-mail: error: argument --timeout: invalid float value:" in stderr
        assert response["post_files_b64"] == observation["pre_files_b64"]
    finally:
        reset_home(home)


@pytest.mark.parametrize("raw", ["0", "-1", "+80", " 80", "80\n", "8_0", "٨٠", "1e2", "99999999999999999999999999999"])
def test_native_invalid_columns_falls_back_to_exact_80_help(tmp_path, raw):
    root, commands = native_commands()
    case = copy.deepcopy(next(case for case in cases() if case["id"] == "wait-mail-help-columns-80"))
    home = tmp_path / "home"
    try:
        reference = capture(case, commands, "candidate", root, home)[0]["response"]
        case["id"] = "wait-mail-reduced-columns-fallback"
        case["environment_deltas"]["COLUMNS"] = raw
        response = capture(case, commands, "candidate", root, home)[0]["response"]
        assert response == reference
        assert response["exit_code"] == 0
    finally:
        reset_home(home)


@pytest.mark.parametrize("help_output", [False, True])
@pytest.mark.parametrize("setting", ["", "0", "1"])
def test_native_stdout_failure_exits_2_independent_of_python_buffer_setting(tmp_path, help_output, setting):
    root, commands = native_commands()
    digest = tmp_path / ("a" * 64 + ".txt")
    digest.write_bytes(b"12\npeer\n")
    digest.with_suffix(".ring").write_bytes(b"12\nrung anyone\n")
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    arguments = ["wait-mail", "--help"] if help_output else [
        "wait-mail", "--digest", str(digest), "--timeout", "0.025", "--interval", "0.01"]
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONUNBUFFERED": setting}
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    with digest.open("rb") as readonly:
        output = subprocess.run([*commands["candidate"], *arguments], cwd=root, env=env,
                                stdout=readonly, stderr=subprocess.PIPE, timeout=5, check=False)
    assert output.returncode == 2
    expected = b"could not write help to stdout" if help_output else b"could not write the mail to stdout"
    assert expected in output.stderr
    assert b"Exception ignored in:" not in output.stderr
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == before


def test_native_arbitrary_width_writer_watermarks_deliver_and_advance_exactly(tmp_path):
    root, commands = native_commands()
    case = delivery_case()
    case["id"] = "wait-mail-reduced-large-watermark"
    counter = b"1" * 4301
    body = b"peer\n"
    case["pre_files_b64"][STEM + ".txt"] = base64.b64encode(counter + b"\n" + body).decode()
    case["pre_files_b64"][STEM + ".ring"] = base64.b64encode(counter + b"\nrung anyone\n").decode()
    home = tmp_path / "home"
    try:
        observation, window = capture(case, commands, "candidate", root, home)
        response = observation["response"]
        assert response["exit_code"] == 0
        assert base64.b64decode(response["stdout_b64"]) == body
        assert base64.b64decode(response["post_files_b64"][STEM + ".seen"]) == counter + b"\n"
        _, instance = delivery_projection(response, window, ".pseudolife-mcp/digests/ledger.log")
        assert instance["policy"] == "nondeterministic-bytes-semantic"
        assert response["post_files_b64"].keys() == observation["pre_files_b64"].keys() | {".pseudolife-mcp/digests/ledger.log"}
        assert not any("wait-armed" in path or ".tmp-" in path for path in response["post_files_b64"])
    finally:
        reset_home(home)
