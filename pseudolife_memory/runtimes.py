"""Side-by-side shim runtimes behind one stable launcher path.

Every client registration (Claude Code, Codex, Claude Desktop, Gemini CLI)
used to name the shim's own executable: a pipx venv's launcher, or the
``python.exe`` of a hand-made virtualenv. Replacing that executable while a
session runs it is impossible on Windows (pip and pipx both leave it
half-removed), so a shim upgrade needed every session closed first, and on
2026-09-28 thirteen idle Desktop sessions refused the step.

This module installs each shim version into its own runtime directory and
registers ONE launcher path that starts the newest complete runtime:

* ``<runtimes root>/NNNNNN/`` — a plain virtualenv holding the package and
  the shim's dependencies, plus ``runtime.json`` (version, source, commit)
  written last, so a directory without the marker is an install that did
  not finish. The directory is named before pip runs, because pip bakes
  the interpreter's absolute path into every console script it writes: a
  runtime built under one name and renamed to another does not start.
  Windows: ``%LOCALAPPDATA%\\pseudolife-mcp\\runtimes``; elsewhere
  ``$XDG_DATA_HOME/pseudolife-mcp/runtimes`` (``~/.local/share/...``).
* ``<launcher>`` — Windows: ``%LOCALAPPDATA%\\pseudolife-mcp\\bin\\
  pseudolife-mcp.exe``, a console-script launcher (the same kind pip writes)
  whose script picks the highest-numbered complete runtime and runs its
  ``pseudolife-mcp.exe``; elsewhere ``~/.local/share/pseudolife-mcp/bin/
  pseudolife-mcp``, a ``/bin/sh`` script that ``exec``s the same choice
  (not ``~/.local/bin``: that is the file pip --user and pipx write, which
  a later ``pip uninstall`` would delete).

A running session keeps the runtime it started from; the next session
start takes the newest. A runtime is removed only when it is not the
newest, no registration still names it, and no process runs from it (the
process table this module reads is the one the installers' in-use check
already used). Registrations that name a runtime path directly are moved
to the launcher in place, each file backed up first.

Standalone on purpose: ``ops/shim_runtime.py`` and ``ops/update_clients.py``
load this file from a checkout by path before any package is installed, so
nothing here imports the rest of ``pseudolife_memory``.
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

PACKAGE = "pseudolife-mcp"
SERVER = "pseudolife-memory"
DESKTOP_SERVER = "pseudolife-desktop"   # Claude Desktop's name for the same shim
MARKER = "runtime.json"
_SEQUENCE_WIDTH = 6
# An unfinished runtime (numbered, no marker) younger than this may still
# be installing in another process; older ones are leftovers.
_ABANDONED_AFTER_S = 3600
_OLD_LAUNCHER_PREFIX = "pseudolife-mcp.exe.old-"

# What a shim runtime installs beside the package itself (``pip install
# --no-deps``): the client-side modes only. ``serve`` and ``embedded`` need
# torch, sentence-transformers and chromadb — gigabytes a stdio shim never
# loads, and the Docker tier's shim never serves (the installers set
# PSEUDOLIFE_MCP_NO_SPAWN=1). Each entry is pinned to the matching line of
# pyproject.toml by tests/test_shim_runtimes.py, so a floor moves in both
# places or not at all.
SHIM_REQUIREMENTS = (
    "mcp>=2.1,<2.2",
    "pydantic>=2.12,<3",
    "pyyaml>=6.0",
    "psycopg[binary]>=3.1",
    "pgvector>=0.3",
)


# ── seams ───────────────────────────────────────────────────────────────────

def run_cli(argv, *, timeout: int = 1800, cwd: str | None = None,
            env: dict | None = None) -> tuple[int, str]:
    """Run a command and return ``(returncode, combined output)``; a
    missing or hung executable reads as a non-zero code with the reason."""
    try:
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              timeout=timeout, check=False, errors="replace",
                              stdin=subprocess.DEVNULL, cwd=cwd, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def home(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())


# ── layout ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Layout:
    root: Path        # where the runtimes live
    launcher: Path    # the one path every client registers
    # Where ``pseudolife-mcp`` typed in a terminal is made to reach the
    # launcher (expose_launcher). POSIX: the directory that gets a
    # ``pseudolife-mcp`` link to it (``~/.local/bin``). Windows: whether the
    # launcher directory goes on the user PATH. Neither is set for a layout
    # built by hand or from overridden paths, so a test fixture never
    # touches this machine's PATH.
    user_bin: Path | None = None
    user_path: bool = False

    @property
    def launcher_dir(self) -> Path:
        return self.launcher.parent


def default_layout(env: dict | None = None, windows: bool | None = None) -> Layout:
    """The layout for this user. ``PSEUDOLIFE_SHIM_RUNTIMES`` and
    ``PSEUDOLIFE_SHIM_LAUNCHER`` override the two paths (a test fixture, or
    an operator who keeps them elsewhere); both must then be set, and the
    launcher is then linked from no directory and put on no PATH unless
    ``PSEUDOLIFE_SHIM_USER_BIN`` names the directory to link it from
    (POSIX). That variable also moves the default ``~/.local/bin``."""
    env = os.environ if env is None else env
    windows = (os.name == "nt") if windows is None else windows
    root_override, launcher_override = env.get("PSEUDOLIFE_SHIM_RUNTIMES"), env.get("PSEUDOLIFE_SHIM_LAUNCHER")
    user_bin_override = env.get("PSEUDOLIFE_SHIM_USER_BIN")
    if root_override and launcher_override:
        launcher = Path(launcher_override)
        # The launcher's suffix decides the runtime shape (Scripts\ + .exe or
        # bin/): a Windows override without .exe would build POSIX runtimes.
        if windows and launcher.suffix.lower() != ".exe":
            raise ValueError("PSEUDOLIFE_SHIM_LAUNCHER must end with .exe on Windows")
        if not windows and launcher.suffix.lower() == ".exe":
            raise ValueError("PSEUDOLIFE_SHIM_LAUNCHER must not end with .exe off Windows")
        return Layout(Path(root_override), launcher,
                      user_bin=Path(user_bin_override) if user_bin_override and not windows else None)
    user = home(env)
    if windows:
        local = Path(env.get("LOCALAPPDATA") or user / "AppData" / "Local") / PACKAGE
        return Layout(local / "runtimes", local / "bin" / "pseudolife-mcp.exe", user_path=True)
    data = Path(env.get("XDG_DATA_HOME") or user / ".local" / "share") / PACKAGE
    return Layout(data / "runtimes", data / "bin" / "pseudolife-mcp",
                  user_bin=Path(user_bin_override) if user_bin_override else user / ".local" / "bin")


def _windows(layout: Layout) -> bool:
    return layout.launcher.suffix.lower() == ".exe"


def _scripts_dir(runtime: Path, windows: bool) -> Path:
    return runtime / ("Scripts" if windows else "bin")


def _console(runtime: Path, windows: bool) -> Path:
    return _scripts_dir(runtime, windows) / ("pseudolife-mcp.exe" if windows else "pseudolife-mcp")


def _python(runtime: Path, windows: bool) -> Path:
    return _scripts_dir(runtime, windows) / ("python.exe" if windows else "python")


# ── runtimes ────────────────────────────────────────────────────────────────

@dataclass
class Runtime:
    path: Path
    sequence: int
    version: str
    installed_at: str = ""
    source: str = ""
    source_commit: str | None = None

    @property
    def name(self) -> str:
        return self.path.name


_RUNTIME_NAME = re.compile(r"^\d{%d}$" % _SEQUENCE_WIDTH)


def _sequence_of(name: str) -> int | None:
    return int(name) if _RUNTIME_NAME.match(name) else None


def _runtime_name(sequence: int) -> str:
    return f"{sequence:0{_SEQUENCE_WIDTH}d}"


def list_runtimes(layout: Layout) -> list[Runtime]:
    """Every complete runtime under the root, lowest sequence first. A
    directory whose marker is missing or unreadable is not a runtime."""
    found: list[Runtime] = []
    try:
        names = os.listdir(layout.root)
    except OSError:
        return found
    for name in names:
        sequence = _sequence_of(name)
        path = layout.root / name
        if sequence is None or not path.is_dir():
            continue
        try:
            marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(marker, dict) or not _console(path, _windows(layout)).is_file():
            continue
        found.append(Runtime(path, sequence, str(marker.get("version") or "unknown"),
                             str(marker.get("installed_at") or ""), str(marker.get("source") or ""),
                             marker.get("source_commit")))
    found.sort(key=lambda r: r.sequence)
    return found


def current_runtime(layout: Layout) -> Runtime | None:
    """The runtime the launcher runs next: the highest sequence that is
    complete."""
    runtimes = list_runtimes(layout)
    return runtimes[-1] if runtimes else None


def _next_sequence(layout: Layout) -> int:
    highest = 0
    try:
        names = os.listdir(layout.root)
    except OSError:
        names = []
    for name in names:
        sequence = _sequence_of(name)
        if sequence is not None:
            highest = max(highest, sequence)
    return highest + 1


class RuntimeInstallError(RuntimeError):
    """An install step failed; ``step`` names it and ``output`` is what the
    tool said. Nothing of the attempt is left behind."""

    def __init__(self, step: str, output: str):
        super().__init__(f"{step}: {output.strip().splitlines()[-1] if output.strip() else 'no output'}")
        self.step = step
        self.output = output


def base_interpreter(python: str | None = None) -> str:
    """The interpreter new runtimes are created from: the one given (a
    command name such as ``python3`` or a path), else this process's
    interpreter — in either case reduced to its base when it is a
    virtualenv's, since a runtime that depends on someone's ``.venv`` dies
    with it (``pyvenv.cfg`` names the base)."""
    if python:
        # A command name (the installers probe "python3") as well as a path.
        resolved = python if os.sep in python or Path(python).is_file() else (shutil.which(python) or python)
        return _base_of(Path(resolved))
    base = getattr(sys, "_base_executable", None)
    if base and Path(base).is_file():
        return _base_of(Path(base))
    return _base_of(Path(sys.executable))


def _base_of(executable: Path) -> str:
    """``executable`` itself unless it lives in a virtualenv, whose base
    interpreter is returned instead (recursively: a venv made from a venv)."""
    seen = set()
    current = executable
    while str(current) not in seen:
        seen.add(str(current))
        cfg = current.parent.parent / "pyvenv.cfg"
        try:
            lines = cfg.read_text(encoding="utf-8").splitlines()
        except OSError:
            return str(current)
        keys = {}
        for line in lines:
            if "=" in line:
                key, _, value = line.partition("=")
                keys[key.strip().lower()] = value.strip()
        candidates = [keys["executable"]] if keys.get("executable") else []
        if keys.get("home"):
            candidates += [str(Path(keys["home"]) / name) for name in ("python.exe", "python3", "python")]
        base = next((c for c in candidates if Path(c).is_file()), None)
        if base is None:
            return str(current)
        current = Path(base)
    return str(current)


_VERSION_PROBE = "import importlib.metadata as m; print(m.version('pseudolife-mcp'))"


_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(\[[^\]]*\])?\s*([<>=!~].*)?$")


def _source_commit(source: str, run: Callable) -> str | None:
    """The commit a checkout source is at, when git can say; ``None`` for a
    requirement (``pseudolife-mcp==0.15.1``) or a tree git cannot describe."""
    if _REQUIREMENT.match(source) and not Path(source).exists():
        return None
    code, out = run(["git", "-C", source, "rev-parse", "HEAD"], timeout=30)
    head = out.strip().splitlines()[-1] if code == 0 and out.strip() else ""
    return head if re.fullmatch(r"[0-9a-f]{40}", head) else None


def install(source: str, layout: Layout, *, python: str | None = None,
            run: Callable = run_cli, log: Callable[[str], None] = lambda _line: None) -> Runtime:
    """Install ``source`` (a checkout path or a pip requirement such as
    ``pseudolife-mcp==0.15.1``) as a new runtime beside the existing ones,
    then make sure the launcher is in place. Returns the new runtime.

    The runtime is built in its final directory and the marker is written
    only once every step passed, so the launcher never picks a half-built
    runtime. A failed step removes the directory and raises
    :class:`RuntimeInstallError`."""
    windows = _windows(layout)
    base = base_interpreter(python)
    layout.root.mkdir(parents=True, exist_ok=True)
    sequence = _next_sequence(layout)
    final = layout.root / _runtime_name(sequence)
    while True:   # claim the number: another install may be taking it now
        try:
            final.mkdir()
            break
        except FileExistsError:
            sequence += 1
            final = layout.root / _runtime_name(sequence)
    try:
        log(f"creating runtime {final.name} with {base}")
        _step(run, "venv", [base, "-m", "venv", str(final)])
        venv_python = _python(final, windows)
        if not venv_python.is_file():
            raise RuntimeInstallError("venv", f"{venv_python} was not created")
        log(f"installing {source} (package only)")
        _step(run, "pip install --no-deps", [venv_python, "-m", "pip", "install", "--no-deps", source])
        log("installing the shim's dependencies")
        _step(run, "pip install (dependencies)", [venv_python, "-m", "pip", "install", *SHIM_REQUIREMENTS])
        code, out = run([str(venv_python), "-c", _VERSION_PROBE], timeout=120)
        version = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
        if code != 0 or not re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version):
            raise RuntimeInstallError("version probe", out or f"exit {code}")
        if not _console(final, windows).is_file():
            raise RuntimeInstallError("console script", f"{_console(final, windows)} was not installed")
    except Exception:
        shutil.rmtree(final, ignore_errors=True)
        raise
    marker = {"version": version, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "source": source, "source_commit": _source_commit(source, run),
              "base_interpreter": base}
    _write_atomic(final / MARKER, json.dumps(marker, indent=2) + "\n")
    runtime = Runtime(final, sequence, version, marker["installed_at"], source, marker["source_commit"])
    log(f"runtime {runtime.name} complete")
    ensure_launcher(layout, runtime, run=run, log=log)
    return runtime


def _step(run: Callable, name: str, argv: list) -> str:
    code, out = run([str(a) for a in argv])
    if code != 0:
        raise RuntimeInstallError(name, out)
    return out


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _replace_config(path: Path, text: str) -> None:
    """Rewrite a client config file in place: through a symlink (a dotfile
    manager's link must stay a link), keeping the file's permission bits
    (``~/.claude.json`` can hold credentials; a 0600 file must not come
    back 0644 from the umask)."""
    target = path.resolve() if path.is_symlink() else path
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        shutil.copymode(target, tmp)
    except OSError:
        pass
    os.replace(tmp, target)


# ── the launcher ────────────────────────────────────────────────────────────

_POSIX_LAUNCHER = """\
#!/bin/sh
# pseudolife-mcp launcher: starts the newest complete shim runtime under the
# directory below. Written by pseudolife_memory.runtimes (the installer and
# ops/update_clients.py); it is rewritten whenever its content changes, so
# do not edit it by hand. Sessions already running keep their runtime.
root='__ROOT__'
chosen=''
for candidate in "$root"/[0-9][0-9][0-9][0-9][0-9][0-9]/; do
    if [ -f "${candidate}runtime.json" ] && [ -x "${candidate}bin/pseudolife-mcp" ]; then
        chosen="$candidate"
    fi
done
if [ -z "$chosen" ]; then
    echo "[pseudolife-mcp] no complete shim runtime under $root; re-run the installer (ops/install.sh) or, from a checkout, python ops/shim_runtime.py install --source <checkout>" >&2
    exit 1
fi
exec "${chosen}bin/pseudolife-mcp" "$@"
"""

# The script inside the Windows launcher, run by the base interpreter the
# launcher's shebang names. It starts the runtime's own console script as a
# child (so the process table shows a process inside the runtime, which is
# what keeps the runtime from being removed) in a job object that ends the
# child with this process.
_WINDOWS_SELECTOR = r'''# -*- coding: utf-8 -*-
# pseudolife-mcp launcher (written by pseudolife_memory.runtimes; do not edit).
import os
import subprocess
import sys

ROOT = __ROOT__


def _newest():
    chosen = None
    try:
        names = os.listdir(ROOT)
    except OSError:
        names = []
    for name in names:
        if len(name) != 6 or not name.isdigit():
            continue
        exe = os.path.join(ROOT, name, "Scripts", "pseudolife-mcp.exe")
        if os.path.isfile(os.path.join(ROOT, name, "runtime.json")) and os.path.isfile(exe):
            if chosen is None or int(name) > chosen[0]:
                chosen = (int(name), exe)
    return chosen[1] if chosen else None


def _job():
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return kernel32, job
    except Exception:
        return None


def main():
    exe = _newest()
    if exe is None:
        sys.stderr.write("[pseudolife-mcp] no complete shim runtime under %s; re-run the installer "
                         "(ops\\install.ps1) or, from a checkout, python ops\\shim_runtime.py "
                         "install --source <checkout>\n" % ROOT)
        return 1
    job = _job()
    proc = subprocess.Popen([exe] + sys.argv[1:])
    if job is not None:
        try:
            from ctypes import wintypes
            job[0].AssignProcessToJobObject(wintypes.HANDLE(job[1]), wintypes.HANDLE(int(proc._handle)))
        except Exception:
            pass
    try:
        return proc.wait()
    except KeyboardInterrupt:
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
'''

# Builds the Windows launcher with the same tool pip uses for console
# scripts (distlib's launcher stub + shebang + zipped script), run under the
# runtime's interpreter so it uses that interpreter's vendored pip.
_WINDOWS_BUILD = r'''
import sys
from pip._vendor.distlib.scripts import ScriptMaker
target, executable, body_file = sys.argv[1:4]
with open(body_file, encoding="utf-8") as handle:
    body = handle.read()
maker = ScriptMaker(None, target)
maker.executable = executable
maker.variants = {""}
maker.clobber = True
maker.script_template = body.replace("%", "%%")
for name in maker.make("pseudolife-mcp = pseudolife_memory.launcher:main"):
    print(name)
'''


def launcher_content(layout: Layout) -> str:
    """The POSIX launcher's text for this layout."""
    root = str(layout.root).replace("'", "'\\''")
    return _POSIX_LAUNCHER.replace("__ROOT__", root)


def selector_content(layout: Layout) -> str:
    """The script inside the Windows launcher for this layout."""
    return _WINDOWS_SELECTOR.replace("__ROOT__", repr(str(layout.root)))


def _build_windows_launcher(layout: Layout, runtime: Runtime, run: Callable, base: str) -> Path:
    """Write the launcher into a temporary directory and return it."""
    build_dir = Path(tempfile.mkdtemp(prefix="pseudolife-launcher-", dir=str(layout.launcher_dir)))
    body = build_dir / "selector.py"
    body.write_text(selector_content(layout), encoding="utf-8")
    code, out = run([str(_python(runtime.path, True)), "-c", _WINDOWS_BUILD,
                     str(build_dir), base, str(body)], timeout=300)
    built = build_dir / layout.launcher.name
    if code != 0 or not built.is_file():
        shutil.rmtree(build_dir, ignore_errors=True)
        raise RuntimeInstallError("launcher build", out or f"exit {code}")
    return built


def _marker_base(runtime: Runtime) -> str | None:
    try:
        base = json.loads((runtime.path / MARKER).read_text(encoding="utf-8")).get("base_interpreter")
    except (OSError, ValueError, AttributeError):
        return None
    return base if isinstance(base, str) and Path(base).is_file() else None


def ensure_launcher(layout: Layout, runtime: Runtime | None = None, *, run: Callable = run_cli,
                    log: Callable[[str], None] = lambda _line: None) -> str:
    """Put the launcher in place, or leave it when its content is already
    right. Returns ``"written"``, ``"replaced"`` or ``"current"``.

    On Windows a launcher a session is running cannot be overwritten, but
    it can be renamed: the old file is moved aside (``pseudolife-mcp.exe.
    old-<stamp>``) and cleaned up by :func:`remove_unused` once nothing runs
    it. The running sessions keep the file they opened."""
    layout.launcher_dir.mkdir(parents=True, exist_ok=True)
    if not _windows(layout):
        wanted = launcher_content(layout)
        try:
            current = layout.launcher.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == wanted:
            return "current"
        tmp = layout.launcher.with_name(layout.launcher.name + ".tmp")
        tmp.write_text(wanted, encoding="utf-8")
        os.chmod(tmp, 0o755)
        os.replace(tmp, layout.launcher)
        log(f"launcher {'written' if current is None else 'replaced'}: {layout.launcher}")
        return "written" if current is None else "replaced"
    runtime = runtime or current_runtime(layout)
    if runtime is None:
        raise RuntimeInstallError("launcher", "no complete runtime to build the launcher with")
    base = _marker_base(runtime) or base_interpreter()
    # distlib stamps the zipped script with the build time, so two builds
    # never match byte for byte: the record beside the launcher says what
    # went into it, and an unchanged record means no rebuild.
    record = {"root": str(layout.root), "base_interpreter": base,
              "selector_sha256": _sha256(selector_content(layout).encode("utf-8"))}
    existing = _read_json(_launcher_record(layout))
    if layout.launcher.is_file() and existing and all(existing.get(k) == v for k, v in record.items()):
        return "current"
    built = _build_windows_launcher(layout, runtime, run, base)
    aside = None
    try:
        replaced = layout.launcher.is_file()
        if replaced:
            aside = layout.launcher.with_name(f"{_OLD_LAUNCHER_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
            os.rename(layout.launcher, aside)
        try:
            _replace_with_retry(built, layout.launcher)
        except OSError:
            # Every registration names this path: with the old file moved
            # aside and the new one refused (a scanner holding a fresh .exe
            # is the usual cause), put the old one back before giving up.
            if aside is not None:
                os.replace(aside, layout.launcher)
            raise
        _write_atomic(_launcher_record(layout), json.dumps(record, indent=2) + "\n")
        log(f"launcher {'replaced' if replaced else 'written'}: {layout.launcher}")
        return "replaced" if replaced else "written"
    finally:
        shutil.rmtree(built.parent, ignore_errors=True)


def _replace_with_retry(source: Path, target: Path, attempts: int = 5) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _launcher_record(layout: Layout) -> Path:
    return layout.launcher.with_name(layout.launcher.name + ".json")


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# ── reaching the launcher by name ───────────────────────────────────────────
#
# 2026-09-29, a Debian 13 host after migration: every registration named the
# launcher, but `pseudolife-mcp` typed in a terminal was still pipx's
# ~/.local/bin link into its venv of the OLD package, so `pseudolife-mcp
# update` ran old code, and after `pipx uninstall` it ran nothing. The
# launcher is therefore made reachable by name: on POSIX through a
# ``pseudolife-mcp`` link in ``~/.local/bin``, on Windows by putting the
# launcher directory first on the user PATH.
#
# Why a symlink survives `pipx uninstall`: pipx collects what to remove from
# its bin dir with pipx.commands.common.get_exposed_paths_for_package (called
# by pipx.commands.uninstall._get_package_bin_dir_app_paths). Where the bin
# dir supports symlinks, it takes only symlinks for which
# ``b.resolve().parent.samefile(venv_bin_path)``, i.e. links that resolve into
# the package's own venv; a regular file is never taken there. This link
# resolves to the launcher, outside every pipx venv, so pipx leaves it (the
# by-name fallback applies only where the bin dir cannot hold symlinks,
# which is Windows, where this module never links). The old pipx link moved
# aside still resolves into the venv, so `pipx uninstall` removes that one
# with the venv. `pipx install --force` would replace the link (plain
# `pipx install` refuses a name that points elsewhere), which is why the
# installers try the side-by-side runtime before pipx.

LINK_NAME = "pseudolife-mcp"
_CONSOLE_ENTRY = re.compile(r"^\s*from pseudolife_memory\.cli import main\s*$", re.M)
_REG_EXPAND_SZ = 2


def _is_console_script(path: Path) -> bool:
    """Whether ``path`` is the console script pip or pipx writes for this
    package's entry point (a python shebang importing ``pseudolife_memory.
    cli.main``); a hand-made wrapper that runs the package some other way
    is not."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return False
    first = head.split("\n", 1)[0]
    return first.startswith("#!") and "python" in first and bool(_CONSOLE_ENTRY.search(head))


def _in_pipx_venv(path: str) -> bool:
    """Whether ``path`` lies in a pipx venv of this package
    (``<PIPX_HOME>/venvs/pseudolife-mcp/...``)."""
    parts = [part.lower() for part in Path(path).parts]
    return any(parts[i] == "venvs" and parts[i + 1] == PACKAGE and any("pipx" in p for p in parts[:i])
               for i in range(len(parts) - 1))


def _link_entry(link: Path, layout: Layout) -> str:
    """What occupies the link's name: ``free``, ``current`` (the launcher
    already), ``pipx`` or ``pip-user`` (this package's old console script,
    which may be moved aside; ``runtime`` for one inside a runtime), or
    ``foreign``."""
    if link.is_symlink():
        target = os.readlink(link)
        absolute = target if os.path.isabs(target) else os.path.join(str(link.parent), target)
        if _same_path(absolute, str(layout.launcher)):
            return "current"
        if _in_pipx_venv(absolute) or _in_pipx_venv(os.path.realpath(absolute)):
            return "pipx"
        resolved = Path(os.path.realpath(absolute))
        if _is_console_script(resolved):
            return "runtime" if _under(str(resolved), layout.root) else "pip-user"
        return "foreign"
    if not link.exists():
        return "free"
    if link.is_file():
        try:
            if link.read_text(encoding="utf-8") == launcher_content(layout):
                return "current"
        except (OSError, UnicodeDecodeError):
            pass
        if _is_console_script(link):
            return "pip-user"
    return "foreign"


def _home_relative(path: Path, env: dict, form: str) -> str:
    """``path`` with the home directory written as ``~`` or ``$HOME``."""
    user = home(env)
    try:
        rest = path.relative_to(user)
    except ValueError:
        return str(path)
    return f"{form}/{rest.as_posix()}" if str(rest) != "." else form


def link_user_bin(layout: Layout, *, env: dict | None = None, which: Callable = shutil.which) -> dict:
    """Make ``<user_bin>/pseudolife-mcp`` a symlink to the launcher (POSIX).
    Returns ``{"state", "detail", "hint"}``: state ``linked`` (the name was
    free), ``replaced`` (this package's old pipx or pip --user entry was
    moved aside as ``pseudolife-mcp.<kind>-<stamp>``, never deleted),
    ``current``, ``left`` (the name is something else, untouched),
    ``failed`` or ``skipped`` (no user bin directory for this layout).
    ``hint`` is one line for the operator when the directory is not on
    ``PATH``, or when an earlier ``PATH`` entry still wins."""
    env = os.environ if env is None else env
    if layout.user_bin is None:
        return {"state": "skipped", "detail": "no user bin directory for this layout", "hint": None}
    link = layout.user_bin / LINK_NAME
    launcher = str(layout.launcher)
    kind = _link_entry(link, layout)
    aside = None
    try:
        if kind == "current":
            state, detail = "current", f"{link} already runs the launcher"
        elif kind == "foreign":
            return {"state": "left", "hint": None,
                    "detail": f"{link} is not this package's (another tool, or a hand-made script); left as it "
                              f"is. `pseudolife-mcp` there does not reach the launcher: run {launcher} by its "
                              "full path, or replace that entry yourself"}
        else:
            layout.user_bin.mkdir(parents=True, exist_ok=True)
            if kind != "free":
                stamp = time.strftime("%Y%m%d-%H%M%S")
                aside = link.with_name(f"{LINK_NAME}.{kind}-{stamp}")
                counter = 0
                while aside.exists() or aside.is_symlink():
                    counter += 1
                    aside = link.with_name(f"{LINK_NAME}.{kind}-{stamp}-{counter}")
                os.rename(link, aside)
            try:
                os.symlink(launcher, str(link))
            except OSError:
                if aside is not None:
                    os.rename(aside, link)
                raise
            state = "linked" if aside is None else "replaced"
            detail = f"linked {link} -> {launcher}"
            if aside is not None:
                detail += f"; the old {kind} entry is kept as {aside}"
                if kind == "pipx":
                    detail += " (`pipx uninstall pseudolife-mcp` removes it with its venv once no session runs it)"
    except OSError as exc:
        return {"state": "failed", "hint": None,
                "detail": f"could not link {link} to {launcher} ({exc}); run the launcher by its full path"}
    path = env.get("PATH", "")
    entries = [entry for entry in path.split(os.pathsep) if entry]
    hint = None
    if not any(_same_path(entry, str(layout.user_bin)) for entry in entries):
        shown = _home_relative(layout.user_bin, env, "~")
        hint = (f"{shown} is not on PATH: add export PATH=\"{_home_relative(layout.user_bin, env, '$HOME')}:$PATH\" "
                "to your shell profile (~/.profile, ~/.bashrc or ~/.zshrc), then open a new terminal")
    else:
        found = which(LINK_NAME, path=path)
        if found and not _same_path(os.path.realpath(found), os.path.realpath(launcher)):
            hint = (f"`pseudolife-mcp` still resolves to {found} first on PATH; remove that entry "
                    f"or put {layout.user_bin} before its directory")
    return {"state": state, "detail": detail, "hint": hint}


class _UserEnvironment:
    """``HKCU\\Environment`` through winreg, shaped like the dict a test
    passes instead: ``get(name)`` is ``(value, type)`` or ``None``, and
    ``[name] = (value, type)`` writes it. The machine environment is never
    opened."""

    def get(self, name: str):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def __setitem__(self, name: str, item) -> None:
        import winreg
        value, kind = item
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, kind, value)


def _broadcast_environment_change() -> None:
    """Tell running programs (Explorer, and so every console opened after)
    that the user environment changed: ``WM_SETTINGCHANGE`` with
    ``"Environment"``, as the System Properties dialog sends it."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                                               wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        result = ctypes.c_size_t()
        # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, 5 s per window
        user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
    except Exception:  # noqa: BLE001 - a missed broadcast only delays the change to the next logon
        pass


def _path_entry_is(entry: str, directory: Path) -> bool:
    expanded = os.path.expandvars(entry.strip().strip('"'))
    if not expanded:
        return False
    return (os.path.normcase(os.path.normpath(expanded))
            == os.path.normcase(os.path.normpath(str(directory))))


def ensure_user_path(directory: Path, *, registry=None, broadcast: Callable | None = None) -> dict:
    """Put ``directory`` first on the user PATH (Windows), read-modify-write:
    the existing value is kept byte for byte behind it, written back as an
    expandable string, and nothing happens when it is already there in any
    spelling. Prepended so it wins over pipx's ``%USERPROFILE%\\.local\\bin``
    and pip's user scripts directory, which may still hold an older
    ``pseudolife-mcp.exe`` (a running exe is never renamed or deleted).
    Returns ``{"state": "added"|"current"|"failed", "detail"}``."""
    registry = _UserEnvironment() if registry is None else registry
    broadcast = _broadcast_environment_change if broadcast is None else broadcast
    try:
        existing = registry.get("Path")
        value = str(existing[0]) if existing and existing[0] is not None else ""
        if any(_path_entry_is(entry, directory) for entry in value.split(";")):
            return {"state": "current", "detail": f"{directory} is already on your user PATH"}
        registry["Path"] = (f"{directory};{value}" if value else str(directory), _REG_EXPAND_SZ)
    except OSError as exc:
        return {"state": "failed", "detail": f"could not add {directory} to your user PATH ({exc})"}
    broadcast()
    return {"state": "added", "detail": f"added {directory} to the front of your user PATH"}


def expose_launcher(layout: Layout, *, env: dict | None = None, registry=None,
                    broadcast: Callable | None = None, which: Callable = shutil.which) -> dict:
    """Make ``pseudolife-mcp`` typed in a terminal reach the launcher: the
    user bin link on POSIX (:func:`link_user_bin`), the user PATH on
    Windows (:func:`ensure_user_path`), plus a ``hint`` when this terminal
    still resolves the name elsewhere. ``skipped`` for a layout that names
    neither (a hand-built or overridden one)."""
    env = os.environ if env is None else env
    if not _windows(layout):
        return link_user_bin(layout, env=env, which=which)
    if not layout.user_path:
        return {"state": "skipped", "detail": "the user PATH is not managed for this layout", "hint": None}
    result = ensure_user_path(layout.launcher_dir, registry=registry, broadcast=broadcast)
    result["hint"] = None
    if result["state"] != "failed":
        found = which(LINK_NAME, path=env.get("PATH"))
        if not found or not _same_path(found, str(layout.launcher)):
            result["hint"] = ("open a new terminal for `pseudolife-mcp` to resolve to the launcher; "
                              + (f"this one still runs {found}" if found else "this one does not find it yet"))
    return result


# ── processes ───────────────────────────────────────────────────────────────

def list_processes() -> list[tuple[int, int, str]] | None:
    """``(pid, parent pid, image)`` of every process this user can see, or
    ``None`` where no process table can be read on this platform. Raises
    ``OSError`` when the table exists but cannot be read.

    ``image`` is the path a runtime would be recognised by: on Windows the
    executable's full path; on Linux ``argv[0]`` when it is absolute (a
    virtualenv's ``bin/python`` is a symlink, and ``/proc/<pid>/exe``
    resolves it to the base interpreter, outside the runtime), else the
    ``exe`` link; on macOS the first word of ``ps``'s ``args``."""
    if os.name == "nt":
        return _windows_processes()
    if Path("/proc").is_dir():
        return _proc_processes()
    if sys.platform == "darwin":
        return _ps_processes()
    return None


def _windows_processes() -> list[tuple[int, int, str]]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):   # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents: list[tuple[int, int]] = []
    try:
        entry = ProcessEntry(dwSize=ctypes.sizeof(ProcessEntry))
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents.append((entry.th32ProcessID, entry.th32ParentProcessID))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not parents:
        raise ctypes.WinError(ctypes.get_last_error())
    rows: list[tuple[int, int, str]] = []
    image = ctypes.create_unicode_buffer(32768)
    for pid, ppid in parents:
        # PROCESS_QUERY_LIMITED_INFORMATION; a process this user may not
        # query (another account's, a protected one) is not one of its shims.
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(len(image))
            if kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                rows.append((pid, ppid, image.value))
        finally:
            kernel32.CloseHandle(handle)
    return rows


def _proc_processes() -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    proc = Path("/proc")
    try:
        names = os.listdir(proc)
    except OSError as exc:
        raise OSError(f"/proc unreadable: {exc}") from exc
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            stat = (proc / name / "stat").read_text(encoding="utf-8", errors="replace")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            argv = (proc / name / "cmdline").read_bytes().split(b"\0")
        except (OSError, ValueError, IndexError):
            continue
        first = argv[0].decode("utf-8", "replace") if argv and argv[0] else ""
        image = first if first.startswith("/") else ""
        if not image:
            try:
                image = os.readlink(proc / name / "exe")
            except OSError:
                continue
        rows.append((pid, ppid, image))
    if not rows:
        raise OSError("/proc listed no processes")
    return rows


def _ps_processes() -> list[tuple[int, int, str]]:
    code, out = run_cli(["ps", "-axo", "pid=,ppid=,args="], timeout=60)
    if code != 0:
        raise OSError(f"ps failed: {out.strip()}")
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[2].split()[0]))
    return rows


def _path_forms(path) -> set[str]:
    """A path as written and as resolved: Windows reports a process's image
    by its final path, while a registration may go through a junction,
    symlink or 8.3 short name."""
    forms = {os.path.normcase(os.path.abspath(path))}
    try:
        forms.add(os.path.normcase(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    return forms


def processes_inside(rows: list[tuple[int, int, str]], path: Path) -> list[int]:
    """The pids whose image is ``path`` or lies under it, this process and
    its parent excluded (an update may run from the runtime it inspects)."""
    held = _path_forms(path)
    own = {os.getpid(), os.getppid()}

    def inside(image: str) -> bool:
        return any(form == p or form.startswith(p.rstrip(os.sep) + os.sep)
                   for form in _path_forms(image) for p in held)

    return [pid for pid, _ppid, image in rows if pid not in own and inside(image)]


# ── removal ─────────────────────────────────────────────────────────────────

def remove_unused(layout: Layout, *, pinned: Iterable[Path] = (),
                  processes: Callable = list_processes,
                  log: Callable[[str], None] = lambda _line: None) -> dict:
    """Remove every runtime that is not the newest complete one, is not in
    ``pinned`` (a registration still names it) and has no process running
    from it; likewise unfinished runtimes older than an hour (a younger one
    may still be installing) and launchers moved aside. Where the process
    table cannot be read, nothing is removed and ``error`` says why; where
    the platform has no process table, only the newest is kept and
    everything else is left, named under ``unverified``."""
    result: dict = {"removed": [], "held": [], "kept": [], "unverified": [], "error": None}
    current = current_runtime(layout)
    complete = {os.path.normcase(str(r.path)) for r in list_runtimes(layout)}
    keep = {os.path.normcase(str(p)) for p in pinned}
    if current is not None:
        keep.add(os.path.normcase(str(current.path)))
    try:
        rows = processes()
    except OSError as exc:
        result["error"] = f"could not read the process table: {exc}"
        return result
    candidates: list[Path] = []
    try:
        for name in sorted(os.listdir(layout.root)):
            path = layout.root / name
            if _sequence_of(name) is None or not path.is_dir():
                continue
            if os.path.normcase(str(path)) not in complete:
                try:
                    young = time.time() - path.stat().st_mtime < _ABANDONED_AFTER_S
                except OSError:
                    young = True
                if young:
                    result["kept"].append(str(path))
                    continue
            candidates.append(path)
    except OSError:
        pass
    try:
        for name in sorted(os.listdir(layout.launcher_dir)):
            if name.startswith(_OLD_LAUNCHER_PREFIX):
                candidates.append(layout.launcher_dir / name)
    except OSError:
        pass
    for path in candidates:
        if os.path.normcase(str(path)) in keep:
            result["kept"].append(str(path))
            continue
        if rows is None:
            result["unverified"].append(str(path))
            continue
        pids = processes_inside(rows, path)
        if pids:
            result["held"].append({"path": str(path), "processes": len(pids)})
            continue
        try:
            if path.is_dir():
                # Rename first: a directory a process runs from cannot be
                # renamed on Windows (a second, free in-use check), and a
                # rename that succeeds takes the whole tree out of the
                # launcher's sight before any file inside is deleted.
                doomed = path.with_name(f"{path.name}.removing-{os.getpid()}")
                os.rename(path, doomed)
                shutil.rmtree(doomed)
            else:
                path.unlink()
        except OSError as exc:
            result["held"].append({"path": str(path), "processes": 0, "error": str(exc)})
            continue
        log(f"removed {path}")
        result["removed"].append(str(path))
    return result


# ── registrations ───────────────────────────────────────────────────────────

@dataclass
class Registration:
    client: str          # claude-code | codex | claude-desktop | gemini
    file: Path
    key: str             # the server's name inside that file
    command: str
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict = field(default_factory=dict)

    @property
    def spawns_a_daemon(self) -> bool:
        """Whether a session of this registration would start its own daemon
        when none answers: no ``PSEUDOLIFE_MCP_NO_SPAWN`` and a loopback (or
        default) daemon URL. A shim runtime cannot serve (no torch), so such
        a registration must not be moved onto one."""
        no_spawn = str(self.env.get("PSEUDOLIFE_MCP_NO_SPAWN", "")).strip().lower()
        if no_spawn in ("1", "true", "yes", "on"):
            return False
        url = str(self.env.get("PSEUDOLIFE_MCP_DAEMON_URL", "")).strip().lower()
        host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].rsplit(":", 1)[0].strip("[]")
        return host in ("", "127.0.0.1", "localhost", "::1")


def _claude_config_file(env: dict) -> Path:
    base = env.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else home(env)) / ".claude.json"


def _codex_config_file(env: dict) -> Path:
    return Path(env.get("CODEX_HOME") or home(env) / ".codex") / "config.toml"


def desktop_config_files(env: dict, windows: bool | None = None, system: str | None = None) -> list[Path]:
    """Where Claude Desktop keeps ``claude_desktop_config.json`` on this
    OS, packaged (MSIX) caches first on Windows."""
    windows = (os.name == "nt") if windows is None else windows
    system = system or sys.platform
    user = home(env)
    found: list[Path] = []
    if windows:
        local = env.get("LOCALAPPDATA")
        if local:
            for package in sorted(Path(local).glob("Packages/Claude_*")):
                found.append(package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
        appdata = env.get("APPDATA") or str(user / "AppData" / "Roaming")
        found.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        return found
    if system == "darwin":
        return [user / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    config = Path(env.get("XDG_CONFIG_HOME") or user / ".config")
    return [config / "Claude" / "claude_desktop_config.json"]


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _json_registrations(client: str, path: Path, keys: Iterable[str]) -> list[Registration]:
    data = _read_json(path)
    servers = data.get("mcpServers") if data else None
    if not isinstance(servers, dict):
        return []
    found = []
    for key in keys:
        entry = servers.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            continue
        if entry.get("type") not in (None, "stdio"):
            continue
        args = entry.get("args") if isinstance(entry.get("args"), list) else []
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        found.append(Registration(client, path, key, entry["command"], [str(a) for a in args],
                                  entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
                                  {str(k): str(v) for k, v in env.items()}))
    return found


def _toml_loads(text: str) -> dict | None:
    try:
        import tomllib
    except ModuleNotFoundError:   # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None
    try:
        return tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError):
        return None


def _codex_registrations(path: Path) -> list[Registration]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    data = _toml_loads(text)
    if data is None:
        return []
    entry = (data.get("mcp_servers") or {}).get(SERVER) if isinstance(data.get("mcp_servers"), dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return []
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    return [Registration("codex", path, SERVER, entry["command"], [str(a) for a in args],
                         entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
                         {str(k): str(v) for k, v in env.items()})]


def find_registrations(env: dict | None = None, windows: bool | None = None) -> list[Registration]:
    """Every stdio registration of the shim this user's clients hold."""
    env = os.environ if env is None else env
    found: list[Registration] = []
    found += _json_registrations("claude-code", _claude_config_file(env), (SERVER,))
    found += _codex_registrations(_codex_config_file(env))
    for path in desktop_config_files(env, windows):
        found += _json_registrations("claude-desktop", path, (DESKTOP_SERVER, SERVER))
    found += _json_registrations("gemini", home(env) / ".gemini" / "settings.json", (SERVER,))
    return found


def _same_path(a: str, b: str) -> bool:
    return bool(_path_forms(a) & _path_forms(b))


def _under(path: str, root: Path) -> bool:
    forms = _path_forms(root)
    return any(form.startswith(r.rstrip(os.sep) + os.sep) for form in _path_forms(path) for r in forms)


def registers_launcher(registration: Registration, layout: Layout) -> bool:
    return _same_path(registration.command, str(layout.launcher))


def registers_runtime_path(registration: Registration, layout: Layout, roots: Iterable[Path] = ()) -> bool:
    """Whether the registration names a shim inside the runtimes root (a
    managed runtime, or a hand-made one placed there), under one of
    ``roots`` (a pipx venv, a scripts directory the caller has identified
    as the shim's) or exactly one of them (the launcher file itself, as
    pipx's bin-dir copy or a pip --user script)."""
    command = registration.command
    return (_under(command, layout.root)
            or any(_under(command, r) or _same_path(command, str(r)) for r in roots))


def registered_runtime(registration: Registration, layout: Layout) -> Path | None:
    """The runtime directory under the root a registration names, if any
    (the directory directly under the root, whatever it is called)."""
    if not _under(registration.command, layout.root):
        return None
    for form in _path_forms(registration.command):
        for root in _path_forms(layout.root):
            prefix = root.rstrip(os.sep) + os.sep
            if form.startswith(prefix):
                return layout.root / form[len(prefix):].split(os.sep, 1)[0]
    return None


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 0
    while backup.exists():
        counter += 1
        backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
    shutil.copy2(path, backup)
    return backup


def _toml_string(value: str) -> str:
    if "'" not in value and "\n" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_TOML_HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_TOML_KEY = re.compile(r"^\s*(command|args|cwd)\s*=")


def _migrate_codex_text(text: str, launcher: str, args: list) -> str | None:
    """The config with the shim's table pointed at the launcher, or ``None``
    when the table is not there in a shape this can edit."""
    lines = text.splitlines(keepends=True)
    start = end = None
    for index, line in enumerate(lines):
        header = _TOML_HEADER.match(line)
        if not header:
            continue
        name = header.group(1).strip().replace('"', "").replace("'", "")
        if start is None:
            if name == f"mcp_servers.{SERVER}":
                start = index + 1
        else:
            end = index
            break
    if start is None:
        return None
    end = len(lines) if end is None else end
    edited = lines[:start]
    saw_command = False
    for line in lines[start:end]:
        key = _TOML_KEY.match(line)
        if not key:
            edited.append(line)
            continue
        name = key.group(1)
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        if name == "command":
            edited.append(f"command = {_toml_string(launcher)}{newline}")
            saw_command = True
        elif name == "args":
            edited.append(f"args = {json.dumps(args)}{newline}")
        # cwd: dropped — the launcher needs no working directory.
    if not saw_command:
        return None
    edited += lines[end:]
    return "".join(edited)


def migrate_registration(registration: Registration, layout: Layout, *, backup: bool = True) -> dict:
    """Point one registration at the launcher, in place. Returns ``{"state",
    "detail", "backup"}`` with state ``current`` (already the launcher),
    ``migrated``, ``manual`` (this cannot edit the file; the detail says what
    to change) or ``failed`` (the write did not verify; the backup was put
    back)."""
    launcher = str(layout.launcher)
    args = migrated_args(registration.args)
    if registers_launcher(registration, layout) and registration.args == args and not registration.cwd:
        return {"state": "current", "detail": f"{registration.client}: already {launcher}", "backup": None}
    path = registration.file
    shown_args = json.dumps(args)
    if registration.client == "codex":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"state": "failed", "detail": f"codex: cannot read {path}: {exc}", "backup": None}
        before = _toml_loads(text)
        edited = _migrate_codex_text(text, launcher, args)
        if before is None or edited is None:
            return {"state": "manual",
                    "detail": f"codex: could not edit {path}; set command = {_toml_string(launcher)}, "
                              f"args = {shown_args} and remove cwd under [mcp_servers.{SERVER}]",
                    "backup": None}
        after = _toml_loads(edited)
        expected = copy.deepcopy(before)
        expected["mcp_servers"][SERVER]["command"] = launcher
        expected["mcp_servers"][SERVER].pop("cwd", None)
        if "args" in expected["mcp_servers"][SERVER]:
            expected["mcp_servers"][SERVER]["args"] = args
        if after != expected:
            return {"state": "manual",
                    "detail": f"codex: the edit of {path} would change more than the shim's table; "
                              f"set command = {_toml_string(launcher)}, args = {shown_args} and remove cwd "
                              f"under [mcp_servers.{SERVER}] by hand",
                    "backup": None}
        saved = _backup(path) if backup else None
        try:
            _replace_config(path, edited)
        except OSError as exc:
            # Our own write failed: the file is whatever it was; the backup
            # is put back only in case the replace left it partial.
            if saved is not None:
                shutil.copy2(saved, path)
            return {"state": "failed", "detail": f"codex: writing {path} failed ({exc}); restored",
                    "backup": str(saved) if saved else None}
        if _toml_loads(path.read_text(encoding="utf-8")) != expected:
            # Someone else wrote after us (Codex itself, say): theirs is the
            # newer content and stays; the backup is not put over it.
            return {"state": "failed", "detail": f"codex: {path} changed under the edit (read-back differs); "
                                                 f"left as it is now, backup kept", "backup": str(saved) if saved else None}
        return {"state": "migrated", "detail": f"codex: {path} now runs {launcher}", "backup": str(saved) if saved else None}
    data = _read_json(path)
    servers = data.get("mcpServers") if data else None
    entry = servers.get(registration.key) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return {"state": "manual", "detail": f"{registration.client}: {path} no longer holds {registration.key}",
                "backup": None}
    saved = _backup(path) if backup else None
    entry["command"] = launcher
    entry["args"] = args
    entry.pop("cwd", None)
    try:
        _replace_config(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        if saved is not None:
            shutil.copy2(saved, path)
        return {"state": "failed", "detail": f"{registration.client}: writing {path} failed ({exc}); restored",
                "backup": str(saved) if saved else None}
    check = _read_json(path)
    written = (check or {}).get("mcpServers", {}).get(registration.key, {})
    if written.get("command") != launcher or written.get("args") != args:
        return {"state": "failed", "detail": f"{registration.client}: {path} changed under the edit (read-back "
                                             f"differs); left as it is now, backup kept",
                "backup": str(saved) if saved else None}
    return {"state": "migrated", "detail": f"{registration.client}: {path} [{registration.key}] now runs {launcher}",
            "backup": str(saved) if saved else None}


def migrated_args(args: list) -> list:
    """The arguments a registration keeps on the launcher: a leading
    ``-m pseudolife_memory.cli`` (the Codex registration form) goes, a mode
    such as ``channel`` stays."""
    args = [str(a) for a in args]
    if len(args) >= 2 and args[0] == "-m" and args[1] in ("pseudolife_memory.cli", "pseudolife_memory"):
        return args[2:]
    return args


def registers_bare_shim(registration: Registration) -> bool:
    """A registration of ``pseudolife-mcp`` by name alone, found on PATH at
    session start: what the launcher's absolute path replaces."""
    command = registration.command.strip()
    return (os.path.basename(command) == command
            and command.lower().removesuffix(".exe") == "pseudolife-mcp")


def migrate_registrations(layout: Layout, registrations: Iterable[Registration] | None = None, *,
                          roots: Iterable[Path] = (), bare: bool = False, env: dict | None = None) -> list[dict]:
    """Move every registration that names a runtime path (or a path under
    ``roots``, or with ``bare`` the shim by name alone) to the launcher;
    others are reported and left. Idempotent: a registration already on the
    launcher reads ``current``. A registration reached through ``roots`` or
    ``bare`` names a full install; one that would spawn its own daemon
    (``spawns_a_daemon``) stays there, since a runtime cannot serve — the
    same rule ``update_clients`` applies (review, 2026-09-29)."""
    registrations = find_registrations(env) if registrations is None else list(registrations)
    results = []
    for registration in registrations:
        if registers_launcher(registration, layout) or registers_runtime_path(registration, layout):
            result = migrate_registration(registration, layout)
        elif registers_runtime_path(registration, layout, roots) or (bare and registers_bare_shim(registration)):
            if registration.spawns_a_daemon:
                result = {"state": "spawning", "backup": None,
                          "detail": f"{registration.client}: {registration.command} spawns its own daemon "
                                    "(no PSEUDOLIFE_MCP_NO_SPAWN=1 and a loopback daemon URL), which a shim "
                                    "runtime cannot serve; left as it is"}
            else:
                result = migrate_registration(registration, layout)
        else:
            result = {"state": "left", "detail": f"{registration.client}: {registration.command} is not a "
                                                 f"managed runtime path; left as it is", "backup": None}
        result["client"] = registration.client
        result["file"] = str(registration.file)
        results.append(result)
    return results


def pinned_runtimes(layout: Layout, registrations: Iterable[Registration]) -> set[Path]:
    """Runtime directories some registration still names directly."""
    pinned = set()
    for registration in registrations:
        runtime = registered_runtime(registration, layout)
        if runtime is not None:
            pinned.add(runtime)
    return pinned


# ── command line ────────────────────────────────────────────────────────────

def _print_result(result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    elif isinstance(result, list):
        for item in result:
            print(f"  {item}")
    else:
        print(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shim_runtime",
                                     description="side-by-side shim runtimes behind one launcher path")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install", help="install a new runtime beside the existing ones")
    p_install.add_argument("--source", required=True, help="a checkout path or a pip requirement")
    p_install.add_argument("--python", default=None, help="base interpreter for the runtime")
    p_install.add_argument("--no-prune", action="store_true", help="keep unused runtimes")
    sub.add_parser("list", help="the complete runtimes, oldest first")
    sub.add_parser("current", help="the runtime the launcher runs next (exit 3 when none)")
    p_launcher = sub.add_parser("launcher", help="the launcher path")
    p_launcher.add_argument("--if-installed", action="store_true",
                            help="exit 3 unless the launcher and a complete runtime exist")
    sub.add_parser("prune", help="remove runtimes nothing runs or names")
    sub.add_parser("expose", help="make `pseudolife-mcp` in a terminal reach the launcher "
                                  "(POSIX: a ~/.local/bin link; Windows: the user PATH)")
    p_migrate = sub.add_parser("migrate", help="point registrations that name a runtime path at the launcher")
    p_migrate.add_argument("--client", action="append", default=None,
                           help="only this client (claude-code, codex, claude-desktop, gemini)")
    p_migrate.add_argument("--from", dest="roots", action="append", default=[],
                           help="also migrate registrations under this path (a pipx venv, a scripts dir)")
    p_migrate.add_argument("--bare", action="store_true",
                           help="also migrate registrations of pseudolife-mcp by name alone")
    args = parser.parse_args(argv)
    layout = default_layout()
    # Progress goes to stderr: a caller reads the launcher path (or the JSON
    # report) from stdout alone.
    log = (lambda _line: None) if args.json else (lambda line: print(f"  {line}", file=sys.stderr))
    if args.command == "install":
        try:
            runtime = install(args.source, layout, python=args.python, log=log)
        except RuntimeInstallError as exc:
            if args.json:
                _print_result({"state": "failed", "step": exc.step, "detail": str(exc), "output": exc.output}, True)
            else:
                print(f"install failed at {exc.step}: {exc}\n{exc.output}", file=sys.stderr)
            return 1
        pruned = None if args.no_prune else remove_unused(
            layout, pinned=pinned_runtimes(layout, find_registrations()), log=log)
        if args.json:
            _print_result({"state": "installed", "runtime": str(runtime.path), "version": runtime.version,
                           "launcher": str(layout.launcher), "pruned": pruned}, True)
        else:
            print(str(layout.launcher))
        return 0
    if args.command == "list":
        runtimes = list_runtimes(layout)
        _print_result([{"path": str(r.path), "version": r.version, "installed_at": r.installed_at,
                        "source": r.source, "source_commit": r.source_commit} for r in runtimes]
                      if args.json else [f"{r.name}  {r.installed_at}  {r.source}" for r in runtimes], args.json)
        return 0
    if args.command == "current":
        runtime = current_runtime(layout)
        if runtime is None:
            return 3
        _print_result({"path": str(runtime.path), "version": runtime.version} if args.json else str(runtime.path),
                      args.json)
        return 0
    if args.command == "launcher":
        if args.if_installed and not (layout.launcher.is_file() and current_runtime(layout)):
            return 3
        print(str(layout.launcher))
        return 0
    if args.command == "expose":
        result = expose_launcher(layout)
        if args.json:
            _print_result(result, True)
        else:
            print(result["detail"])
            if result.get("hint"):
                print(result["hint"])
        return 1 if result["state"] == "failed" else 0
    if args.command == "prune":
        result = remove_unused(layout, pinned=pinned_runtimes(layout, find_registrations()), log=log)
        _print_result(result, args.json)
        return 0 if not result["error"] else 1
    if args.command == "migrate":
        registrations = find_registrations()
        if args.client:
            registrations = [r for r in registrations if r.client in args.client]
        results = migrate_registrations(layout, registrations, roots=[Path(r) for r in args.roots], bare=args.bare)
        if args.json:
            _print_result(results, True)
        else:
            for r in results:
                print(f"  {r['state']:<9} {r['detail']}", file=sys.stderr)
        if any(r["state"] == "failed" for r in results):
            return 1
        return 0 if any(r["state"] in ("migrated", "current") for r in results) else 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
