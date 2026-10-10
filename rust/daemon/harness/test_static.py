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


@pytest.mark.parametrize("kind,cache", [(None, "no-store"), ("image/webp", "max-age=86400")])
def test_platform_golden_checks_mime_and_cache_without_changing_bytes(kind, cache):
    c = {"path": "/ui/assets/logo.webp"}
    response = {"status": 200, "bytes": "exact", "headers": {
        "content-type": "image/webp", "cache-control": "max-age=86400", "content-length": "5"}}
    with patch("pseudolife_memory.web.api.mimetypes.guess_type", return_value=(kind, None)):
        run.platform_static_type(c, response)
    assert response["headers"]["content-type"] == (kind or "application/octet-stream")
    assert response["headers"]["cache-control"] == cache
    assert response["bytes"] == "exact"
    assert response["headers"]["content-length"] == "5"
    wrong = {**response, "headers": {**response["headers"], "cache-control": "wrong"}}
    assert run.compare_case({**c, "name": "mapping", "method": "GET", "declared": None}, response, wrong)["diffs"]
