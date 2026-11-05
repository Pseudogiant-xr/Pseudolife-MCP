"""A checkout rerun reuses only a complete, compatible runtime of that source."""
import json
from pathlib import Path
import sys
import subprocess

import pytest

from tests.test_shim_runtimes import FakeTools, _layout, rt


class Tools(FakeTools):
    broken_probe = False
    mismatched = False
    before_install = None

    def __call__(self, argv, **kwargs):
        if len(argv) > 3 and str(argv[3]) == getattr(rt, "_REUSE_PROBE", None):
            self.calls.append([str(a) for a in argv])
            package = Path(argv[0]).parent.parent / "lib/site-packages/pseudolife_memory"
            missing_app = not (package / "shim.py").is_file() or not (package / "cli.py").is_file()
            return (1, "runtime differs") if self.broken_probe or missing_app or self.version != argv[4] else (0, "ready\n")
        if len(argv) > 3 and str(argv[3]) == rt._REQUIREMENTS_PROBE:
            self.calls.append([str(a) for a in argv])
            return 0, json.dumps({"mismatched": ["fixture SDK mismatch"] if self.mismatched else []})
        installing = list(argv[1:4]) == ["-m", "pip", "install"] and "--no-deps" in argv
        if installing and self.before_install:
            callback, self.before_install = self.before_install, None
            callback()
        result = super().__call__(argv, **kwargs)
        if installing and result[0] == 0 and (Path(argv[-1]) / "pseudolife_memory/shim.py").is_file():
            target = Path(argv[0]).parent.parent / "lib/site-packages/pseudolife_memory/shim.py"
            target.write_bytes((Path(argv[-1]) / "pseudolife_memory/shim.py").read_bytes())
            (target.parent / "cli.py").write_text("def main(): pass\n")
        return result


@pytest.fixture(params=["windows", "posix"])
def checkout(tmp_path, request):
    source = tmp_path / "checkout"
    package = source / "pseudolife_memory"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nname="fixture"\n')
    (package / "shim.py").write_text("version = 1\n")
    return source, _layout(tmp_path, request.param), Tools(request.param)


def install(checkout, *, log=lambda line: None):
    source, layout, tools = checkout
    return rt.install(str(source), layout, python=sys.executable, run=tools, log=log)


def test_unchanged_checkout_and_dependencies_reuse_without_pip_or_venv(checkout):
    first = install(checkout)
    _, layout, tools = checkout
    tools.calls.clear()
    messages = []
    second = install(checkout, log=messages.append)
    assert second.path == first.path
    assert len(rt.list_runtimes(layout)) == 1
    assert not any(c[1:3] in (["-m", "pip"], ["-m", "venv"]) for c in tools.calls)
    assert len(messages) == 1 and "unchanged" in messages[0]


@pytest.mark.parametrize("changed", ["package", "requirements", "dependencies", "version", "ranges", "old-marker"])
def test_changed_or_unverified_inputs_build_a_new_runtime(checkout, monkeypatch, changed):
    first = install(checkout)
    source, _, tools = checkout
    if changed == "package":
        (source / "pseudolife_memory/shim.py").write_text("version = 2\n")
    elif changed == "requirements":
        monkeypatch.setattr(rt, "SHIM_REQUIREMENTS", (*rt.SHIM_REQUIREMENTS, "platformdirs>=4"))
    elif changed == "dependencies":
        tools.broken_probe = True
    elif changed == "version":
        tools.version = "0.99.0"
    elif changed == "ranges":
        tools.mismatched = True
    else:
        path = first.path / rt.MARKER
        data = json.loads(path.read_text())
        data.pop("source_fingerprint", None)
        path.write_text(json.dumps(data))
    assert install(checkout).path != first.path


def test_missing_console_script_is_never_reused(checkout):
    first = install(checkout)
    _, layout, _ = checkout
    rt._console(first.path, rt._windows(layout)).unlink()
    assert install(checkout).path != first.path


def test_a_native_launcher_stays_active_when_the_python_runtime_is_reused(checkout, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_SHIM_ENGINE", raising=False)
    first = install(checkout)
    _, layout, _ = checkout
    native = b"MZ-native-fixture" if rt._windows(layout) else b"\x7fELF\xff\x00"
    layout.launcher.write_bytes(native)
    assert install(checkout).path == first.path
    assert layout.launcher.read_bytes() == native


def test_a_checkout_changed_during_install_is_not_reused_after_a_revert(checkout):
    source, _, tools = checkout
    module = source / "pseudolife_memory/shim.py"
    tools.before_install = lambda: module.write_text("version = 2\n")
    first = install(checkout)
    module.write_text("version = 1\n")
    second = install(checkout)
    assert first.path != second.path
    assert (second.path / "lib/site-packages/pseudolife_memory/shim.py").read_text() == "version = 1\n"


@pytest.mark.parametrize("missing", [None, "cli", "shim", "package"])
def test_reuse_probe_checks_application_imports_in_an_isolated_interpreter(tmp_path, missing):
    for name in ("mcp", "httpx", "anyio", "pydantic", "yaml", "psycopg", "pgvector"):
        (tmp_path / f"{name}.py").write_text("# fixture dependency\n")
    metadata = tmp_path / "pseudolife_mcp-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: pseudolife-mcp\nVersion: 1.0\n")
    if missing != "package":
        app = tmp_path / "pseudolife_memory"
        app.mkdir()
        (app / "__init__.py").write_text("")
        for name, function in (("cli", "main"), ("shim", "run_shim")):
            if name != missing:
                (app / f"{name}.py").write_text(f"def {function}(): pass\n")
    code = ("import sys\nfrom pathlib import Path\n"
            "fixture=sys.argv.pop(1)\nbase=Path(sys.base_prefix).resolve()\n"
            "sys.path=[fixture]+[p for p in sys.path if p and Path(p).resolve().is_relative_to(base) and 'site-packages' not in p.lower()]\n"
            "sys.meta_path=[f for f in sys.meta_path if getattr(f,'__module__',type(f).__module__) in ('_frozen_importlib','_frozen_importlib_external')]\n"
            + rt._REUSE_PROBE)
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path), "1.0"],
                          capture_output=True, text=True, timeout=30)
    assert (proc.returncode == 0) == (missing is None), proc.stdout + proc.stderr
