"""One command moves the client side with the daemon: ops/update_clients.py.

`ops/update.*` rebuilds the daemon and nothing else; the shim, the Claude
Code plugin cache and Codex's hook copies each needed their own step, and
each was forgotten in turn (2026-09-21). `--all` on the update scripts now
runs this helper after the daemon is healthy. It installs a new shim
runtime beside the old one and moves registrations to the launcher path
(pseudolife_memory/runtimes.py; never refusing for a running session),
refreshes the plugin cache when its bytes differ from the marketplace
clone (``claude plugin update`` into a new commit-named folder beside the
one sessions run, never an uninstall), and reports whether Codex's content-addressed hook
copy matches the checkout. Every CLI call goes through ``run_cli`` and every
lookup through ``which``/``home``, so these tests drive the real logic with
a fake CLI and a fixture home and never touch this machine's registrations.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
uc = importlib.import_module("pseudolife_memory.client_updates")   # what ops/update_clients.py runs
REAL_RUN_CLI = uc.run_cli                                   # the fixture fakes both

PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
EXE = ".exe" if os.name == "nt" else ""
SCRIPTS = "Scripts" if os.name == "nt" else "bin"


class FakeCli:
    """Answers ``run_cli`` from a table and records every call. The runtime
    install steps (venv, pip, the version probe, the Windows launcher build)
    leave behind the directory shapes the real tools would."""

    def __init__(self, home: Path):
        self.home = home
        self.calls: list[list[str]] = []
        self.pipx_list = (1, "")
        self.pipx_bin_dir: str | None = None
        self.venv = (0, "")
        self.pip_no_deps = (0, "Successfully installed pseudolife-mcp")
        self.pip_deps = (0, "Successfully installed mcp")
        self.version = "0.15.0"
        self.processes: list[tuple[int, int, str]] = []   # what list_processes reports
        self.marketplace_update = (0, "updated")
        self.marketplace_add: tuple[int, str] | None = None   # forces `claude plugin marketplace add`'s answer
        self.install_help = (0, "  -y, --yes   Accept")
        self.install_records = True
        self.clone_plugin: Path | None = None
        self.cache_plugin: Path | None = None
        # What the marketplace clone offers: Claude Code names a manifest
        # without a version by the clone's commit (12 hex characters).
        self.offered_version = "4352892f0db6"
        self.update_result: tuple[int, str] | None = None   # forces `claude plugin update`'s answer
        self.update_calls: list[tuple[list[str], dict]] = []
        self.user_scripts: Path | None = None  # what a fake python reports as its --user scripts dir
        self.install_kinds: dict[str, str] = {}   # interpreter path -> editable | site | missing
        self.run_kwargs: list[dict] = []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.run_kwargs.append(kw)
        name = Path(argv[0]).name.lower().removesuffix(".exe")
        rest = argv[1:]
        if argv[0] == "git":
            return 0, "c" * 40
        if rest[:2] == ["-m", "venv"]:
            if self.venv[0] == 0:
                runtime = Path(rest[2])
                scripts = runtime / SCRIPTS
                scripts.mkdir(parents=True, exist_ok=True)
                (scripts / f"python{EXE}").write_text("fake interpreter", encoding="utf-8")
                (runtime / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8")
            return self.venv
        if rest[:3] == ["-m", "pip", "install"] and "--no-deps" in rest:
            if self.pip_no_deps[0] == 0:
                runtime = Path(argv[0]).parent.parent
                site = runtime / "Lib" / "site-packages" / "pseudolife_memory"
                site.mkdir(parents=True, exist_ok=True)
                (site / "__init__.py").write_text("", encoding="utf-8")
                (runtime / SCRIPTS / f"pseudolife-mcp{EXE}").write_text("console", encoding="utf-8")
            return self.pip_no_deps
        if rest[:3] == ["-m", "pip", "install"]:
            return self.pip_deps
        if name.startswith("python") and rest[:2] == ["-I", "-c"] and "importlib.metadata" in rest[2]:
            return 0, self.version + "\n"
        if name.startswith("python") and rest[:1] == ["-c"] and "ScriptMaker" in rest[1]:
            target, executable, body = rest[1:][1], rest[1:][2], rest[1:][3]
            (Path(target) / "pseudolife-mcp.exe").write_bytes(
                b"MZ-fake\n#!" + executable.encode() + b"\n" + Path(body).read_bytes())
            return 0, ""
        if name.startswith("python") and rest[:1] == ["-c"] and "package_dir" in rest[1]:
            lib = str(Path(argv[0]).parent.parent / "Lib" / "site-packages")
            # A fixture runtime with a real site-packages answers from disk, as
            # the probe would (the package gone after a failed pip reads "missing").
            on_disk = "site" if (Path(lib) / "pseudolife_memory").is_dir() else "missing"
            kind = self.install_kinds.get(argv[0].lower(), on_disk if Path(lib).is_dir() else "site")
            if kind == "crashed":
                return 1, "Traceback (most recent call last):\n  File \"<string>\", line 1\nRuntimeError: boom"
            if kind == "missing":
                return 0, json.dumps({"package_dir": None, "libs": [lib]})
            if kind == "user-site":
                user = str(self.home / "AppData" / "Roaming" / "Python" / "site-packages")
                return 0, json.dumps({"package_dir": str(Path(user) / "pseudolife_memory"), "libs": [lib, user]})
            if kind in ("editable", "noisy-editable"):
                report = json.dumps({"package_dir": str(self.home / "src" / "pseudolife_memory"), "libs": [lib]})
                if kind == "noisy-editable":
                    # run_cli appends stderr after stdout: a warning printed by
                    # a .pth or sitecustomize lands after the JSON line.
                    report += "\n<string>:1: DeprecationWarning: something in a .pth\n"
                return 0, report
            return 0, json.dumps({"package_dir": str(Path(lib) / "pseudolife_memory"), "libs": [lib]})
        if name.startswith("python") and rest[:1] == ["-c"] and "sysconfig" in rest[1]:
            return (0, str(self.user_scripts)) if self.user_scripts else (1, "no user scheme")
        if name == "pipx" and rest[:2] == ["list", "--json"]:
            return self.pipx_list
        if name == "pipx" and rest[:3] == ["environment", "--value", "PIPX_BIN_DIR"]:
            return (0, self.pipx_bin_dir + "\n") if self.pipx_bin_dir else (1, "")
        if name == "claude" and rest[:3] == ["plugin", "marketplace", "update"]:
            return self.marketplace_update
        if name == "claude" and rest[:3] == ["plugin", "marketplace", "add"]:
            return self._marketplace_add(rest[3])
        if name == "claude" and rest[:3] == ["plugin", "install", "--help"]:
            return self.install_help
        if name == "claude" and rest[:2] == ["plugin", "uninstall"]:
            _record_plugin(self.home, None)
            return 0, "uninstalled"
        if name == "claude" and rest[:2] == ["plugin", "install"]:
            # Claude Code 2.1.283 replaces a same-version cache folder in
            # place; on Windows a folder a session runs hooks from refuses the
            # rename (2026-09-30, after the uninstall had already run).
            if self.cache_plugin and _in_use(self.cache_plugin):
                return 1, ("The installed copy of this plugin version at " + str(self.cache_plugin)
                           + " could not be replaced: it is in use by another program (EPERM)")
            if self.install_records and self.clone_plugin and self.cache_plugin:
                shutil.rmtree(self.cache_plugin, ignore_errors=True)
                shutil.copytree(self.clone_plugin, self.cache_plugin)
                _record_plugin(self.home, self.cache_plugin)
            return 0, "installed"
        if name == "claude" and rest[:2] == ["plugin", "update"]:
            return self._plugin_update(rest[2:], kw)
        return 91, f"unexpected call {argv}"

    def _marketplace_add(self, source: str):
        """``claude plugin marketplace add <url>`` as measured on Claude Code
        2.1.287 (2026-10-05, a throwaway CLAUDE_CONFIG_DIR): refused while
        settings.json declares the marketplace with another source; otherwise
        the existing entry is re-pointed in known_marketplaces.json (its
        clone refreshed, the plugin record untouched) and the source is
        declared in settings.json under extraKnownMarketplaces."""
        if self.marketplace_add is not None:
            return self.marketplace_add
        wanted = {"source": "git", "url": source}
        settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
        entry = data.get("extraKnownMarketplaces", {}).get("pseudolife-mcp")
        if entry is not None and entry.get("source") != wanted:
            return 1, ('Adding marketplace…✘ Failed to add marketplace: Cannot add marketplace "pseudolife-mcp": '
                       "its source doesn't match its extraKnownMarketplaces entry in user or managed settings; "
                       "add it from the source that entry lists, or change the entry.")
        known_file = uc.plugins_root() / "known_marketplaces.json"
        known = json.loads(known_file.read_text(encoding="utf-8")) if known_file.is_file() else {}
        known.setdefault("pseudolife-mcp", {})["source"] = wanted
        known_file.parent.mkdir(parents=True, exist_ok=True)
        known_file.write_text(json.dumps(known, indent=2), encoding="utf-8")
        data.setdefault("extraKnownMarketplaces", {})["pseudolife-mcp"] = {"source": wanted}
        settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return 0, "✔ Successfully added marketplace: pseudolife-mcp (declared in user settings)"

    def _plugin_update(self, args, kw):
        """``claude plugin update`` as measured on Claude Code 2.1.283: a
        marketplace offering a different version is installed into a NEW
        cache folder beside the recorded one, the record is switched, and
        the old folder is stamped ``.orphaned_at`` and never touched while a
        session marks it ``.in_use``; the same version is "already at the
        latest version" and nothing moves."""
        self.update_calls.append((args, kw))
        if self.update_result is not None:
            return self.update_result
        record = uc._plugin_record(uc.plugins_root())
        if record is None:
            return 1, f'Plugin "{PLUGIN_ID}" is not installed'
        if self.offered_version == record["version"]:
            return 0, f"pseudolife-memory is already at the latest version ({record['version']})."
        old = Path(record["installPath"])
        new = old.parent / self.offered_version
        shutil.copytree(self.clone_plugin, new)
        _record_plugin(self.home, new, version=self.offered_version, scope=record.get("scope", "user"),
                       project=record.get("projectPath"))
        (old / ".orphaned_at").write_text("1790757041269", encoding="utf-8")
        return 0, (f'Plugin "pseudolife-memory" updated from {record["version"]} to {self.offered_version} '
                   f"for scope {record.get('scope', 'user')}. Restart to apply changes.")


def _in_use(cache: Path) -> bool:
    return any((cache / ".in_use").glob("*")) if (cache / ".in_use").is_dir() else False


def _record_plugin(home: Path, cache: Path | None, version: str = "0.15.0", scope: str = "user",
                   project: str | None = None) -> None:
    plugins = uc.plugins_root()
    plugins.mkdir(parents=True, exist_ok=True)
    data = {"version": 2, "plugins": {}}
    if cache is not None:
        record = {"scope": scope, "installPath": str(cache), "version": version}
        if project:
            record["projectPath"] = project
        data["plugins"][PLUGIN_ID] = [record]
    (plugins / "installed_plugins.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


HTTPS_SOURCE = {"source": "git", "url": "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"}
# What the owner/repo shorthand the installers used up to 0.16.1 recorded.
GITHUB_SOURCE = {"source": "github", "repo": "Pseudogiant-xr/Pseudolife-MCP"}


def _record_marketplace(home: Path, clone_root: Path, source: dict = HTTPS_SOURCE) -> None:
    plugins = uc.plugins_root()
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "known_marketplaces.json").write_text(json.dumps({
        "pseudolife-mcp": {"source": source, "installLocation": str(clone_root)},
    }, indent=2), encoding="utf-8")


def _plugin_tree(root: Path, tweak: str = "") -> Path:
    """A copy of the checkout's plugin tree under ``root``; ``tweak`` appends
    a comment to one hook so the copy differs by content."""
    shutil.copytree(ROOT / "plugin", root / "plugin")
    if tweak:
        hook = root / "plugin" / "hooks" / "session-end.sh"
        hook.write_bytes(hook.read_bytes() + f"\n# {tweak}\n".encode())
    return root / "plugin"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeCli(home)
    monkeypatch.setattr(uc, "run_cli", fake)
    monkeypatch.setattr(uc, "home", lambda: home)
    monkeypatch.setattr(uc, "_install_kinds", {})  # one probe memo per test, as per run
    tools = {"claude": str(tmp_path / f"claude{EXE}"), "codex": str(tmp_path / f"codex{EXE}"),
             "pipx": str(tmp_path / f"pipx{EXE}")}
    fake.tools = tools
    monkeypatch.setattr(uc, "which", lambda name: tools.get(name))
    # This machine's process table is not the test's: nothing runs from the
    # fixture runtimes unless a test says so.
    monkeypatch.setattr(uc, "list_processes", lambda: fake.processes, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    # The runtime layout and every client config under the fixture home:
    # this machine's runtimes and registrations are never read or written.
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(home / "runtimes"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(home / "bin" / f"pseudolife-mcp{EXE}"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(uc, "_RUNTIMES_MODULE", None, raising=False)
    # This machine's extractor autostart tasks and units are not read either.
    fake.autostart = {"registered": {}, "notes": {}}
    monkeypatch.setattr(uc, "_autostart_module", lambda repo: SimpleNamespace(
        KINDS={"claude": {}, "codex": {}},
        _registered_command=lambda kind: fake.autostart["registered"].get(kind),
        registration_note=lambda kind: fake.autostart["notes"].get(kind, "")))
    return fake


# ── the shim ────────────────────────────────────────────────────────────────
#
# Registrations are read from the client config files under the fixture
# home; the runtime layout lives under the fixture too (the `cli` fixture
# points PSEUDOLIFE_SHIM_RUNTIMES / PSEUDOLIFE_SHIM_LAUNCHER there), so
# nothing here touches this machine's runtimes or registrations.

def _layout(cli):
    return uc.runtimes_module().default_layout(uc.client_env())


def _register_claude(cli, command: str, args: list[str] | None = None) -> Path:
    path = cli.home / ".claude.json"
    data = _read(path)
    data.setdefault("mcpServers", {})["pseudolife-memory"] = {
        "type": "stdio", "command": command, "args": args or [],
        "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1", "PSEUDOLIFE_WRITER_ID": "claude-code"}}
    data.setdefault("projects", {"/x": {}})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _register_codex(cli, command: str, args: list[str] | None = None, cwd: str | None = None) -> Path:
    codex_home = cli.home / "codex"
    codex_home.mkdir(exist_ok=True)
    path = codex_home / "config.toml"
    existing = path.read_text(encoding="utf-8") if path.is_file() else 'model = "gpt-5"\n'
    text = existing + (
        "\n[mcp_servers.pseudolife-memory]\n"
        f"command = '{command}'\n"
        f"args = {json.dumps(args or [])}\n"
        "startup_timeout_sec = 240\n"
        + (f"cwd = '{cwd}'\n" if cwd else "")
        + "\n[mcp_servers.pseudolife-memory.env]\nPSEUDOLIFE_WRITER_ID = \"codex\"\n"
        + "PSEUDOLIFE_MCP_NO_SPAWN = \"1\"\n")
    path.write_text(text, encoding="utf-8")
    return path


def _register_gemini(cli, command: str) -> Path:
    (cli.home / ".gemini").mkdir(exist_ok=True)
    path = cli.home / ".gemini" / "settings.json"
    path.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {"command": command,
                                                                     "env": {"PSEUDOLIFE_WRITER_ID": "gemini"}}}}),
                    encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _legacy_runtime(cli, name: str = "coordination-e41a575a") -> Path:
    """A hand-made virtualenv under the runtimes root, as the maintainer's
    2026-09 registrations name (no marker, not numbered)."""
    root = _layout(cli).root / name
    (root / SCRIPTS).mkdir(parents=True)
    (root / SCRIPTS / f"python{EXE}").write_text("", encoding="utf-8")
    (root / SCRIPTS / f"pseudolife-mcp{EXE}").write_text("", encoding="utf-8")
    (root / "pyvenv.cfg").write_text("include-system-site-packages = true\n", encoding="utf-8")
    return root


def _runtime_dirs(cli) -> list[str]:
    root = _layout(cli).root
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _install_calls(cli):
    return [c for c in cli.calls if c[1:2] == ["-m"] and c[2] in ("venv", "pip")]


def test_a_launcher_registration_gets_a_new_runtime_beside_the_current_one(cli):
    """The registration already runs the launcher: a deploy installs the
    checkout as a new runtime, the launcher starts it next, the old one
    goes because nothing runs it."""
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    first = uc.update_shim(ROOT)
    assert first["state"] == "installed:0.15.0", first
    assert _runtime_dirs(cli) == ["000001"] and layout.launcher.is_file()
    assert "claude-code: already runs the launcher" in first["detail"]
    cli.version = "0.15.1"
    second = uc.update_shim(ROOT)
    assert second["state"] == "installed:0.15.1"
    assert _runtime_dirs(cli) == ["000002"], second
    assert f"removed {layout.root / '000001'}" in second["detail"]
    assert "sessions already running keep their runtime" in second["detail"]
    # the registration was never rewritten
    assert _read(cli.home / ".claude.json")["mcpServers"]["pseudolife-memory"]["command"] == str(layout.launcher)
    assert not list(cli.home.glob("*.bak-*"))


def test_a_release_the_current_runtime_already_is_is_not_installed_again(cli):
    """`update --clients-only` after a release update installed one more
    runtime of the same release on every run (2026-10-04). The current
    runtime installed from that same pinned release is reported current;
    another release, or --reinstall, installs."""
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    cli.version = "0.16.0"
    first = uc.update_shim("pseudolife-mcp==0.16.0")
    assert first["state"] == "installed:0.16.0", first
    installs = len(_install_calls(cli))
    layout.launcher.unlink()   # what a reinstall used to repair in passing
    again = uc.update_shim("pseudolife-mcp==0.16.0")
    assert again["state"] == "current:0.16.0", again
    assert layout.launcher.is_file()
    assert len(_install_calls(cli)) == installs and _runtime_dirs(cli) == ["000001"]
    assert "runtime 000001 (0.16.0) is already installed from pseudolife-mcp==0.16.0" in again["detail"]
    forced = uc.update_shim("pseudolife-mcp==0.16.0", reinstall=True)
    assert forced["state"] == "installed:0.16.0" and _runtime_dirs(cli) == ["000002"]
    cli.version = "0.16.1"
    assert uc.update_shim("pseudolife-mcp==0.16.1")["state"] == "installed:0.16.1"
    assert _runtime_dirs(cli) == ["000003"]


def test_registrations_naming_a_runtime_path_are_moved_to_the_launcher_with_backups(cli):
    """Claude Code registers the hand-made runtime's launcher, Codex its
    python -m form with a cwd: both move to the launcher, the runtime is
    installed, the hand-made directory is named but never removed."""
    layout = _layout(cli)
    legacy = _legacy_runtime(cli)
    _register_claude(cli, str(legacy / SCRIPTS / f"pseudolife-mcp{EXE}"))
    _register_codex(cli, str(legacy / SCRIPTS / f"python{EXE}"), ["-m", "pseudolife_memory.cli"], cwd=str(legacy))
    _register_gemini(cli, str(legacy / SCRIPTS / f"pseudolife-mcp{EXE}"))
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result
    claude = _read(cli.home / ".claude.json")["mcpServers"]["pseudolife-memory"]
    assert claude["command"] == str(layout.launcher) and claude["args"] == []
    assert claude["env"]["PSEUDOLIFE_WRITER_ID"] == "claude-code"
    import tomllib
    codex = tomllib.loads((cli.home / "codex" / "config.toml").read_text(encoding="utf-8"))
    table = codex["mcp_servers"]["pseudolife-memory"]
    assert table["command"] == str(layout.launcher) and table["args"] == [] and "cwd" not in table
    assert table["startup_timeout_sec"] == 240
    assert table["env"] == {"PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_NO_SPAWN": "1"}
    gemini = _read(cli.home / ".gemini" / "settings.json")["mcpServers"]["pseudolife-memory"]
    assert gemini["command"] == str(layout.launcher)
    backups = sorted(p.name for p in cli.home.rglob("*.bak-*"))
    assert len(backups) == 3 and all(str(cli.home / b.split(".bak-")[0]) for b in backups)
    assert "(backup " in result["detail"]
    assert legacy.is_dir() and f"{legacy} is a hand-made runtime no registration names any more" in result["detail"]
    # a rerun changes nothing and backs nothing up again
    again = uc.update_shim(ROOT)
    assert again["state"] == "installed:0.15.0" and "already runs the launcher" in again["detail"]
    assert sorted(p.name for p in cli.home.rglob("*.bak-*")) == backups


def test_an_old_runtime_a_session_still_runs_survives_and_is_named(cli):
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    uc.update_shim(ROOT)
    old = layout.root / "000001"
    cli.processes = [(4100, 10, str(old / SCRIPTS / f"python{EXE}")), (4300, 30, "C:/Windows/explorer.exe")]
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0"
    assert _runtime_dirs(cli) == ["000001", "000002"]
    assert f"{old} still runs 1 process; it is removed by a later update once idle" in result["detail"]
    cli.processes = []
    result = uc.update_shim(ROOT)
    assert _runtime_dirs(cli) == ["000003"] and f"removed {old}" in result["detail"]


def test_a_runtime_a_running_tunnels_frozen_bridge_imports_from_is_kept(cli, monkeypatch):
    """The bridge a running tunnel froze imports from the runtime that froze
    it; unreadable tunnel records keep every runtime."""
    from pseudolife_memory import tunnel_runtime
    from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, private_write
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    uc.update_shim(ROOT)
    old = layout.root / "000001"
    store = ProfileStore(cli.home / ".pseudolife-mcp" / "tunnel")
    profile = Profile("dot", "http://127.0.0.1:8765", str(cli.home / "token"), tunnel_id="tunnel_0123",
                      consent=True, state="ready")
    with monkeypatch.context() as patched:
        patched.setattr(tunnel_runtime.site, "getsitepackages", lambda: [str(old / "Lib" / "site-packages")])
        snapshot = tunnel_runtime.snapshot_bridge(profile, store, ["pseudolife-mcp", "tunnel", "shim"])
    record = store.root / "dot.process.json"
    private_write(record, json.dumps({"pid": 4100, "snapshot": snapshot}).encode())
    cli.processes = [(4100, 10, str(cli.home / "elsewhere" / "tunnel-client.exe"))]   # the tunnel runs
    result = uc.update_shim(ROOT)
    assert _runtime_dirs(cli) == ["000001", "000002"], result
    record.write_bytes(b"not a record")
    result = uc.update_shim(ROOT)
    assert "older runtimes kept: saved tunnel records" in result["detail"]
    assert _runtime_dirs(cli) == ["000001", "000002", "000003"]
    record.unlink()
    uc.update_shim(ROOT)
    assert _runtime_dirs(cli) == ["000004"]


def test_a_runtime_another_client_still_names_is_kept(cli):
    """Gemini keeps pointing at runtime 000001 by hand (a registration the
    migration could not edit): the runtime is pinned, not removed."""
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    uc.update_shim(ROOT)
    old = layout.root / "000001"
    _register_gemini(cli, str(old / SCRIPTS / f"pseudolife-mcp{EXE}"))
    # make the gemini file unwritable to the migration by making it not JSON after reading
    monkey = uc.runtimes_module()
    original = monkey.migrate_registration

    def refuse(registration, layout_, **kw):
        if registration.client == "gemini":
            return {"state": "failed", "detail": "gemini: simulated write failure", "backup": None}
        return original(registration, layout_, **kw)

    monkey.migrate_registration = refuse
    try:
        result = uc.update_shim(ROOT)
    finally:
        monkey.migrate_registration = original
    assert result["state"] == "failed" and "simulated write failure" in result["detail"]
    assert old.is_dir() and _runtime_dirs(cli) == ["000001", "000002"]


def test_a_pipx_registration_moves_to_the_launcher_and_names_the_environment(cli, tmp_path):
    venv = tmp_path / "pipx-home" / "venvs" / "pseudolife-mcp"
    launcher = venv / SCRIPTS / f"pseudolife-mcp{EXE}"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(launcher)}]}}}}}))
    _register_claude(cli, str(launcher))
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result
    assert _read(cli.home / ".claude.json")["mcpServers"]["pseudolife-memory"]["command"] == str(_layout(cli).launcher)
    assert "pipx uninstall pseudolife-mcp" in result["detail"]
    # ops/shim_python.py may pick that venv for an extractor shim unit (POSIX).
    assert "shim_autostart.py show claude|codex" in result["detail"]
    assert launcher.is_file()   # nothing of the old environment is touched


def test_the_pipx_uninstall_advice_yields_to_an_extractor_shim_that_runs_from_it(cli, tmp_path):
    """ops/shim_python.py can choose pipx's venv as an extractor shim unit's
    interpreter and records it in ops/.env; uninstalling pipx's package then
    breaks the unit (2026-09-29), so the advice names the setting instead."""
    venv = tmp_path / "pipx-home" / "venvs" / "pseudolife-mcp"
    launcher = venv / SCRIPTS / f"pseudolife-mcp{EXE}"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(launcher)}]}}}}}))
    _register_claude(cli, str(launcher))
    checkout = tmp_path / "checkout"
    (checkout / "ops").mkdir(parents=True)
    (checkout / "ops" / ".env").write_text(
        f'PSEUDOLIFE_CLAUDE_SHIM_PORT=8766\nPSEUDOLIFE_CODEX_SHIM_PYTHON="{venv / SCRIPTS / "python"}"\n',
        encoding="utf-8")
    result = uc.update_shim(ROOT, repo=checkout)
    assert result["state"] == "installed:0.15.0", result
    assert "pipx uninstall" not in result["detail"]
    assert "PSEUDOLIFE_CODEX_SHIM_PYTHON" in result["detail"] and "keep" in result["detail"]


def test_a_virtualenv_launcher_elsewhere_moves_to_the_launcher(cli, tmp_path):
    scripts = tmp_path / "venv" / SCRIPTS
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    _register_claude(cli, str(scripts / f"pseudolife-mcp{EXE}"))
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result
    assert _read(cli.home / ".claude.json")["mcpServers"]["pseudolife-memory"]["command"] == str(_layout(cli).launcher)


def test_editable_checkout_shim_is_reported_not_reinstalled(cli):
    """The code is live from the checkout; the metadata refresh needs every
    session closed (the launcher is in use), so it is named, not run."""
    _register_claude(cli, str(ROOT / ".venv" / SCRIPTS / f"pseudolife-mcp{EXE}"))
    result = uc.update_shim(ROOT)
    assert result["state"] == "editable"
    assert "pip install -e" in result["detail"]
    assert not _install_calls(cli) and _runtime_dirs(cli) == []


@pytest.mark.parametrize("probed", ["site", "crashed"])
def test_a_checkout_venv_whose_probe_is_not_editable_names_the_checkout(cli, tmp_path, probed):
    """The refresh command names the project the probe found only when the
    probe found an editable install; a plain install left in the checkout's
    .venv (the 2026-09-21 state) or a probe failure must not put a
    site-packages path or an error text into `pip install -e`."""
    repo = tmp_path / "checkout"
    scripts = repo / ".venv" / SCRIPTS
    scripts.mkdir(parents=True)
    python = scripts / f"python{EXE}"
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = probed
    _register_claude(cli, str(scripts / f"pseudolife-mcp{EXE}"))
    result = uc.update_shim(repo)
    assert result["state"] == "editable"
    assert f"-m pip install -e \"{repo}\" --no-deps" in result["detail"]
    assert "site-packages" not in result["detail"] and "boom" not in result["detail"]


def test_an_editable_install_anywhere_is_named_never_reinstalled(cli, tmp_path):
    scripts = tmp_path / "dev-venv" / SCRIPTS
    scripts.mkdir(parents=True)
    python = scripts / f"python{EXE}"
    python.write_text("", encoding="utf-8")
    (tmp_path / "dev-venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "editable"
    _register_codex(cli, str(python), ["-m", "pseudolife_memory.cli"])
    result = uc.update_shim(ROOT)
    assert result["state"] == "editable" and str(cli.home / "src") in result["detail"]
    assert not _install_calls(cli)


def test_a_probe_that_did_not_answer_is_left_alone(cli, tmp_path):
    scripts = tmp_path / "venv" / SCRIPTS
    scripts.mkdir(parents=True)
    python = scripts / f"python{EXE}"
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "crashed"
    _register_codex(cli, str(python), ["-m", "pseudolife_memory.cli"])
    result = uc.update_shim(ROOT)
    assert result["state"] == "unknown" and "boom" in result["detail"]
    assert not _install_calls(cli)


def test_a_noisy_probe_answer_still_reads_as_editable(cli, tmp_path):
    scripts = tmp_path / "venv" / SCRIPTS
    scripts.mkdir(parents=True)
    python = scripts / f"python{EXE}"
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "noisy-editable"
    _register_codex(cli, str(python), ["-m", "pseudolife_memory.cli"])
    assert uc.update_shim(ROOT)["state"] == "editable"


def test_custom_registration_is_left_alone_and_named(cli):
    _register_claude(cli, "/opt/custom/bridge", ["-m", "private_bridge"])
    result = uc.update_shim(ROOT)
    assert result["state"] == "unmanaged"
    assert "/opt/custom/bridge" in result["detail"]
    assert not _install_calls(cli)


def test_no_registration_anywhere_is_reported(cli):
    assert uc.update_shim(ROOT)["state"] == "not-registered"


def test_a_failed_runtime_install_is_reported_and_touches_no_registration(cli):
    layout = _layout(cli)
    legacy = _legacy_runtime(cli)
    _register_claude(cli, str(legacy / SCRIPTS / f"pseudolife-mcp{EXE}"))
    cli.pip_no_deps = (1, "WARNING: Retrying\nERROR: Could not find a version that satisfies the requirement\n"
                          "[notice] A new release of pip is available")
    result = uc.update_shim(ROOT)
    assert result["state"] == "failed"
    assert "failed at pip install --no-deps (ERROR: Could not find a version" in result["detail"]
    assert "registrations were not touched" in result["detail"] and "--only shim" in result["detail"]
    assert _read(cli.home / ".claude.json")["mcpServers"]["pseudolife-memory"]["command"] != str(layout.launcher)
    assert _runtime_dirs(cli) == ["coordination-e41a575a"]   # no half-built runtime, no launcher
    assert not layout.launcher.exists()


def test_an_unreadable_process_table_keeps_old_runtimes_and_says_so(cli, monkeypatch):
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    uc.update_shim(ROOT)

    def unreadable():
        raise OSError("Access is denied")

    monkeypatch.setattr(uc, "list_processes", unreadable)
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0"
    assert "older runtimes kept: could not read the process table: Access is denied" in result["detail"]
    assert _runtime_dirs(cli) == ["000001", "000002"]


def test_two_registrations_install_one_runtime(cli):
    legacy = _legacy_runtime(cli)
    _register_claude(cli, str(legacy / SCRIPTS / f"pseudolife-mcp{EXE}"))
    _register_codex(cli, str(legacy / SCRIPTS / f"python{EXE}"), ["-m", "pseudolife_memory.cli"])
    uc.update_shim(ROOT)
    assert sum(1 for c in cli.calls if c[1:3] == ["-m", "venv"]) == 1
    assert _runtime_dirs(cli) == ["000001", "coordination-e41a575a"]


def test_the_install_kind_probe_runs_under_a_real_interpreter():
    """The probe is a program in a string; this runs it for real (the
    interpreter running the tests imports the package from the checkout,
    an editable-shaped answer)."""
    uc._install_kinds.clear()
    kind, where = uc.install_kind(Path(sys.executable))
    assert kind in ("editable", "site"), (kind, where)
    if kind == "editable":
        assert Path(where).resolve() == ROOT.resolve()


def test_install_kind_probe_runs_from_a_neutral_directory(cli, tmp_path):
    """`python -c` puts the working directory first on sys.path; run from
    the checkout every interpreter would read as editable."""
    python = tmp_path / "venv" / SCRIPTS / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    uc.install_kind(python)
    probe = next(kw for call, kw in zip(cli.calls, cli.run_kwargs) if "package_dir" in " ".join(call))
    assert probe.get("cwd") and Path(probe["cwd"]).resolve() != ROOT.resolve()


def test_failure_line_quotes_pips_error_not_its_notice():
    out = ("WARNING: Error parsing dependencies of x: whatever\n"
           "ERROR: Could not install packages due to an OSError: [WinError 32] locked\n"
           "\n[notice] A new release of pip is available: 24.0 -> 25.1\n")
    assert uc._failure_line(out, 1).startswith("ERROR: Could not install packages")


def test_a_same_named_venv_elsewhere_does_not_claim_the_launcher(cli, tmp_path):
    launcher = tmp_path / "somewhere" / "bin" / f"pseudolife-mcp{EXE}"
    listing = {"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(tmp_path / "pipx" / "venvs" / "pseudolife-mcp" / "bin" / "pseudolife-mcp")}]}}}}}
    assert not uc._pipx_owns(launcher, listing)
    assert uc._pipx_owns(tmp_path / "pipx" / "venvs" / "pseudolife-mcp" / "bin" / "pseudolife-mcp", listing)


def test_versioned_python_registration_is_recognised(cli, tmp_path):
    scripts = tmp_path / "venv" / "bin"
    scripts.mkdir(parents=True)
    python = scripts / "python3.12"
    python.write_text("", encoding="utf-8")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    _register_codex(cli, str(python), ["-m", "pseudolife_memory.cli"])
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result


def test_run_cli_never_inherits_stdin(monkeypatch):
    """A prompting CLI must fail fast, not hold a captured deploy for the
    whole timeout with nothing on screen."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        class P: returncode, stdout, stderr = 0, "ok", ""
        return P()

    monkeypatch.setattr(uc.subprocess, "run", fake_run)
    assert uc.run_cli(["x"]) == (0, "ok")
    assert seen["stdin"] is subprocess.DEVNULL


def test_main_exits_non_zero_when_the_runtime_install_failed(cli, capsys):
    _register_claude(cli, str(_layout(cli).launcher))
    cli.venv = (1, "Error: [Errno 13] Permission denied")
    assert uc.main(["--repo", str(ROOT), "--only", "shim"]) == 1
    assert "[!] Shim" in capsys.readouterr().out


def test_main_reports_an_installed_runtime_as_done(cli, capsys):
    _register_claude(cli, str(_layout(cli).launcher))
    assert uc.main(["--repo", str(ROOT), "--only", "shim"]) == 0
    out = capsys.readouterr().out
    assert "[x] Shim" in out and "installed:0.15.0" in out



def test_a_registration_that_spawns_its_own_daemon_is_left_where_its_full_install_is(cli, tmp_path):
    """A pip/lite-tier registration with no PSEUDOLIFE_MCP_NO_SPAWN relies on
    the spawn fallback, which a shim runtime (no torch) cannot serve."""
    scripts = tmp_path / "venv" / SCRIPTS
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    path = cli.home / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": str(scripts / f"pseudolife-mcp{EXE}"), "args": [],
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-code"}}}}), encoding="utf-8")
    result = uc.update_shim(ROOT)
    assert result["state"] == "spawning" and "spawns its own daemon" in result["detail"]
    assert not _install_calls(cli) and _runtime_dirs(cli) == []
    assert _read(path)["mcpServers"]["pseudolife-memory"]["command"] == str(scripts / f"pseudolife-mcp{EXE}")


def test_pipx_bin_dir_copy_of_the_launcher_moves_to_the_launcher(cli, tmp_path):
    """On Windows without Developer Mode pipx puts a copy (not a symlink) of
    the launcher in its bin dir; `pipx list` does not name it, so the bin
    dir is asked for."""
    bin_dir = tmp_path / "pipx-home" / "bin"
    bin_dir.mkdir(parents=True)
    copy = bin_dir / f"pseudolife-mcp{EXE}"
    copy.write_text("", encoding="utf-8")
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(tmp_path / "pipx-home" / "venvs" / "pseudolife-mcp" / SCRIPTS / f"pseudolife-mcp{EXE}")}]}}}}}))
    cli.pipx_bin_dir = str(bin_dir)
    _register_claude(cli, str(copy))
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result
    assert _read(cli.home / ".claude.json")["mcpServers"]["pseudolife-memory"]["command"] == str(_layout(cli).launcher)
    assert "pipx uninstall pseudolife-mcp" in result["detail"]


def test_the_ladder_says_how_the_launcher_is_reached_by_name(cli, monkeypatch, capsys):
    """After the runtime installs, `pseudolife-mcp` in a terminal is made to
    reach the launcher (a ~/.local/bin link on POSIX, the user PATH on
    Windows) and the ladder names what happened, with the new-terminal note;
    the layout it is asked about is the one the runtime went into."""
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    seen = []

    def expose(layout_, **kw):
        seen.append(layout_)
        return {"state": "added", "detail": f"added {layout_.launcher_dir} to the front of your user PATH",
                "hint": "open a new terminal for `pseudolife-mcp` to resolve to the launcher; "
                        "this one still runs C:/old/pseudolife-mcp.exe"}

    monkeypatch.setattr(uc.runtimes_module(), "expose_launcher", expose)
    assert uc.main(["--repo", str(ROOT), "--only", "shim"]) == 0
    out = capsys.readouterr().out
    assert seen == [layout]
    assert f"added {layout.launcher_dir} to the front of your user PATH" in out
    assert "open a new terminal for `pseudolife-mcp` to resolve to the launcher; this one still runs" in out


def test_a_failed_path_step_is_named_but_does_not_fail_the_shim(cli, monkeypatch):
    """Every registration names the launcher by its full path: a PATH step
    that could not be done is reported, the installed runtime stands."""
    _register_claude(cli, str(_layout(cli).launcher))
    monkeypatch.setattr(uc.runtimes_module(), "expose_launcher", lambda layout_, **kw: {
        "state": "failed", "detail": "could not add X to your user PATH (denied)", "hint": None})
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result
    assert "could not add X to your user PATH (denied)" in result["detail"]


def test_an_overridden_layout_touches_no_path(cli):
    _register_claude(cli, str(_layout(cli).launcher))
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0"
    assert "PATH" not in result["detail"] and "linked" not in result["detail"]


@pytest.mark.skipif(os.name == "nt", reason="the ~/.local/bin link is the POSIX mechanism")
def test_the_old_pipx_link_in_the_user_bin_is_replaced_by_the_launcher(cli, monkeypatch):
    """The Debian host of 2026-09-29: ~/.local/bin/pseudolife-mcp was pipx's
    link to the old package. The update moves it aside and links the
    launcher there, and the ladder says so."""
    layout = _layout(cli)
    _register_claude(cli, str(layout.launcher))
    user_bin = cli.home / ".local" / "bin"
    old = cli.home / ".local" / "share" / "pipx" / "venvs" / "pseudolife-mcp" / "bin" / "pseudolife-mcp"
    old.parent.mkdir(parents=True)
    old.write_text("#!/usr/bin/python3\nfrom pseudolife_memory.cli import main\n", encoding="utf-8")
    user_bin.mkdir(parents=True)
    os.symlink(str(old), str(user_bin / "pseudolife-mcp"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_USER_BIN", str(user_bin))
    monkeypatch.setenv("PATH", os.pathsep.join([str(user_bin), "/usr/bin", "/bin"]))
    result = uc.update_shim(ROOT)
    assert result["state"] == "installed:0.15.0", result
    assert os.readlink(user_bin / "pseudolife-mcp") == str(layout.launcher)
    assert f"linked {user_bin / 'pseudolife-mcp'} -> {layout.launcher}" in result["detail"]
    assert "the old pipx entry is kept as" in result["detail"]

# ── the plugin cache ────────────────────────────────────────────────────────

def _plugin_fixture(cli, tmp_path, *, differ: bool, installed: bool = True, in_use: bool = False,
                    source: dict = HTTPS_SOURCE):
    """A marketplace clone of the checkout's plugin and an installed cache
    folder at 0.15.0. ``in_use`` marks the cache the way Claude Code does
    while a session runs from it: ``.in_use/<pid>`` holding the pid and its
    process start time."""
    clone_root = tmp_path / "marketplace"
    clone = _plugin_tree(clone_root)
    cache = tmp_path / "cache" / "pseudolife-mcp" / "pseudolife-memory" / "0.15.0"
    shutil.copytree(ROOT / "plugin", cache)
    if differ:
        hook = cache / "hooks" / "session-end.sh"
        hook.write_bytes(hook.read_bytes() + b"\n# stale cache\n")
    if in_use:
        (cache / ".in_use").mkdir()
        (cache / ".in_use" / "42652").write_text('{"pid":42652,"procStartFt":"134352300019189034"}',
                                                 encoding="utf-8")
    _record_marketplace(cli.home, clone_root, source)
    _record_plugin(cli.home, cache if installed else None)
    cli.clone_plugin, cli.cache_plugin = clone, cache
    return clone, cache


def _claude_calls(cli):
    return [c[1:] for c in cli.calls if Path(c[0]).name.lower().startswith("claude")]


def _snapshot(tree: Path) -> dict[str, bytes]:
    return {p.relative_to(tree).as_posix(): p.read_bytes() for p in sorted(tree.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("in_use", [True, False], ids=["sessions-open", "sessions-closed"])
def test_a_changed_plugin_installs_beside_the_copy_sessions_run(cli, tmp_path, in_use):
    """2026-09-30: with sessions open the updater uninstalled the plugin, then
    `claude plugin install` could not replace the in-use 0.15.0 folder
    (EPERM) and the plugin stayed uninstalled until the apps were restarted.
    The plugin now carries no version, so Claude Code names each marketplace
    commit's copy by its commit and `claude plugin update` installs it into a
    new folder beside the old one, whether or not a session runs it. The
    updater never uninstalls, and the copy running sessions loaded is left
    byte for byte as it was."""
    clone, cache = _plugin_fixture(cli, tmp_path, differ=True, in_use=in_use)
    before = _snapshot(cache)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "refreshed:4352892f0db6", result
    acted = _claude_calls(cli)
    assert ["plugin", "marketplace", "update", "pseudolife-mcp"] in acted
    assert ["plugin", "update", PLUGIN_ID, "--scope", "user"] in acted
    assert not any(c[:2] in (["plugin", "uninstall"], ["plugin", "install"]) for c in acted), acted
    record = uc._plugin_record(uc.plugins_root())
    new = Path(record["installPath"])
    assert new == cache.parent / "4352892f0db6" and not uc.tree_differs(clone, new)
    # The old copy is Claude Code's to sweep once no session marks it.
    after = {k: v for k, v in _snapshot(cache).items() if k != ".orphaned_at"}
    assert after == before
    assert "sessions already running keep" in result["detail"]


def test_claude_code_markers_do_not_make_a_matching_cache_look_stale(cli, tmp_path):
    """The 2026-09-30 root cause: Claude Code writes `.in_use/<pid>` into the
    cache while a session runs from it (and `.orphaned_at` on a copy no
    record names), so a byte comparison that counted them saw a stale cache
    whenever any session was open."""
    _, cache = _plugin_fixture(cli, tmp_path, differ=False, in_use=True)
    (cache / ".orphaned_at").write_text("1789969837541", encoding="utf-8")
    (cache / ".in_use-links").mkdir()
    (cache / ".in_use-links" / "7").write_text("x", encoding="utf-8")
    result = uc.update_plugin(ROOT)
    assert result["state"] == "current:0.15.0", result
    assert not any(c[:2] != ["plugin", "marketplace"] for c in _claude_calls(cli))


@pytest.mark.parametrize("in_use", [True, False], ids=["sessions-open", "sessions-closed"])
def test_a_failed_update_leaves_the_installed_plugin_in_place(cli, tmp_path, in_use):
    """Nothing is removed before a replacement is installed: when Claude Code
    cannot install the new copy the record and the cache stay as they were
    and the ladder says so."""
    _, cache = _plugin_fixture(cli, tmp_path, differ=True, in_use=in_use)
    before = uc._plugin_record(uc.plugins_root())
    cli.update_result = (1, "EPERM: operation not permitted, rename")
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed"
    assert "EPERM" in result["detail"] and "left installed" in result["detail"]
    assert uc._plugin_record(uc.plugins_root()) == before and cache.is_dir()
    assert not any(c[:2] in (["plugin", "uninstall"], ["plugin", "install"]) for c in _claude_calls(cli))


def test_a_marketplace_that_still_offers_the_installed_version_is_named_not_uninstalled(cli, tmp_path):
    """A clone whose manifest still pins the installed version (an older
    clone, a fork) cannot be installed beside the copy sessions run, and
    replacing that copy in place is what stranded the plugin. The updater
    says so instead of uninstalling."""
    clone, cache = _plugin_fixture(cli, tmp_path, differ=True, in_use=True)
    manifest = clone / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text(encoding="utf-8")), "version": "0.15.0"}),
                        encoding="utf-8")
    cli.offered_version = "0.15.0"
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed"
    assert "same version" in result["detail"] and "left installed" in result["detail"]
    assert uc._plugin_record(uc.plugins_root())["installPath"] == str(cache)
    assert not any(c[:2] in (["plugin", "uninstall"], ["plugin", "install"]) for c in _claude_calls(cli))


def test_a_missing_cache_folder_is_named_as_such_not_as_a_pinned_version(cli, tmp_path):
    """The record names a folder that is gone (removed by hand) while the
    clone is still at the recorded commit: Claude Code has nothing newer to
    install. That is not the pinned-version case, and saying so would send
    the user round the same failing retry (review finding, 2026-09-30)."""
    _, cache = _plugin_fixture(cli, tmp_path, differ=False)
    shutil.rmtree(cache)
    cli.offered_version = "0.15.0"
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed"
    assert "same version" not in result["detail"]
    assert "missing or was changed by hand" in result["detail"] and "left installed" in result["detail"]
    assert not any(c[:2] in (["plugin", "uninstall"], ["plugin", "install"]) for c in _claude_calls(cli))


def test_an_update_that_drops_the_record_points_at_the_installer(cli, tmp_path):
    """Should `claude plugin update` itself ever leave no record, the ladder
    says the plugin is not installed and how to install it."""
    _plugin_fixture(cli, tmp_path, differ=True)

    def drop(args, kw):
        _record_plugin(cli.home, None)
        return 1, "something went wrong"
    cli._plugin_update = drop
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed"
    assert "no longer recorded as installed" in result["detail"] and uc.INSTALLER_HINT in result["detail"]


def test_a_project_install_without_its_project_is_not_updated_from_here(cli, tmp_path):
    """A project or local install is found from its project; without the
    project path the update would act on whatever project the updater runs
    in, so it is named and left alone."""
    _, cache = _plugin_fixture(cli, tmp_path, differ=True)
    _record_plugin(cli.home, cache, scope="local")
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed" and "names no project" in result["detail"]
    assert cli.update_calls == []


# What Claude Code 2.1.287 printed for a github-source marketplace with no
# github.com host key in known_hosts (measured 2026-10-04 in a sandboxed
# CLAUDE_CONFIG_DIR; exit 1, the failure on stderr after the progress line).
HOST_KEY_FAILURE = (
    "Updating marketplace: pseudolife-mcp...\n"
    "✘ Failed to update marketplace(s): Failed to refresh marketplace 'pseudolife-mcp': Failed to clone "
    "marketplace repository: SSH host key is not in your known_hosts file. To add it, connect once manually "
    "(this will show the fingerprint for you to verify):\n"
    "  ssh -T git@github.com\n\n"
    "Or use an HTTPS URL instead (recommended for public repos).\n\n"
    "Original error: Cloning into '/root/.claude/plugins/marketplaces/pseudolife-mcp..clone'...\n"
    "No ED25519 host key is known for github.com and you have requested strict checking.\n"
    "Host key verification failed.\n"
    "fatal: Could not read from remote repository.\n")


# run_cli decodes with the locale codec: on a cp1252 Windows console Node's
# UTF-8 cross mark arrives as three other characters ahead of the line.
GARBLED_HOST_KEY_FAILURE = HOST_KEY_FAILURE.encode("utf-8").decode("cp1252", errors="replace")


@pytest.mark.parametrize("answer", [(1, HOST_KEY_FAILURE), (0, HOST_KEY_FAILURE), (0, GARBLED_HOST_KEY_FAILURE)],
                         ids=["exit-1", "exit-0-with-failure-line", "exit-0-with-garbled-mark"])
def test_a_failed_marketplace_update_never_reads_as_current(cli, tmp_path, answer):
    """2026-10-04 on the homelab box: the marketplace update failed (no
    github.com host key for root), the clone stayed at the previous release,
    and a cache matching that stale clone was reported `current` while the
    box kept the old release's hooks. A clone that could not be refreshed
    proves nothing about the cache, whichever way the CLI signals it."""
    _plugin_fixture(cli, tmp_path, differ=False)
    cli.marketplace_update = answer
    result = uc.update_plugin(ROOT)
    assert result["marketplace_update"] == "failed"
    assert result["state"] == "failed", result
    assert "Failed to refresh marketplace 'pseudolife-mcp'" in result["detail"]
    assert "known_hosts" in result["detail"] and "fingerprint" in result["detail"]
    assert "claude plugin marketplace add https://github.com/Pseudogiant-xr/Pseudolife-MCP.git" in result["detail"]
    assert not any(c[:2] == ["plugin", "update"] for c in _claude_calls(cli))


def _declare_marketplace(home: Path, source: dict) -> Path:
    settings = home / "settings.json"   # the fixture's CLAUDE_CONFIG_DIR is the fake home
    settings.write_text(json.dumps({"env": {"X": "1"}, "extraKnownMarketplaces": {
        "pseudolife-mcp": {"source": source}}}), encoding="utf-8")
    return settings


def _settings_with(source: dict | None) -> dict:
    """A user settings file as an old install leaves it: other keys, another
    marketplace, and (when given) this marketplace declared with ``source``."""
    declared = {"other-mkt": {"source": {"source": "github", "repo": "someone/other"}}}
    if source is not None:
        declared["pseudolife-mcp"] = {"source": source, "autoUpdate": True}
    return {"env": {"X": "1"}, "enabledPlugins": {PLUGIN_ID: True}, "extraKnownMarketplaces": declared}


def _add_other_marketplace(known_file: Path) -> None:
    known = json.loads(known_file.read_text(encoding="utf-8"))
    known["other-mkt"] = {"source": {"source": "github", "repo": "someone/other"}, "installLocation": "x"}
    known_file.write_text(json.dumps(known, indent=2), encoding="utf-8")


def _marketplace_adds(cli):
    return [c for c in _claude_calls(cli) if c[:3] == ["plugin", "marketplace", "add"]]


def _backups(cli) -> list[Path]:
    return sorted(uc.plugins_root().glob("backup-*-marketplace-https"))


REFUSED_ADD = (1, 'Adding marketplace…✘ Failed to add marketplace: Cannot add marketplace "pseudolife-mcp": '
                  "its source doesn't match its extraKnownMarketplaces entry in user or managed settings; add "
                  "it from the source that entry lists, or change the entry.")


@pytest.mark.parametrize("declared", [GITHUB_SOURCE, None], ids=["declared-in-settings", "undeclared"])
def test_an_old_github_marketplace_moves_to_https_before_the_refresh(cli, tmp_path, declared):
    """Installers up to 0.16.1 added the marketplace by the owner/repo
    shorthand, which Claude Code records as a `github` source and refreshes
    over SSH; on a host with no GitHub SSH key every refresh fails (homelab
    box, 2026-10-04, fixed by hand). The plugin step now re-points it to the
    HTTPS URL itself: settings.json's declaration first (Claude Code refuses
    the add while it names another source), then the add, which re-points
    known_marketplaces.json. Other keys and other marketplaces are kept,
    every file is backed up first, and the refresh runs after it."""
    _plugin_fixture(cli, tmp_path, differ=True, source=GITHUB_SOURCE)
    known_file = uc.plugins_root() / "known_marketplaces.json"
    _add_other_marketplace(known_file)
    settings = cli.home / "settings.json"   # the fixture's CLAUDE_CONFIG_DIR is the fake home
    original = json.dumps(_settings_with(declared), indent=2)
    settings.write_text(original, encoding="utf-8")
    known_before = known_file.read_bytes()

    result = uc.update_plugin(ROOT)

    assert result["state"] == "refreshed:4352892f0db6", result
    assert result["marketplace_source"] == "migrated"
    assert "HTTPS" in result["detail"] and "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git" in result["detail"]
    acted = _claude_calls(cli)
    add = ["plugin", "marketplace", "add", "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"]
    assert add in acted
    assert acted.index(add) < acted.index(["plugin", "marketplace", "update", "pseudolife-mcp"])
    assert not any(c[:3] == ["plugin", "marketplace", "remove"] for c in acted)
    data = json.loads(settings.read_text(encoding="utf-8"))
    expected = _settings_with(HTTPS_SOURCE)
    if declared is None:
        expected["extraKnownMarketplaces"]["pseudolife-mcp"] = {"source": HTTPS_SOURCE}
    assert data == expected
    known = json.loads(known_file.read_text(encoding="utf-8"))
    assert known["pseudolife-mcp"]["source"] == HTTPS_SOURCE
    assert known["other-mkt"]["source"] == {"source": "github", "repo": "someone/other"}
    (backup,) = _backups(cli)
    assert (backup / "settings.json").read_text(encoding="utf-8") == original
    assert (backup / "known_marketplaces.json").read_bytes() == known_before
    assert (backup / "installed_plugins.json").is_file()
    assert str(backup) in result["detail"]


def test_an_https_marketplace_is_left_alone_and_reported_current(cli, tmp_path):
    """Idempotent: once the marketplace fetches over HTTPS there is no add,
    no edit and no backup, and the step says so."""
    _plugin_fixture(cli, tmp_path, differ=False)
    settings = cli.home / "settings.json"
    settings.write_text(json.dumps(_settings_with(HTTPS_SOURCE)), encoding="utf-8")
    before = settings.read_bytes()
    result = uc.update_plugin(ROOT)
    assert result["state"] == "current:0.15.0", result
    assert result["marketplace_source"] == "current"
    assert _marketplace_adds(cli) == [] and _backups(cli) == []
    assert settings.read_bytes() == before


@pytest.mark.parametrize("source", [
    {"source": "github", "repo": "someone/Pseudolife-MCP-fork"},
    {"source": "directory", "path": "C:/src/Pseudolife-MCP"},
    {"source": "github", "repo": "Pseudogiant-xr/Pseudolife-MCP", "ref": "my-branch"},
], ids=["fork", "local-directory", "pinned-ref"])
def test_a_marketplace_someone_pointed_elsewhere_is_not_touched(cli, tmp_path, source):
    """Only the shorthand the installers wrote is moved: a fork, a local
    checkout or a pinned ref was somebody's choice."""
    _plugin_fixture(cli, tmp_path, differ=False, source=source)
    settings = cli.home / "settings.json"
    settings.write_text(json.dumps(_settings_with(source)), encoding="utf-8")
    before = settings.read_bytes()
    result = uc.update_plugin(ROOT)
    assert result["marketplace_source"] == "kept", result
    assert _marketplace_adds(cli) == [] and _backups(cli) == []
    assert settings.read_bytes() == before


def test_a_refused_add_puts_the_entry_back_and_the_refresh_still_runs(cli, tmp_path):
    """Claude Code refuses the add while managed settings (which no user
    process may edit) still declare the old source. Only this marketplace's
    entry goes back, so a running session's write to settings.json made
    while the add ran survives (review finding, 2026-10-05). The move
    changed nothing, so the refresh still runs (SSH works on some hosts),
    and the step fails with the CLI's own line."""
    _plugin_fixture(cli, tmp_path, differ=True, source=GITHUB_SOURCE)
    settings = cli.home / "settings.json"
    settings.write_text(json.dumps(_settings_with(GITHUB_SOURCE), indent=2), encoding="utf-8")

    def session_writes_then_refused(source):
        data = json.loads(settings.read_text(encoding="utf-8"))
        data["theme"] = "dark"   # a running Claude Code session saves a setting meanwhile
        settings.write_text(json.dumps(data), encoding="utf-8")
        return REFUSED_ADD
    cli._marketplace_add = session_writes_then_refused
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed" and result["marketplace_source"] == "failed", result
    assert "its source doesn't match its extraKnownMarketplaces entry" in result["detail"]
    assert "put back as it was" in result["detail"]
    assert json.loads(settings.read_text(encoding="utf-8")) == {**_settings_with(GITHUB_SOURCE), "theme": "dark"}
    acted = _claude_calls(cli)
    assert ["plugin", "marketplace", "update", "pseudolife-mcp"] in acted
    assert any(c[:2] == ["plugin", "update"] for c in acted)


@pytest.mark.parametrize("declared", ["github", "undeclared", "no-settings-file"])
def test_an_add_that_does_not_re_point_the_record_is_a_failure(cli, tmp_path, declared):
    """The read-back is the proof: a zero exit that leaves
    known_marketplaces.json on the github source is not a migration. The add
    declares the marketplace in settings.json itself, so the entry is put
    back as it was even where this never edited it (review finding,
    2026-10-05): otherwise settings and known disagree and Claude Code
    lists no marketplace at all."""
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    settings = cli.home / "settings.json"
    original = None
    if declared != "no-settings-file":
        original = _settings_with(GITHUB_SOURCE if declared == "github" else None)
        settings.write_text(json.dumps(original), encoding="utf-8")

    def declares_but_does_not_re_point(source):
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
        data.setdefault("extraKnownMarketplaces", {})["pseudolife-mcp"] = {
            "source": {"source": "git", "url": source}}
        settings.write_text(json.dumps(data), encoding="utf-8")
        return 0, "✔ Marketplace 'pseudolife-mcp' already on disk"
    cli._marketplace_add = declares_but_does_not_re_point
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed" and result["marketplace_source"] == "failed", result
    assert "still records" in result["detail"]
    if original is None:
        assert not settings.exists()
    else:
        assert json.loads(settings.read_text(encoding="utf-8")) == original


def test_a_symlinked_settings_file_keeps_its_link(cli, tmp_path):
    """settings.json linked from a dotfiles checkout is edited at its target;
    replacing the link with a plain file would cut it off (review finding,
    2026-10-05)."""
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_settings_with(GITHUB_SOURCE)), encoding="utf-8")
    link = cli.home / "settings.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    result = uc.update_plugin(ROOT)
    assert result["marketplace_source"] == "migrated", result
    assert link.is_symlink()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["extraKnownMarketplaces"]["pseudolife-mcp"]["source"] == HTTPS_SOURCE
    assert data["extraKnownMarketplaces"]["pseudolife-mcp"]["autoUpdate"] is True


def test_a_settings_file_that_cannot_be_backed_up_is_a_failed_line_not_a_crash(cli, tmp_path, monkeypatch):
    """A file another process holds, or a plugins directory that cannot be
    written: the step says so and changes nothing, and the ladder goes on
    (review finding, 2026-10-05)."""
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    settings = cli.home / "settings.json"
    settings.write_text(json.dumps(_settings_with(GITHUB_SOURCE)), encoding="utf-8")
    before = settings.read_bytes()

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(uc.shutil, "copy2", denied)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed" and result["marketplace_source"] == "failed", result
    assert "Permission denied" in result["detail"] and "nothing was changed" in result["detail"]
    assert settings.read_bytes() == before and _marketplace_adds(cli) == []


def test_an_add_that_drops_the_plugin_record_is_named(cli, tmp_path):
    """Measured: the add leaves installed_plugins.json byte-identical. Should
    a Claude Code release ever uninstall on re-point, the step says how to
    put the plugin back instead of carrying on."""
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    real_add = cli._marketplace_add

    def add_and_drop(source):
        answer = real_add(source)
        _record_plugin(cli.home, None)
        return answer
    cli._marketplace_add = add_and_drop
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed", result
    assert f"claude plugin install {PLUGIN_ID}" in result["detail"]


def test_unreadable_settings_are_named_not_rewritten(cli, tmp_path):
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    settings = cli.home / "settings.json"
    settings.write_text('{"env": {"X": "1"},', encoding="utf-8")
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed" and result["marketplace_source"] == "failed", result
    assert str(settings) in result["detail"] and "not valid JSON" in result["detail"]
    assert settings.read_text(encoding="utf-8") == '{"env": {"X": "1"},'
    assert _marketplace_adds(cli) == []


def test_claude_config_dir_is_where_settings_and_plugins_are_read(cli, tmp_path, monkeypatch):
    """Claude Code keeps settings.json and its plugins directory under
    CLAUDE_CONFIG_DIR when it is set (measured on 2.1.287, 2026-10-05); the
    migration edits that settings.json and never ~/.claude's."""
    config = tmp_path / "config-dir"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    assert uc.plugins_root() == config / "plugins"
    decoy = cli.home / ".claude" / "settings.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text(json.dumps(_settings_with(GITHUB_SOURCE)), encoding="utf-8")
    decoy_before = decoy.read_bytes()
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    (config / "settings.json").write_text(json.dumps(_settings_with(GITHUB_SOURCE)), encoding="utf-8")
    result = uc.update_plugin(ROOT)
    assert result["marketplace_source"] == "migrated", result
    data = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    assert data["extraKnownMarketplaces"]["pseudolife-mcp"]["source"] == HTTPS_SOURCE
    assert decoy.read_bytes() == decoy_before


def test_the_plugins_directory_defaults_to_the_home_claude_folder(cli, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert uc.plugins_root() == cli.home / ".claude" / "plugins"


def test_the_installer_helper_reports_in_one_line(cli, tmp_path, capsys):
    """ops/plugin_marketplace.py (the installers' step) prints one line for a
    marketplace it moved or found current, nothing when none is recorded,
    and exits 1 only on a failure."""
    assert uc.marketplace_main() == 0 and capsys.readouterr().out == ""
    _plugin_fixture(cli, tmp_path, differ=False, source=GITHUB_SOURCE)
    assert uc.marketplace_main() == 0
    moved = capsys.readouterr().out
    assert moved.count("\n") == 1 and "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git" in moved
    assert uc.marketplace_main() == 0
    current = capsys.readouterr().out
    assert current.count("\n") == 1 and "nothing to change" in current
    (cli.home / "settings.json").write_text("{", encoding="utf-8")
    assert uc.marketplace_main() == 1
    assert "not valid JSON" in capsys.readouterr().out


def test_the_installer_helper_runs_from_a_bare_checkout(tmp_path):
    """The installers run it with whatever Python 3.10+ the host has, before
    any shim is installed: stdlib only, from the checkout."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_PLUGIN_CACHE_DIR"}
    env.update({"CLAUDE_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "ops" / "plugin_marketplace.py")],
                          capture_output=True, text=True, timeout=60, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("declared", [None, {"source": "git", "url": "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"}],
                         ids=["undeclared", "declared-https"])
def test_the_settings_step_is_only_named_where_a_declaration_blocks_the_add(cli, tmp_path, declared):
    _plugin_fixture(cli, tmp_path, differ=False)
    if declared:
        _declare_marketplace(cli.home, declared)
    cli.marketplace_update = (1, HOST_KEY_FAILURE)
    detail = uc.update_plugin(ROOT)["detail"]
    assert "extraKnownMarketplaces" not in detail and "declares" not in detail
    assert "claude plugin marketplace add https://github.com/Pseudogiant-xr/Pseudolife-MCP.git" in detail


def test_a_marketplace_update_that_timed_out_is_named(cli, tmp_path):
    """A hung clone stopped at run_cli's timeout carries no CLI line; the
    reason run_cli gives is quoted instead, with the generic remedy."""
    _plugin_fixture(cli, tmp_path, differ=False)
    cli.marketplace_update = (uc.TIMED_OUT, "TimeoutExpired: Command 'claude' timed out after 600 seconds")
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed", result
    assert "timed out after 600 seconds" in result["detail"]
    assert "run claude plugin marketplace update pseudolife-mcp" in result["detail"]


def test_a_failed_marketplace_update_fails_the_ladder(cli, tmp_path, capsys):
    """The run's exit code is what an unattended deploy reads."""
    _plugin_fixture(cli, tmp_path, differ=False)
    cli.marketplace_update = (1, HOST_KEY_FAILURE)
    assert uc.main(["--repo", str(ROOT), "--only", "plugin"]) == 1
    assert "[!] Plugin" in capsys.readouterr().out


def test_plugin_refresh_from_a_clone_that_could_not_be_updated_is_not_a_success(cli, tmp_path):
    """Offline: the clone is what it is, and installing its copy is still
    progress, so the refresh runs; but the clone may be behind the
    marketplace, so the step says so instead of `refreshed`."""
    _plugin_fixture(cli, tmp_path, differ=True)
    cli.marketplace_update = (1, "Updating marketplace: pseudolife-mcp...\n"
                                 "✘ Failed to update marketplace(s): fatal: unable to access "
                                 "'https://github.com/Pseudogiant-xr/Pseudolife-MCP.git/'")
    result = uc.update_plugin(ROOT)
    assert result["marketplace_update"] == "failed"
    assert result["state"] == "failed", result
    assert uc._plugin_record(uc.plugins_root())["version"] == "4352892f0db6"
    assert "4352892f0db6 installed from the marketplace clone" in result["detail"]
    assert "fatal: unable to access" in result["detail"]
    assert "known_hosts" not in result["detail"]


def test_a_successful_marketplace_update_with_noise_still_reads_as_current(cli, tmp_path):
    """Only a failure line counts: a successful update's own output is not
    mistaken for one."""
    _plugin_fixture(cli, tmp_path, differ=False)
    cli.marketplace_update = (0, "Updating marketplace: pseudolife-mcp...\n"
                                 "✔ Successfully updated marketplace: pseudolife-mcp")
    result = uc.update_plugin(ROOT)
    assert result["marketplace_update"] == "ok" and result["state"] == "current:0.15.0", result


def test_a_project_scoped_install_is_updated_in_its_project(cli, tmp_path):
    """`claude plugin update` finds a project or local install from the
    project's directory, at that scope."""
    _, cache = _plugin_fixture(cli, tmp_path, differ=True)
    project = tmp_path / "proj"
    project.mkdir()
    _record_plugin(cli.home, cache, scope="local", project=str(project))
    result = uc.update_plugin(ROOT)
    assert result["state"] == "refreshed:4352892f0db6", result
    args, kw = cli.update_calls[-1]
    assert args == [PLUGIN_ID, "--scope", "local"] and kw.get("cwd") == str(project)


def test_the_plugins_directory_follows_claude_codes_override(cli, tmp_path, monkeypatch):
    """`CLAUDE_CODE_PLUGIN_CACHE_DIR` moves Claude Code's whole plugins
    directory (the records, the marketplaces, the cache); the updater reads
    the same one Claude Code writes."""
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", str(tmp_path / "elsewhere"))
    _plugin_fixture(cli, tmp_path, differ=True)
    assert uc.plugins_root() == tmp_path / "elsewhere"
    assert uc.update_plugin(ROOT)["state"] == "refreshed:4352892f0db6"


def test_plugin_not_installed_points_at_the_installer(cli, tmp_path):
    _plugin_fixture(cli, tmp_path, differ=False, installed=False)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "not-installed"
    assert "install" in result["detail"]


def test_a_missing_marketplace_clone_names_the_https_add(cli, tmp_path):
    """The advice adds the marketplace by its HTTPS URL: the owner/repo
    shorthand records a `github` source that Claude Code refreshes over
    SSH, which fails on a host with no GitHub key (2026-10-04)."""
    clone, _ = _plugin_fixture(cli, tmp_path, differ=False)
    shutil.rmtree(clone)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed"
    assert ("claude plugin marketplace add https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"
            in result["detail"])


def test_main_exits_zero_when_the_plugin_updates_beside_a_running_session(cli, tmp_path, capsys):
    """The acceptance bar: sessions open, one command, exit 0, nothing to
    rerun."""
    _plugin_fixture(cli, tmp_path, differ=True, in_use=True)
    assert uc.main(["--repo", str(ROOT), "--only", "plugin"]) == 0
    out = capsys.readouterr().out
    assert "[x] Plugin" in out and "refreshed:4352892f0db6" in out
    assert "close" not in out.lower() and "rerun" not in out.lower()


def test_tree_differs_ignores_line_endings_and_git_metadata(tmp_path):
    a = _plugin_tree(tmp_path / "a")
    b = _plugin_tree(tmp_path / "b")
    hook = b / "hooks" / "session-start.sh"
    hook.write_bytes(hook.read_bytes().replace(b"\n", b"\r\n"))
    (b / ".git").mkdir()
    (b / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    assert uc.tree_differs(a, b) is False
    # Claude Code's own bookkeeping in a cache folder: its top-level dot
    # entries (.in_use/<pid>, .orphaned_at, and whatever a later release
    # adds). The plugin ships none besides .claude-plugin
    # (tests/test_plugin_packaging.py), so skipping them hides no change.
    (b / ".in_use").mkdir()
    (b / ".in_use" / "42652").write_text('{"pid":42652}', encoding="utf-8")
    (b / ".orphaned_at").write_text("1789969837541", encoding="utf-8")
    assert uc.tree_differs(a, b) is False
    (b / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    assert uc.tree_differs(a, b) is True
    shutil.copyfile(a / ".claude-plugin" / "plugin.json", b / ".claude-plugin" / "plugin.json")
    (b / "commands" / "dream.md").write_text("changed", encoding="utf-8")
    assert uc.tree_differs(a, b) is True


# ── Codex hooks ─────────────────────────────────────────────────────────────

def test_codex_hooks_bundle_presence_does_not_claim_trust_or_execution(cli, tmp_path):
    hooks_root = cli.home / "codex" / "pseudolife" / "hooks"
    # An empty matching directory proves only presence, not active hook definitions.
    (hooks_root / uc.codex_bundle_digest(ROOT)).mkdir(parents=True)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "bundle-present"
    assert uc._marker(result["state"]) == "[-]"
    assert "trust and execution not checked" in result["detail"]
    assert "rerun setup to verify with consent" in result["detail"]
    assert "python ops/setup-codex-hooks.py --source manual --trust ask" in result["detail"]


@pytest.mark.parametrize("json_output", [False, True])
def test_manual_bundle_presence_always_prints_setup_verification_command(cli, capsys, json_output):
    hooks_root = cli.home / "codex" / "pseudolife" / "hooks"
    (hooks_root / uc.codex_bundle_digest(ROOT)).mkdir(parents=True)
    args = ["--repo", str(ROOT), "--only", "codex"]
    assert uc.main(args + (["--json"] if json_output else [])) == 0
    output = capsys.readouterr().out
    if json_output:
        result = json.loads(output)["codex"]
        assert result["state"] == "bundle-present"
        detail = result["detail"]
    else:
        assert "[-] Codex hooks" in output and "[x]" not in output
        detail = output
    assert "trust and execution not checked" in detail
    assert "python ops/setup-codex-hooks.py --source manual --trust ask" in detail


def test_codex_hooks_stale_names_the_setup_command(cli, tmp_path):
    hooks_root = cli.home / "codex" / "pseudolife" / "hooks"
    (hooks_root / ("0" * 20)).mkdir(parents=True)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "stale"
    assert "setup-codex-hooks.py" in result["detail"]


def test_codex_hooks_not_configured_without_a_hooks_root(cli):
    assert uc.check_codex_hooks(ROOT)["state"] == "not-configured"


def _plugin_handler_keys():
    """Codex's approval key for each handler position in the checkout's
    hooks.json (as written to ~/.codex/config.toml, checked 2026-09-25)."""
    manifest = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))
    event_keys = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
                  "SessionEnd": "session_end", "Stop": "stop", "SubagentStop": "subagent_stop",
                  "PreToolUse": "pre_tool_use", "SubagentStart": "subagent_start"}
    return [f"pseudolife-memory@pseudolife-mcp:hooks/hooks.json:{event_keys[event]}:{g}:{h}"
            for event, groups in manifest["hooks"].items()
            for g, group in enumerate(groups) for h, _ in enumerate(group["hooks"])]


def _codex_plugin_config(cli, approved=()):
    codex_home = cli.home / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    text = '[plugins."pseudolife-memory@pseudolife-mcp"]\nenabled = true\n'
    for key in approved:
        text += f'\n[hooks.state."{key}"]\ntrusted_hash = "sha256:{"0" * 64}"\n'
    (codex_home / "config.toml").write_text(text, encoding="utf-8")
    return codex_home


def test_codex_plugin_hooks_current_when_its_clone_matches_the_checkout(cli):
    """The maintainer's Codex runs the plugin, not manual copies; its
    marketplace clone is what to compare (found live, 2026-09-21)."""
    codex_home = _codex_plugin_config(cli, _plugin_handler_keys())
    clone = codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin"
    shutil.copytree(ROOT / "plugin", clone)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "current" and "plugin" in result["detail"]


def test_codex_plugin_hooks_report_unapproved_handlers(cli):
    """Codex runs only approved handlers and skips new ones without a word in
    the desktop app. The 2026-09-24 split added two handler positions, so a
    Codex home approved before it silently lost the mail previews."""
    keys = _plugin_handler_keys()
    legacy = [k for k in keys if k.endswith(":0:0")]
    assert len(legacy) < len(keys)
    codex_home = _codex_plugin_config(cli, legacy)
    shutil.copytree(ROOT / "plugin", codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin")
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "needs-approval"
    assert "setup-codex-hooks.py --source plugin --trust ask" in result["detail"]
    assert uc._marker(result["state"]) == "[!]"


def test_codex_plugin_hooks_respect_a_handler_the_user_disabled(cli):
    keys = _plugin_handler_keys()
    codex_home = _codex_plugin_config(cli, [k for k in keys if ":stop:" not in k])
    with open(codex_home / "config.toml", "a", encoding="utf-8") as config:
        for key in (k for k in keys if ":stop:" in k):
            config.write(f'\n[hooks.state."{key}"]\nenabled = false\n')
    shutil.copytree(ROOT / "plugin", codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin")
    assert uc.check_codex_hooks(ROOT)["state"] == "current"


def test_codex_plugin_hooks_behind_or_stale_when_its_clone_differs(cli):
    """A clone with older scripts is only behind (Codex approves hooks.json's
    definitions, not the scripts); an older hooks.json needs approval."""
    codex_home = _codex_plugin_config(cli)
    clone = codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin"
    shutil.copytree(ROOT / "plugin", clone)
    hook = clone / "hooks" / "session-end.sh"
    hook.write_bytes(hook.read_bytes() + b"\n# older\n")
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "behind" and "plugin manager" in result["detail"]
    manifest = clone / "hooks" / "hooks.json"
    manifest.write_bytes(manifest.read_bytes().replace(b'"timeout": 5', b'"timeout": 6', 1))
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "stale" and "setup-codex-hooks.py" in result["detail"]


def test_codex_plugin_hooks_without_a_clone_are_reported_as_plugin_managed(cli):
    _codex_plugin_config(cli)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "plugin-managed" and "plugin manager" in result["detail"]


# ── the command ─────────────────────────────────────────────────────────────

def test_main_prints_a_ladder_and_fails_only_on_a_failed_step(cli, tmp_path, capsys):
    _plugin_fixture(cli, tmp_path, differ=False)
    _register_claude(cli, str(_layout(cli).launcher))
    assert uc.main(["--repo", str(ROOT)]) == 0
    out = capsys.readouterr().out
    assert "[x] Shim" in out and "[x] Plugin" in out and "Codex hooks" in out
    cli.pip_no_deps = (1, "boom")
    assert uc.main(["--repo", str(ROOT)]) == 1
    assert "[!] Shim" in capsys.readouterr().out


def test_main_only_runs_the_selected_steps(cli, tmp_path, capsys):
    _plugin_fixture(cli, tmp_path, differ=False)
    assert uc.main(["--repo", str(ROOT), "--only", "plugin"]) == 0
    out = capsys.readouterr().out
    assert "Plugin" in out and "Shim" not in out
    assert not _install_calls(cli) and not (cli.home / "runtimes").exists()


def test_main_json_output_is_machine_readable(cli, tmp_path, capsys):
    _plugin_fixture(cli, tmp_path, differ=False)
    assert uc.main(["--repo", str(ROOT), "--only", "plugin", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plugin"]["state"] == "current:0.15.0" and report["ok"] is True


def test_an_autostart_task_that_still_carries_the_model_is_named_after_the_ladder(cli, capsys):
    """An extractor autostart task or unit registered before
    ops/shim_autostart.py keeps starting the model on its own command line
    at logon, whatever ops/.env says; no update said so. The note is
    informational: it never fails the run, and an unregistered kind (no
    CLI extractor shim here) says nothing."""
    note = ("the scheduled task 'Pseudolife Claude Shim' still carries the model and the rest on its command "
            "line: at logon it starts those, not ops/.env. Run ops/install-shim-autostart.ps1 once (elevated)")
    cli.autostart["registered"] = {"claude": "old command line --model old-model"}
    cli.autostart["notes"] = {"claude": note, "codex": "the unit is not registered"}
    report = uc.run_steps((), repo=ROOT, source="unused")
    assert report["ok"] is True
    assert report["autostart"]["state"] == "stale" and report["autostart"]["notes"] == [f"claude: {note}"]
    uc.print_ladder(report)
    assert f"[!] Extractor autostart" in capsys.readouterr().out
    # registered and running the runner: current; nothing registered: no line at all
    cli.autostart["notes"] = {}
    assert uc.run_steps((), repo=ROOT, source="unused")["autostart"]["state"] == "current"
    cli.autostart["registered"] = {}
    report = uc.run_steps((), repo=ROOT, source="unused")
    assert report["autostart"]["state"] == "none"
    uc.print_ladder(report)
    assert "autostart" not in capsys.readouterr().out
    # no checkout known: no autostart step
    assert "autostart" not in uc.run_steps((), repo=None, source="unused")


def test_an_unreadable_autostart_registration_never_fails_the_run(cli, monkeypatch):
    def broken(repo):
        raise OSError("no ops/shim_autostart.py in this checkout")

    monkeypatch.setattr(uc, "_autostart_module", broken)
    report = uc.run_steps((), repo=ROOT, source="unused")
    assert report["ok"] is True and report["autostart"]["state"] == "unknown"
    assert "no ops/shim_autostart.py" in report["autostart"]["detail"]


def test_helper_runs_as_a_script():
    proc = subprocess.run([sys.executable, str(ROOT / "ops/update_clients.py"), "--help"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "--only" in proc.stdout


def test_helper_does_not_import_the_installed_package(tmp_path):
    """Run from outside the checkout with an isolated interpreter: the
    installed ``pseudolife_memory`` may be an older release without
    ``plugin_hooks`` (the live run on 2026-09-21 died exactly there), so
    the helper must load what it needs from the checkout by path."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    # The plugin-managed branch is the one that digests the checkout's hooks.
    (codex_home / "config.toml").write_text(
        '[plugins."pseudolife-memory@pseudolife-mcp"]\nenabled = true\n', encoding="utf-8")
    env["CODEX_HOME"] = str(codex_home)
    env["USERPROFILE"] = env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "ops/update_clients.py"),
                           "--only", "codex", "--repo", str(ROOT)],
                          capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env=env)
    assert proc.returncode == 0, proc.stderr
    assert "Codex hooks" in proc.stdout and "plugin-managed" in proc.stdout


def test_update_scripts_offer_all_and_hand_it_to_the_python_deploy():
    """The wrappers map -All / --all onto the one Python deploy
    (pseudolife_memory/update_cli.py via ops/update.py), which runs this
    module's steps after the daemon is healthy."""
    sh = (ROOT / "ops/update.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "ops/update.ps1").read_text(encoding="utf-8")
    assert "--all)" in sh and "args+=(--all)" in sh and 'ops/update.py' in sh
    assert "[switch]$All" in ps1 and '$updateArgs += "--all"' in ps1 and '"update.py"' in ps1
    deploy = (ROOT / "pseudolife_memory/update_cli.py").read_text(encoding="utf-8")
    assert deploy.index("client_updates.run_steps") > deploy.index("def wait_health")


@pytest.mark.parametrize("state", ['"not a table"', "[1, 2]"])
def test_codex_plugin_approval_check_survives_a_malformed_config(cli, state):
    """A report that crashes on one odd config value loses the whole ladder."""
    codex_home = _codex_plugin_config(cli)
    with open(codex_home / "config.toml", "a", encoding="utf-8") as config:
        config.write(f"\n[hooks]\nstate = {state}\n")
    shutil.copytree(ROOT / "plugin", codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin")
    assert uc.check_codex_hooks(ROOT)["state"] in ("current", "needs-approval")
