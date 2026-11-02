"""A child re-run of this package imports the package its parent ran, never
a ``pseudolife_memory`` sitting in the working directory (an older
checkout): ``conductor-watch --detach`` from inside one failed with
"unknown mode 'conductor-watch'"."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import pseudolife_memory
from pseudolife_memory import child_python

PACKAGE = Path(pseudolife_memory.__file__).resolve().parent

needs_safe_path = pytest.mark.skipif(sys.version_info < (3, 11),
                                     reason="-P needs Python 3.11; 3.10 keeps plain -m")


def _decoy(root: Path) -> Path:
    """A stale checkout: a regular package (a namespace directory would lose
    to the real package and prove nothing) whose import is unmistakable."""
    package = root / "pseudolife_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import sys\n__version__ = '0.0.0-decoy'\n"
        "print('DECOY ' + __file__)\nsys.stdout.flush()\nsys.exit(97)\n", encoding="utf-8")
    (package / "cli.py").write_text("print('DECOY CLI')\n", encoding="utf-8")
    return root


def _run(argv: list[str], cwd: Path, *path: Path) -> subprocess.CompletedProcess:
    """Run ``argv`` from ``cwd`` with ``path`` first on its import path, as
    an installed runtime's site-packages is: this test process may have found
    the package through an entry of its own (pytest's start directory) that
    no child inherits."""
    existing = os.environ.get("PYTHONPATH")
    entries = [str(p) for p in path] + ([existing] if existing else [])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(entries)}
    return subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


@needs_safe_path
def test_the_child_imports_the_parents_package_not_the_working_directorys(tmp_path, monkeypatch):
    checkout = _decoy(tmp_path / "old-checkout")
    probes = tmp_path / "probes"
    probes.mkdir()
    (probes / "child_python_probe.py").write_text(
        "import pseudolife_memory\nprint(pseudolife_memory.__file__)\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    argv = child_python.module_argv("child_python_probe")
    assert argv[0] == sys.executable
    proc = _run(argv, checkout, probes, PACKAGE.parent)
    assert "DECOY" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()).resolve() == PACKAGE / "__init__.py"


@needs_safe_path
def test_a_detached_cli_mode_runs_from_inside_an_older_checkout(tmp_path, monkeypatch):
    """The reported failure, end to end through the real CLI entry point."""
    checkout = _decoy(tmp_path / "old-checkout")
    monkeypatch.chdir(checkout)
    proc = _run(child_python.cli_argv("--version"), checkout, PACKAGE.parent)
    assert "DECOY" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == f"pseudolife-mcp {pseudolife_memory.__version__}"


def test_from_the_packages_own_directory_the_child_keeps_plain_dash_m(monkeypatch):
    """A checkout run with no install finds the package only through the
    working directory; there the working directory is the parent's own code
    and leaving it off would make the child fail to import."""
    monkeypatch.chdir(PACKAGE.parent)
    assert child_python.cli_argv("serve") == [sys.executable, "-m", "pseudolife_memory.cli", "serve"]


def test_python_310_keeps_plain_dash_m(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(child_python.sys, "version_info", (3, 10, 14))
    assert child_python.cli_argv("serve") == [sys.executable, "-m", "pseudolife_memory.cli", "serve"]


@needs_safe_path
def test_elsewhere_the_child_leaves_the_working_directory_off_its_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert child_python.cli_argv("serve") == [sys.executable, "-P", "-m", "pseudolife_memory.cli", "serve"]


# Spawns of this package's modules that deliberately do not go through the
# helper: the daemon container's own interpreter, and doctor's handshake,
# which runs from a fresh empty directory with the package bound on its
# PYTHONPATH (doctor_cli._bind_child_to_this_package).
_NOT_THIS_PROCESS = {"daemon_exec.py", "doctor_cli.py", "child_python.py"}
_LITERAL_SPAWN = re.compile(r"""["']-m["']\s*,\s*["']pseudolife_memory\b""")


def test_every_child_of_this_package_goes_through_the_helper():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in _NOT_THIS_PROCESS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _LITERAL_SPAWN.search(line):
                offenders.append(f"{path.relative_to(PACKAGE.parent)}:{number}: {line.strip()}")
    assert not offenders, ("start these through pseudolife_memory.child_python.cli_argv / module_argv, "
                           "or list the file with its reason:\n" + "\n".join(offenders))
