"""Doctor failure reports must retain safe, actionable recovery advice."""
import json
import sys
from unittest.mock import AsyncMock

import pytest

from pseudolife_memory import doctor_cli, shim


@pytest.mark.parametrize("failure,hint", [
    ("unreachable", "Start the intended daemon"),
    ("timeout", "--timeout"),
    ("transport", "exact registered interpreter"),
])
def test_doctor_reports_specific_safe_recovery(monkeypatch, capsys, failure, hint):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw:
                        None if failure == "unreachable" else {"status": "ok"})
    handshake = AsyncMock(side_effect=TimeoutError() if failure == "timeout"
                          else RuntimeError("fixture-secret-must-not-leak"))
    monkeypatch.setattr(doctor_cli, "_handshake", handshake)
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is False
    assert hint in report["recovery"]
    assert "fixture-secret-must-not-leak" not in output
    if failure == "unreachable":
        handshake.assert_not_called()
