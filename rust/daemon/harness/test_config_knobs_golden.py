"""Pin the recorded knob table and exercise the package-free recorder."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_config_knobs  # noqa: E402


def test_committed_config_knob_golden_matches_the_recorded_fixture():
    current = gen_config_knobs.TARGET.read_text(encoding="utf-8")
    # The controlled snapshot starts with the frozen capture. Review this
    # pin and the native config tests when intentionally updating the fixture.
    assert hashlib.sha256(current.encode("utf-8")).hexdigest() == (
        "cedbebf83ce98a2c6c224dcafd39923e5afef2963ec426b12bc255017b1361b2"
    )


def test_recorder_cold_import_without_product_package():
    from rust.harness_support.import_blocker import cold_harness_imports

    with cold_harness_imports():
        import gen_config_knobs as recorder

        assert recorder.TARGET.is_file()


@pytest.mark.parametrize("status, stdout, expected", [
    (0, "test config_io::tests::record_config_knobs ... ok\n", 0),
    (0, "running 0 tests\n", 1),
    (101, "config-knobs.json differs at line 1\n", 101),
])
def test_comparison_launcher_requires_a_real_result(monkeypatch, status, stdout, expected):
    monkeypatch.setenv("PSEUDOLIFE_DAEMON_MUTANT", "config-read-no-saved")
    monkeypatch.setenv("PL_CONFIG_KNOBS_RESERVED", "1")

    def run(argv, *, env, **kwargs):
        assert argv[1:] == ["--exact", "config_io::tests::record_config_knobs"]
        assert "PSEUDOLIFE_DAEMON_MUTANT" not in env
        assert "PL_CONFIG_KNOBS_RESERVED" not in env
        return subprocess.CompletedProcess(argv, status, stdout=stdout, stderr="")

    monkeypatch.setattr(gen_config_knobs.subprocess, "run", run)
    assert gen_config_knobs.main(["--rust-test-bin", "fixture"]) == expected
