"""Positive measurement admits only exact delivery and bounded clock fields."""
import base64
import copy
import time

import pytest

from evals.rust_port.wait_mail_measurement import measurement_case, project_invocation
from evals.rust_port.wait_mail_policy import LEDGER, PREFIX


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def invocation(platform):
    case = measurement_case()
    before = {".pseudolife-mcp/token": encoded(b"owned"), **case["pre_files_b64"]}
    after = copy.deepcopy(before)
    epoch = 1791242386
    clock = time.strftime("%H:%M:%S", time.localtime(epoch)).encode("ascii")
    newline = b"\r\n" if platform == "windows" else b"\n"
    body = "peer — café 🧠\n".encode("utf-8")
    after[".pseudolife-mcp/digests/" + "a" * 64 + ".seen"] = encoded(b"12\n")
    after[LEDGER] = encoded(str(epoch).encode() + b"\twait\taaaaaaaa\t12\t14\trung anyone" + newline)
    response = {"exit_code": 0, "stdout_b64": encoded(body),
                "stderr_b64": encoded(PREFIX + clock + b" (rung anyone, watermark 12, 0 s after arming):" + newline)}
    return case, response, before, after, [epoch, epoch + 0.5], platform


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_delivery_measurement_validates_each_raw_invocation(platform):
    cell = invocation(platform)
    original = copy.deepcopy(cell)
    projected, policy = project_invocation(*cell)
    assert policy["fields"] == ["stderr delivery HH:MM:SS", "ledger column 1"]
    assert projected["exit_code"] == 0
    assert cell == original
    for field in ("exit_code", "stdout_b64", "stderr_b64"):
        changed = copy.deepcopy(cell)
        response = changed[1]
        response[field] = 2 if field == "exit_code" else encoded(base64.b64decode(response[field]) + b"\x00")
        with pytest.raises(ValueError):
            project_invocation(*changed)
    for path in (LEDGER, ".pseudolife-mcp/digests/" + "a" * 64 + ".seen", ".pseudolife-mcp/token"):
        changed = copy.deepcopy(cell)
        changed[3][path] = encoded(base64.b64decode(changed[3][path]) + b"\x00")
        with pytest.raises(ValueError):
            project_invocation(*changed)
    changed = copy.deepcopy(cell)
    changed[0]["id"] = "unlisted-measurement"
    with pytest.raises(ValueError):
        project_invocation(*changed)
    changed = copy.deepcopy(cell)
    changed[2][LEDGER] = encoded(b"old ledger\n")
    with pytest.raises(ValueError):
        project_invocation(*changed)
    changed = copy.deepcopy(cell)
    changed[4][:] = [1791242390, 1791242390.5]
    with pytest.raises(ValueError):
        project_invocation(*changed)

