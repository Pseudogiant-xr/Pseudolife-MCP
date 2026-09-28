"""Move the client side with the daemon: the shim, the plugin cache, Codex hooks.

``ops/update.ps1`` / ``ops/update.sh`` rebuild the daemon and nothing else.
The stdio shim behind each registration, the Claude Code plugin cache and
Codex's content-addressed copy of the hook scripts are separate installs
that each needed their own step, and on 2026-09-21 each was forgotten in
turn. ``--all`` on the update scripts runs this after the daemon is
healthy; it is also usable on its own.

    python ops/update_clients.py [--repo PATH] [--only shim,plugin,codex] [--json]

Shim: every stdio registration of the shim (Claude Code's ``~/.claude.json``,
Codex's ``config.toml``, Claude Desktop's and Gemini CLI's config files)
is read from its file. When any of them runs the launcher, a runtime under
the runtimes root, a pipx-managed launcher or a virtualenv's launcher or
``python -m pseudolife_memory.cli``, a NEW shim runtime is installed from
the checkout beside the existing ones (``pseudolife_memory/runtimes.py``),
the launcher every client should run is put in place, and registrations
that still name a runtime, pipx or virtualenv path are moved to the
launcher in place, each file backed up first. Nothing running is ever
replaced: sessions keep the runtime they started from, the next session
start takes the new one, and an old runtime is removed only once no
process runs from it and no registration names it. A launcher inside the
checkout's ``.venv`` is an editable install — the code is already live
and is named, not reinstalled. Anything else is left alone and named.

Plugin: the version string is pinned to the package version, so a plugin
change on master does not move ``/plugin update``. The marketplace clone
is refreshed, its plugin tree compared by bytes (CRLF read as LF) against
the installed cache, and only when they differ is the plugin uninstalled
and reinstalled — then compared again, because the read-back is the proof.

Codex: hooks are trusted by hash, so a refresh is a consent step
(``ops/setup-codex-hooks.py``); this reports whether the copy for the
checkout's current scripts exists and names the command when not.

Every CLI call goes through :func:`run_cli`, every lookup through
:func:`which` and :func:`home`, so the tests drive the real logic with
fakes and never touch this machine's registrations. Nothing here prints
tokens: registration output is parsed for the command line only.
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


def run_cli(argv, *, timeout: int = 600, cwd: str | None = None) -> tuple[int, str]:
    """Run a CLI and return ``(returncode, combined output)``; a missing or
    hung executable reads as a non-zero code with the reason as output."""
    try:
        # No stdin: a CLI that decides to prompt fails fast instead of holding
        # a captured-output deploy for the whole timeout with nothing shown.
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              timeout=timeout, check=False, errors="replace",
                              stdin=subprocess.DEVNULL, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
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


def _classify(command: str, args: str, repo: Path, pipx_listing: dict | None = None) -> tuple[str, Path | None]:
    """How the registered shim was installed: ``editable``, ``pip`` (with
    the interpreter to use), ``pip-user`` (with the owning interpreter),
    ``pipx``, or ``unmanaged``."""
    path = Path(command)
    try:
        inside_checkout = path.resolve().is_relative_to((repo / ".venv").resolve())
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

_RUNTIMES_MODULE = None


def runtimes_module():
    """``pseudolife_memory.runtimes`` from this helper's own checkout, loaded
    by path (an installed release may predate it). The checkout being
    installed (``--repo``) is the runtime's source, not this helper's code."""
    global _RUNTIMES_MODULE
    if _RUNTIMES_MODULE is None:
        _RUNTIMES_MODULE = _load_from_checkout(ROOT, "pseudolife_memory/runtimes.py", "checkout_runtimes")
    return _RUNTIMES_MODULE


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


def _legacy_directories(rt, layout) -> list[Path]:
    """Directories under the runtimes root that are not numbered runtimes:
    hand-made virtualenvs from before the launcher, left for the operator."""
    try:
        names = sorted(os.listdir(layout.root))
    except OSError:
        return []
    return [layout.root / n for n in names if rt._sequence_of(n) is None and (layout.root / n).is_dir()]


def update_shim(repo: Path) -> dict:
    repo = Path(repo)
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
    managed: list = []          # (registration, kind)
    results: list[dict] = []
    for registration in registrations:
        if rt.registers_launcher(registration, layout):
            managed.append((registration, "launcher"))
            continue
        if any(rt.registers_runtime_path(registration, layout, [root]) and not rt.registers_runtime_path(
                registration, layout) for root in pipx_roots):
            managed.append((registration, "pipx"))
            continue
        if rt.registers_runtime_path(registration, layout):
            managed.append((registration, "runtime"))
            continue
        kind, interpreter = _classify(registration.command, " ".join(registration.args), repo, pipx_listing)
        if kind in ("pipx", "pip", "pip-user"):
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
                                      f"install --upgrade \"{repo}\""})
    if not managed:
        order = ("failed", "unknown", "editable", "unmanaged")
        results.sort(key=lambda r: order.index(r["state"]) if r["state"] in order else len(order))
        return {"state": results[0]["state"], "detail": "; ".join(r["detail"] for r in results)}
    lines: list[str] = []
    try:
        runtime = rt.install(str(repo), layout, run=run_cli, log=lines.append)
    except rt.RuntimeInstallError as exc:
        detail = (f"installing a new shim runtime from {repo} failed at {exc.step} ({_failure_line(exc.output, 1)}); "
                  f"registrations were not touched and the runtime that was current stays current. "
                  f"Retry: python \"{Path(__file__).resolve()}\" --only shim --repo \"{repo}\"")
        return {"state": "failed", "detail": "; ".join([detail] + [r["detail"] for r in results])}
    detail = [f"runtime {runtime.name} ({runtime.version}"
              + (f", commit {runtime.source_commit[:8]}" if runtime.source_commit else "")
              + f") installed from {repo} behind {layout.launcher}"]
    failed = False
    for registration, kind in managed:
        if kind == "launcher":
            detail.append(f"{registration.client}: already runs the launcher")
            continue
        moved = rt.migrate_registration(registration, layout)
        failed = failed or moved["state"] == "failed"
        note = moved["detail"] + (f" (backup {moved['backup']})" if moved.get("backup") else "")
        if kind == "pipx":
            note += "; its pipx environment is no longer registered: `pipx uninstall pseudolife-mcp` once no session runs it"
        detail.append(note)
    pinned = rt.pinned_runtimes(layout, rt.find_registrations(env))
    pruned = rt.remove_unused(layout, pinned=pinned, processes=list_processes)
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
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        files[path.relative_to(root).as_posix()] = _normalised(path)
    return files


def tree_differs(a: Path, b: Path) -> bool:
    """Whether two plugin trees differ in file set or (CRLF-insensitive)
    content; ``.git`` metadata is ignored."""
    return _tree_files(Path(a)) != _tree_files(Path(b))


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


def update_plugin(repo: Path) -> dict:
    claude = which("claude")
    if not claude:
        return {"state": "no-cli", "detail": "the claude CLI is not on PATH"}
    plugins_dir = home() / ".claude" / "plugins"
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
    run_cli([claude, "plugin", "uninstall", PLUGIN_ID])
    _, help_text = run_cli([claude, "plugin", "install", "--help"])
    yes = ["--yes"] if "--yes" in help_text else []
    code, out = run_cli([claude, "plugin", "install", *yes, PLUGIN_ID])
    record = _plugin_record(plugins_dir)
    cache = Path(str(record.get("installPath", ""))) if record else cache
    if code == 0 and record and cache.is_dir() and not tree_differs(clone, cache):
        return {"state": f"refreshed:{record.get('version', version)}",
                "marketplace_update": marketplace_update,
                "detail": f"cache reinstalled from the marketplace clone (v{record.get('version', version)}); "
                          f"restart Claude Code sessions to load it"}
    return {"state": "failed", "marketplace_update": marketplace_update,
            "detail": f"claude plugin install {PLUGIN_ID} did not leave a cache matching the clone "
                      f"({out.strip().splitlines()[-1] if out.strip() else code}); check `claude plugin list`, "
                      f"or inside Claude Code: /plugin marketplace add Pseudogiant-xr/Pseudolife-MCP then "
                      f"/plugin install {PLUGIN_ID}"}


# ── Codex hooks ─────────────────────────────────────────────────────────────

def _load_from_checkout(repo: Path, relative: str, name: str):
    """Import a module by file path from the checkout being installed. The
    package importable from the running interpreter may be an older release
    (or an editable install of another checkout), so nothing here relies
    on ``import pseudolife_memory``."""
    spec = importlib.util.spec_from_file_location(name, Path(repo) / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def checkout_hooks_digest(repo: Path, directory: Path) -> str | None:
    """``pseudolife_memory.plugin_hooks.hooks_digest`` from the checkout."""
    return _load_from_checkout(repo, "pseudolife_memory/plugin_hooks.py", "checkout_plugin_hooks").hooks_digest(directory)


def codex_bundle_digest(repo: Path) -> str:
    """The directory name ``ops/setup-codex-hooks.py`` gives the checkout's
    current hook scripts, computed with its own function."""
    module = _load_from_checkout(repo, "ops/setup-codex-hooks.py", "codex_hook_setup")
    return module.bundle_digest(module.bundle_bytes(Path(repo) / "plugin" / "hooks"))


def _unapproved_plugin_handlers(repo: Path, config_text: str) -> list[str] | None:
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
        manifest = json.loads((Path(repo) / "plugin" / "hooks" / "hooks.json")
                              .read_text(encoding="utf-8"))["hooks"]
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


def check_codex_hooks(repo: Path) -> dict:
    """Codex hooks come either as content-addressed manual copies under
    ``<codex home>/pseudolife/hooks/<digest>`` (``setup-codex-hooks.py``) or
    from the Codex plugin, whose marketplace clone Codex keeps itself. Both
    are refreshed with consent (hooks are trusted by hash), so this reports
    and names the command rather than acting."""
    codex_home = Path(os.environ.get("CODEX_HOME") or home() / ".codex")
    hooks_root = codex_home / "pseudolife" / "hooks"
    if hooks_root.is_dir():
        if (hooks_root / codex_bundle_digest(repo)).is_dir():
            return {"state": "bundle-present",
                    "detail": "bundle present; trust and execution not checked; rerun setup to verify with "
                              "consent: python ops/setup-codex-hooks.py --source manual --trust ask"}
        return {"state": "stale",
                "detail": "the installed copy predates the checkout's hook scripts; hooks are trusted by "
                          "hash, so refresh with consent: python ops/setup-codex-hooks.py --trust ask"}
    config = codex_home / "config.toml"
    try:
        config_text = config.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    if f'"{PLUGIN_ID}"' not in config_text:
        return {"state": "not-configured", "detail": "no Codex hook copies or plugin under the Codex home"}
    clone_hooks = codex_home / ".tmp" / "marketplaces" / MARKETPLACE / "plugin" / "hooks"
    checkout = checkout_hooks_digest(repo, Path(repo) / "plugin" / "hooks")
    if clone_hooks.is_dir() and checkout_hooks_digest(repo, clone_hooks) == checkout:
        missing = _unapproved_plugin_handlers(repo, config_text)
        if missing:
            return {"state": "needs-approval",
                    "detail": f"Codex skips {len(missing)} plugin hook handler(s) it has not approved; "
                              "approve them: python ops/setup-codex-hooks.py --source plugin --trust ask "
                              "(or /hooks in the Codex terminal app)"}
        return {"state": "current", "detail": "Codex runs the plugin's hooks; its marketplace clone matches "
                                              "the checkout's scripts"}
    return {"state": "stale" if clone_hooks.is_dir() else "plugin-managed",
            "detail": "Codex runs the plugin's hooks; refresh them through Codex's plugin manager, or "
                      "python ops/setup-codex-hooks.py --source plugin --trust ask"}


# ── the command ─────────────────────────────────────────────────────────────

def _marker(state: str) -> str:
    if state == "failed":
        return "[!]"
    if state.startswith(("installed", "refreshed", "current")) or state == "editable":
        return "[x]"
    if state in ("stale", "needs-approval"):
        return "[!]"
    return "[-]"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=str(ROOT), help="checkout to install from (default: this one)")
    parser.add_argument("--only", default="shim,plugin,codex",
                        help="comma-separated subset of shim,plugin,codex (default: all)")
    parser.add_argument("--json", action="store_true", help="one JSON report instead of the ladder")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    report: dict = {}
    if "shim" in steps:
        report["shim"] = update_shim(repo)
    if "plugin" in steps:
        report["plugin"] = update_plugin(repo)
    if "codex" in steps:
        report["codex"] = check_codex_hooks(repo)
    # A shim left un-upgraded because sessions run it still needs the rerun.
    report["ok"] = all(r["state"] != "failed" for k, r in report.items() if k != "ok")
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    labels = {"shim": "Shim", "plugin": "Plugin", "codex": "Codex hooks"}
    for key, label in labels.items():
        if key in report:
            result = report[key]
            print(f"  {_marker(result['state'])} {label:<14} {result['state']} - {result['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
