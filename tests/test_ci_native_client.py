"""The Python lanes that run installer tests build the native client first.

The installers install the native `pseudolife-mcp`; their success path runs
in CI only when a real binary built from the same commit is at hand. Each
lane below builds it (a debug `pseudolife-stdio`, reusing the Rust job's
cache where one exists), smoke-runs it, and exports
PSEUDOLIFE_NATIVE_TEST_BINARY with PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED=1,
so a test that needs the binary fails rather than skips in CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
LANES = {"test": "rust", "test-lite-windows": "rust", "test-lite-macos": "native-test-macos",
         "test-windows-nightly": "rust"}
BUILD = "Build the native client the installer tests run"


def jobs(workflow: str = "ci.yml") -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize("lane", sorted(LANES))
def test_each_installer_lane_builds_the_client_before_its_tests(lane):
    steps = jobs()[lane]["steps"]
    names = [step.get("name") or step.get("uses") for step in steps]
    build = names.index(BUILD)
    # The suite step: pytest itself, or ops/ci_tests.sh, which runs it.
    first_test = next(i for i, step in enumerate(steps)
                      if "pytest" in step.get("run", "") or "ci_tests.sh" in step.get("run", ""))
    assert build < first_test, lane
    run = steps[build]["run"]
    assert steps[build]["working-directory"] == "rust"          # rust-toolchain.toml applies
    assert "cargo build --locked -p pseudolife-stdio" in run
    assert "& $binary version" in run and run.index("& $binary version") < run.index("PSEUDOLIFE_NATIVE_TEST_BINARY=")
    assert '"PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED=1"' in run
    assert run.count("$LASTEXITCODE -ne 0") >= 2, "a failed build or smoke must fail the lane"


@pytest.mark.parametrize("lane, key", sorted(LANES.items()))
def test_the_client_build_reuses_the_rust_jobs_cache_where_one_exists(lane, key):
    steps = jobs()[lane]["steps"]
    cache = next(step for step in steps if str(step.get("uses", "")).startswith("Swatinem/rust-cache@"))
    assert cache["with"]["shared-key"] == key
    assert cache["with"]["prefix-key"] == "v0-rust-${{ steps.cache_week.outputs.week }}"
    assert cache["with"]["workspaces"] == "rust"
    # Only the macOS lane saves (no Rust job runs on macOS); the others read
    # the Rust job's entry and never write one beside it.
    assert (cache["with"].get("save-if") is False) is (key == "rust")
    toolchain = next(step for step in steps if str(step.get("uses", "")).startswith("dtolnay/rust-toolchain@"))
    pinned = re.search(r'^channel = "([^"]+)"$', (ROOT / "rust/rust-toolchain.toml").read_text(encoding="utf-8"), re.M)
    assert toolchain["with"]["toolchain"] == pinned.group(1)
    assert "Keep only the pinned Rust toolchain" in [step.get("name") for step in steps]


def test_the_shared_key_is_the_rust_jobs_own():
    rust = jobs("rust.yml")["rust"]["steps"]
    cache = next(step for step in rust if str(step.get("uses", "")).startswith("Swatinem/rust-cache@"))
    # rust-cache derives the key "rust" from the job id when no shared-key is set.
    assert "shared-key" not in cache["with"] and "key" not in cache["with"]
    assert cache["with"]["prefix-key"] == "v0-rust-${{ steps.cache_week.outputs.week }}"
