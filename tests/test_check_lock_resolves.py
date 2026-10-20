"""ops/check_lock_resolves.py and CI's lock-resolve job, offline.

The check replays the daemon image's constrained install as a pip dry run so
a self-contradicting ops/requirements.lock.txt fails on its pull request
(Dependabot's #875 was green everywhere and would have broken the image
build). These tests pin the command it builds, its refusal off the image's
Python, its failure message, and the CI job that runs it; the real resolve
needs the network and runs in that job.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ops import check_lock_resolves as check

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "ops" / "Dockerfile.daemon").read_text(encoding="utf-8")
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

CONFLICT_OUTPUT = """\
Collecting aiohttp==3.14.4
  Downloading aiohttp-3.14.4-cp312-cp312-manylinux_2_17_x86_64.whl.metadata (8.1 kB)
INFO: pip is looking at multiple versions of aiohttp to determine which version is compatible with other requirements. This could take a while.
ERROR: Cannot install kubernetes because these package versions have conflicting dependencies.

The conflict is caused by:
    aiohttp 3.14.4 depends on yarl<2.0 and >=1.25.1
    The user requested (constraint) yarl==1.24.2

To fix this you could try to:
1. loosen the range of package versions you've specified

ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/#dealing-with-dependency-conflicts
"""


@pytest.fixture
def on_image_python(monkeypatch):
    """Pretend to be the image's interpreter, and record pip instead of running it."""
    monkeypatch.setattr(check, "running_python", lambda: ("linux", check.image_python(DOCKERFILE)))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_run.result

    fake_run.result = subprocess.CompletedProcess([], 0, "Would install aiohttp-3.14.3 yarl-1.24.2\n", "")
    monkeypatch.setattr(check.subprocess, "run", fake_run)
    return SimpleNamespace(calls=calls, run=fake_run)


def test_the_dry_run_is_the_image_install_plus_only_the_documented_flags(on_image_python, capsys):
    assert check.main([]) == 0
    assert on_image_python.calls == [[
        sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
        "--disable-pip-version-check", "--extra-index-url", "https://download.pytorch.org/whl/cpu",
        "-c", str(ROOT / "ops" / "requirements.lock.txt"),
        *PROJECT["dependencies"], *PROJECT["optional-dependencies"]["onnx"], "setuptools==83.0.0",
    ]]
    assert "resolves the way the daemon image installs it (2 packages" in capsys.readouterr().out


def test_the_extra_index_is_the_one_the_image_installs_torch_from():
    assert 'pip install --index-url https://download.pytorch.org/whl/cpu "torch==' in DOCKERFILE
    assert check.torch_index(DOCKERFILE) == "https://download.pytorch.org/whl/cpu"


def test_a_conflicting_lock_fails_with_only_pips_conflict_block(on_image_python, capsys):
    on_image_python.run.result = subprocess.CompletedProcess([], 1, "", CONFLICT_OUTPUT)
    assert check.main([]) == 1
    err = capsys.readouterr().err
    assert "aiohttp 3.14.4 depends on yarl<2.0 and >=1.25.1" in err
    assert "The user requested (constraint) yarl==1.24.2" in err
    assert "ERROR: ResolutionImpossible" in err
    assert "Collecting" not in err and "To fix this" not in err
    assert "daemon image build would fail" in err


def test_a_failure_without_a_conflict_block_shows_pips_last_lines(on_image_python, capsys):
    on_image_python.run.result = subprocess.CompletedProcess(
        [], 1, "", "ERROR: Could not find a version that satisfies the requirement torch==9.9.9+cpu\n")
    assert check.main([]) == 1
    assert "torch==9.9.9+cpu" in capsys.readouterr().err


@pytest.mark.parametrize("platform, version", [("linux", "3.11"), ("linux", "3.13"),
                                               ("win32", None), ("darwin", None)])
def test_it_refuses_off_the_images_python_without_running_pip(monkeypatch, capsys, platform, version):
    image = check.image_python(DOCKERFILE)
    monkeypatch.setattr(check, "running_python", lambda: (platform, version or image))
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: pytest.fail("pip ran"))
    assert check.main([]) == 2
    err = capsys.readouterr().err
    assert "Nothing was checked" in err
    assert f"python:{check.image_python(DOCKERFILE)}-slim python ops/check_lock_resolves.py" in err


def test_a_dockerfile_whose_install_moved_fails_loudly(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "Dockerfile.daemon").write_text("FROM python:3.12-slim\nRUN pip install x\n")
    with pytest.raises(SystemExit, match="step 2"):
        check.dry_run_command((tmp_path / "ops" / "Dockerfile.daemon").read_text(), tmp_path)


def test_ci_runs_it_unscoped_on_the_images_python():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    job = jobs["lock-resolve"]
    assert "if" not in job and "needs" not in job, "a scoped or dependent job can be skipped"
    assert job["runs-on"] == "ubuntu-latest"
    setup = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert str(setup["with"]["python-version"]) == check.image_python(DOCKERFILE)
    assert any(s.get("run") == "python ops/check_lock_resolves.py" for s in job["steps"])
