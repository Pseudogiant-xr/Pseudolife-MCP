"""Move the client side with the daemon: the shim, the plugin cache, Codex hooks.

``ops/update.ps1`` / ``ops/update.sh`` rebuild the daemon and nothing else.
The stdio shim behind each registration, the Claude Code plugin cache and
Codex's content-addressed copy of the hook scripts are separate installs
that each needed their own step, and on 2026-09-21 each was forgotten in
turn. ``--all`` on the update scripts runs this after the daemon is
healthy; it is also usable on its own.

    python ops/update_clients.py [--repo PATH] [--only shim,plugin,codex] [--json]

Shim: the command registered with Claude Code (``claude mcp get``) and
Codex (``codex mcp get``) says how the shim was installed. A launcher
inside the checkout's ``.venv`` is an editable install — the code is
already live and the metadata refresh needs every session closed, so it
is named, not run. A pipx-managed one is ``pipx install --force``-ed
from the checkout. A launcher or ``python -m pseudolife_memory.cli``
inside some other virtualenv is upgraded through that interpreter's pip.
Anything else is left alone and named.

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
import hashlib
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


_install_kinds: dict[str, tuple[str, str]] = {}


def install_kind(interpreter: Path) -> tuple[str, str]:
    """One probe per interpreter per run (a registration is classified,
    de-duplicated and then reported, each of which asks)."""
    key = str(interpreter).lower()
    if key not in _install_kinds:
        _install_kinds[key] = _probe_install_kind(interpreter)
    return _install_kinds[key]


def _probe_install_kind(interpreter: Path) -> tuple[str, str]:
    """``("editable", <project dir>)`` when the interpreter imports the
    package from outside its library directories (a PEP 660 editable
    install, a legacy egg-link, or a .pth pointing at a checkout),
    ``("site", "")`` when it imports from a library directory,
    ``("missing", "")`` when the probe answered that it does not import at
    all, and ``("unknown", <reason>)`` when the probe did not answer.
    "The probe failed" and "the package is absent" are different facts and
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
        return "unknown", f"probe exited {code}: {_failure_line(out, code)}"
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
        return "unknown", f"probe printed no report: {_failure_line(out, code)}"
    package_dir = report.get("package_dir")
    if not package_dir:
        return "missing", ""
    package = Path(package_dir)
    for lib in report.get("libs", []):
        try:
            if package.resolve().is_relative_to(Path(lib).resolve()):
                return "site", ""
        except (OSError, ValueError):
            continue
    return "editable", str(package.parent)


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


def update_shim(repo: Path) -> dict:
    repo = Path(repo)
    registrations: list[tuple[str, str, str]] = []
    for client in ("claude", "codex"):
        cli = which(client)
        if not cli:
            continue
        code, text = run_cli([cli, "mcp", "get", SERVER])
        if code != 0:
            continue
        found = _registered_command(text)
        if found:
            registrations.append((client, *found))
    if not registrations:
        return {"state": "not-registered",
                "detail": f"no stdio registration of {SERVER} in Claude Code or Codex; "
                          f"nothing to upgrade ({INSTALLER_HINT})"}
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
    done: set[str] = set()
    results: list[dict] = []
    for client, command, args in registrations:
        kind, interpreter = _classify(command, args, repo, pipx_listing)
        key = str(interpreter or command).lower()
        if key in done:
            continue
        done.add(key)
        if kind == "editable":
            venv_python = interpreter or _interpreter_beside(Path(command)) or Path(command).parent / "python"
            project = install_kind(venv_python)[1] if Path(venv_python).is_file() else ""
            results.append({"state": "editable",
                            "detail": f"{client}: {command} runs a source tree directly"
                                      + (f" ({project})" if project else "")
                                      + "; the code is already live and is never pip-upgraded. To refresh "
                                      f"its package metadata, close every session and run: "
                                      f"\"{venv_python}\" -m pip install -e \"{project or repo}\" --no-deps"})
        elif kind == "unknown":
            reason = install_kind(interpreter)[1]
            results.append({"state": "unknown",
                            "detail": f"{client}: could not tell how \"{interpreter}\" installed the package "
                                      f"({reason}); left alone rather than pip-upgraded. Once sure it is "
                                      f"not an editable install: \"{interpreter}\" -m pip install --upgrade "
                                      f"\"{repo}\""})
        elif kind == "pipx":
            code, out = run_cli([pipx, "install", "--force", str(repo)])
            results.append({"state": "reinstalled:pipx", "detail": f"{client}: pipx install --force {repo}"}
                           if code == 0 else
                           {"state": "failed", "detail": f"{client}: pipx install --force {repo} failed "
                                                         f"({_failure_line(out, code)}); "
                                                         f"close every session using the shim and retry"})
        elif kind in ("pip", "pip-user"):
            scope = ["--user"] if kind == "pip-user" else []
            shown = f"\"{interpreter}\" -m pip install {' '.join(scope + ['--upgrade'])} {repo}"
            code, out = run_cli([str(interpreter), "-m", "pip", "install", *scope, "--upgrade", str(repo)])
            results.append({"state": f"reinstalled:{kind}", "detail": f"{client}: {shown}"}
                           if code == 0 else
                           {"state": "failed",
                            "detail": f"{client}: {shown} failed "
                                      f"({_failure_line(out, code)}); "
                                      f"close every session using the shim and retry"})
        else:
            results.append({"state": "unmanaged",
                            "detail": f"{client}: {command} {args}".strip() + " is not a registration this "
                                      "helper recognises (checkout .venv, pipx, a virtualenv's launcher or "
                                      "python -m, or pip --user); upgrade it in its own environment, e.g. "
                                      f"<its python> -m pip install --upgrade \"{repo}\""})
    order = ("failed", "unknown", "reinstalled:pipx", "reinstalled:pip", "reinstalled:pip-user", "editable",
             "unmanaged")
    results.sort(key=lambda r: order.index(r["state"]) if r["state"] in order else len(order))
    return {"state": results[0]["state"], "detail": "; ".join(r["detail"] for r in results)}


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
            return {"state": "current", "detail": "the copy for the checkout's hook scripts is installed"}
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
    if state.startswith(("reinstalled", "refreshed", "current")) or state == "editable":
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
