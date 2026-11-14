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


def _python_310_in_an_older_checkout(monkeypatch, tmp_path):
    """Python 3.10 with no installed launcher and no venv, run from a
    directory holding another checkout's package: the tunnel and the
    scheduled update fall back to ``python -m pseudolife_memory.cli``."""
    from pseudolife_memory import tunnel_runtime, tunnel_service
    checkout = _decoy(tmp_path / "old-checkout")
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("interpreter", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(child_python.sys, "version_info", (3, 10, 14))
    monkeypatch.setattr(child_python.sys, "executable", str(python))
    monkeypatch.setattr(child_python, "_shadow_named", False)
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(tmp_path / "no-launcher"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "rt"))
    monkeypatch.setattr(tunnel_runtime, "installed_launcher", lambda: None)
    monkeypatch.setattr(tunnel_runtime, "find_program", lambda name: None)
    monkeypatch.setattr(tunnel_runtime.sys, "base_prefix", tunnel_runtime.sys.prefix)
    monkeypatch.setattr(tunnel_runtime, "checked_path", lambda path: Path(path))
    monkeypatch.setattr(tunnel_service, "checked_path", lambda path: Path(path))


def test_python_310_saved_commands_name_no_package(monkeypatch, tmp_path, capsys):
    """A saved command (the scheduled update's, the tunnel service's) runs
    later from another working directory, not this one: naming the package
    here would point at the wrong directory. An immediate spawn afterwards
    still names it."""
    from pseudolife_memory import tunnel_service, unattended_update
    from tests.test_tunnel_runtime import _ready_profile
    _python_310_in_an_older_checkout(monkeypatch, tmp_path)
    store, profile = _ready_profile(tmp_path)
    assert unattended_update._command()[-4:] == ["-m", "pseudolife_memory.cli", "update", "--unattended"]
    unit = tunnel_service.render_service(profile, store, ["pseudolife-mcp", "tunnel", "shim"], platform="linux")
    assert "pseudolife_memory.cli" in unit
    assert capsys.readouterr().err == ""
    child_python.cli_argv("serve")
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_python_310_tunnel_service_preview_and_install_name_no_package(monkeypatch, tmp_path, capsys):
    """``tunnel service preview`` and ``tunnel service install``, through the
    CLI entry point, describe or write a command that runs later from the
    service manager's working directory: no notice, and none used up."""
    from pseudolife_memory import tunnel_cli, tunnel_service
    from tests.test_tunnel_runtime import _ready_profile
    _python_310_in_an_older_checkout(monkeypatch, tmp_path)
    store, profile = _ready_profile(tmp_path)
    where = ["--profile", profile.name, "--profile-dir", str(store.root)]
    assert tunnel_cli.main(["service", "preview", *where]) == 0
    preview = capsys.readouterr()
    assert "pseudolife_memory.cli" in preview.out
    assert preview.err == ""
    installed = []
    monkeypatch.setattr(tunnel_service, "install_service",
                        lambda profile, store, command, **kw: installed.append(command) or {"installed": True})
    assert tunnel_cli.main(["service", "install", *where, "--accept-autostart"]) == 0
    assert installed and installed[0][1:4] == ["-m", "pseudolife_memory.cli", "tunnel"]
    assert capsys.readouterr().err == ""
    child_python.cli_argv("serve")
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_python_310_a_tunnel_started_now_names_the_package_once(monkeypatch, tmp_path, capsys):
    """``tunnel start`` launches its supervisor at once from this working
    directory, so the child may import the checkout there: say so."""
    from pseudolife_memory import tunnel_runtime
    from tests.test_tunnel_runtime import _ready_profile
    _python_310_in_an_older_checkout(monkeypatch, tmp_path)
    store, profile = _ready_profile(tmp_path)
    launched = []

    class Supervisor:
        def poll(self):
            return None

    def popen(argv, **kwargs):
        launched.append(argv)
        return Supervisor()

    monkeypatch.setattr(tunnel_runtime, "status_profile", lambda *a: {"running": False, "ready": True})
    monkeypatch.setattr(tunnel_runtime, "_root_linux", lambda: False)
    monkeypatch.setattr(tunnel_runtime, "ensure_runtime", lambda *a, **k: Path("unused"))
    monkeypatch.setattr(tunnel_runtime.subprocess, "Popen", popen)
    tunnel_runtime.start_profile(profile, store, ["pseudolife-mcp", "tunnel", "shim"])
    assert launched and launched[0][1:4] == ["-m", "pseudolife_memory.cli", "tunnel"]
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and "pseudolife_memory" in lines[0]


@needs_safe_path
def test_elsewhere_the_child_leaves_the_working_directory_off_its_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert child_python.cli_argv("serve") == [sys.executable, "-P", "-m", "pseudolife_memory.cli", "serve"]


def _ours(value: str) -> bool:
    return value == "pseudolife_memory" or value.startswith("pseudolife_memory.")


def _is_dash_m(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "-m"


def _spawns_in(source: str) -> list[tuple[int, str]]:
    """``(line, enclosing function or Class.method)`` of every place in
    ``source`` that may run one of this package's modules with ``-m``:

    - a list or tuple holding ``"-m"`` followed by a string naming one of
      its modules, or by any other expression (a name, an attribute such as
      ``child_python.CLI``, an f-string, a call). A module-level string
      constant is resolved; anything else counts, since what it holds cannot
      be told from here;
    - a list or tuple ending in ``"-m"``, whose module is added later
      (``+ [module]``, ``.append(module)``);
    - the joined form, a string ``"-mpseudolife_memory..."``.
    """
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
        if isinstance(node, ast.Name) and node.id in constants:
            return _ours(constants[node.id])
        return True

    def spawns(node: ast.AST) -> bool:
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            pairs = zip(node.elts, node.elts[1:])
            return (any(_is_dash_m(a) and runs_ours(b) for a, b in pairs)
                    or _is_dash_m(node.elts[-1]))
        return (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.startswith("-m") and _ours(node.value[2:]))

    found = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, (*scope, child.name))
                continue
            if spawns(child):
                found.append((child.lineno, ".".join(scope) or "<module>"))
            visit(child, scope)

    visit(tree, ())
    return found


# The call sites that start one of this package's modules without the
# helper, each on purpose, keyed by module path under the package and
# function (Class.method for a method), with how many such places each
# holds. A new site, or one of these gone, fails the guard below.
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


def _package_spawns(root: Path) -> dict:
    """``(module path under root, function) -> ["path:line", ...]`` over
    every module under ``root``."""
    found: dict = {}
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root).as_posix()
        for number, function in _spawns_in(path.read_text(encoding="utf-8")):
            found.setdefault((module, function), []).append(f"{module}:{number}")
    return found


def test_every_child_of_this_package_goes_through_the_helper():
    found = _package_spawns(PACKAGE)
    unexpected = {key: where for key, where in found.items() if len(where) != _EXEMPT.get(key)}
    assert not unexpected, ("start these through pseudolife_memory.child_python.cli_argv / module_argv, "
                            f"or exempt the call site with its reason: {unexpected}")
    assert set(found) == set(_EXEMPT), f"exempt call sites that no longer exist: {set(_EXEMPT) - set(found)}"


def test_the_guard_keys_each_module_by_its_path_in_the_package(tmp_path):
    """Several modules share a name (coordination.py, __init__.py); each
    keeps its own count."""
    spawn = 'def start(py):\n    return [py, "-m", "pseudolife_memory.cli"]\n'
    for part in ("storage", "web"):
        (tmp_path / part).mkdir()
        (tmp_path / part / "coordination.py").write_text(spawn, encoding="utf-8")
    counts = {key: len(where) for key, where in _package_spawns(tmp_path).items()}
    assert counts == {("storage/coordination.py", "start"): 1, ("web/coordination.py", "start"): 1}


def test_the_guard_tells_same_named_methods_apart():
    source = ('class A:\n    def start(self, py):\n        return [py, "-m", "pseudolife_memory.cli"]\n'
              'class B:\n    def start(self, py):\n        return [py, "-m", "pseudolife_memory.cli"]\n')
    assert [function for _, function in _spawns_in(source)] == ["A.start", "B.start"]


def test_the_guard_sees_a_module_named_by_any_expression():
    cases = {
        "an attribute": ('import sys\nfrom pseudolife_memory import child_python\n'
                         'def start():\n    return [sys.executable, "-m", child_python.CLI]\n'),
        "an f-string": 'def start(py, mode):\n    return [py, "-m", f"pseudolife_memory.{mode}"]\n',
        "a call": 'def start(py):\n    return [py, "-m", module_name()]\n',
        "a concatenation": 'def start(py, mod):\n    return [py, "-m"] + [mod]\n',
        "an append": 'def start(py, mod):\n    argv = [py, "-m"]\n    argv.append(mod)\n    return argv\n',
        "the joined form": 'def start(py):\n    return [py, "-mpseudolife_memory.cli"]\n',
    }
    for case, source in cases.items():
        assert [function for _, function in _spawns_in(source)] == ["start"], case


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
