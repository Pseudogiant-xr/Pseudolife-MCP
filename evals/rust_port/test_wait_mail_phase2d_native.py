"""Actual native delivery and help drift, separate from immutable oracle tests."""
import base64
import copy
import os
from pathlib import Path
import sys
import time

import pytest

from evals.rust_port.cli_process import observe, reset_home
from evals.rust_port.cli_wait_mail import cases
from evals.rust_port.wait_mail_policy import PREFIX, delivery_projection


LEDGER = ".pseudolife-mcp/digests/ledger.log"


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def test_delivery_clock_policy_rejects_outside_window_and_retains_other_bytes():
    epoch = 1791242386
    clock = time.strftime("%H:%M:%S", time.localtime(epoch)).encode("ascii")
    response = {"exit_code": 0, "stdout_b64": encoded(b"peer\n"),
                "stderr_b64": encoded(PREFIX + clock + b" (rung anyone, watermark 12, 0 s after arming):\n"),
                "post_files_b64": {LEDGER: encoded(str(epoch).encode() + b"\twait\taaaaaaaa\t12\t5\trung anyone\n")}}
    projected, instance = delivery_projection(response, [epoch, epoch + 0.5], LEDGER)
    assert instance["raw_epoch"] == epoch
    assert projected["stdout_b64"] == response["stdout_b64"]
    with pytest.raises(ValueError, match="outside"):
        delivery_projection(response, [epoch + 2, epoch + 2.5], LEDGER)
    for raw_epoch in (str(epoch + 2).encode(), b"0" + str(epoch).encode(), b"+" + str(epoch).encode()):
        changed = copy.deepcopy(response)
        changed["post_files_b64"][LEDGER] = encoded(raw_epoch + b"\twait\taaaaaaaa\t12\t5\trung anyone\n")
        with pytest.raises(ValueError, match="ledger clock"):
            delivery_projection(changed, [epoch, epoch + 0.5], LEDGER)
    for field in ("stdout_b64", "stderr_b64"):
        changed = copy.deepcopy(response)
        changed[field] = encoded(base64.b64decode(changed[field]) + b"\x00")
        assert delivery_projection(changed, [epoch, epoch + 0.5], LEDGER)[0] != projected
    changed = copy.deepcopy(response)
    changed["post_files_b64"][LEDGER] = encoded(base64.b64decode(response["post_files_b64"][LEDGER]).replace(b"\t12\t", b"\t13\t"))
    assert delivery_projection(changed, [epoch, epoch + 0.5], LEDGER)[0] != projected
    changed = copy.deepcopy(response)
    changed["post_files_b64"]["extra.dat"] = encoded(b"unexpected")
    assert delivery_projection(changed, [epoch, epoch + 0.5], LEDGER)[0] != projected


def native_commands():
    binary = os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not binary:
        if os.environ.get("PSEUDOLIFE_WAIT_MAIL_NATIVE_REQUIRED") == "1":
            pytest.fail("hosted wait-mail lane requires the selected native binary")
        pytest.skip("native binary must be selected explicitly")
    assert Path(binary).is_file()
    root = Path(os.environ.get("PSEUDOLIFE_WAIT_MAIL_ORACLE_ROOT", Path(__file__).resolve().parents[2]))
    return root, {"oracle": [sys.executable, "-m", "pseudolife_memory.cli"], "candidate": [binary]}


def capture(case, commands, arm, root, home):
    result = observe(case, commands[arm], commands, root=root, home=home,
                     url="http://127.0.0.1:29871")
    return result, result["execution"]["wall_window"]


def test_native_wait_mail_positive_delivery_and_clock_bounds(tmp_path):
    root, commands = native_commands()
    case = next(cell for cell in cases() if cell["id"] == "wait-mail-unicode-delivery")
    home = tmp_path / "home"
    projections = []
    try:
        for arm in commands:
            result, window = capture(case, commands, arm, root, home)
            response = result["response"]
            projected, instance = delivery_projection(response, window, LEDGER)
            assert instance["policy"] == "nondeterministic-bytes-semantic"
            assert base64.b64decode(response["stdout_b64"]) == "peer — café 🧠\n".encode("utf-8")
            seen = ".pseudolife-mcp/digests/" + "a" * 64 + ".seen"
            assert base64.b64decode(response["post_files_b64"][seen]) == b"12\n"
            assert not any("wait-armed" in path or ".tmp-" in path for path in response["post_files_b64"])
            assert response["post_files_b64"].keys() == result["pre_files_b64"].keys() | {LEDGER}
            projections.append(projected)
        assert projections[0] == projections[1]
    finally:
        reset_home(home)


def test_native_wait_mail_help_matches_oracle_at_columns_80(tmp_path):
    root, commands = native_commands()
    case = next(cell for cell in cases() if cell["id"] == "wait-mail-help-columns-80")
    home = tmp_path / "home"
    try:
        responses = [capture(case, commands, arm, root, home)[0]["response"] for arm in commands]
        assert responses[0] == responses[1]
        assert responses[0]["exit_code"] == 0
        assert base64.b64decode(responses[0]["stderr_b64"]) == b""
    finally:
        reset_home(home)
