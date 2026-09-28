"""One command moves the client side with the daemon: ops/update_clients.py.

`ops/update.*` rebuilds the daemon and nothing else; the shim, the Claude
Code plugin cache and Codex's hook copies each needed their own step, and
each was forgotten in turn (2026-09-21). `--all` on the update scripts now
runs this helper after the daemon is healthy. It installs a new shim
runtime beside the old one and moves registrations to the launcher path
(pseudolife_memory/runtimes.py; never refusing for a running session),
refreshes the plugin cache by
comparing bytes against the marketplace clone (the version string cannot
move between releases), and reports whether Codex's content-addressed hook
copy matches the checkout. Every CLI call goes through ``run_cli`` and every
lookup through ``which``/``home``, so these tests drive the real logic with
a fake CLI and a fixture home and never touch this machine's registrations.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("update_clients", ROOT / "ops/update_clients.py")
uc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uc)
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
        self.install_help = (0, "  -y, --yes   Accept")
        self.install_records = True
        self.clone_plugin: Path | None = None
        self.cache_plugin: Path | None = None
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
        if name.startswith("python") and rest[:1] == ["-c"] and "importlib.metadata" in rest[1]:
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
        if name == "claude" and rest[:3] == ["plugin", "install", "--help"]:
            return self.install_help
        if name == "claude" and rest[:2] == ["plugin", "uninstall"]:
            _record_plugin(self.home, None)
            return 0, "uninstalled"
        if name == "claude" and rest[:2] == ["plugin", "install"]:
            if self.install_records and self.clone_plugin and self.cache_plugin:
                shutil.rmtree(self.cache_plugin, ignore_errors=True)
                shutil.copytree(self.clone_plugin, self.cache_plugin)
                _record_plugin(self.home, self.cache_plugin)
            return 0, "installed"
        return 91, f"unexpected call {argv}"


def _record_plugin(home: Path, cache: Path | None, version: str = "0.15.0") -> None:
    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    data = {"version": 2, "plugins": {}}
    if cache is not None:
        data["plugins"][PLUGIN_ID] = [{"scope": "user", "installPath": str(cache), "version": version}]
    (plugins / "installed_plugins.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _record_marketplace(home: Path, clone_root: Path) -> None:
    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "known_marketplaces.json").write_text(json.dumps({
        "pseudolife-mcp": {"source": {"source": "github", "repo": "Pseudogiant-xr/Pseudolife-MCP"},
                           "installLocation": str(clone_root)},
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
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(uc, "_RUNTIMES_MODULE", None, raising=False)
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
    assert launcher.is_file()   # nothing of the old environment is touched


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

# ── the plugin cache ────────────────────────────────────────────────────────

def _plugin_fixture(cli, tmp_path, *, differ: bool, installed: bool = True):
    clone_root = tmp_path / "marketplace"
    clone = _plugin_tree(clone_root)
    cache = tmp_path / "cache" / "0.15.0"
    shutil.copytree(ROOT / "plugin", cache)
    if differ:
        hook = cache / "hooks" / "session-end.sh"
        hook.write_bytes(hook.read_bytes() + b"\n# stale cache\n")
    _record_marketplace(cli.home, clone_root)
    _record_plugin(cli.home, cache if installed else None)
    cli.clone_plugin, cli.cache_plugin = clone, cache
    return clone, cache


def test_plugin_cache_is_refreshed_when_its_bytes_differ_from_the_clone(cli, tmp_path):
    clone, cache = _plugin_fixture(cli, tmp_path, differ=True)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "refreshed:0.15.0"
    assert result["marketplace_update"] == "ok"
    acted = [c[1:] for c in cli.calls if Path(c[0]).name.lower().startswith("claude")]
    assert ["plugin", "marketplace", "update", "pseudolife-mcp"] in acted
    assert ["plugin", "uninstall", PLUGIN_ID] in acted
    assert ["plugin", "install", "--yes", PLUGIN_ID] in acted
    assert uc.tree_differs(clone, cache) is False


def test_plugin_cache_that_matches_the_clone_is_left_alone(cli, tmp_path):
    _plugin_fixture(cli, tmp_path, differ=False)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "current:0.15.0"
    assert not any("uninstall" in c or "install" in c[1:2] for c in cli.calls)


def test_plugin_refresh_survives_a_failed_marketplace_update(cli, tmp_path):
    """Offline: the clone is what it is; the comparison still runs against
    it and the failure is on the ladder, not fatal."""
    _plugin_fixture(cli, tmp_path, differ=True)
    cli.marketplace_update = (1, "fatal: unable to access")
    result = uc.update_plugin(ROOT)
    assert result["marketplace_update"] == "failed"
    assert result["state"] == "refreshed:0.15.0"


def test_plugin_refresh_that_leaves_the_cache_different_is_a_failure(cli, tmp_path):
    _plugin_fixture(cli, tmp_path, differ=True)
    cli.install_records = False
    result = uc.update_plugin(ROOT)
    assert result["state"] == "failed"
    assert "plugin install" in result["detail"]


def test_plugin_not_installed_points_at_the_installer(cli, tmp_path):
    _plugin_fixture(cli, tmp_path, differ=False, installed=False)
    result = uc.update_plugin(ROOT)
    assert result["state"] == "not-installed"
    assert "install" in result["detail"]


def test_tree_differs_ignores_line_endings_and_git_metadata(tmp_path):
    a = _plugin_tree(tmp_path / "a")
    b = _plugin_tree(tmp_path / "b")
    hook = b / "hooks" / "session-start.sh"
    hook.write_bytes(hook.read_bytes().replace(b"\n", b"\r\n"))
    (b / ".git").mkdir()
    (b / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    assert uc.tree_differs(a, b) is False
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
                  "SessionEnd": "session_end", "Stop": "stop"}
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


def test_codex_plugin_hooks_stale_when_its_clone_differs(cli):
    codex_home = _codex_plugin_config(cli)
    clone = codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin"
    shutil.copytree(ROOT / "plugin", clone)
    hook = clone / "hooks" / "session-end.sh"
    hook.write_bytes(hook.read_bytes() + b"\n# older\n")
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


def test_update_scripts_offer_all_and_call_the_helper_after_health():
    sh = (ROOT / "ops/update.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "ops/update.ps1").read_text(encoding="utf-8")
    assert "--all)" in sh and "update_clients.py" in sh
    assert "[switch]$All" in ps1 and "update_clients.py" in ps1
    # The call site (the last mention; the usage header mentions it first).
    assert sh.rindex("update_clients.py") > sh.index('step "Healthy."')
    assert ps1.rindex("update_clients.py") > ps1.index('Step "Healthy.')


@pytest.mark.parametrize("state", ['"not a table"', "[1, 2]"])
def test_codex_plugin_approval_check_survives_a_malformed_config(cli, state):
    """A report that crashes on one odd config value loses the whole ladder."""
    codex_home = _codex_plugin_config(cli)
    with open(codex_home / "config.toml", "a", encoding="utf-8") as config:
        config.write(f"\n[hooks]\nstate = {state}\n")
    shutil.copytree(ROOT / "plugin", codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin")
    assert uc.check_codex_hooks(ROOT)["state"] in ("current", "needs-approval")
