"""Paired controls for counters and the prompt hook's truncate/write window."""
import base64
import copy
import os
import subprocess
import time

import pytest

from evals.rust_port.cli_process import reset_home
from evals.rust_port.cli_wait_mail import cases
from evals.rust_port.test_wait_mail_phase2d_native import capture, native_commands
from evals.rust_port.wait_mail_policy import delivery_projection


STEM = ".pseudolife-mcp/digests/" + "a" * 64
LEDGER = ".pseudolife-mcp/digests/ledger.log"


@pytest.mark.parametrize("seen,counter,expected", [
    (b"", b"12", b"12"), (b"\n", b"12", b"12"),
    (b" \t\r\n\x0b\x0c", b"12", b"12"),
    (b"12", b"13", b"13"), (b"12\r\n", b"13", b"13"),
    (b"00000\n", b"00042", b"42"),
    (b" \t00042\r\n", b"00043", b"43"),
])
def test_paired_seen_strip_empty_and_zero_padded_counters(tmp_path, seen, counter, expected):
    root, commands = native_commands()
    case = copy.deepcopy(next(cell for cell in cases() if cell["id"] == "wait-mail-unicode-delivery"))
    case["id"] = "wait-mail-producer-counter"
    body = b"peer\n"
    for suffix, raw in (("seen", seen), ("txt", counter + b"\n" + body),
                        ("ring", counter + b"\nrung anyone\n")):
        case["pre_files_b64"][STEM + "." + suffix] = base64.b64encode(raw).decode("ascii")
    home = tmp_path / "home"
    projections = []
    try:
        for arm in commands:
            result, window = capture(case, commands, arm, root, home)
            response = result["response"]
            assert response["exit_code"] == 0
            assert base64.b64decode(response["stdout_b64"]) == body
            assert base64.b64decode(response["post_files_b64"][STEM + ".seen"]) == expected + b"\n"
            assert not any("wait-armed" in path or ".tmp-" in path for path in response["post_files_b64"])
            projections.append(delivery_projection(response, window, LEDGER)[0])
        assert projections[0] == projections[1]
    finally:
        reset_home(home)


def test_paired_prompt_hook_two_step_seen_write(tmp_path):
    root, commands = native_commands()
    for arm, command in commands.items():
        home = tmp_path / arm
        home.mkdir()
        digest = home / ("a" * 64 + ".txt")
        digest.write_bytes(b"41\n")
        ring = digest.with_suffix(".ring")
        ring.write_bytes(b"41\nrung anyone\n")
        seen = digest.with_suffix(".seen")
        seen.write_bytes(b"41\n")
        env = os.environ.copy()
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(root))
        process = subprocess.Popen([*command, "wait-mail", "--digest", str(digest),
                                    "--timeout", "3", "--interval", "0.01"],
                                   cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 2
            while not list(home.glob("*.wait-armed")):
                assert process.poll() is None, process.communicate()
                assert time.monotonic() < deadline, "listener did not arm"
                time.sleep(0.005)
            # Hold the window between the hook's redirection and printf write.
            # Close the truncating handle so native atomic marker replacement can run.
            with seen.open("wb"):
                pass
            assert seen.read_bytes() == b""
            replacement = home / "next.txt"
            replacement.write_bytes(b"00042\nnew peer\n")
            replace_during_poll(replacement, digest, deadline)
            replacement = home / "next.ring"
            replacement.write_bytes(b"00042\nrung anyone\n")
            replace_during_poll(replacement, ring, deadline)
            stdout, stderr = process.communicate(timeout=4)
            assert process.returncode == 0, stderr
            assert stdout == b"new peer\n"
            assert b"watermark 42" in stderr
            assert seen.read_bytes() == b"42\n"
            # Complete the producer's second step after the observed empty-window read.
            seen.write_bytes(b"00042\n")
            assert seen.read_bytes() == b"00042\n"
            assert not list(home.glob("*.wait-armed"))
            assert not list(home.glob(".tmp-*"))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=4)
            reset_home(home)


def replace_during_poll(source, destination, deadline):
    while True:
        try:
            source.replace(destination)
            return
        except PermissionError as error:
            if os.name != "nt" or error.winerror != 5 or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)
