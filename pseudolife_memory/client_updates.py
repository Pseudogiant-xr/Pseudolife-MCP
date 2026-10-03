"""Move the client side with the daemon: the shim, the plugin cache, Codex hooks.

The daemon is one of three installs. The stdio shim behind each registration
and the Claude Code plugin cache are separate installs that each needed
their own step, and on 2026-09-21 each was forgotten in turn. This module
moves them together: ``ops/update_clients.py`` runs it from a checkout
(``ops/update.ps1 -All`` / ``update.sh --all`` call that after the daemon
is healthy), and ``pseudolife-mcp update`` runs it from an installed
package with no checkout at all, installing the shim runtime from PyPI.

    python ops/update_clients.py [--repo PATH] [--source SPEC] [--only shim,plugin,codex] [--json]

Shim: every stdio registration of the shim (Claude Code's ``~/.claude.json``,
Codex's ``config.toml``, Claude Desktop's and Gemini CLI's config files)
is read from its file. When any of them runs the launcher, a runtime under
the runtimes root, a pipx-managed launcher or a virtualenv's launcher or
``python -m pseudolife_memory.cli``, a NEW shim runtime is installed from
``source`` (the checkout, or ``pseudolife-mcp==<version>``) beside the
existing ones (``pseudolife_memory.runtimes``), the launcher every client
should run is put in place, and registrations that still name a runtime,
pipx or virtualenv path are moved to the launcher in place, each file
backed up first. Nothing running is ever replaced: sessions keep the
runtime they started from, the next session start takes the new one, and
an old runtime is removed only once no process runs from it and no
registration names it. A launcher inside the checkout's ``.venv`` is an
editable install — the code is already live and is named, not
reinstalled. A registration that would spawn its own daemon (pip / lite
tier) stays where its full install is. Anything else is left alone and
named.

Plugin: the manifest carries no version, so Claude Code names each
marketplace commit's copy by its commit. The marketplace clone is refreshed,
its plugin tree compared by bytes (CRLF read as LF, Claude Code's own
markers skipped) against the installed cache, and only when they differ is
``claude plugin update`` run: it installs the new copy beside the one
running sessions loaded and switches the record, then the tree is compared
again, because the read-back is the proof. Nothing is ever uninstalled.

Codex: Codex approves a hook by its definition in hooks.json, not by the
script it runs (measured on Codex 0.158.0, 2026-09-30). Manual copies run
through a launcher whose commands never change, so this refreshes them to
the checkout's scripts with no approval. A plugin copy whose scripts are
behind while its hooks.json is current is refreshed through Codex's own
``codex plugin marketplace upgrade`` (its approvals carry over); only a
changed hooks.json names the approval steps (``ops/setup-codex-hooks.py``).

Every CLI call goes through :func:`run_cli`, client homes through
:func:`home`, and ordinary CLI lookups through :func:`which`. Codex and
the git used to inspect its marketplace resolve only from absolute paths,
never implicitly from the working directory, through
:func:`codex_executable` and :func:`_git_executable`, so tests drive the
logic with fakes and never touch this machine's registrations. Nothing here prints
tokens: registration files are parsed for the command line only.
``ops/update_clients.py`` runs this from the checkout by putting it first
on ``sys.path``, so an older installed release never answers for the
checkout being installed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pseudolife_memory import __version__
from pseudolife_memory import runtimes as _runtimes
from pseudolife_memory.plugin_hooks import HOOK_SCRIPTS, hooks_digest

# The checkout this module runs from, when it does (the package sits at its
# root); an installed package has no checkout and ``checkout_root()`` is None.
ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
MARKETPLACE = "pseudolife-mcp"
SERVER = "pseudolife-memory"   # the MCP server name registered with clients
PACKAGE = "pseudolife-mcp"     # the distribution / launcher / pipx venv name
INSTALLER_HINT = "run the installer (ops/install.sh or ops\\install.ps1) with claude as a client"


# ── seams ───────────────────────────────────────────────────────────────────

def which(name: str) -> str | None:
    return shutil.which(name)


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())


# run_cli's code for a command it stopped at its timeout (GNU timeout's).
TIMED_OUT = 124


def run_cli(argv, *, timeout: int = 600, cwd: str | None = None, env: dict | None = None,
            encoding: str | None = None) -> tuple[int, str]:
    """Run a CLI and return ``(returncode, combined output)``; a missing
    executable reads as code 1 and a hung one as ``TIMED_OUT``, with the
    reason as output."""
    try:
        # No stdin: a CLI that decides to prompt fails fast instead of holding
        # a captured-output deploy for the whole timeout with nothing shown.
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              timeout=timeout, check=False, encoding=encoding, errors="replace",
                              stdin=subprocess.DEVNULL, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run stops the direct process only. An npm codex.cmd
        # wrapper on Windows can leave its Codex child running after a
        # timeout; this helper does not own or terminate the process tree.
        return TIMED_OUT, f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── the shim ────────────────────────────────────────────────────────────────

def _registered_command(text: str) -> tuple[str, str] | None:
    """``(command, args)`` from a ``claude mcp get`` / ``codex mcp get``
    listing, or ``None`` for an HTTP registration or nothing usable."""
    command = re.search(r"^\s*command:\s*(.+?)\s*$", text, re.I | re.M)
    if not command:
        return None
    args = re.search(r"^\s*args:\s*(.*?)\s*$", text, re.I | re.M)
    return command.group(1), (args.group(1) if args else "")


def _interpreter_beside(launcher: Path) -> Path | None:
    for name in ("python.exe", "python", "python3"):
        candidate = launcher.parent / name
        if candidate.is_file():
            return candidate
    return None


_PYTHON_STEM = re.compile(r"python3?(?:\.\d+)?")
_USER_SCRIPTS_PROBE = "import os, sysconfig; print(sysconfig.get_path('scripts', os.name + '_user'))"
# Where the interpreter imports the package from, beside its library dirs:
# a package imported from outside them is an editable / source-tree install.
_INSTALL_KIND_PROBE = (
    "import json, os, sysconfig\n"
    "try:\n    import pseudolife_memory as p; f = os.path.dirname(os.path.abspath(p.__file__))\n"
    "except Exception:\n    f = None\n"
    "libs = [sysconfig.get_paths().get('purelib'), sysconfig.get_paths().get('platlib')]\n"
    "try:\n    libs.append(sysconfig.get_path('purelib', os.name + '_user'))\n"
    "except Exception:\n    pass\n"
    "print(json.dumps({'package_dir': f, 'libs': [l for l in libs if l]}))"
)


_install_kinds: dict[str, tuple[str, str, list[str]]] = {}


def install_kind(interpreter: Path) -> tuple[str, str]:
    """One probe per interpreter per run (a registration is classified,
    de-duplicated and then reported, each of which asks)."""
    return _probed(interpreter)[:2]


def install_libs(interpreter: Path) -> list[Path]:
    """The library directories the probe reported for this interpreter."""
    return [Path(lib) for lib in _probed(interpreter)[2]]


def _probed(interpreter: Path) -> tuple[str, str, list[str]]:
    key = str(interpreter).lower()
    if key not in _install_kinds:
        _install_kinds[key] = _probe_install_kind(interpreter)
    return _install_kinds[key]


def _probe_install_kind(interpreter: Path) -> tuple[str, str, list[str]]:
    """``("editable", <project dir>)`` when the interpreter imports the
    package from outside its library directories (a PEP 660 editable
    install, a legacy egg-link, or a .pth pointing at a checkout),
    ``("site", "")`` when it imports from a library directory,
    ``("missing", "")`` when the probe answered that it does not import at
    all, and ``("unknown", <reason>)`` when the probe did not answer — each
    followed by the library directories the probe reported (none when it
    did not answer). "The probe failed" and "the package is absent" are different facts and
    only the second licenses a pip install. Editable-ness is a property of
    the install, not of any path this helper knows: on 2026-09-21 a
    path-only check pip-upgraded the maintainer's checkout venv and
    stripped its editable install."""
    # From a neutral directory: `python -c` puts the working directory first
    # on sys.path, and this helper runs from a checkout that holds the
    # package, which would make every interpreter look editable.
    code, out = run_cli([str(interpreter), "-c", _INSTALL_KIND_PROBE], timeout=60,
                        cwd=tempfile.gettempdir())
    if code != 0:
        return "unknown", f"probe exited {code}: {_failure_line(out, code)}", []
    # run_cli appends stderr after stdout, so a warning from a .pth or
    # sitecustomize can follow the JSON line: find the report, wherever it is.
    report = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "libs" in candidate:
            report = candidate
            break
    if report is None:
        return "unknown", f"probe printed no report: {_failure_line(out, code)}", []
    libs = [str(lib) for lib in report.get("libs") or [] if lib]
    package_dir = report.get("package_dir")
    if not package_dir:
        return "missing", "", libs
    package = Path(package_dir)
    for lib in libs:
        try:
            if package.resolve().is_relative_to(Path(lib).resolve()):
                return "site", str(package), libs
        except (OSError, ValueError):
            continue
    return "editable", str(package.parent), libs


def _pip_or_editable(kind: str, interpreter: Path) -> tuple[str, Path | None]:
    """Never pip over an editable install: the code is live from its
    source tree and the upgrade would replace it with a frozen copy (or,
    with the launcher in use, roll back and strip the install)."""
    probed = install_kind(interpreter)[0]
    if probed == "editable":
        return "editable", interpreter
    if probed == "unknown":
        # Not classifiable is not "safe to upgrade": name it, leave it.
        return "unknown", interpreter
    return kind, interpreter


def _failure_line(out: str, code: int) -> str:
    """pip's real complaint: its last ``ERROR:`` line, ahead of an earlier
    ``WARNING: Error parsing …`` and of the trailing ``[notice]`` lines."""
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if line.upper().startswith("ERROR"):
            return line
    for line in lines:
        if "Error" in line:
            return line
    return lines[-1] if lines else str(code)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _pipx_owns(launcher: Path, pipx_listing: dict) -> bool:
    """Whether the launcher is one pipx installed for ``pseudolife-mcp``
    (``pipx list --json`` names each app path), or at least lives under a
    pipx tree. A pipx venv of the same name elsewhere is not a reason to
    ``pipx install --force`` over an unrelated registration."""
    venv = next((v for k, v in pipx_listing.get("venvs", {}).items() if k.lower() == PACKAGE), None)
    if not isinstance(venv, dict):
        return False
    paths = venv.get("metadata", {}).get("main_package", {}).get("app_paths", [])
    for entry in paths:
        candidate = entry.get("__Path__") if isinstance(entry, dict) else entry
        if candidate and _same_dir(Path(str(candidate)), launcher):
            return True
    return "pipx" in str(launcher).lower()


def _user_scripts_owner(launcher: Path) -> Path | None:
    """The interpreter whose ``pip install --user`` scripts directory holds
    the launcher — the installers' non-pipx path puts it there with no
    interpreter beside it."""
    seen: set[str] = set()
    for name in ("python3", "python"):
        candidate = which(name)
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        code, out = run_cli([candidate, "-c", _USER_SCRIPTS_PROBE], timeout=60)
        if code == 0 and out.strip() and _same_dir(Path(out.strip().splitlines()[-1]), launcher.parent):
            return Path(candidate)
    return None


def _classify(command: str, args: str, repo: Path | None, pipx_listing: dict | None = None) -> tuple[str, Path | None]:
    """How the registered shim was installed: ``editable``, ``pip`` (with
    the interpreter to use), ``pip-user`` (with the owning interpreter),
    ``pipx``, or ``unmanaged``."""
    path = Path(command)
    try:
        inside_checkout = repo is not None and path.resolve().is_relative_to((repo / ".venv").resolve())
    except (OSError, ValueError):
        inside_checkout = False
    if inside_checkout:
        return "editable", None
    stem = path.name.lower().removesuffix(".exe")
    if _PYTHON_STEM.fullmatch(stem) and "pseudolife_memory" in args and path.is_file():
        return _pip_or_editable("pip", path)
    if stem == PACKAGE:
        if pipx_listing is not None and _pipx_owns(path, pipx_listing):
            return "pipx", None
        interpreter = _interpreter_beside(path)
        if interpreter is not None:
            return _pip_or_editable("pip", interpreter)
        owner = _user_scripts_owner(path)
        if owner is not None:
            return _pip_or_editable("pip-user", owner)
    return "unmanaged", None


# ── the runtime behind the launcher ─────────────────────────────────────────
#
# 2026-09-25: `pip install --upgrade` into a runtime that ~36 sessions were
# running failed with WinError 32 on its Scripts\pseudolife-mcp.exe and left
# the runtime without its package (pip had stashed site-packages first);
# pipx's `install --force` deletes the venv before rebuilding it. Every
# registration therefore had to wait for every session to close, and on
# 2026-09-28 thirteen idle Desktop sessions held the step. The shim now
# installs into a NEW runtime beside the old one (pseudolife_memory/
# runtimes.py) behind one launcher path that starts the newest complete
# runtime; sessions already running keep theirs, and an old runtime goes
# only once nothing runs from it and no registration names it.

def checkout_root() -> Path | None:
    """The checkout this module runs from, or ``None`` for an installed
    package (no ``pyproject.toml`` beside the package)."""
    return ROOT if (ROOT / "pyproject.toml").is_file() and (ROOT / "ops").is_dir() else None


def runtimes_module():
    """``pseudolife_memory.runtimes`` (a seam the tests replace)."""
    return _runtimes


def list_processes() -> list[tuple[int, int, str]] | None:
    """``(pid, parent pid, image path)`` of every process this user can
    query, ``None`` where the platform has no process table (see
    ``runtimes.list_processes``); raises ``OSError`` when it cannot be read."""
    return runtimes_module().list_processes()


def client_env() -> dict:
    """The environment the client configs are found in: this process's, with
    the home directory the ``home`` seam names."""
    user = str(home())
    return {**os.environ, "HOME": user, "USERPROFILE": user}


def _venv_root(path: Path) -> Path | None:
    root = path.parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def _pipx_venv_roots(pipx_listing: dict) -> list[Path]:
    """The pipx venv of ``pseudolife-mcp``, from the app paths its metadata
    names inside it."""
    venv = next((v for k, v in pipx_listing.get("venvs", {}).items() if k.lower() == PACKAGE), None)
    paths = venv.get("metadata", {}).get("main_package", {}).get("app_paths", []) if isinstance(venv, dict) else []
    roots = []
    for entry in paths:
        candidate = entry.get("__Path__") if isinstance(entry, dict) else entry
        root = _venv_root(Path(str(candidate))) if candidate else None
        if root and root not in roots:
            roots.append(root)
    return roots


def _extractor_pythons_in(repo: Path | None, venvs: list[Path]) -> list[tuple[str, str]]:
    """The ``PSEUDOLIFE_<KIND>_SHIM_PYTHON`` entries of ``<repo>/ops/.env``
    whose interpreter lives inside one of ``venvs``. ``ops/shim_python.py``
    may pick pipx's venv for an extractor shim autostart unit (POSIX) and
    records it there, so uninstalling that venv breaks the unit (2026-09-29).
    Paths are compared unresolved: a venv's python is often a symlink out of
    it."""
    if repo is None or not venvs:
        return []
    try:
        lines = (Path(repo) / "ops" / ".env").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    roots = [os.path.normcase(os.path.abspath(v)) for v in venvs]
    held = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip("'\"")
        if not (sep and value and key.startswith("PSEUDOLIFE_") and key.endswith("_SHIM_PYTHON")):
            continue
        path = os.path.normcase(os.path.abspath(value))
        if any(path == root or path.startswith(root + os.sep) for root in roots):
            held.append((key, value))
    return held


def _legacy_directories(rt, layout) -> list[Path]:
    """Directories under the runtimes root that are not numbered runtimes:
    hand-made virtualenvs from before the launcher, left for the operator."""
    try:
        names = sorted(os.listdir(layout.root))
    except OSError:
        return []
    return [layout.root / n for n in names if rt._sequence_of(n) is None and (layout.root / n).is_dir()]


def update_shim(source, repo: Path | None = None) -> dict:
    """Install ``source`` (a checkout path, or a requirement such as
    ``pseudolife-mcp==0.15.1``) as a new shim runtime and move the
    registrations to the launcher. ``repo`` is the checkout whose ``.venv``
    counts as an editable install; it defaults to ``source`` when that is a
    directory."""
    source = str(source)
    if repo is None and Path(source).is_dir():
        repo = Path(source)
    repo = Path(repo) if repo else None
    upgrade_hint = f'"{repo}"' if repo else source
    rt = runtimes_module()
    env = client_env()
    layout = rt.default_layout(env)
    registrations = rt.find_registrations(env)
    if not registrations:
        return {"state": "not-registered",
                "detail": f"no stdio registration of {SERVER} in Claude Code, Codex, Claude Desktop or "
                          f"Gemini CLI; nothing to upgrade ({INSTALLER_HINT})"}
    pipx = which("pipx")
    pipx_listing: dict = {}
    if pipx:
        code, text = run_cli([pipx, "list", "--json"])
        try:
            pipx_listing = json.loads(text) if code == 0 else {}
        except ValueError:
            pipx_listing = {}
        if not isinstance(pipx_listing, dict):
            pipx_listing = {}
    pipx_roots = _pipx_venv_roots(pipx_listing)
    pipx_venvs = list(pipx_roots)
    if pipx:
        # pipx exposes a COPY of the launcher in its bin dir on Windows
        # without Developer Mode (no symlink): that copy is pipx's too.
        code, text = run_cli([pipx, "environment", "--value", "PIPX_BIN_DIR"])
        bin_dir = text.strip().splitlines()[-1].strip() if code == 0 and text.strip() else ""
        if bin_dir:
            pipx_roots.append(Path(bin_dir) / f"pseudolife-mcp{'.exe' if os.name == 'nt' else ''}")
    managed: list = []          # (registration, kind)
    results: list[dict] = []
    for registration in registrations:
        if rt.registers_launcher(registration, layout):
            managed.append((registration, "launcher"))
            continue
        if rt.registers_runtime_path(registration, layout):
            managed.append((registration, "runtime"))
            continue
        pipx_owned = rt.registers_runtime_path(registration, layout, pipx_roots)
        kind, interpreter = ("pipx", None) if pipx_owned else _classify(
            registration.command, " ".join(registration.args), repo, pipx_listing)
        if kind in ("pipx", "pip", "pip-user") and registration.spawns_a_daemon:
            # A shim runtime holds no daemon (no torch): a registration that
            # would spawn one (pip / lite tier, no PSEUDOLIFE_MCP_NO_SPAWN)
            # stays where its full install is.
            results.append({"state": "spawning",
                            "detail": f"{registration.client}: {registration.command} spawns its own daemon "
                                      "(no PSEUDOLIFE_MCP_NO_SPAWN=1 and a loopback daemon URL), which a shim "
                                      "runtime cannot serve; left as it is. Upgrade its own install: "
                                      f"<its python> -m pip install --upgrade {upgrade_hint}"})
        elif kind in ("pipx", "pip", "pip-user"):
            managed.append((registration, kind))
        elif kind == "editable":
            venv_python = interpreter or _interpreter_beside(Path(registration.command)) \
                or Path(registration.command).parent / "python"
            probed, found = install_kind(venv_python) if Path(venv_python).is_file() else ("", "")
            project = found if probed == "editable" else ""
            results.append({"state": "editable",
                            "detail": f"{registration.client}: {registration.command} runs a source tree directly"
                                      + (f" ({project})" if project else "")
                                      + "; the code is already live and is never reinstalled. To refresh its "
                                      f"package metadata, close every session and run: "
                                      f"\"{venv_python}\" -m pip install -e \"{project or repo}\" --no-deps"})
        elif kind == "unknown":
            reason = install_kind(interpreter)[1]
            results.append({"state": "unknown",
                            "detail": f"{registration.client}: could not tell how \"{interpreter}\" installed the "
                                      f"package ({reason}); left alone. Once sure it is not an editable install, "
                                      f"re-run the installer to move this registration to the launcher"})
        else:
            results.append({"state": "unmanaged",
                            "detail": f"{registration.client}: {registration.command} {' '.join(registration.args)}".strip()
                                      + " is not a registration this helper recognises (the launcher, a runtime "
                                      "under the runtimes root, pipx, a virtualenv's launcher or python -m, or "
                                      "pip --user); upgrade it in its own environment, e.g. <its python> -m pip "
                                      f"install --upgrade {upgrade_hint}"})
    if not managed:
        order = ("failed", "unknown", "spawning", "editable", "unmanaged")
        results.sort(key=lambda r: order.index(r["state"]) if r["state"] in order else len(order))
        return {"state": results[0]["state"], "detail": "; ".join(r["detail"] for r in results)}
    lines: list[str] = []
    try:
        runtime = rt.install(source, layout, run=run_cli, log=lines.append)
    except rt.RuntimeInstallError as exc:
        retry = (f"python \"{repo / 'ops' / 'update_clients.py'}\" --only shim --repo \"{repo}\"" if repo
                 else "pseudolife-mcp update --clients-only")
        detail = (f"installing a new shim runtime from {source} failed at {exc.step} ({_failure_line(exc.output, 1)}); "
                  f"registrations were not touched and the runtime that was current stays current. "
                  f"Retry: {retry}")
        return {"state": "failed", "detail": "; ".join([detail] + [r["detail"] for r in results])}
    detail = [f"runtime {runtime.name} ({runtime.version}"
              + (f", commit {runtime.source_commit[:8]}" if runtime.source_commit else "")
              + f") installed from {source} behind {layout.launcher}"]
    failed = False
    for registration, kind in managed:
        if kind == "launcher":
            detail.append(f"{registration.client}: already runs the launcher")
            continue
        moved = rt.migrate_registration(registration, layout)
        failed = failed or moved["state"] == "failed"
        note = moved["detail"] + (f" (backup {moved['backup']})" if moved.get("backup") else "")
        if kind == "pipx":
            venvs = pipx_venvs + [r for r in [_venv_root(Path(registration.command))] if r]
            held = _extractor_pythons_in(repo, venvs)
            if held:
                note += ("; its pipx environment is no longer registered, but keep it: "
                         + ", ".join(f"ops/.env's {key} runs an extractor shim autostart from it ({value})"
                                     for key, value in held))
            else:
                note += ("; its pipx environment is no longer registered: `pipx uninstall pseudolife-mcp` once no "
                         "session runs it, unless an extractor shim autostart uses that venv (`python "
                         "ops/shim_autostart.py show claude|codex` names its interpreter)")
        detail.append(note)
    # `pseudolife-mcp` typed in a terminal reaches the launcher too (a
    # ~/.local/bin link on POSIX, the user PATH on Windows): on 2026-09-29 it
    # still ran pipx's copy of the old package after every registration had
    # moved. A step that cannot be done is named and fails nothing: every
    # registration names the launcher by its full path.
    exposed = rt.expose_launcher(layout, env=env)
    if exposed["state"] != "skipped":
        detail.append(exposed["detail"] + (f"; {exposed['hint']}" if exposed.get("hint") else ""))
    pinned = rt.pinned_runtimes(layout, rt.find_registrations(env))
    pruned = rt.remove_unused(layout, pinned=pinned, processes=list_processes, tunnels=rt.default_tunnel_root(env))
    if pruned["error"]:
        detail.append(f"older runtimes kept: {pruned['error']}")
    for entry in pruned["held"]:
        detail.append(f"{entry['path']} still runs {entry['processes']} process"
                      f"{'es' if entry['processes'] != 1 else ''}; it is removed by a later update once idle")
    if pruned["removed"]:
        detail.append("removed " + ", ".join(pruned["removed"]))
    for path in pruned["unverified"]:
        detail.append(f"{path} left (no process table on this platform to say whether a session runs it)")
    for legacy in _legacy_directories(rt, layout):
        if not any(rt.registered_runtime(r, layout) == legacy for r in rt.find_registrations(env)):
            detail.append(f"{legacy} is a hand-made runtime no registration names any more; remove it once no "
                          f"session runs from it")
    detail.append("sessions already running keep their runtime; new sessions start on the new one")
    state = "failed" if failed else f"installed:{runtime.version}"
    return {"state": state, "detail": "; ".join(detail + [r["detail"] for r in results])}


# ── the plugin cache ────────────────────────────────────────────────────────

def _normalised(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _tree_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if not path.is_file() or ".git" in parts:
            continue
        # Claude Code keeps its own bookkeeping in a cache folder's top-level
        # dot entries: `.in_use/<pid>` while a session runs from it,
        # `.orphaned_at` once no record names it. Counting them made every
        # cache look stale while any session was open (2026-09-30). The
        # plugin ships no top-level dot entry but its manifest directory
        # (pinned by tests/test_plugin_packaging.py).
        if parts[0].startswith(".") and parts[0] != ".claude-plugin":
            continue
        files[path.relative_to(root).as_posix()] = _normalised(path)
    return files


def tree_differs(a: Path, b: Path) -> bool:
    """Whether two plugin trees differ in file set or (CRLF-insensitive)
    content; ``.git`` metadata and Claude Code's top-level markers are
    ignored."""
    return _tree_files(Path(a)) != _tree_files(Path(b))


def plugins_root() -> Path:
    """Claude Code's plugins directory (records, marketplace clones, cache):
    ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` when set, else ``~/.claude/plugins``
    (``CLAUDE_CONFIG_DIR`` is not followed here, as before)."""
    override = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
    return Path(override) if override else home() / ".claude" / "plugins"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _plugin_record(plugins_dir: Path) -> dict | None:
    records = _read_json(plugins_dir / "installed_plugins.json").get("plugins", {}).get(PLUGIN_ID)
    return records[0] if isinstance(records, list) and records else None


def _clone_plugin_dir(plugins_dir: Path) -> Path:
    known = _read_json(plugins_dir / "known_marketplaces.json").get(MARKETPLACE, {})
    location = known.get("installLocation") if isinstance(known, dict) else None
    return Path(location) / "plugin" if location else plugins_dir / "marketplaces" / MARKETPLACE / "plugin"


def update_plugin(repo: Path | None = None) -> dict:
    """Move the installed plugin to the marketplace clone's copy without
    touching the one running sessions loaded.

    Claude Code names a cache folder by the plugin's manifest version, and
    the plugin carries none, so each marketplace commit is its own version
    (its commit, 12 hex characters). ``claude plugin update`` installs it
    into a new folder beside the recorded one, switches the record, and
    stamps the old folder ``.orphaned_at``; Claude Code deletes that folder
    14 days later once no session marks it ``.in_use`` (measured on 2.1.283,
    2026-09-30). Nothing here uninstalls: on 2026-09-30 an uninstall followed
    by an install the in-use folder refused (EPERM) left the plugin
    uninstalled until the apps were restarted."""
    claude = which("claude")
    if not claude:
        return {"state": "no-cli", "detail": "the claude CLI is not on PATH"}
    plugins_dir = plugins_root()
    record = _plugin_record(plugins_dir)
    if not record:
        return {"state": "not-installed", "detail": f"{PLUGIN_ID} is not installed; {INSTALLER_HINT}"}
    version = str(record.get("version", ""))
    cache = Path(str(record.get("installPath", "")))
    code, _ = run_cli([claude, "plugin", "marketplace", "update", MARKETPLACE])
    marketplace_update = "ok" if code == 0 else "failed"
    clone = _clone_plugin_dir(plugins_dir)
    if not (clone / ".claude-plugin" / "plugin.json").is_file():
        return {"state": "failed", "marketplace_update": marketplace_update,
                "detail": f"no marketplace clone at {clone}; run: claude plugin marketplace add "
                          f"Pseudogiant-xr/Pseudolife-MCP"}
    if cache.is_dir() and not tree_differs(clone, cache):
        return {"state": f"current:{version}", "marketplace_update": marketplace_update,
                "detail": f"cache matches the marketplace clone (v{version})"}
    # A project or local install is found from its project, at its scope;
    # from anywhere else the update would act on another project's install.
    scope = str(record.get("scope") or "user")
    project = record.get("projectPath") if scope in ("project", "local") else None
    if scope in ("project", "local") and not project:
        return {"state": "failed", "marketplace_update": marketplace_update,
                "detail": f"the {scope}-scope install of {PLUGIN_ID} names no project; left as it is. Run "
                          f"claude plugin update {PLUGIN_ID} --scope {scope} from that project"}
    code, out = run_cli([claude, "plugin", "update", PLUGIN_ID, "--scope", scope],
                        cwd=str(project) if project else None)
    said = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
    updated = _plugin_record(plugins_dir)
    new_cache = Path(str(updated.get("installPath", ""))) if updated else None
    if code == 0 and updated and new_cache.is_dir() and not tree_differs(clone, new_cache):
        new_version = str(updated.get("version", ""))
        return {"state": f"refreshed:{new_version}", "marketplace_update": marketplace_update,
                "detail": f"{new_version} installed from the marketplace clone beside {version}; sessions "
                          f"already running keep the copy they loaded, new sessions start on this one, and "
                          f"Claude Code deletes the old copy 14 days after it was replaced, once no session "
                          f"runs from it"}
    if updated is None:
        return {"state": "failed", "marketplace_update": marketplace_update,
                "detail": f"{PLUGIN_ID} is no longer recorded as installed after claude plugin update "
                          f"({said}); {INSTALLER_HINT}"}
    kept = (f"{version} was left installed and sessions keep running it" if updated == record
            else f"the record now names {updated.get('installPath')} ({updated.get('version')})")
    retry = (f"python \"{Path(repo) / 'ops' / 'update_clients.py'}\" --only plugin" if repo
             else "pseudolife-mcp update --clients-only")
    offered = _read_json(clone / ".claude-plugin" / "plugin.json").get("version")
    if code == 0 and updated == record and offered == version:
        # Claude Code found nothing newer: the clone's manifest still
        # carries the installed version, and replacing that folder in place
        # is what failed under running sessions.
        return {"state": "failed", "marketplace_update": marketplace_update,
                "detail": f"the marketplace clone's plugin differs from the cache but offers the same version "
                          f"({version}; claude said: {said}), so Claude Code will not install it beside the "
                          f"copy sessions run; {kept}. A current clone's plugin.json carries no version: "
                          f"claude plugin marketplace update {MARKETPLACE}, then {retry}"}
    if code == 0 and updated == record:
        # Nothing newer to install, yet the cache is not the clone: the
        # folder the record names is gone or was edited in place.
        return {"state": "failed", "marketplace_update": marketplace_update,
                "detail": f"the cache folder the plugin record names ({cache}) is missing or was changed by "
                          f"hand, and Claude Code has nothing newer to install (claude said: {said}); {kept}. "
                          f"With no Claude Code session open: claude plugin uninstall {PLUGIN_ID}, then "
                          f"claude plugin install {PLUGIN_ID}"}
    return {"state": "failed", "marketplace_update": marketplace_update,
            "detail": f"claude plugin update {PLUGIN_ID} did not leave a cache matching the clone ({said}); "
                      f"{kept}. Retry: {retry}"}


# ── Codex hooks ─────────────────────────────────────────────────────────────

def _load_from_checkout(repo: Path, relative: str, name: str):
    """Import a script by file path from the checkout being installed
    (``ops/setup-codex-hooks.py`` is not a package module)."""
    spec = importlib.util.spec_from_file_location(name, Path(repo) / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def checkout_hooks_digest(repo: Path | None, directory: Path) -> str | None:
    """The hook scripts' digest, the same function every side uses."""
    return hooks_digest(directory)


def codex_bundle_digest(repo: Path) -> str:
    """The directory name ``ops/setup-codex-hooks.py`` gives the checkout's
    current hook scripts, computed with its own function."""
    module = _load_from_checkout(repo, "ops/setup-codex-hooks.py", "codex_hook_setup")
    return module.bundle_digest(module.bundle_bytes(Path(repo) / "plugin" / "hooks"))


def _unapproved_plugin_handlers(manifest_dir: Path, config_text: str) -> list[str] | None:
    """Handler positions of the checkout's plugin hooks.json that Codex has no
    approval for, or ``None`` when the config cannot be read here.

    Codex runs a plugin handler only once its approval is recorded under
    ``hooks.state."<plugin>:hooks/hooks.json:<event>:<group>:<handler>"``
    (config.toml, checked 2026-09-25), and a new position is skipped without
    a word in the desktop app. A handler the user disabled is their choice.
    Whether an approved definition has since changed is Codex's hash, not
    recomputed here; this catches the new positions a plugin update adds."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        return None
    try:
        state = (tomllib.loads(config_text).get("hooks") or {}).get("state") or {}
        manifest = json.loads((Path(manifest_dir) / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    except (ValueError, OSError, KeyError, AttributeError):
        return None
    if not isinstance(state, dict) or not isinstance(manifest, dict):
        return None
    missing = []
    for event, groups in manifest.items():
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
        for g, group in enumerate(groups):
            for h, _ in enumerate(group.get("hooks") or []):
                key = f"{PLUGIN_ID}:hooks/hooks.json:{name}:{g}:{h}"
                entry = state.get(key)
                if not isinstance(entry, dict) or not (
                        entry.get("trusted_hash") or entry.get("enabled") is False):
                    missing.append(key)
    return missing


# What Codex loses while its hook copy is unapproved, and the steps back.
# Printed by every update path (pseudolife-mcp update, ops/update.*,
# ops/update_clients.py, the unattended updater's board notice) when and only
# when Codex would ask again: a changed hooks.json (Codex approves hook
# definitions, not scripts), new handler positions, or manual copies that
# still name a script bundle. An approval is the user's, so no update can
# finish this.
CODEX_HOOK_NAMES = HOOK_SCRIPTS + ("hooks.json",)
CODEX_REAPPROVAL_VERIFY = "pseudolife-mcp doctor reports codex_hooks = current"


def changed_hook_files(installed: Path, reference: Path) -> list[str] | None:
    """The hook files whose content differs between Codex's copy and the
    reference scripts (CRLF-insensitive; a file missing on one side counts),
    or ``None`` when either directory cannot be read."""
    installed, reference = Path(installed), Path(reference)
    if not installed.is_dir() or not reference.is_dir():
        return None
    changed = []
    for name in CODEX_HOOK_NAMES:
        sides = []
        for directory in (installed, reference):
            try:
                sides.append(_normalised(directory / name))
            except OSError:
                sides.append(None)
        if sides[0] != sides[1]:
            changed.append(name)
    return changed


def codex_reapproval_text(codex: dict | None) -> str:
    """The complete, copy-pasteable steps when Codex's hook copy is not the
    current scripts (``check_codex_hooks`` state ``stale``,
    ``needs-approval`` or ``plugin-managed``); '' for every other state, so
    an update whose hooks did not change prints nothing about Codex."""
    state = (codex or {}).get("state")
    # plugin-managed (the config names the plugin but Codex holds no clone
    # where it is looked for) says nothing about a change: no steps.
    if state not in ("stale", "needs-approval"):
        return ""
    changed = (codex or {}).get("changed_files")
    if changed:
        which = f"changed: {', '.join(changed)}"
    elif state == "needs-approval":
        which = "new handler positions Codex has not approved"
    else:
        which = "Codex's copy differs from the current scripts"
    off = ("so until this is done Codex sessions start without the memory briefing, the per-turn "
           "memory and mail notes, the board check-in, the SessionEnd close and the Stop-hook park gate.")
    if (codex or {}).get("source") == "manual":
        # Manual copies whose commands still name a script bundle
        # (ops/setup-codex-hooks.py before 2026-09-30): one approval moves
        # them to the launcher, whose commands never change, and later
        # updates refresh them without asking.
        return "\n".join([
            "Codex: its manual hook copy still names an older script bundle, so it needs one last approval "
            "to move to the launcher; after that, updates refresh it without asking. Until then Codex keeps "
            "running the older scripts.",
            "  1. From a checkout: python ops/setup-codex-hooks.py --source manual --trust ask "
            "(unattended: --trust yes)",
            "  2. Verify: the same command reports status ready; pseudolife-mcp doctor reports "
            "codex_hooks = bundle-present for manual copies.",
        ])
    return "\n".join([
        f"Codex: its hook copy needs re-approval ({which}). Codex trusts hooks by hash and runs only "
        f"approved handlers, {off}",
        "  1. Update the plugin in Codex's plugin manager (or: codex plugin marketplace upgrade "
        f"{MARKETPLACE}), so its copy holds the new scripts "
        "(python ops/setup-codex-hooks.py --source plugin approves what Codex lists; it does not pull one)",
        "  2. Approve the handlers: in a Codex session run /hooks and approve every pseudolife-memory "
        "handler; unattended: python ops/setup-codex-hooks.py --source plugin --trust yes, or the "
        "installer's --codex-hook-trust yes (-CodexHookTrust yes on Windows)",
        f"  3. Verify: {CODEX_REAPPROVAL_VERIFY}.",
    ])


def _daemon_scripts_dir(daemon_digest: str | None) -> Path | None:
    """Where the daemon's own hook scripts can be read on this host without
    a checkout: the Claude plugin cache, when its scripts digest to what the
    daemon reports. ``None`` otherwise (the files cannot be named then)."""
    if not daemon_digest:
        return None
    record = _plugin_record(plugins_root())
    if not record:
        return None
    hooks = Path(str(record.get("installPath", ""))) / "hooks"
    return hooks if hooks.is_dir() and hooks_digest(hooks) == daemon_digest else None


def codex_plugin_hooks(codex_home: Path) -> Path:
    """The plugin hook directory Codex runs: its installed copy when there
    is one, else the marketplace clone it installs from.

    Codex runs a plugin's hooks from the installed copy, one folder named
    ``local`` whatever the commit (``hooks/list`` sourcePath, Codex 0.160.0,
    2026-10-03); the clone under ``.tmp/marketplaces`` is only its source."""
    installed = codex_home / "plugins" / "cache" / MARKETPLACE / PLUGIN_ID.split("@")[0] / "local" / "hooks"
    return installed if installed.is_dir() else codex_home / ".tmp" / "marketplaces" / MARKETPLACE / "plugin" / "hooks"


def codex_executable(codex_home: Path) -> str | None:
    """The Codex CLI to run the marketplace upgrade with, as an absolute
    path, or ``None``: ``PSEUDOLIFE_CODEX_BIN``, ``codex`` on an absolute
    PATH entry, or the Windows desktop app's build folder (the doorbell's
    resolver, which never takes the working directory), then the standalone
    package the desktop app installs under the Codex home (Windows, Codex
    0.160.0, 2026-10-03: ``packages/standalone/current/bin/codex.exe``,
    a per-platform release behind the ``current`` link)."""
    from pseudolife_memory import codex_doorbell
    found = codex_doorbell.resolve_codex_command()
    if found:
        return found[0]
    # An invalid configured path is a setup error, as in the doorbell;
    # another installed binary must not silently replace it.
    if os.environ.get("PSEUDOLIFE_CODEX_BIN", "").strip():
        return None
    standalone = codex_home / "packages" / "standalone" / "current" / "bin" / (
        "codex.exe" if os.name == "nt" else "codex")
    return str(standalone.resolve()) if standalone.is_file() else None


# A git fetch of the marketplace repository plus Codex's reinstall of its
# copy: about 5 s on the maintainer's Windows host (2026-10-03, throwaway
# CODEX_HOME); the margin is for a slow network.
_CODEX_UPGRADE_TIMEOUT_S = 300


def _json_object(text: str) -> dict | None:
    """The first JSON object in ``text`` (Codex prints its report on stdout
    and may warn on stderr, which ``run_cli`` appends)."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _marketplace_source(codex_home: Path) -> tuple[str | None, str | None]:
    """``(git source, ref)`` of the marketplace Codex upgrades from: its
    ``[marketplaces.<name>]`` table in config.toml, else the clone's
    ``.codex-marketplace-install.json`` (written by Codex's upgrade)."""
    table: dict = {}
    try:
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        text = ""
    try:
        import tomllib
        table = (tomllib.loads(text).get("marketplaces") or {}).get(MARKETPLACE) or {}
    except ImportError:
        # Python 3.10 has no tomllib: read the table's plain string keys,
        # as Codex writes them (`key = "value"` or `key = 'value'`).
        header = re.search(rf"^\[marketplaces\.\"?{re.escape(MARKETPLACE)}\"?\]\s*$", text, re.M)
        body = text[header.end():].split("\n[", 1)[0] if header else ""
        table = {m.group(1): m.group(3) for m in re.finditer(
            r"^\s*([A-Za-z_]+)\s*=\s*([\"'])(.*?)\2\s*$", body, re.M)}
    except (ValueError, AttributeError):
        pass
    if not isinstance(table, dict) or not table.get("source"):
        table = _read_json(codex_home / ".tmp" / "marketplaces" / MARKETPLACE / ".codex-marketplace-install.json")
    if table.get("source_type", "git") != "git" or not table.get("source"):
        return None, None
    ref = table.get("ref") or table.get("ref_name")
    return str(table["source"]), (str(ref) if ref else None)


# A blob-less depth-1 clone reads one file of the marketplace's branch:
# 1.5 s and 159 KB from GitHub on the maintainer's host (2026-10-03).
_MARKETPLACE_READ_TIMEOUT_S = 120


def _git_executable() -> str | None:
    """Git from an absolute PATH directory, without shutil.which's implicit
    Windows working-directory search (the Codex doorbell's resolver rule)."""
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        names = [f"git{ext}" for ext in pathext.split(os.pathsep)
                 if ext.lower() in (".com", ".exe", ".bat", ".cmd")]
    else:
        names = ["git"]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = directory.strip().strip('"')
        # Path.is_absolute also rejects drive-relative Windows paths.
        if not directory or not Path(directory).is_absolute():
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def _marketplace_hooks_json(codex_home: Path) -> tuple[bytes | None, str]:
    """The hooks.json the marketplace's branch would install (CRLF read as
    LF), or ``(None, why not)``. Read from a private blob-less clone in a
    temporary directory, never from Codex's own clone, which stays Codex's
    to write."""
    git = _git_executable()
    if not git:
        return None, "git is not on PATH to read the marketplace's hooks.json first"
    source, ref = _marketplace_source(codex_home)
    if not source:
        return None, f"no git source for the {MARKETPLACE} marketplace in the Codex home's config.toml"
    # No prompt may hold the update: git's own, Git Credential Manager's,
    # or ssh's (a passphrase or host key, read from the terminal, not stdin).
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    with tempfile.TemporaryDirectory(prefix="pseudolife-codex-marketplace-", ignore_cleanup_errors=True) as tmp:
        clone = str(Path(tmp) / "m")
        branch = ["--branch", ref] if ref else []
        code, out = run_cli([git, "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--no-checkout",
                             *branch, "--", source, clone], timeout=_MARKETPLACE_READ_TIMEOUT_S, env=env)
        if code == 0:
            code, out = run_cli([git, "-C", clone, "show", "HEAD:plugin/hooks/hooks.json"],
                                timeout=_MARKETPLACE_READ_TIMEOUT_S, env=env, encoding="utf-8")
    try:
        valid = code == 0 and isinstance(json.loads(out).get("hooks"), dict)
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        lines = out.strip().splitlines()
        return None, f"could not read the marketplace's hooks.json ({lines[-1] if lines else f'exit {code}'})"
    # Bytes, as every other hooks.json comparison here (changed_hook_files).
    return out.replace("\r\n", "\n").encode("utf-8"), ""


def _upgrade_codex_plugin(codex_home: Path, repo: Path | None, daemon_digest: str | None,
                          changed: list[str]) -> dict:
    """Refresh a plugin copy whose scripts are behind while its hooks.json is
    current, through Codex's own ``codex plugin marketplace upgrade``.

    Measured on Codex 0.160.0 (2026-10-03, throwaway CODEX_HOME): the
    upgrade fetches the git marketplace and replaces both its clone and the
    installed copy hooks run from; config.toml stays byte-identical and
    ``hooks/list`` reports the same keys and hashes, so approvals carry
    over; with nothing newer it changes nothing. Codex keeps one installed
    copy and replaces it in place, as its plugin manager's update does;
    there is no copy beside it to keep for running sessions. The upgrade
    takes the marketplace's branch, not the checkout or the release, so it
    runs only when that branch's hooks.json matches the installed one (read
    first from a private clone); the copy is read back afterwards, because
    the read-back is the proof."""
    behind = f"Codex's plugin copy is behind the current scripts (changed: {', '.join(changed)})"
    manual = (f"run codex plugin marketplace upgrade {MARKETPLACE}, or update the plugin in Codex's plugin "
              "manager; that upgrade may change hooks.json and need approval because the marketplace's "
              "branch has not been checked")

    def stays(reason: str, files: list[str] = changed) -> dict:
        return {"state": "behind", "source": "plugin", "changed_files": files,
                "detail": f"{behind}; {reason}"}

    codex = codex_executable(codex_home)
    if not codex:
        if os.environ.get("PSEUDOLIFE_CODEX_BIN", "").strip():
            return stays("PSEUDOLIFE_CODEX_BIN is not an absolute path to an existing file (setup error), "
                         f"so it was not refreshed here: {manual}")
        return stays("no Codex CLI was found (PATH, PSEUDOLIFE_CODEX_BIN, or the desktop app's own copy), so "
                     f"it was not refreshed here: {manual}")
    # The marketplace serves its branch, which can carry a hooks.json the
    # checkout or the deployed release does not; Codex would then ask for
    # an approval nobody gave. Upgrade only when the branch's matches.
    offered, why = _marketplace_hooks_json(codex_home)
    if offered is None:
        return stays(f"{why}, so it was not refreshed here: {manual}")
    try:
        installed = _normalised(codex_plugin_hooks(codex_home) / "hooks.json")
    except OSError:
        installed = None
    if offered != installed:
        return stays("the marketplace's branch also changes hooks.json, which Codex would ask to approve, so it "
                     "was not refreshed automatically. When you are ready to approve: update the plugin in "
                     f"Codex's plugin manager (or codex plugin marketplace upgrade {MARKETPLACE}), then approve "
                     "every pseudolife-memory handler with /hooks in a Codex session or "
                     "python ops/setup-codex-hooks.py --source plugin --trust ask")
    manual = (f"run codex plugin marketplace upgrade {MARKETPLACE}, or update the plugin in Codex's plugin "
              "manager; its approvals carry over if hooks.json stays as inspected (the branch matched "
              "the installed definition; Codex approves definitions, not scripts)")
    code, out = run_cli([codex, "plugin", "marketplace", "upgrade", MARKETPLACE, "--json"],
                        timeout=_CODEX_UPGRADE_TIMEOUT_S, env=dict(os.environ, CODEX_HOME=str(codex_home)))
    report = _json_object(out) if code == 0 else None
    errors = [str(e.get("message", e)) if isinstance(e, dict) else str(e)
              for e in ((report or {}).get("errors") or [])]
    if code != 0 or report is None or errors:
        lines = [line for line in out.strip().splitlines() if not line.startswith("WARNING")]
        said = "; ".join(errors) or (lines[-1] if lines else f"exit {code}")
        return stays(f"codex plugin marketplace upgrade {MARKETPLACE} did not refresh it ({said}): {manual}")
    after = check_codex_hooks(repo, daemon_digest)
    if after["state"] == "current":
        return {"state": "refreshed", "source": "plugin", "changed_files": changed,
                "detail": f"Codex's plugin copy refreshed through codex plugin marketplace upgrade {MARKETPLACE} "
                          f"(changed: {', '.join(changed)}); its approvals carry over: Codex approves hook "
                          "definitions, which did not change, not the scripts"}
    if after["state"] == "behind":
        # The marketplace serves its branch (master), which can be behind a
        # checkout under test or past the release just deployed.
        files = after.get("changed_files") or changed
        whose = "the checkout's" if repo else "the daemon's"
        if not report.get("upgradedRoots"):
            return stays(f"the marketplace has nothing newer than Codex's copy, which holds the marketplace's "
                         f"branch and differs from {whose} scripts", files)
        return stays(f"Codex upgraded its copy to the marketplace's branch, which still differs from {whose} "
                     f"scripts (changed: {', '.join(files)})", files)
    # What Codex installed needs an approval after all (the branch moved
    # between the read and Codex's fetch, or a handler position is new):
    # approving it is the user's.
    return dict(after, detail=f"after codex plugin marketplace upgrade {MARKETPLACE}: {after['detail']}")


def check_codex_hooks(repo: Path | None = None, daemon_digest: str | None = None,
                      refresh: bool = False) -> dict:
    """Codex hooks come either as manual copies under ``<codex
    home>/pseudolife/hooks`` (``setup-codex-hooks.py``) or from the Codex
    plugin, whose marketplace clone Codex keeps itself. Codex approves a hook
    by its definition, not by the script it runs (measured on Codex 0.158.0,
    2026-09-30), so only a changed ``hooks.json`` asks for approval again.
    Manual copies run through a launcher whose commands never change; with
    ``refresh`` (the update step) they are moved to the checkout's scripts
    here, with no consent needed, and a plugin copy whose scripts alone are
    behind is refreshed through ``codex plugin marketplace upgrade``.
    Everything else is reported. The current
    scripts are the checkout's when ``repo`` is given, else the daemon's
    (``daemon_digest``, its ``/health`` ``hooks_digest``)."""
    repo = Path(repo) if repo else None
    # Resolved as setup-codex-hooks.py resolves it: the launcher commands in
    # hooks.json name the resolved path.
    codex_home = Path(os.environ.get("CODEX_HOME") or home() / ".codex").expanduser().resolve()
    hooks_root = codex_home / "pseudolife" / "hooks"
    if hooks_root.is_dir():
        if repo is None or not (repo / "ops" / "setup-codex-hooks.py").is_file():
            return {"state": "bundle-present",
                    "detail": "manual hook copies present; whether they are current needs a checkout: "
                              "python ops/setup-codex-hooks.py --source manual --trust ask"}
        setup = _load_from_checkout(repo, "ops/setup-codex-hooks.py", "codex_hook_setup")
        checkout = codex_bundle_digest(repo)
        # An older checkout's setup script has no launcher: read as before.
        launcher_installed = getattr(setup, "launcher_installed", None)
        if launcher_installed is not None and launcher_installed(codex_home):
            try:
                if setup._pointer(codex_home) == checkout:
                    setup._vet_launcher(codex_home)
                    return {"state": "current", "source": "manual",
                            "detail": "manual hook copies run the checkout's scripts through the launcher"}
                if not refresh:
                    return {"state": "behind", "source": "manual",
                            "detail": "manual hook copies run older scripts; the update refreshes them, with no "
                                      "approval needed (Codex approves the launcher's commands, not the scripts)"}
                moved = setup.refresh_manual(codex_home, repo / "plugin" / "hooks")
            except (setup.SetupError, OSError, UnicodeError) as exc:
                return {"state": "failed", "source": "manual",
                        "detail": f"manual hook copies were not refreshed or verified: {exc}"}
            return {"state": "current", "source": "manual",
                    "detail": f"manual hook copies refreshed to the checkout's scripts (bundle {moved['bundle']}); "
                              "Codex keeps its approval, which covers the launcher's commands, not the scripts"}
        if (hooks_root / checkout).is_dir():
            return {"state": "bundle-present",
                    "detail": "bundle present; trust and execution not checked; rerun setup to verify with "
                              "consent: python ops/setup-codex-hooks.py --source manual --trust ask"}
        return {"state": "stale", "source": "manual",
                "detail": "the manual hook copies name an older script bundle in their commands, so moving "
                          "them needs one more approval; after it, updates refresh them without asking: "
                          "python ops/setup-codex-hooks.py --source manual --trust ask"}
    config = codex_home / "config.toml"
    try:
        config_text = config.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    if f'"{PLUGIN_ID}"' not in config_text:
        return {"state": "not-configured", "detail": "no Codex hook copies or plugin under the Codex home"}
    clone_hooks = codex_plugin_hooks(codex_home)
    current = hooks_digest(repo / "plugin" / "hooks") if repo else daemon_digest
    if current is None:
        return {"state": "unknown", "detail": "Codex runs the plugin's hooks; nothing to compare its copy with "
                                              "(no checkout, and the daemon reported no hooks_digest)"}
    # Name the files that differ when the current scripts can be read here:
    # the checkout's, or the daemon's through the plugin cache. The scripts'
    # digest leaves hooks.json out, so it is compared here too.
    reference = (repo / "plugin" / "hooks") if repo else _daemon_scripts_dir(daemon_digest)
    changed = changed_hook_files(clone_hooks, reference) if (clone_hooks.is_dir() and reference) else None
    if clone_hooks.is_dir() and hooks_digest(clone_hooks) == current and not changed:
        missing = _unapproved_plugin_handlers(clone_hooks, config_text)
        if missing:
            return {"state": "needs-approval", "source": "plugin", "changed_files": [],
                    "detail": f"Codex skips {len(missing)} plugin hook handler(s) it has not approved; "
                              "approve them: python ops/setup-codex-hooks.py --source plugin --trust ask "
                              "(or /hooks in the Codex terminal app)"}
        return {"state": "current", "detail": "Codex runs the plugin's hooks; its copy matches "
                                              + ("the checkout's scripts" if repo else "the daemon's scripts")}
    if changed and "hooks.json" not in changed:
        # The installed definitions match the reference, but a plain check
        # does not inspect what the marketplace's branch would bring.
        if refresh:
            return _upgrade_codex_plugin(codex_home, repo, daemon_digest, changed)
        return {"state": "behind", "source": "plugin", "changed_files": changed,
                "detail": f"Codex's plugin copy is behind the current scripts (changed: {', '.join(changed)}); "
                          "the update refreshes it (codex plugin marketplace upgrade " + MARKETPLACE + "), or "
                          "update the plugin in Codex's plugin manager; that upgrade may change hooks.json "
                          "and need approval because the marketplace's branch has not been checked"}
    return {"state": "stale" if clone_hooks.is_dir() else "plugin-managed", "source": "plugin",
            "changed_files": changed,
            "detail": "Codex runs the plugin's hooks; refresh them through Codex's plugin manager, or "
                      "python ops/setup-codex-hooks.py --source plugin --trust ask"}


# ── the command ─────────────────────────────────────────────────────────────

def print_codex_reapproval(report: dict) -> None:
    """After the ladder: the complete re-approval steps, when Codex's copy
    differs from the current scripts."""
    text = codex_reapproval_text(report.get("codex"))
    if text:
        print(text)


# ── extractor autostart ─────────────────────────────────────────────────────

def _autostart_module(repo: Path):
    """The checkout's ``ops/shim_autostart.py``, loaded by file path."""
    return _load_from_checkout(repo, "ops/shim_autostart.py", "pseudolife_shim_autostart")


def check_autostart(repo: Path) -> dict:
    """Informational: an extractor autostart task or unit registered before
    ``ops/shim_autostart.py`` still carries the model and the rest on its
    command line, so at logon it starts those, not ``ops/.env``; no update
    moves it. ``registration_note`` for every registered kind; a kind with
    no registration (no CLI extractor shim on this host) says nothing.
    Never fails the run."""
    try:
        module = _autostart_module(repo)
        kinds = sorted(module.KINDS)
    except Exception as exc:  # noqa: BLE001 - informational: say it, never fail the run
        return {"state": "unknown", "detail": f"could not read the extractor autostart registrations ({exc})"}
    registered, notes = 0, []
    for kind in kinds:
        try:
            if module._registered_command(kind) is None:
                continue
            registered += 1
            note = module.registration_note(kind)
        except Exception as exc:  # noqa: BLE001
            note = f"its registration could not be read ({exc})"
        if note:
            notes.append(f"{kind}: {note}")
    if notes:
        return {"state": "stale", "detail": "; ".join(notes), "notes": notes}
    if registered:
        return {"state": "current", "detail": "the registered extractor autostart reads ops/.env at every start"}
    return {"state": "none", "detail": "no extractor autostart task or unit is registered"}


def _marker(state: str) -> str:
    if state == "failed":
        return "[!]"
    if state.startswith(("installed", "refreshed", "current")) or state in ("editable", "spawning"):
        return "[x]"
    if state in ("stale", "needs-approval"):
        return "[!]"
    return "[-]"


# ── saved tunnels ───────────────────────────────────────────────────────────
#
# A refresh freezes the refreshing process's bridge and site-packages into
# the running tunnel (tunnel_runtime.snapshot_bridge). Run in this process
# that would be the release being replaced, or a checkout and its
# virtualenv; run by the launcher after the shim step, it is the release
# just installed.

# Per running tunnel: tunnel_runtime._request_refresh waits up to 75 s for
# the supervisor's answer, plus the launcher's own start.
_TUNNEL_REFRESH_TIMEOUT_S = 90


def _tunnel_report(out: str) -> dict | None:
    """``tunnel update``'s JSON report (its last JSON object line)."""
    for line in reversed(out.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("state") in ("current", "failed") and isinstance(data.get("detail"), str):
            return {"state": data["state"], "needs_attention": bool(data.get("needs_attention")),
                    "detail": data["detail"] + "; run by the installed launcher",
                    "profiles": data["profiles"] if isinstance(data.get("profiles"), list) else []}
    return None


def update_tunnels(shim: dict | None) -> dict | None:
    """Move running saved tunnels onto the release just installed: ``tunnel
    update`` run by the installed launcher, never in this process. ``None``
    when no tunnel profile is saved; ``shim`` is the shim step's result."""
    from pseudolife_memory.tunnel_profiles import ProfileStore
    from pseudolife_memory.tunnel_runtime import status_profile
    try:
        store = ProfileStore()
        names = store.list()
        running = [name for name in names if status_profile(name, store)["running"]]
    except Exception:
        return {"state": "failed", "detail": "private tunnel profiles could not be checked; run tunnel doctor"}
    if not names:
        return None
    if not running:
        return {"state": "current", "detail": f"{len(names)} saved tunnel profile(s), none running; nothing to refresh"}
    which = ", ".join(running)
    rt = runtimes_module()
    try:
        layout = rt.default_layout(client_env())
    except ValueError:
        layout = None
    if layout is None or not layout.launcher.is_file() or rt.current_runtime(layout) is None:
        return {"state": "skipped",
                "detail": f"running tunnels ({which}) were not refreshed: no installed launcher to refresh them "
                          "through, and a refresh from here would freeze this process's code into them; run "
                          "`pseudolife-mcp tunnel update` from the installed command"}
    command = f'"{layout.launcher}" tunnel update'
    if shim is not None and shim.get("state") == "failed":
        return {"state": "skipped",
                "detail": f"running tunnels ({which}) stay on their current release because the shim step failed; "
                          f"once it passes, run {command}"}
    timeout = 60 + _TUNNEL_REFRESH_TIMEOUT_S * len(running)
    # No PYTHON* variable: an exported PYTHONPATH (a checkout) would be what
    # the refresh freezes into the bridge.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    code, out = run_cli([layout.launcher, "tunnel", "update"], timeout=timeout, env=env)
    result = _tunnel_report(out)
    if result is not None and (code == 0) == (result["state"] == "current"):
        return result
    if code == TIMED_OUT:
        return {"state": "failed",
                "detail": f"running tunnels ({which}) were not refreshed: {command} timed out after {timeout} s; "
                          "check `pseudolife-mcp tunnel status`, then run it again"}
    # Its output is not repeated: a crash's text can carry private file data.
    return {"state": "failed",
            "detail": f"running tunnels ({which}) were not refreshed: {command} ended with exit {code}; run it "
                      "again to see why, then `pseudolife-mcp tunnel doctor`"}


def run_steps(steps, *, repo: Path | None, source: str, daemon_digest: str | None = None) -> dict:
    """The client-side ladder as a report: ``shim``, ``plugin``, ``codex``
    (those in ``steps``), ``autostart`` when a checkout is known, and
    ``ok``."""
    report: dict = {}
    if "shim" in steps:
        report["shim"] = update_shim(source, repo)
    if "plugin" in steps:
        report["plugin"] = update_plugin(repo)
    if "codex" in steps:
        report["codex"] = check_codex_hooks(repo, daemon_digest, refresh=True)
    if repo is not None:
        # Informational, after the client steps: never "failed", so never
        # fails the run.
        report["autostart"] = check_autostart(repo)
    from pseudolife_memory.tunnel_profiles import default_root
    # No tunnel directory: tunnels were never set up, so no step at all.
    if os.path.lexists(default_root()):
        tunnel = update_tunnels(report.get("shim"))
        if tunnel is not None:
            report["tunnel"] = tunnel
    # A shim left un-upgraded because sessions run it still needs the rerun.
    report["ok"] = all(r["state"] != "failed" for k, r in report.items() if k != "ok")
    return report


def print_ladder(report: dict) -> None:
    labels = {"shim": "Shim", "plugin": "Plugin", "codex": "Codex hooks", "autostart": "Extractor autostart", "tunnel": "Secure tunnel"}
    for key, label in labels.items():
        if key in report and not (key == "autostart" and report[key]["state"] == "none"):
            result = report[key]
            print(f"  {_marker(result['state'])} {label:<14} {result['state']} - {result['detail']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    checkout = checkout_root()
    parser.add_argument("--repo", default=str(checkout) if checkout else None,
                        help="checkout to install from (default: this one, when running from a checkout)")
    parser.add_argument("--source", default=None,
                        help="pip source for the shim runtime (default: the checkout, else pseudolife-mcp==<this version>)")
    parser.add_argument("--only", default="shim,plugin,codex",
                        help="comma-separated subset of shim,plugin,codex (default: all)")
    parser.add_argument("--json", action="store_true", help="one JSON report instead of the ladder")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve() if args.repo else None
    source = args.source or (str(repo) if repo else f"pseudolife-mcp=={__version__}")
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    report = run_steps(steps, repo=repo, source=source)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    print_ladder(report)
    print_codex_reapproval(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
