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


def run_cli(argv, *, timeout: int = 600) -> tuple[int, str]:
    """Run a CLI and return ``(returncode, combined output)``; a missing or
    hung executable reads as a non-zero code with the reason as output."""
    try:
        # No stdin: a CLI that decides to prompt fails fast instead of holding
        # a captured-output deploy for the whole timeout with nothing shown.
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              timeout=timeout, check=False, errors="replace",
                              stdin=subprocess.DEVNULL)
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
        return "pip", path
    if stem == PACKAGE:
        if pipx_listing is not None and _pipx_owns(path, pipx_listing):
            return "pipx", None
        interpreter = _interpreter_beside(path)
        if interpreter is not None:
            return "pip", interpreter
        owner = _user_scripts_owner(path)
        if owner is not None:
            return "pip-user", owner
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
            venv_python = _interpreter_beside(Path(command)) or Path(command).parent / "python"
            results.append({"state": "editable",
                            "detail": f"{client}: {command} runs the checkout directly; the code is already "
                                      f"live. To refresh its package metadata, close every session and run: "
                                      f"\"{venv_python}\" -m pip install -e \"{repo}\" --no-deps"})
        elif kind == "pipx":
            code, out = run_cli([pipx, "install", "--force", str(repo)])
            results.append({"state": "reinstalled:pipx", "detail": f"{client}: pipx install --force {repo}"}
                           if code == 0 else
                           {"state": "failed", "detail": f"{client}: pipx install --force {repo} failed "
                                                         f"({out.strip().splitlines()[-1] if out.strip() else code}); "
                                                         f"close every session using the shim and retry"})
        elif kind in ("pip", "pip-user"):
            scope = ["--user"] if kind == "pip-user" else []
            shown = f"\"{interpreter}\" -m pip install {' '.join(scope + ['--upgrade'])} {repo}"
            code, out = run_cli([str(interpreter), "-m", "pip", "install", *scope, "--upgrade", str(repo)])
            results.append({"state": f"reinstalled:{kind}", "detail": f"{client}: {shown}"}
                           if code == 0 else
                           {"state": "failed",
                            "detail": f"{client}: {shown} failed "
                                      f"({out.strip().splitlines()[-1] if out.strip() else code}); "
                                      f"close every session using the shim and retry"})
        else:
            results.append({"state": "unmanaged",
                            "detail": f"{client}: {command} {args}".strip() + " is not a registration this "
                                      "helper recognises (checkout .venv, pipx, a virtualenv's launcher or "
                                      "python -m, or pip --user); upgrade it in its own environment, e.g. "
                                      f"<its python> -m pip install --upgrade \"{repo}\""})
    order = ("failed", "reinstalled:pipx", "reinstalled:pip", "reinstalled:pip-user", "editable", "unmanaged")
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
        plugin_managed = f'"{PLUGIN_ID}"' in config.read_text(encoding="utf-8")
    except OSError:
        plugin_managed = False
    if not plugin_managed:
        return {"state": "not-configured", "detail": "no Codex hook copies or plugin under the Codex home"}
    clone_hooks = codex_home / ".tmp" / "marketplaces" / MARKETPLACE / "plugin" / "hooks"
    checkout = checkout_hooks_digest(repo, Path(repo) / "plugin" / "hooks")
    if clone_hooks.is_dir() and checkout_hooks_digest(repo, clone_hooks) == checkout:
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
    if state == "stale":
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
