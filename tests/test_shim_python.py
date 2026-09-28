"""Contracts for ``ops/shim_python.py``, the interpreter picker behind the
extractor shims' autostart scripts.

``evals/claude_shim.py`` and ``evals/codex_shim.py`` import the dream system
prompt from ``pseudolife_memory``, which pulls in the package's dependencies
(torch among them). On a Docker-tier Linux host with no checkout ``.venv``
the autostart scripts fell back to a bare ``python3`` and registered a unit
that exited 1 in a restart loop (Debian 13, 2026-09-29). The picker tries
the interpreters that can hold the package, in a fixed order, verifies each
the way the unit will use it, says which one it chose and why, and refuses
with the fix when none qualifies.

The verification itself is exercised for real (this interpreter passes; a
fresh venv without the package fails); the ordering tests stub the probe so
they do not import torch once per candidate.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("shim_python", ROOT / "ops" / "shim_python.py")
shim_python = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shim_python)


@pytest.fixture(scope="session")
def bare_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real interpreter that cannot import the package: a venv without pip."""
    root = tmp_path_factory.mktemp("bare-venv")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)],
                   check=True, capture_output=True, timeout=120)
    return shim_python.venv_python(root)


def _touch_python(root: Path) -> Path:
    py = shim_python.venv_python(root)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    py.chmod(0o755)
    return py


def _touch_path_python(bin_dir: Path, name: str = "python3") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / (name + (".exe" if os.name == "nt" else ""))
    py.write_text("", encoding="utf-8")
    py.chmod(0o755)
    return py


def _abs(path: Path) -> Path:
    """Absolute WITHOUT following symlinks: a venv's interpreter is only the
    venv's when it is invoked by its own path."""
    return Path(os.path.abspath(path))


def _stub_probe(monkeypatch: pytest.MonkeyPatch, good: set[Path]) -> list[Path]:
    """Replace the import probe: ``good`` interpreters pass, the rest fail
    the way a bare python does. Compared by the path as invoked, never the
    symlink target. Returns the list of interpreters asked."""
    asked: list[Path] = []

    def probe(python: Path, repo: Path) -> tuple[bool, str]:
        asked.append(_abs(python))
        if _abs(python) in {_abs(p) for p in good}:
            return True, ""
        return False, "ModuleNotFoundError: No module named 'torch'"

    monkeypatch.setattr(shim_python, "probe", probe)
    return asked


def _symlink_python(root: Path, target: Path) -> Path:
    """A venv interpreter the way a POSIX venv lays it out: a symlink to the
    base interpreter. Skips where this account cannot make symlinks."""
    py = shim_python.venv_python(root)
    py.parent.mkdir(parents=True, exist_ok=True)
    try:
        py.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    return py


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """An empty PATH, no pipx home and a checkout without a .venv."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return {"repo": repo, "bin": bin_dir, "venv_dir": tmp_path / "shim-venv"}


def _run(capsys: pytest.CaptureFixture[str], *args: str | Path) -> tuple[int, str, str]:
    code = shim_python.main([str(a) for a in args])
    out, err = capsys.readouterr()
    return code, out, err


# -- the probe itself, for real ----------------------------------------------------

def test_the_probe_accepts_an_interpreter_that_imports_the_package() -> None:
    ok, reason = shim_python.probe(Path(sys.executable), ROOT)
    assert ok, reason


def test_the_probe_rejects_a_venv_without_the_package(bare_python: Path) -> None:
    """The shim adds the checkout to sys.path itself, so a bare interpreter
    imports the *package* fine and fails on its dependencies; the probe must
    import what the shims import, with the checkout on the path, or a bare
    python3 passes and the unit crash-loops."""
    ok, reason = shim_python.probe(bare_python, ROOT)
    assert not ok
    assert "No module named" in reason


# -- an explicit interpreter is verified, never replaced -----------------------------

def test_an_explicit_interpreter_that_imports_is_used_as_named(
        capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--repo", ROOT, "--python", sys.executable)
    assert code == 0, err
    assert Path(out.strip()) == Path(sys.executable)
    assert "shim interpreter:" in err and "--python" in err


def test_an_explicit_interpreter_that_cannot_import_is_refused_not_replaced(
        capsys: pytest.CaptureFixture[str], bare_python: Path) -> None:
    code, out, err = _run(capsys, "--repo", ROOT, "--python", bare_python)
    assert code == 1
    assert out == ""
    assert str(bare_python) in err
    assert "No module named" in err
    assert "pseudolife_memory" in err and "--python" in err
    assert "shim interpreter:" not in err


def test_an_explicit_interpreter_that_does_not_exist_is_refused(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "python"
    code, out, err = _run(capsys, "--repo", ROOT, "--python", missing)
    assert code == 1
    assert out == ""
    assert str(missing) in err and "not found" in err


# -- the search order -----------------------------------------------------------------

def test_the_checkouts_venv_comes_first(monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
                                       capsys: pytest.CaptureFixture[str]) -> None:
    repo_venv = _touch_python(isolated["repo"] / ".venv")
    monkeypatch.setenv("PIPX_HOME", str(isolated["repo"].parent / "pipx"))
    pipx_venv = _touch_python(Path(os.environ["PIPX_HOME"]) / "venvs" / "pseudolife-mcp")
    path_python = _touch_path_python(isolated["bin"])
    asked = _stub_probe(monkeypatch, {repo_venv, pipx_venv, path_python})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 0, err
    assert Path(out.strip()) == repo_venv
    assert asked == [_abs(repo_venv)]
    assert "checkout" in err and ".venv" in err


def test_pipxs_venv_when_the_checkout_has_none(monkeypatch: pytest.MonkeyPatch,
                                              isolated: dict[str, Path],
                                              capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PIPX_HOME", str(isolated["repo"].parent / "pipx"))
    pipx_venv = _touch_python(Path(os.environ["PIPX_HOME"]) / "venvs" / "pseudolife-mcp")
    path_python = _touch_path_python(isolated["bin"])
    asked = _stub_probe(monkeypatch, {pipx_venv, path_python})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 0, err
    assert Path(out.strip()) == pipx_venv
    assert asked == [_abs(pipx_venv)]
    assert "pipx" in err


def test_a_venv_this_helper_made_earlier_is_reused_before_path_pythons(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    shim_venv = _touch_python(isolated["venv_dir"])
    path_python = _touch_path_python(isolated["bin"])
    asked = _stub_probe(monkeypatch, {shim_venv, path_python})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 0, err
    assert Path(out.strip()) == shim_venv
    assert asked == [_abs(shim_venv)]


def test_a_path_python_that_imports_the_package_is_used(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    """``pip install --user <checkout>`` (the installer's path when pipx is
    absent) puts the package where the PATH python3 imports it."""
    path_python = _touch_path_python(isolated["bin"])
    asked = _stub_probe(monkeypatch, {path_python})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 0, err
    assert Path(out.strip()) == _abs(path_python)
    assert asked == [_abs(path_python)]
    assert "PATH" in err


def test_a_path_python_that_cannot_import_is_skipped_with_its_reason(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    path_python = _touch_path_python(isolated["bin"])
    shim_venv = _touch_python(isolated["venv_dir"] / "later")
    _stub_probe(monkeypatch, {shim_venv})
    monkeypatch.setattr(shim_python, "create_venv",
                        lambda venv_dir, repo, log: shim_venv)
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 0, err
    # normcase: on Windows shutil.which spells it python3.EXE (PATHEXT).
    assert os.path.normcase(f"skipped {_abs(path_python)}: ") in os.path.normcase(err)
    assert "No module named 'torch'" in err


def test_a_venvs_symlinked_interpreter_is_probed_and_chosen_by_its_own_path(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """A POSIX venv's ``bin/python`` is a symlink to the base interpreter,
    and only the venv's own path makes Python find the venv's packages
    (pyvenv.cfg beside it). Following the link probed pipx's venv as bare
    ``/usr/bin/python3.13`` on the Debian 13 box (2026-09-29) and skipped
    it; had it passed, the unit would have named the system interpreter."""
    base = _touch_path_python(tmp_path / "base-bin", "python3.13")
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "pipx"))
    pipx_venv = _symlink_python(tmp_path / "pipx" / "venvs" / "pseudolife-mcp", base)
    asked = _stub_probe(monkeypatch, {pipx_venv})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"],
                          "--no-create")
    assert code == 0, err
    assert Path(out.strip()) == _abs(pipx_venv)
    assert asked == [_abs(pipx_venv)]


def test_two_venvs_on_one_base_interpreter_are_both_tried(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Every venv built from one Python links to the same file, so
    de-duplicating by link target dropped all but the first."""
    base = _touch_path_python(tmp_path / "base-bin", "python3.13")
    repo_venv = _symlink_python(isolated["repo"] / ".venv", base)
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "pipx"))
    pipx_venv = _symlink_python(tmp_path / "pipx" / "venvs" / "pseudolife-mcp", base)
    asked = _stub_probe(monkeypatch, {pipx_venv})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"],
                          "--no-create")
    assert code == 0, err
    assert Path(out.strip()) == _abs(pipx_venv)
    assert asked == [_abs(repo_venv), _abs(pipx_venv)]


# -- the last resort: a venv of its own -------------------------------------------------

def test_a_venv_is_created_only_when_nothing_else_imports_the_package(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    _touch_path_python(isolated["bin"])
    created: list[tuple[Path, Path]] = []

    def create_venv(venv_dir: Path, repo: Path, log) -> Path:
        created.append((Path(venv_dir), Path(repo)))
        return _touch_python(venv_dir)

    monkeypatch.setattr(shim_python, "create_venv", create_venv)
    _stub_probe(monkeypatch, {shim_python.venv_python(isolated["venv_dir"])})
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 0, err
    assert created == [(isolated["venv_dir"], isolated["repo"])]
    assert Path(out.strip()) == shim_python.venv_python(isolated["venv_dir"])
    assert "created" in err


def test_no_create_refuses_and_names_the_command(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    _touch_path_python(isolated["bin"])
    _stub_probe(monkeypatch, set())
    monkeypatch.setattr(shim_python, "create_venv",
                        lambda *a, **k: pytest.fail("must not create under --no-create"))
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"],
                          "--no-create")
    assert code == 1
    assert out == ""
    assert "-m venv" in err and str(isolated["venv_dir"]) in err
    assert "--python" in err


def test_a_created_venv_that_still_cannot_import_is_refused(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(shim_python, "create_venv",
                        lambda venv_dir, repo, log: _touch_python(venv_dir))
    _stub_probe(monkeypatch, set())
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 1
    assert out == ""
    assert "No module named 'torch'" in err
    assert "shim interpreter:" not in err


def test_a_failed_venv_creation_is_refused_with_pips_words(
        monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    def create_venv(venv_dir: Path, repo: Path, log) -> Path | None:
        log("pip: ERROR: No matching distribution found for torch")
        return None

    monkeypatch.setattr(shim_python, "create_venv", create_venv)
    _stub_probe(monkeypatch, set())
    code, out, err = _run(capsys, "--repo", isolated["repo"], "--venv-dir", isolated["venv_dir"])
    assert code == 1
    assert out == ""
    assert "No matching distribution" in err


# -- the Windows autostart scripts run the same pick -----------------------------------

PWSH = shutil.which("pwsh")


def _ps1_block(rel: str) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "# >>> shim python >>>" in text, f"{rel} has no 'shim python' marker block"
    return text.split("# >>> shim python >>>", 1)[1].split("# <<< shim python <<<", 1)[0]


def _run_ps1_pick(rel: str, python_exe: str) -> subprocess.CompletedProcess[str]:
    """The .ps1's own block under pwsh, with the ops directory named for it
    and ``python`` on PATH resolving to this test's interpreter."""
    q = lambda s: str(s).replace("'", "''")
    script = ("$ErrorActionPreference = 'Stop'\n"
              f"$repo = '{q(ROOT)}'\n$opsDir = '{q(ROOT / 'ops')}'\n"
              f"$PythonExe = '{q(python_exe)}'\n"
              + _ps1_block(rel).replace("$PSScriptRoot", "$opsDir")
              + "\nWrite-Output \"PICKED=$PythonExe\"\n")
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # A script FILE, as the installer runs it: under `-Command -` pwsh reads
    # stdin like a console and exits 0 after a throw.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pick.ps1"
        path.write_text(script, encoding="utf-8")
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(path)],
                              capture_output=True, text=True, timeout=600, env=env, check=False)


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("rel", ["ops/install-shim-autostart.ps1",
                                 "ops/install-codex-shim-autostart.ps1"])
def test_the_ps1_pick_keeps_an_interpreter_that_imports_the_package(rel: str) -> None:
    proc = _run_ps1_pick(rel, sys.executable)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"PICKED={sys.executable}" in proc.stdout
    assert "shim interpreter:" in proc.stdout + proc.stderr


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("rel", ["ops/install-shim-autostart.ps1",
                                 "ops/install-codex-shim-autostart.ps1"])
def test_the_ps1_pick_throws_on_an_interpreter_that_cannot_import(
        rel: str, bare_python: Path) -> None:
    proc = _run_ps1_pick(rel, str(bare_python))
    assert proc.returncode != 0
    assert "PICKED=" not in proc.stdout
    assert "shim autostart not registered" in proc.stdout + proc.stderr
    assert "No module named" in proc.stdout + proc.stderr


def test_a_relative_explicit_interpreter_is_made_absolute(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe runs from the checkout and the unit from its own directory,
    so a relative --python was checked in one place and run in another
    (review finding, 2026-09-29): it is made absolute against the caller's
    directory before anything else."""
    here = Path(sys.executable).parent
    monkeypatch.chdir(here)
    code, out, err = _run(capsys, "--repo", ROOT, "--python", Path(sys.executable).name)
    assert code == 0, err
    assert Path(out.strip()).is_absolute()
    assert Path(out.strip()) == Path(sys.executable)
