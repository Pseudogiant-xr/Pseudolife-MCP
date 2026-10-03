"""A selected production tree cannot replace the measured instrument."""
import hashlib
import json
from pathlib import Path
import subprocess

from evals.rust_port import provenance
from evals.rust_port.harness import isolated_env


def stale_tree(root):
    package = root / "evals" / "rust_port"
    package.mkdir(parents=True)
    (root / "evals" / "__init__.py").write_text("STALE = True\n")
    (package / "__init__.py").write_text("STALE = True\n")
    for name in ("oracle", "fixtures", "harness", "pytest_plugin", "processes"):
        (package / (name + ".py")).write_text("print('stale-instrument'); raise SystemExit(91)\n")
    production = root / "pseudolife_memory"
    production.mkdir()
    (production / "__init__.py").write_text("SELECTED = True\n")
    return package


def test_oracle_child_ignores_conflicting_stale_instrument(tmp_path):
    stale_tree(tmp_path)
    result = subprocess.run(provenance.module_command("evals.rust_port.oracle", tmp_path, ["--help"]),
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "stale-instrument" not in result.stdout
    assert "--oracle-root" in result.stdout


def test_plugin_and_helper_paths_hashes_are_current_but_production_is_selected(tmp_path):
    stale_tree(tmp_path)
    probe = tmp_path / "instrument_probe.py"
    probe.write_text(
        "import hashlib,json,pseudolife_memory; "
        "from pathlib import Path; "
        "from evals.rust_port import pytest_plugin,harness,fixtures,processes; "
        "print(json.dumps({'production':pseudolife_memory.SELECTED,"
        "'modules':{m.__name__:{'path':m.__file__,'sha256':hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()} "
        "for m in (pytest_plugin,harness,fixtures,processes)}}))\n")
    result = subprocess.run(provenance.module_command("instrument_probe", tmp_path),
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    observed = json.loads(result.stdout)
    assert observed["production"] is True
    for name, detail in observed["modules"].items():
        path = Path(provenance.__file__).parent / (name.rsplit(".", 1)[1] + ".py")
        assert Path(detail["path"]).resolve() == path.resolve()
        assert detail["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_pytest_loads_current_external_plugin_in_conflicting_oracle_tree(tmp_path):
    stale_tree(tmp_path)
    test = tmp_path / "test_instrument_probe.py"
    test.write_text(
        "from pathlib import Path\n"
        "def test_actual_plugin_and_helper():\n"
        "    from evals.rust_port import pytest_plugin,harness\n"
        f"    expected=Path({str(Path(provenance.__file__).parent)!r})\n"
        "    assert Path(pytest_plugin.__file__).resolve()==expected/'pytest_plugin.py'\n"
        "    assert Path(harness.__file__).resolve()==expected/'harness.py'\n")
    result = subprocess.run(provenance.module_command("pytest", tmp_path, [
        "-p", "evals.rust_port.pytest_plugin", str(test), "-q", "--confcutdir", str(tmp_path)]),
        cwd=tmp_path, env=isolated_env(tmp_path / "home"), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert "1 passed" in result.stdout
