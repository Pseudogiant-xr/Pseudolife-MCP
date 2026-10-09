"""Safety controls for the static harness's loopback-only bind fixture."""
from pathlib import Path
import subprocess
from unittest.mock import patch
import os

import pytest

import run


@pytest.mark.parametrize("reply", [subprocess.CompletedProcess([], 0, b"false\n", b""),
                                  subprocess.CompletedProcess([], 2, b"true\n", b""),
                                  subprocess.CompletedProcess([], 0, b"", b"")])
def test_trust_bind_refuses_unsupported_binary_before_bank_or_listener(tmp_path, reply):
    with patch("subprocess.run", return_value=reply), patch.object(run.pg, "create") as create:
        with pytest.raises(RuntimeError, match="refuse before any wildcard listener"):
            run.run_scenario(run.TrustBind(), Path("not-executed"), tmp_path, "live", False)
    create.assert_not_called()


def test_production_capability_switch_does_not_replace_startup(tmp_path):
    candidate = os.environ.get("PL_STATIC_DEFAULT_BIN")
    if not candidate:
        pytest.skip("requires the explicit production candidate")
    env = run.daemons.base_env(tmp_path, {"PSEUDOLIFE_DAEMON_HARNESS_CAPABILITIES": "1"})
    result = subprocess.run([candidate], env=env, capture_output=True, timeout=10)
    # Without a DSN ordinary startup refuses; it never prints capabilities.
    assert result.returncode == 2
    assert result.stdout == b""
