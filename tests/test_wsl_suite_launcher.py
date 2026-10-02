"""ops/wsl-suite.sh's guard for runs dispatched to a second machine.

ops/remote-suite.ps1 runs a commit's own ops/wsl-suite.sh on another
machine with PSEUDOLIFE_SUITE_DISPATCHED=1. On the maintainer's homelab box
127.0.0.1:5433 is the live bank's server, and a fixed
PSEUDOLIFE_TEST_DATABASE_URL covers only the fixtures: default paths (the
admin URL, the dev-server probe) still go to 5433 (review of #528,
2026-10-03). So a dispatched run starts only with PSEUDOLIFE_TEST_PG_HOST_PORT
naming another server, and only under that machine's own suite lease
(full-suite@<host>), or the first machine's gates would hold off for it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.ops_harness import BASH, hermetic_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "wsl-suite.sh"
REFUSAL = "wsl-suite: a dispatched run"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


def _run(tmp_path: Path, *, lease_file: str | None = None, **env: str | None):
    home = tmp_path / "home"
    (home / ".pseudolife-mcp" / "locks").mkdir(parents=True, exist_ok=True)
    if lease_file is not None:
        (home / ".pseudolife-mcp" / "locks" / "full-suite.lease").write_text(
            lease_file, encoding="utf-8")
    base = {"HOME": str(home), "PSEUDOLIFE_TEST_PG_HOST_PORT": None,
            "PSEUDOLIFE_TEST_DATABASE_URL": None, "PSEUDOLIFE_SUITE_LEASE": None,
            "PSEUDOLIFE_SUITE_DISPATCHED": None,
            # Past the guard the script must stop cheaply: a commit with no
            # repository behind it fails at the mirror, before any venv.
            "PSEUDOLIFE_SUITE_COMMIT": "0" * 40,
            "PSEUDOLIFE_SUITE_GIT_COMMON": str(tmp_path / "no-such-repo")}
    base.update(env)
    proc = subprocess.run([BASH, str(SCRIPT), "--version"], capture_output=True,
                          text=True, timeout=120, env=hermetic_env(**base))
    return proc


@pytest.mark.parametrize("env", [
    {"PSEUDOLIFE_TEST_DATABASE_URL": "postgresql://u:p@127.0.0.1:5434/fixed"},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:5433"},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "localhost:5433"},
    # pg_defaults strips the value and int()s the port, so both reach 5433
    # (re-review of #528, 2026-10-03): the guard compares the number.
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:05433"},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:5433 "},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:"},
    {},
], ids=["url-only", "default-port", "default-port-by-name", "zero-padded-port",
        "trailing-space", "no-port", "nothing"])
def test_a_dispatched_run_needs_a_test_server_off_5433(tmp_path, env):
    proc = _run(tmp_path, lease_file="full-suite@box\n",
                PSEUDOLIFE_SUITE_DISPATCHED="1", **env)
    assert proc.returncode == 2, proc.stderr
    assert REFUSAL in proc.stderr and "PSEUDOLIFE_TEST_PG_HOST_PORT" in proc.stderr


@pytest.mark.parametrize("lease_file, env", [
    (None, {}),
    ("full-suite\n", {}),
    ("full-suite@box\n", {"PSEUDOLIFE_SUITE_LEASE": "full-suite"}),
], ids=["no-lease-file", "plain-lease-file", "env-overrides-to-plain"])
def test_a_dispatched_run_needs_the_machines_own_lease(tmp_path, lease_file, env):
    proc = _run(tmp_path, lease_file=lease_file, PSEUDOLIFE_SUITE_DISPATCHED="1",
                PSEUDOLIFE_TEST_PG_HOST_PORT="127.0.0.1:5434", **env)
    assert proc.returncode == 2, proc.stderr
    assert REFUSAL in proc.stderr and "full-suite@" in proc.stderr


def test_a_dispatched_run_with_both_passes_the_guard(tmp_path):
    proc = _run(tmp_path, lease_file="full-suite@box\n", PSEUDOLIFE_SUITE_DISPATCHED="1",
                PSEUDOLIFE_TEST_PG_HOST_PORT="127.0.0.1:5434")
    assert REFUSAL not in proc.stderr, proc.stderr


def test_an_ordinary_run_has_no_such_guard(tmp_path):
    proc = _run(tmp_path)
    assert REFUSAL not in proc.stderr, proc.stderr
