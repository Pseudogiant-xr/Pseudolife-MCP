"""``ops/update.ps1`` and ``ops/update.sh`` are flag mappers over
``ops/update.py`` (pseudolife_memory/update_cli.py): every flag reaches
the Python deploy, with the checkout set, and the exit code comes back.
The deploy itself is tested in tests/test_update_cli.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ops_harness import BASH, PWSH

ROOT = Path(__file__).resolve().parents[1]

_FAKE_UPDATE = '''
import json, os, sys
with open(os.environ["FIXTURE_ARGS"], "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
sys.exit(int(os.environ.get("FIXTURE_EXIT", "0")))
'''


def _sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "ops").mkdir(parents=True)
    shutil.copyfile(ROOT / "ops" / "update.ps1", root / "ops" / "update.ps1")
    shutil.copyfile(ROOT / "ops" / "update.sh", root / "ops" / "update.sh")
    (root / "ops" / "update.sh").chmod(0o755)
    (root / "ops" / "update.py").write_text(_FAKE_UPDATE, encoding="utf-8")
    return root


def _env(tmp_path: Path, exit_code: int = 0) -> dict:
    env = os.environ.copy()
    env["FIXTURE_ARGS"] = str(tmp_path / "args.json")
    env["FIXTURE_EXIT"] = str(exit_code)
    # the wrappers pick python off PATH: the interpreter running the tests, first
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _args(tmp_path: Path) -> list[str]:
    return json.loads((tmp_path / "args.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(PWSH is None, reason="pwsh not available")
@pytest.mark.parametrize("exit_code", [0, 1])
def test_update_ps1_maps_every_flag_and_returns_the_exit_code(tmp_path, exit_code):
    root = _sandbox(tmp_path)
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "ops" / "update.ps1"), "-Tag", "pre-x", "-NoBackup",
                           "-ForceRollbackTag", "-KeepRollbacks", "5", "-KeepCacheHours", "24", "-NoCachePrune",
                           "-HealthRetries", "2", "-HealthDelayMs", "50", "-All", "-AllowDirty"],
                          capture_output=True, text=True, timeout=120, env=_env(tmp_path, exit_code))
    assert proc.returncode == exit_code, proc.stderr
    args = _args(tmp_path)
    assert args[:2] == ["--checkout", str(root)]
    for flag in ("--rollback-tag pre-x", "--no-backup", "--force-rollback-tag", "--keep-rollbacks 5",
                 "--keep-cache-hours 24", "--no-cache-prune", "--health-retries 2", "--health-delay-ms 50",
                 "--all", "--allow-dirty"):
        assert flag in " ".join(args), args


@pytest.mark.skipif(PWSH is None, reason="pwsh not available")
def test_update_ps1_defaults(tmp_path):
    root = _sandbox(tmp_path)
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "ops" / "update.ps1")],
                          capture_output=True, text=True, timeout=120, env=_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    args = " ".join(_args(tmp_path))
    assert "--keep-rollbacks 2" in args and "--health-retries 30" in args and "--health-delay-ms 1500" in args
    for absent in ("--no-backup", "--all", "--allow-dirty", "--rollback-tag", "--force-rollback-tag", "--no-cache-prune"):
        assert absent not in args


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("exit_code", [0, 1])
def test_update_sh_maps_every_flag_and_returns_the_exit_code(tmp_path, exit_code):
    root = _sandbox(tmp_path)
    env = _env(tmp_path, exit_code)
    env["HEALTH_RETRIES"], env["HEALTH_DELAY_MS"] = "2", "50"
    proc = subprocess.run([BASH, str(root / "ops" / "update.sh"), "--tag", "pre-x", "--no-backup", "--force-rollback-tag",
                           "--keep-rollbacks", "5", "--keep-cache-hours", "24", "--no-cache-prune", "--all", "--allow-dirty"],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == exit_code, proc.stderr
    args = _args(tmp_path)
    assert args[0] == "--checkout" and Path(args[1]).name == "checkout"
    for flag in ("--rollback-tag pre-x", "--no-backup", "--force-rollback-tag", "--keep-rollbacks 5",
                 "--keep-cache-hours 24", "--no-cache-prune", "--health-retries 2", "--health-delay-ms 50",
                 "--all", "--allow-dirty"):
        assert flag in " ".join(args), args


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_update_sh_rejects_an_unknown_flag_before_running_anything(tmp_path):
    root = _sandbox(tmp_path)
    proc = subprocess.run([BASH, str(root / "ops" / "update.sh"), "--bogus"], capture_output=True, text=True,
                          timeout=60, env=_env(tmp_path))
    assert proc.returncode == 2 and "unknown argument" in proc.stderr
    assert not (tmp_path / "args.json").exists()
