"""A child re-run of this package imports the package its parent ran, never
a ``pseudolife_memory`` sitting in the working directory (an older
checkout): ``conductor-watch --detach`` from inside one failed with
"unknown mode 'conductor-watch'"."""

from __future__ import annotations

import ast
import os
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


def test_python_310_names_a_package_in_the_working_directory_once(monkeypatch, tmp_path, capsys):
    """3.10 cannot leave the working directory off the child's path, so a
    ``pseudolife_memory`` there is named once, with the fix."""
    checkout = _decoy(tmp_path / "old-checkout")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(child_python.sys, "version_info", (3, 10, 14))
    monkeypatch.setattr(child_python, "_shadow_named", False)
    assert child_python.cli_argv("serve") == [sys.executable, "-m", "pseudolife_memory.cli", "serve"]
    child_python.cli_argv("serve")
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert str(checkout / "pseudolife_memory") in lines[0]
    assert "Python 3.10" in lines[0] and "run it from another directory" in lines[0]


def test_python_310_says_nothing_without_a_package_in_the_working_directory(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(child_python.sys, "version_info", (3, 10, 14))
    monkeypatch.setattr(child_python, "_shadow_named", False)
    (tmp_path / "pseudolife_memory.txt").write_text("not a package", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    child_python.cli_argv("serve")
    # The package's own directory holds this process's package: nothing to name.
    monkeypatch.chdir(PACKAGE.parent)
    child_python.cli_argv("serve")
    assert capsys.readouterr().err == ""


@needs_safe_path
def test_elsewhere_the_child_leaves_the_working_directory_off_its_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert child_python.cli_argv("serve") == [sys.executable, "-P", "-m", "pseudolife_memory.cli", "serve"]


def _ours(value: str) -> bool:
    return value == "pseudolife_memory" or value.startswith("pseudolife_memory.")


def _spawns_in(source: str) -> list[tuple[int, str]]:
    """``(line, enclosing function)`` of every list or tuple in ``source``
    that runs one of this package's modules with ``-m``: ``"-m"`` followed by
    a string naming one, or by a name. A module-level string constant is
    resolved; any other name counts, since what it holds cannot be told."""
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else [])
        value = getattr(node, "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants.update({t.id: value.value for t in targets if isinstance(t, ast.Name)})

    def runs_ours(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and _ours(node.value)
        if isinstance(node, ast.Name):
            return _ours(constants[node.id]) if node.id in constants else True
        return False

    found = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, (ast.List, ast.Tuple)):
                pairs = zip(child.elts, child.elts[1:])
                if any(isinstance(a, ast.Constant) and a.value == "-m" and runs_ours(b) for a, b in pairs):
                    found.append((child.lineno, inner))
            visit(child, inner)

    visit(tree, "<module>")
    return found


# The call sites that start one of this package's modules without the
# helper, each on purpose, with how many such lists each holds. A new site,
# or one of these gone, fails the guard below.
_EXEMPT = {
    # The helper itself: plain -m and -P -m.
    ("child_python.py", "module_argv"): 2,
    # The daemon container's own interpreter, not this process's.
    ("daemon_exec.py", "operator_cli"): 2,
    # The docker tier runs the container's interpreter; the local tier
    # beside it goes through the helper.
    ("maintainer_setup.py", "_host"): 1,
    # Doctor's handshake runs from a fresh empty directory with this
    # package bound on its PYTHONPATH (_bind_child_to_this_package).
    ("doctor_cli.py", "_handshake"): 1,
}


def test_every_child_of_this_package_goes_through_the_helper():
    found: dict = {}
    where: dict = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        for number, function in _spawns_in(path.read_text(encoding="utf-8")):
            key = (path.name, function)
            found[key] = found.get(key, 0) + 1
            where.setdefault(key, []).append(f"{path.relative_to(PACKAGE.parent)}:{number}")
    unexpected = {key: where[key] for key in found if found[key] != _EXEMPT.get(key)}
    assert not unexpected, ("start these through pseudolife_memory.child_python.cli_argv / module_argv, "
                            f"or exempt the call site with its reason: {unexpected}")
    assert set(found) == set(_EXEMPT), f"exempt call sites that no longer exist: {set(_EXEMPT) - set(found)}"


def test_the_guard_sees_a_spawn_split_over_lines_or_named_by_a_constant():
    split = 'import sys\ndef start():\n    return [sys.executable,\n            "-m",\n            "pseudolife_memory.cli", "serve"]\n'
    assert _spawns_in(split) == [(3, "start")]
    named = 'MODULE = "pseudolife_memory.maintainer_setup"\ndef host(py):\n    return (py, "-m", MODULE)\n'
    assert _spawns_in(named) == [(3, "host")]
    unresolved = 'def run(py, module):\n    return [py, "-m", module]\n'
    assert _spawns_in(unresolved) == [(2, "run")]


def test_the_guard_ignores_near_misses():
    # runtimes.migrated_args reads a registration's arguments; it starts nothing.
    reads = ('def migrated_args(args):\n    if len(args) >= 2 and args[0] == "-m" and args[1] in '
             '("pseudolife_memory.cli", "pseudolife_memory"):\n        return args[2:]\n')
    assert _spawns_in(reads) == []
    # credentials' repair hint is one string printed for a person to run.
    hint = 'def hint():\n    module = "-m pseudolife_memory.credentials repair"\n    return module\n'
    assert _spawns_in(hint) == []
    other = 'def start(py):\n    return [py, "-m", "pseudolife_memory_extra"]\n'
    assert _spawns_in(other) == []
    pip = 'def start(py):\n    return [py, "-m", "pip", "install", "pseudolife_memory"]\n'
    assert _spawns_in(pip) == []
