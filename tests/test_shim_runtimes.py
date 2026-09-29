"""Side-by-side shim runtimes behind one launcher (pseudolife_memory/runtimes.py).

A shim version installs into its own runtime directory; every client
registers one launcher path that starts the newest complete runtime; a
runtime is removed only when nothing runs from it and no registration
names it. The install steps (venv, pip, the version probe, the Windows
launcher build) go through ``run_cli`` and are faked here by building the
directory shapes those tools would leave; the process table is faked the
same way. A few tests use a real virtualenv and this machine's real
process table.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtimes_under_test", ROOT / "pseudolife_memory" / "runtimes.py")
rt = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rt
SPEC.loader.exec_module(rt)

SHAPES = ["windows", "posix"]


def _layout(tmp_path: Path, shape: str) -> "rt.Layout":
    launcher = tmp_path / "bin" / ("pseudolife-mcp.exe" if shape == "windows" else "pseudolife-mcp")
    return rt.Layout(tmp_path / "runtimes", launcher)


def _scripts(runtime: Path, shape: str) -> Path:
    return runtime / ("Scripts" if shape == "windows" else "bin")


def _console_name(shape: str) -> str:
    return "pseudolife-mcp.exe" if shape == "windows" else "pseudolife-mcp"


def _python_name(shape: str) -> str:
    return "python.exe" if shape == "windows" else "python"


class FakeTools:
    """Answers ``run_cli`` by leaving behind what venv, pip and distlib
    would: a virtualenv skeleton, the package, the console script, the
    launcher bytes."""

    def __init__(self, shape: str):
        self.shape = shape
        self.calls: list[list[str]] = []
        self.version = "0.15.0"
        self.pip_no_deps = (0, "Successfully installed pseudolife-mcp")
        self.pip_deps = (0, "Successfully installed mcp")
        self.venv = (0, "")
        self.build = (0, "")

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] == "git":
            return 0, "f" * 40
        if argv[1:3] == ["-m", "venv"]:
            if self.venv[0] == 0:
                staging = Path(argv[3])
                scripts = _scripts(staging, self.shape)
                scripts.mkdir(parents=True)
                (scripts / _python_name(self.shape)).write_text("fake interpreter", encoding="utf-8")
                (staging / "pyvenv.cfg").write_text(f"home = {Path(argv[0]).parent}\n", encoding="utf-8")
            return self.venv
        runtime = Path(argv[0]).parent.parent
        if argv[1:4] == ["-m", "pip", "install"] and "--no-deps" in argv:
            if self.pip_no_deps[0] == 0:
                site = runtime / "lib" / "site-packages" / "pseudolife_memory"
                site.mkdir(parents=True, exist_ok=True)
                (site / "__init__.py").write_text(f"__version__ = '{self.version}'\n", encoding="utf-8")
                (_scripts(runtime, self.shape) / _console_name(self.shape)).write_text("console", encoding="utf-8")
            return self.pip_no_deps
        if argv[1:4] == ["-m", "pip", "install"]:
            return self.pip_deps
        if argv[1] == "-c" and "importlib.metadata" in argv[2]:
            return 0, self.version + "\n"
        if argv[1] == "-c" and "ScriptMaker" in argv[2]:
            if self.build[0] == 0:
                target, executable, body = argv[3], argv[4], argv[5]
                (Path(target) / "pseudolife-mcp.exe").write_bytes(
                    b"MZ-fake-launcher\n#!" + executable.encode() + b"\n" + Path(body).read_bytes())
            return self.build
        return 91, f"unexpected call {argv}"


@pytest.fixture(params=SHAPES)
def shape(request):
    return request.param


@pytest.fixture
def tools(shape):
    return FakeTools(shape)


def _install(layout, tools, version: str = "0.15.0", source: str = "/src/checkout"):
    tools.version = version
    return rt.install(source, layout, python=sys.executable, run=tools)


# ── install ─────────────────────────────────────────────────────────────────

def test_install_creates_a_complete_runtime_and_the_launcher(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    runtime = _install(layout, tools)
    assert runtime.path == layout.root / "000001"
    marker = json.loads((runtime.path / rt.MARKER).read_text(encoding="utf-8"))
    assert marker["version"] == "0.15.0" and marker["source"] == "/src/checkout"
    assert marker["source_commit"] == "f" * 40
    assert layout.launcher.is_file()
    assert rt.current_runtime(layout).path == runtime.path
    # the package went in with --no-deps, then the shim's own dependencies
    pip_calls = [c for c in tools.calls if c[1:4] == ["-m", "pip", "install"]]
    assert pip_calls[0][4:] == ["--no-deps", "/src/checkout"]
    assert pip_calls[1][4:] == list(rt.SHIM_REQUIREMENTS)
    if shape == "posix":
        text = layout.launcher.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh") and str(layout.root) in text
        assert os.name == "nt" or layout.launcher.stat().st_mode & stat.S_IXUSR
    else:
        assert layout.launcher.read_bytes().startswith(b"MZ")
        assert repr(str(layout.root)).encode() in layout.launcher.read_bytes()


def test_notices_name_the_launcher_by_its_path_unless_path_finds_it(tmp_path, shape, tools):
    """The installers never put the launcher directory on PATH: a bare
    `pseudolife-mcp` then finds an older pipx install, or nothing (the
    first update on a Debian host, 2026-09-29)."""
    layout = _layout(tmp_path, shape)
    assert rt.launcher_command(layout, which=lambda name: None) == "pseudolife-mcp"   # no launcher yet
    _install(layout, tools)
    assert rt.launcher_command(layout, which=lambda name: None) == str(layout.launcher)
    stale = tmp_path / "pipx-bin" / layout.launcher.name
    stale.parent.mkdir()
    stale.write_text("old pipx shim", encoding="utf-8")
    assert rt.launcher_command(layout, which=lambda name: str(stale)) == str(layout.launcher)
    assert rt.launcher_command(layout, which=lambda name: str(layout.launcher)) == "pseudolife-mcp"
    spaced = rt.Layout(layout.root, tmp_path / "with space" / layout.launcher.name)
    spaced.launcher.parent.mkdir()
    spaced.launcher.write_text("launcher", encoding="utf-8")
    assert rt.launcher_command(spaced, which=lambda name: None) == f'"{spaced.launcher}"'


def test_the_running_runtime_and_its_source_are_known(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    checkout = _install(layout, tools)
    release = _install(layout, tools, version="0.15.1", source="pseudolife-mcp==0.15.1")
    assert rt.running_runtime(layout, prefix=str(checkout.path)).path == checkout.path
    assert rt.running_runtime(layout, prefix=str(tmp_path / "elsewhere")) is None
    assert rt.from_checkout(checkout) is True
    release.source_commit = None
    assert rt.from_checkout(release) is False


def test_a_second_version_installs_beside_the_first_and_becomes_current(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    first = _install(layout, tools, "0.15.0")
    second = _install(layout, tools, "0.15.1")
    assert first.path.is_dir() and second.path == layout.root / "000002"
    assert [(r.name, r.version) for r in rt.list_runtimes(layout)] == [("000001", "0.15.0"), ("000002", "0.15.1")]
    assert rt.current_runtime(layout).path == second.path
    # a same-version reinstall (a deploy from a checkout) still lands beside
    third = _install(layout, tools, "0.15.1")
    assert third.path == layout.root / "000003" and third.version == "0.15.1"
    assert rt.current_runtime(layout).path == third.path


def test_the_launcher_is_left_alone_when_its_content_is_already_right(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    _install(layout, tools, "0.15.0")
    before = layout.launcher.read_bytes()
    assert rt.ensure_launcher(layout, run=tools) == "current"
    assert layout.launcher.read_bytes() == before
    assert not list(layout.launcher_dir.glob("pseudolife-mcp.exe.old-*"))


def test_a_changed_launcher_is_moved_aside_never_overwritten(tmp_path, shape, tools):
    """On Windows a running launcher cannot be overwritten but can be
    renamed; the same rename keeps the POSIX replacement atomic."""
    layout = _layout(tmp_path, shape)
    _install(layout, tools, "0.15.0")
    layout.launcher.write_bytes(b"an older launcher")
    if shape == "windows":
        record = layout.launcher.with_name("pseudolife-mcp.exe.json")
        record.write_text(json.dumps({"root": "elsewhere"}), encoding="utf-8")
    assert rt.ensure_launcher(layout, run=tools) == "replaced"
    assert layout.launcher.read_bytes() != b"an older launcher"
    if shape == "windows":
        aside = list(layout.launcher_dir.glob("pseudolife-mcp.exe.old-*"))
        assert len(aside) == 1 and aside[0].read_bytes() == b"an older launcher"


def test_a_failed_install_leaves_no_staging_and_keeps_the_current_runtime(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    first = _install(layout, tools, "0.15.0")
    tools.pip_no_deps = (1, "ERROR: Could not find a version that satisfies the requirement")
    with pytest.raises(rt.RuntimeInstallError) as caught:
        _install(layout, tools, "0.15.1")
    assert caught.value.step == "pip install --no-deps"
    assert "Could not find a version" in str(caught.value)
    assert sorted(p.name for p in layout.root.iterdir()) == [first.name]
    assert rt.current_runtime(layout).path == first.path


def test_a_failed_venv_or_probe_is_named(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    tools.venv = (1, "Error: Command '...' returned non-zero exit status 1")
    with pytest.raises(rt.RuntimeInstallError) as caught:
        _install(layout, tools)
    assert caught.value.step == "venv"
    assert not list(layout.root.iterdir())


def test_an_incomplete_directory_is_not_a_runtime(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    runtime = _install(layout, tools, "0.15.0")
    half = layout.root / "000002"
    _scripts(half, shape).mkdir(parents=True)
    (_scripts(half, shape) / _console_name(shape)).write_text("console", encoding="utf-8")
    assert [r.name for r in rt.list_runtimes(layout)] == [runtime.name]   # no marker
    (half / rt.MARKER).write_text("{not json", encoding="utf-8")
    assert [r.name for r in rt.list_runtimes(layout)] == [runtime.name]
    legacy = layout.root / "coordination-e41a575a"
    _scripts(legacy, shape).mkdir(parents=True)
    assert [r.name for r in rt.list_runtimes(layout)] == [runtime.name]
    # the next install counts the incomplete directory's sequence
    third = _install(layout, tools, "0.15.2")
    assert third.name == "000003"


def test_shim_requirements_match_pyproject():
    """The runtime installs the package with --no-deps and these
    requirements beside it; each must be pyproject's own line for that
    dependency, so a floor moves in both places."""
    try:
        import tomllib
    except ModuleNotFoundError:
        pytest.skip("tomllib needs Python 3.11")
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]

    def name(requirement: str) -> str:
        head = requirement.split(">")[0].split("<")[0].split("=")[0].split("[")[0]
        return head.strip().lower().replace("_", "-")

    by_name = {name(entry): entry for entry in declared}
    for requirement in rt.SHIM_REQUIREMENTS:
        assert by_name.get(name(requirement)) == requirement, (requirement, by_name.get(name(requirement)))
    assert not {"torch", "sentence-transformers", "chromadb"} & {name(r) for r in rt.SHIM_REQUIREMENTS}


# ── removal ─────────────────────────────────────────────────────────────────

def _process_in(runtime: Path, shape: str, pid: int = 4100) -> tuple[int, int, str]:
    return pid, 10, str(_scripts(runtime, shape) / _python_name(shape))


def test_an_old_runtime_survives_while_a_process_runs_from_it_and_goes_after(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    old = _install(layout, tools, "0.15.0")
    new = _install(layout, tools, "0.15.1")
    held = rt.remove_unused(layout, processes=lambda: [_process_in(old.path, shape),
                                                       (4300, 30, str(tmp_path / "elsewhere" / "python.exe"))])
    assert held["held"] == [{"path": str(old.path), "processes": 1}]
    assert held["removed"] == [] and old.path.is_dir() and new.path.is_dir()
    freed = rt.remove_unused(layout, processes=lambda: [(4300, 30, str(tmp_path / "elsewhere" / "python.exe"))])
    assert freed["removed"] == [str(old.path)] and freed["held"] == []
    assert not old.path.exists() and new.path.is_dir()
    assert rt.current_runtime(layout).path == new.path


def test_the_newest_runtime_is_never_removed_even_when_idle(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    only = _install(layout, tools, "0.15.0")
    result = rt.remove_unused(layout, processes=lambda: [])
    assert result["kept"] == [str(only.path)] and result["removed"] == []
    assert only.path.is_dir()


def test_a_runtime_a_registration_still_names_is_kept(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    old = _install(layout, tools, "0.15.0")
    _install(layout, tools, "0.15.1")
    result = rt.remove_unused(layout, pinned=[old.path], processes=lambda: [])
    assert str(old.path) in result["kept"] and old.path.is_dir()


def test_an_unreadable_process_table_removes_nothing(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    old = _install(layout, tools, "0.15.0")
    _install(layout, tools, "0.15.1")

    def unreadable():
        raise OSError("Access is denied")

    result = rt.remove_unused(layout, processes=unreadable)
    assert "Access is denied" in result["error"] and result["removed"] == []
    assert old.path.is_dir()


def test_a_platform_without_a_process_table_leaves_old_runtimes_named(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    old = _install(layout, tools, "0.15.0")
    _install(layout, tools, "0.15.1")
    result = rt.remove_unused(layout, processes=lambda: None)
    assert result["unverified"] == [str(old.path)] and old.path.is_dir()


def test_abandoned_installs_and_moved_aside_launchers_are_cleaned(tmp_path, shape, tools):
    """An unfinished runtime (no marker) is another process's install in
    progress while it is young, and a leftover once it is an hour old."""
    layout = _layout(tmp_path, shape)
    _install(layout, tools, "0.15.0")
    leftover = layout.root / "000009"
    _scripts(leftover, shape).mkdir(parents=True)
    aside = layout.launcher_dir / "pseudolife-mcp.exe.old-20260929-120000-1"
    aside.write_bytes(b"old")
    result = rt.remove_unused(layout, processes=lambda: [(5, 1, str(aside))])
    assert str(leftover) in result["kept"] and leftover.is_dir()
    assert result["held"] == [{"path": str(aside), "processes": 1}] and aside.exists()
    two_hours_ago = time.time() - 7200
    os.utime(leftover, (two_hours_ago, two_hours_ago))
    result = rt.remove_unused(layout, processes=lambda: [])
    assert sorted(result["removed"]) == sorted([str(leftover), str(aside)])
    assert not leftover.exists() and not aside.exists()


def test_the_helper_itself_never_counts_as_holding_a_runtime(tmp_path, shape, tools):
    layout = _layout(tmp_path, shape)
    old = _install(layout, tools, "0.15.0")
    _install(layout, tools, "0.15.1")
    rows = [(os.getpid(), os.getppid(), str(_scripts(old.path, shape) / _python_name(shape))),
            (os.getppid(), 1, str(_scripts(old.path, shape) / _python_name(shape)))]
    result = rt.remove_unused(layout, processes=lambda: rows)
    assert result["removed"] == [str(old.path)]


def _real_venv(path: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)],
                   check=True, capture_output=True, timeout=180)
    return path / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def _real_runtime(layout, name: str) -> Path:
    runtime = layout.root / name
    python = _real_venv(runtime)
    console = python.with_name("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    console.write_text("console", encoding="utf-8")
    (runtime / rt.MARKER).write_text(json.dumps({"version": "0.15." + name[-1]}), encoding="utf-8")
    return python


@pytest.mark.skipif(rt.list_processes() is None, reason="no process table on this platform")
def test_a_real_process_in_a_real_virtualenv_holds_its_runtime(tmp_path):
    """Nothing faked: a process started from the old runtime's own
    interpreter (what every session is) keeps it; once it exits the
    runtime goes."""
    layout = _layout(tmp_path, "windows" if os.name == "nt" else "posix")
    old_python = _real_runtime(layout, "000001")
    _real_runtime(layout, "000002")
    session = subprocess.Popen([str(old_python), "-c", "import time; time.sleep(120)"])
    try:
        time.sleep(0.5)
        held = rt.remove_unused(layout)
    finally:
        session.kill()
        session.wait(timeout=30)
    assert held["held"] == [{"path": str(old_python.parent.parent), "processes": 1}], held
    assert old_python.parent.parent.is_dir()
    for _ in range(20):   # the table lags the exit briefly
        freed = rt.remove_unused(layout)
        if freed["removed"]:
            break
        time.sleep(0.25)
    assert freed["removed"] == [str(old_python.parent.parent)], freed
    assert not old_python.parent.parent.exists()


# ── the launcher's choice ───────────────────────────────────────────────────

def test_the_windows_selector_picks_the_highest_complete_runtime(tmp_path):
    layout = _layout(tmp_path, "windows")
    for name, complete in (("000001", True), ("000003", False), ("000002", True),
                           ("coordination-e41a575a", True), ("00004", True), ("0000005", True)):
        scripts = layout.root / name / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "pseudolife-mcp.exe").write_text("console", encoding="utf-8")
        if complete:
            (layout.root / name / rt.MARKER).write_text("{}", encoding="utf-8")
    namespace: dict = {"__name__": "selector"}
    exec(compile(rt.selector_content(layout), "<selector>", "exec"), namespace)
    assert Path(namespace["_newest"]()) == layout.root / "000002" / "Scripts" / "pseudolife-mcp.exe"
    shutil.rmtree(layout.root)
    assert namespace["_newest"]() is None


def _sh_variants() -> list[str]:
    variants = []
    for candidate in (shutil.which("sh") if os.name != "nt" else None,
                      r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None):
        if candidate and Path(candidate).is_file():
            variants.append(candidate)
    return variants


def _sh_path(sh: str, path: Path) -> str:
    if os.name != "nt":
        return str(path)
    out = subprocess.run([sh, "-c", 'cygpath -u "$1"', "_", str(path)], capture_output=True,
                         text=True, check=True, timeout=30).stdout.strip()
    return out


@pytest.mark.parametrize("sh", _sh_variants(), ids=lambda p: Path(p).parent.name)
def test_the_posix_launcher_execs_the_highest_complete_runtime(tmp_path, sh):
    root = tmp_path / "runtimes"
    # the launcher text is /bin/sh's view of the root (Git Bash on Windows: a /c/... path)
    layout = rt.Layout(PurePosixPath(_sh_path(sh, root)), tmp_path / "bin" / "pseudolife-mcp")
    for name, complete in (("000001", True), ("000003", False), ("000002", True), ("00004", True)):
        console = root / name / "bin" / "pseudolife-mcp"
        console.parent.mkdir(parents=True)
        console.write_text(f'#!/bin/sh\necho "ran {name} $@"\n', encoding="utf-8")
        console.chmod(0o755)
        if complete:
            (root / name / rt.MARKER).write_text("{}", encoding="utf-8")
    assert rt.ensure_launcher(layout) == "written"
    proc = subprocess.run([sh, str(layout.launcher), "briefing", "--hook-json"], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == "ran 000002 briefing --hook-json", proc.stderr
    shutil.rmtree(root)
    proc = subprocess.run([sh, str(layout.launcher)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1 and "no complete shim runtime" in proc.stderr


@pytest.mark.skipif(os.name != "nt", reason="the Windows launcher is a console-script executable")
def test_the_real_windows_launcher_runs_the_runtime_it_chooses(tmp_path):
    """Build the launcher for real with the runtime's pip (distlib's stub)
    and run it: the fake runtime's console script is a copy of the venv's
    own python.exe, so the launcher's argument pass-through is visible."""
    layout = _layout(tmp_path, "windows")
    runtime = layout.root / "000001"
    subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True, capture_output=True, timeout=300)
    shutil.copy2(runtime / "Scripts" / "python.exe", runtime / "Scripts" / "pseudolife-mcp.exe")
    (runtime / rt.MARKER).write_text(json.dumps({"version": "0.15.0", "base_interpreter": rt.base_interpreter()}),
                                     encoding="utf-8")
    assert rt.ensure_launcher(layout) == "written"
    assert layout.launcher.read_bytes()[:2] == b"MZ"
    proc = subprocess.run([str(layout.launcher), "-c", "import sys; print('runtime', sys.prefix)"],
                          capture_output=True, text=True, timeout=120, input="")
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.split("runtime", 1)[1].strip()).resolve() == runtime.resolve()
    # a second build with the same inputs leaves the file alone; a changed
    # input (the record says what went in) rebuilds and moves the old aside
    assert rt.ensure_launcher(layout) == "current"
    record = layout.launcher.with_name("pseudolife-mcp.exe.json")
    stale = json.loads(record.read_text(encoding="utf-8"))
    stale["selector_sha256"] = "0" * 64
    record.write_text(json.dumps(stale), encoding="utf-8")
    assert rt.ensure_launcher(layout) == "replaced"
    assert len(list(layout.launcher_dir.glob("pseudolife-mcp.exe.old-*"))) == 1
    assert json.loads(record.read_text(encoding="utf-8"))["selector_sha256"] != "0" * 64


def test_the_checkout_script_runs_without_an_installed_package(tmp_path):
    launcher = tmp_path / "bin" / _console_name("windows" if os.name == "nt" else "posix")
    env = {**os.environ, "PSEUDOLIFE_SHIM_RUNTIMES": str(tmp_path / "runtimes"),
           "PSEUDOLIFE_SHIM_LAUNCHER": str(launcher)}
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "launcher"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == str(launcher)
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "launcher", "--if-installed"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 3
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "current"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 3


def test_default_layout_by_platform(tmp_path):
    local = r"X:\profile\AppData\Local"
    windows = rt.default_layout({"LOCALAPPDATA": local, "USERPROFILE": r"X:\profile"}, windows=True)
    assert windows.root == Path(local) / "pseudolife-mcp" / "runtimes"
    assert windows.launcher == Path(local) / "pseudolife-mcp" / "bin" / "pseudolife-mcp.exe"
    posix = rt.default_layout({"HOME": "/home/u"}, windows=False)
    assert posix.root == Path("/home/u/.local/share/pseudolife-mcp/runtimes")
    # not ~/.local/bin: pip --user and pipx write that file
    assert posix.launcher == Path("/home/u/.local/share/pseudolife-mcp/bin/pseudolife-mcp")
    xdg = rt.default_layout({"HOME": "/home/u", "XDG_DATA_HOME": "/data"}, windows=False)
    assert xdg.root == Path("/data/pseudolife-mcp/runtimes")
    overridden = rt.default_layout({"HOME": "/home/u", "PSEUDOLIFE_SHIM_RUNTIMES": str(tmp_path / "r"),
                                    "PSEUDOLIFE_SHIM_LAUNCHER": str(tmp_path / "l")}, windows=False)
    assert overridden == rt.Layout(tmp_path / "r", tmp_path / "l")
    # the launcher's suffix decides the runtime shape, so an override must match the platform
    with pytest.raises(ValueError):
        rt.default_layout({"PSEUDOLIFE_SHIM_RUNTIMES": str(tmp_path / "r"),
                           "PSEUDOLIFE_SHIM_LAUNCHER": str(tmp_path / "l")}, windows=True)
    with pytest.raises(ValueError):
        rt.default_layout({"PSEUDOLIFE_SHIM_RUNTIMES": str(tmp_path / "r"),
                           "PSEUDOLIFE_SHIM_LAUNCHER": str(tmp_path / "l.exe")}, windows=False)


# ── registrations ───────────────────────────────────────────────────────────

CODEX_CONFIG = """\
model = "gpt-5"

[marketplaces.pseudolife-mcp]
source_type = "git"
source = "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"

[mcp_servers.pseudolife-memory]
command = '__RUNTIME_PY__'
args = ["-m", "pseudolife_memory.cli"]
startup_timeout_sec = 240
tool_timeout_sec = 180
required = true
cwd = '__RUNTIME__'

[mcp_servers.pseudolife-memory.env]
PSEUDOLIFE_MCP_NO_SPAWN = "1"
PSEUDOLIFE_WRITER_ID = "codex"
PSEUDOLIFE_MCP_TOKEN_FILE = '__HOME__/.codex/pseudolife/token'

[mcp_servers.pseudolife-memory.tools.memory_search]
approval_mode = "approve"

[mcp_servers.other]
command = "other-tool"
args = ["--serve"]
"""


def _fixture_home(tmp_path: Path, layout, runtime: Path) -> tuple[Path, dict]:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(home / ".codex"),
           "LOCALAPPDATA": str(home / "AppData" / "Local"), "APPDATA": str(home / "AppData" / "Roaming"),
           "XDG_CONFIG_HOME": str(home / ".config")}
    (home / ".claude.json").write_text(json.dumps({
        "numStartups": 12,
        "mcpServers": {"pseudolife-memory": {
            "type": "stdio", "command": str(runtime / "Scripts" / "pseudolife-mcp.exe"), "args": [],
            "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1", "PSEUDOLIFE_AGENT_LABEL": "claude-code"}},
            "other": {"type": "stdio", "command": "other-tool", "args": []}},
        "projects": {"/x": {"allowedTools": []}},
    }, indent=2), encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        CODEX_CONFIG.replace("__RUNTIME_PY__", str(runtime / "Scripts" / "python.exe"))
        .replace("__RUNTIME__", str(runtime)).replace("__HOME__", str(home)), encoding="utf-8")
    desktop = home / "AppData" / "Roaming" / "Claude"
    desktop.mkdir(parents=True)
    (desktop / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": str(runtime / "Scripts" / "pseudolife-mcp.exe"),
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-desktop"}}}}, indent=2), encoding="utf-8")
    (home / ".config" / "Claude").mkdir(parents=True)
    (home / ".config" / "Claude" / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {
        "pseudolife-desktop": {"command": str(runtime / "bin" / "pseudolife-mcp")}}}), encoding="utf-8")
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": str(runtime / "Scripts" / "pseudolife-mcp.exe"), "cwd": str(runtime),
        "env": {"PSEUDOLIFE_WRITER_ID": "gemini"}}}}), encoding="utf-8")
    return home, env


def test_registrations_are_found_in_every_client_config(tmp_path):
    layout = _layout(tmp_path, "windows")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    found = rt.find_registrations(env, windows=True)
    assert [(r.client, r.key) for r in found] == [("claude-code", "pseudolife-memory"), ("codex", "pseudolife-memory"),
                                                  ("claude-desktop", "pseudolife-desktop"), ("gemini", "pseudolife-memory")]
    codex = found[1]
    assert codex.command == str(runtime / "Scripts" / "python.exe") and codex.args == ["-m", "pseudolife_memory.cli"]
    assert codex.cwd == str(runtime)
    assert rt.registered_runtime(codex, layout) == runtime
    posix = rt.find_registrations(env, windows=False)
    assert [(r.client, r.key) for r in posix] == [("claude-code", "pseudolife-memory"), ("codex", "pseudolife-memory"),
                                                  ("claude-desktop", "pseudolife-desktop"), ("gemini", "pseudolife-memory")]
    assert posix[2].command == str(runtime / "bin" / "pseudolife-mcp")


def test_registrations_that_name_a_runtime_path_migrate_to_the_launcher_once(tmp_path):
    layout = _layout(tmp_path, "windows")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    before_claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    results = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True))
    assert [r["state"] for r in results] == ["migrated"] * 4, results
    assert all(r["backup"] and Path(r["backup"]).is_file() for r in results)
    # Claude Code: only the shim entry changed, everything else is as it was
    after_claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    entry = after_claude["mcpServers"]["pseudolife-memory"]
    assert entry["command"] == str(layout.launcher) and entry["args"] == []
    assert entry["env"] == before_claude["mcpServers"]["pseudolife-memory"]["env"]
    after_claude["mcpServers"]["pseudolife-memory"] = before_claude["mcpServers"]["pseudolife-memory"]
    assert after_claude == before_claude
    # Codex: the table's command/args/cwd moved, its env, tools and neighbours stayed
    import tomllib
    codex = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    table = codex["mcp_servers"]["pseudolife-memory"]
    assert table["command"] == str(layout.launcher) and table["args"] == [] and "cwd" not in table
    assert table["startup_timeout_sec"] == 240 and table["env"]["PSEUDOLIFE_WRITER_ID"] == "codex"
    assert table["tools"]["memory_search"]["approval_mode"] == "approve"
    assert codex["mcp_servers"]["other"] == {"command": "other-tool", "args": ["--serve"]}
    assert codex["model"] == "gpt-5" and codex["marketplaces"]["pseudolife-mcp"]["source_type"] == "git"
    # Gemini: its cwd went too
    gemini = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert gemini["mcpServers"]["pseudolife-memory"] == {"command": str(layout.launcher), "args": [],
                                                         "env": {"PSEUDOLIFE_WRITER_ID": "gemini"}}
    backups = sorted(p.name for p in home.rglob("*.bak-*"))
    # a second pass changes nothing and writes no more backups
    again = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True))
    assert [r["state"] for r in again] == ["current"] * 4, again
    assert sorted(p.name for p in home.rglob("*.bak-*")) == backups
    assert rt.pinned_runtimes(layout, rt.find_registrations(env, windows=True)) == set()


def test_a_registration_that_is_not_a_runtime_path_is_left_alone(tmp_path):
    layout = _layout(tmp_path, "windows")
    elsewhere = tmp_path / "checkout" / ".venv"
    home, env = _fixture_home(tmp_path, layout, elsewhere)
    text_before = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    results = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True))
    assert [r["state"] for r in results] == ["left"] * 4
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == text_before
    assert not list(home.rglob("*.bak-*"))
    # unless the caller names that root as one of the shim's — and then only
    # the registrations that would not spawn their own daemon (Claude Code's
    # and Codex's carry PSEUDOLIFE_MCP_NO_SPAWN here; Desktop's and Gemini's
    # do not, see the spawning test below).
    results = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True), roots=[elsewhere])
    assert [r["state"] for r in results] == ["migrated", "migrated", "spawning", "spawning"]


def test_a_codex_table_the_editor_cannot_isolate_is_reported_for_hand_editing(tmp_path):
    layout = _layout(tmp_path, "windows")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    config = home / ".codex" / "config.toml"
    config.write_text(f'mcp_servers = {{ "pseudolife-memory" = {{ command = "{(runtime / "Scripts" / "python.exe").as_posix()}", '
                      'args = ["-m", "pseudolife_memory.cli"] } }\n', encoding="utf-8")
    [codex] = [r for r in rt.find_registrations(env, windows=True) if r.client == "codex"]
    result = rt.migrate_registration(codex, layout)
    assert result["state"] == "manual"
    assert "by hand" in result["detail"] or "set command" in result["detail"]
    assert config.read_text(encoding="utf-8").startswith("mcp_servers = {")
    assert not list(home.rglob("*.bak-*"))


def test_codex_registration_migrates_from_the_checkout_script(tmp_path):
    layout = _layout(tmp_path, "windows" if os.name == "nt" else "posix")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    env = {**os.environ, **env, "PSEUDOLIFE_SHIM_RUNTIMES": str(layout.root),
           "PSEUDOLIFE_SHIM_LAUNCHER": str(layout.launcher)}
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "--json", "migrate", "--client", "codex"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert [r["state"] for r in report] == ["migrated"] and report[0]["client"] == "codex"
    claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert claude["mcpServers"]["pseudolife-memory"]["command"] != str(layout.launcher)   # not asked for
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "migrate", "--client", "codex"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0 and "current" in proc.stderr and proc.stdout == ""
    # a bare registration moves only when asked (--bare), to the absolute
    # launcher — and only with the no-spawn guard, as the installers set it
    (home / ".gemini" / "settings.json").write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": "pseudolife-mcp", "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "migrate", "--client", "gemini"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 3
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "migrate", "--client", "gemini", "--bare"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0
    gemini = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert gemini["mcpServers"]["pseudolife-memory"]["command"] == str(layout.launcher)


# ── review fixes (2026-09-29) ───────────────────────────────────────────────

def test_migration_keeps_a_mode_argument_and_drops_only_the_module_prefix(tmp_path):
    """`channel` is an opt-in shim mode: a registration running it must keep
    it on the launcher; Codex's `-m pseudolife_memory.cli channel` form
    loses only the module prefix. A registration already on the launcher
    with a mode is current, not rewritten."""
    layout = _layout(tmp_path, "windows")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    claude["mcpServers"]["pseudolife-memory"]["args"] = ["channel"]
    (home / ".claude.json").write_text(json.dumps(claude), encoding="utf-8")
    config = home / ".codex" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        'args = ["-m", "pseudolife_memory.cli"]', 'args = ["-m", "pseudolife_memory.cli", "channel"]'), encoding="utf-8")
    results = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True))
    by_client = {r["client"]: r["state"] for r in results}
    assert by_client["claude-code"] == "migrated" and by_client["codex"] == "migrated"
    assert json.loads((home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["args"] == ["channel"]
    import tomllib
    assert tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["pseudolife-memory"]["args"] == ["channel"]
    again = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True))
    assert {r["client"]: r["state"] for r in again}["claude-code"] == "current"
    assert rt.migrated_args(["-m", "pseudolife_memory.cli"]) == []
    assert rt.migrated_args(["-m", "pseudolife_memory", "channel"]) == ["channel"]
    assert rt.migrated_args(["channel"]) == ["channel"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_migration_keeps_the_config_files_permission_bits(tmp_path):
    layout = _layout(tmp_path, "posix")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    for path in (home / ".claude.json", home / ".codex" / "config.toml"):
        path.chmod(0o600)
    results = rt.migrate_registrations(layout, rt.find_registrations(env, windows=False),
                                       roots=[runtime])
    assert [r["state"] for r in results if r["client"] in ("claude-code", "codex")] == ["migrated", "migrated"]
    for path in (home / ".claude.json", home / ".codex" / "config.toml"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_migration_edits_through_a_symlinked_config_file(tmp_path):
    layout = _layout(tmp_path, "windows")
    runtime = layout.root / "coordination-e41a575a"
    home, env = _fixture_home(tmp_path, layout, runtime)
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    shutil.move(str(home / ".claude.json"), str(real))
    try:
        os.symlink(real, home / ".claude.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")
    [claude] = [r for r in rt.find_registrations(env, windows=True) if r.client == "claude-code"]
    result = rt.migrate_registration(claude, layout)
    assert result["state"] == "migrated"
    assert (home / ".claude.json").is_symlink()
    assert json.loads(real.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["command"] == str(layout.launcher)


def test_a_root_that_is_the_registered_file_itself_matches(tmp_path):
    """pipx exposes a COPY of the launcher in its bin dir on Windows and pip
    --user writes a script beside other tools': the installer passes that
    exact path, which must count even though nothing is under it."""
    layout = _layout(tmp_path, "windows")
    launcher = tmp_path / "pipx-bin" / "pseudolife-mcp.exe"
    registration = rt.Registration("claude-code", tmp_path / "x.json", "pseudolife-memory", str(launcher))
    assert rt.registers_runtime_path(registration, layout, [launcher])
    assert not rt.registers_runtime_path(registration, layout, [tmp_path / "pipx-bin" / "other.exe"])
    assert not rt.registers_runtime_path(registration, layout, [])


def test_a_registration_that_would_spawn_a_daemon_is_recognised(tmp_path):
    reg = lambda env: rt.Registration("claude-code", tmp_path / "x.json", "pseudolife-memory", "x", env=env)
    assert reg({}).spawns_a_daemon
    assert reg({"PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}).spawns_a_daemon
    assert not reg({"PSEUDOLIFE_MCP_NO_SPAWN": "1"}).spawns_a_daemon
    assert not reg({"PSEUDOLIFE_MCP_DAEMON_URL": "http://100.64.0.2:8765"}).spawns_a_daemon
    assert not reg({"PSEUDOLIFE_MCP_DAEMON_URL": "https://bank.example.com"}).spawns_a_daemon


def test_a_refused_launcher_swap_puts_the_old_launcher_back(tmp_path, monkeypatch):
    """Every registration names the launcher: a replace the OS refuses (a
    scanner holding the fresh .exe) must not leave the path empty."""
    tools = FakeTools("windows")
    layout = _layout(tmp_path, "windows")
    _install(layout, tools, "0.15.0")
    before = layout.launcher.read_bytes()
    record = layout.launcher.with_name("pseudolife-mcp.exe.json")
    record.write_text(json.dumps({"root": "elsewhere"}), encoding="utf-8")

    def refuse(source, target, attempts=5):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(rt, "_replace_with_retry", refuse)
    with pytest.raises(PermissionError):
        rt.ensure_launcher(layout, run=tools)
    assert layout.launcher.read_bytes() == before
    assert not list(layout.launcher_dir.glob("pseudolife-mcp.exe.old-*"))
    assert not list(layout.launcher_dir.glob("pseudolife-launcher-*"))


def test_a_given_venv_interpreter_is_reduced_to_its_base(tmp_path):
    """A runtime created from someone's .venv python would die with that
    venv: the base interpreter named in pyvenv.cfg is used instead."""
    venv = tmp_path / "dev-venv"
    python = _real_venv(venv)
    base = rt.base_interpreter(str(python))
    # not compared through resolve(): on POSIX the venv's python is a
    # symlink to the base, which is exactly the file the base must be
    assert not Path(base).is_relative_to(venv), base
    assert Path(base).is_file() and (tmp_path / "dev-venv" / "pyvenv.cfg").is_file()
    # a venv made from that venv still resolves to the real base
    inner = tmp_path / "inner"
    subprocess.run([str(python), "-m", "venv", "--without-pip", str(inner)], check=True, capture_output=True, timeout=180)
    inner_python = inner / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    assert Path(rt.base_interpreter(str(inner_python))).resolve() == Path(base).resolve()


def test_a_registration_that_spawns_its_own_daemon_is_never_moved_onto_a_runtime(tmp_path):
    """A shim runtime holds no daemon (no torch). A registration of a full
    install with no ``PSEUDOLIFE_MCP_NO_SPAWN`` and a loopback daemon URL
    (the pip and lite tiers) starts its own daemon when none answers, so it
    stays where its install is — the guide promised as much, but only
    ``update_clients.py`` kept the promise; the installers migrate through
    ``shim_runtime.py migrate`` (review, 2026-09-29)."""
    layout = _layout(tmp_path, "windows")
    elsewhere = tmp_path / "checkout" / ".venv"
    home, env = _fixture_home(tmp_path, layout, elsewhere)
    before = {p: p.read_text(encoding="utf-8") for p in home.rglob("*.json")}
    results = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True), roots=[elsewhere])
    by_client = {r["client"]: r for r in results}
    assert by_client["claude-code"]["state"] == "migrated"           # PSEUDOLIFE_MCP_NO_SPAWN=1
    for client in ("claude-desktop", "gemini"):                        # no guard: would spawn
        assert by_client[client]["state"] == "spawning", by_client[client]
        assert "spawns its own daemon" in by_client[client]["detail"]
        assert by_client[client]["backup"] is None
    for p, text in before.items():
        if p.name != ".claude.json":
            assert p.read_text(encoding="utf-8") == text, p
    # A registration already on the launcher is not re-examined.
    again = rt.migrate_registrations(layout, rt.find_registrations(env, windows=True), roots=[elsewhere])
    assert {r["client"]: r["state"] for r in again}["claude-code"] == "current"


# ── reaching the launcher by name ───────────────────────────────────────────
#
# 2026-09-29, a Debian 13 host after migration: `which -a pseudolife-mcp`
# still found pipx's ~/.local/bin link to the OLD package, so "run
# `pseudolife-mcp update`" ran the old code, and after `pipx uninstall` ran
# nothing. POSIX: ~/.local/bin/pseudolife-mcp becomes a link to the
# launcher. Windows: the launcher directory goes on the user PATH. Every
# test here works under tmp_path with an injected registry: this machine's
# PATH, registry and ~/.local/bin are never written.

def _can_symlink(directory: Path) -> bool:
    probe = directory / "symlink-probe"
    try:
        os.symlink(str(directory), str(probe))
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


@pytest.fixture
def posix_link(tmp_path):
    """A POSIX-shaped layout under tmp_path with its launcher written and a
    user bin directory beside it; skipped where this user cannot symlink
    (Windows without Developer Mode)."""
    if not _can_symlink(tmp_path):
        pytest.skip("this user cannot create symlinks here")
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    layout = rt.Layout(home / ".local" / "share" / "pseudolife-mcp" / "runtimes",
                       home / ".local" / "share" / "pseudolife-mcp" / "bin" / "pseudolife-mcp",
                       user_bin=user_bin)
    rt.ensure_launcher(layout)
    user_bin.mkdir(parents=True)
    env = {"HOME": str(home), "PATH": os.pathsep.join([str(user_bin), "/usr/bin", "/bin"])}
    return layout, env


def _console_script(path: Path) -> Path:
    """What pip and pipx write for this package's entry point."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/opt/python/bin/python3\n# -*- coding: utf-8 -*-\nimport re\nimport sys\n"
                    "from pseudolife_memory.cli import main\nif __name__ == '__main__':\n"
                    "    sys.exit(main())\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def test_a_free_user_bin_name_becomes_a_link_to_the_launcher(posix_link):
    layout, env = posix_link
    link = layout.user_bin / "pseudolife-mcp"
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "linked", result
    assert link.is_symlink() and os.readlink(link) == str(layout.launcher)
    assert str(link) in result["detail"] and result["hint"] is None
    again = rt.expose_launcher(layout, env=env)
    assert again["state"] == "current" and again["hint"] is None


def test_a_pipx_link_to_the_old_shim_is_moved_aside_never_deleted(posix_link):
    layout, env = posix_link
    home = Path(env["HOME"])
    old = _console_script(home / ".local" / "share" / "pipx" / "venvs" / "pseudolife-mcp" / "bin" / "pseudolife-mcp")
    link = layout.user_bin / "pseudolife-mcp"
    os.symlink(str(old), str(link))
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "replaced", result
    assert os.readlink(link) == str(layout.launcher)
    aside = list(layout.user_bin.glob("pseudolife-mcp.pipx-*"))
    assert len(aside) == 1 and aside[0].is_symlink() and os.readlink(aside[0]) == str(old)
    assert str(aside[0]) in result["detail"] and "pipx uninstall" in result["detail"]
    assert old.is_file()


def test_a_pip_user_console_script_is_moved_aside(posix_link):
    layout, env = posix_link
    link = _console_script(layout.user_bin / "pseudolife-mcp")
    before = link.read_text(encoding="utf-8")
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "replaced", result
    assert os.readlink(link) == str(layout.launcher)
    aside = list(layout.user_bin.glob("pseudolife-mcp.pip-user-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == before


def test_a_link_into_a_pip_user_scripts_dir_is_moved_aside(posix_link):
    layout, env = posix_link
    home = Path(env["HOME"])
    old = _console_script(home / "Library" / "Python" / "3.12" / "bin" / "pseudolife-mcp")
    link = layout.user_bin / "pseudolife-mcp"
    os.symlink(str(old), str(link))
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "replaced", result
    assert len(list(layout.user_bin.glob("pseudolife-mcp.pip-user-*"))) == 1


@pytest.mark.parametrize("entry", ["script", "link"])
def test_a_name_that_belongs_to_something_else_is_left_alone(posix_link, entry):
    layout, env = posix_link
    link = layout.user_bin / "pseudolife-mcp"
    if entry == "script":
        link.write_text("#!/bin/sh\nexec my-own-wrapper \"$@\"\n", encoding="utf-8")
        before = link.read_text(encoding="utf-8")
    else:
        other = layout.user_bin.parent / "tools" / "pseudolife-mcp"
        other.parent.mkdir()
        other.write_text("#!/bin/sh\n", encoding="utf-8")
        os.symlink(str(other), str(link))
        before = os.readlink(link)
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "left", result
    assert (link.read_text(encoding="utf-8") if entry == "script" else os.readlink(link)) == before
    assert not list(layout.user_bin.glob("pseudolife-mcp.*"))
    assert "left as it is" in result["detail"] and str(layout.launcher) in result["detail"]


def test_a_user_bin_off_path_gets_the_one_line_fix(posix_link):
    layout, env = posix_link
    env = {**env, "PATH": os.pathsep.join(["/usr/bin", "/bin"])}
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "linked"
    assert result["hint"] == ('~/.local/bin is not on PATH: add export PATH="$HOME/.local/bin:$PATH" '
                              'to your shell profile (~/.profile, ~/.bashrc or ~/.zshrc), then open a new terminal')


def test_an_earlier_path_entry_that_still_wins_is_named(posix_link):
    layout, env = posix_link
    earlier = Path(env["HOME"]) / "usr-local-bin"
    stale = _console_script(earlier / "pseudolife-mcp")
    env = {**env, "PATH": os.pathsep.join([str(earlier), env["PATH"]])}
    result = rt.expose_launcher(layout, env=env)
    assert result["state"] == "linked"
    assert result["hint"] and str(stale) in result["hint"] and "first" in result["hint"]


def test_default_layout_names_where_the_launcher_is_reached_from(tmp_path):
    posix = rt.default_layout({"HOME": "/home/u"}, windows=False)
    assert posix.user_bin == Path("/home/u/.local/bin") and not posix.user_path
    chosen = rt.default_layout({"HOME": "/home/u", "PSEUDOLIFE_SHIM_USER_BIN": str(tmp_path / "b")}, windows=False)
    assert chosen.user_bin == tmp_path / "b"
    windows = rt.default_layout({"LOCALAPPDATA": r"X:\profile\AppData\Local", "USERPROFILE": r"X:\profile"},
                                windows=True)
    assert windows.user_path and windows.user_bin is None
    # Overridden paths (a test fixture, an operator's own layout) touch no
    # PATH unless a user bin directory is named as well.
    overrides = {"PSEUDOLIFE_SHIM_RUNTIMES": str(tmp_path / "r")}
    posix_over = rt.default_layout({**overrides, "PSEUDOLIFE_SHIM_LAUNCHER": str(tmp_path / "l")}, windows=False)
    assert posix_over.user_bin is None and not posix_over.user_path
    named = rt.default_layout({**overrides, "PSEUDOLIFE_SHIM_LAUNCHER": str(tmp_path / "l"),
                               "PSEUDOLIFE_SHIM_USER_BIN": str(tmp_path / "b")}, windows=False)
    assert named.user_bin == tmp_path / "b"
    win_over = rt.default_layout({**overrides, "PSEUDOLIFE_SHIM_LAUNCHER": str(tmp_path / "l.exe")}, windows=True)
    assert win_over.user_bin is None and not win_over.user_path


class _NoRegistry:
    def get(self, name):
        raise AssertionError("the registry was read")

    def __setitem__(self, name, value):
        raise AssertionError("the registry was written")


def test_a_layout_without_a_user_bin_or_user_path_changes_nothing(tmp_path, shape):
    layout = _layout(tmp_path, shape)
    result = rt.expose_launcher(layout, env={"PATH": ""}, registry=_NoRegistry(),
                                broadcast=lambda: pytest.fail("broadcast"))
    assert result["state"] == "skipped"


@pytest.mark.skipif(os.name != "nt", reason="Windows user PATH semantics (case-insensitive, ; separated)")
def test_the_launcher_directory_is_prepended_to_the_user_path_once(tmp_path):
    directory = tmp_path / "pseudolife-mcp" / "bin"
    registry = {"Path": (r"%USERPROFILE%\.local\bin;C:\Tools;", 1)}
    broadcasts = []
    result = rt.ensure_user_path(directory, registry=registry, broadcast=lambda: broadcasts.append(1))
    assert result["state"] == "added", result
    assert registry["Path"] == (str(directory) + r";%USERPROFILE%\.local\bin;C:\Tools;", 2)  # REG_EXPAND_SZ
    assert broadcasts == [1]
    again = rt.ensure_user_path(directory, registry=registry, broadcast=lambda: broadcasts.append(1))
    assert again["state"] == "current" and broadcasts == [1]
    # already there in another spelling: left as it is
    spelled = {"Path": (r"C:\Tools;" + str(directory).upper() + "\\", 2)}
    assert rt.ensure_user_path(directory, registry=spelled, broadcast=lambda: broadcasts.append(1))["state"] == "current"
    assert spelled["Path"] == (r"C:\Tools;" + str(directory).upper() + "\\", 2) and broadcasts == [1]
    # no user Path value at all
    empty: dict = {}
    assert rt.ensure_user_path(directory, registry=empty, broadcast=lambda: None)["state"] == "added"
    assert empty["Path"] == (str(directory), 2)


@pytest.mark.skipif(os.name != "nt", reason="Windows user PATH semantics")
def test_a_terminal_that_still_runs_another_copy_is_told_to_open_a_new_one(tmp_path):
    layout = rt.Layout(tmp_path / "runtimes", tmp_path / "bin" / "pseudolife-mcp.exe", user_path=True)
    old = r"C:\Users\<user>\.local\bin\pseudolife-mcp.exe"
    registry: dict = {"Path": (r"%USERPROFILE%\.local\bin", 2)}
    result = rt.expose_launcher(layout, env={"PATH": ""}, registry=registry, broadcast=lambda: None,
                                which=lambda name, path=None: old)
    assert result["state"] == "added"
    assert registry["Path"][0].startswith(str(layout.launcher_dir) + ";")
    assert result["hint"] == ("open a new terminal for `pseudolife-mcp` to resolve to the launcher; "
                              f"this one still runs {old}")
    current = rt.expose_launcher(layout, env={"PATH": ""}, registry=registry, broadcast=lambda: None,
                                 which=lambda name, path=None: str(layout.launcher))
    assert current["state"] == "current" and current["hint"] is None


def test_the_checkout_script_exposes_nothing_for_an_overridden_layout(tmp_path):
    launcher = tmp_path / "bin" / _console_name("windows" if os.name == "nt" else "posix")
    env = {k: v for k, v in os.environ.items() if k != "PSEUDOLIFE_SHIM_USER_BIN"}
    env.update({"PSEUDOLIFE_SHIM_RUNTIMES": str(tmp_path / "runtimes"), "PSEUDOLIFE_SHIM_LAUNCHER": str(launcher)})
    proc = subprocess.run([sys.executable, str(ROOT / "ops" / "shim_runtime.py"), "--json", "expose"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["state"] == "skipped"
