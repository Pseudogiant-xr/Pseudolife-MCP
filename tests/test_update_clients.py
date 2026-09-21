"""One command moves the client side with the daemon: ops/update_clients.py.

`ops/update.*` rebuilds the daemon and nothing else; the shim, the Claude
Code plugin cache and Codex's hook copies each needed their own step, and
each was forgotten in turn (2026-09-21). `--all` on the update scripts now
runs this helper after the daemon is healthy. It reinstalls the shim behind
the registered command where that is safe, refreshes the plugin cache by
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

PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
EXE = ".exe" if os.name == "nt" else ""


class FakeCli:
    """Answers ``run_cli`` from a table and records every call."""

    def __init__(self, home: Path):
        self.home = home
        self.calls: list[list[str]] = []
        self.claude_get = (1, "No MCP server found with name: pseudolife-memory")
        self.codex_get = (1, "")
        self.pipx_list = (1, "")
        self.pipx_install = (0, "installed")
        self.pip_install = (0, "Successfully installed")
        self.marketplace_update = (0, "updated")
        self.install_help = (0, "  -y, --yes   Accept")
        self.install_records = True
        self.clone_plugin: Path | None = None
        self.cache_plugin: Path | None = None
        self.user_scripts: Path | None = None  # what a fake python reports as its --user scripts dir
        self.run_kwargs: list[dict] = []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.run_kwargs.append(kw)
        name = Path(argv[0]).name.lower().removesuffix(".exe")
        rest = argv[1:]
        if name.startswith("python") and rest[:1] == ["-c"] and "sysconfig" in rest[1]:
            return (0, str(self.user_scripts)) if self.user_scripts else (1, "no user scheme")
        if name == "claude" and rest[:3] == ["mcp", "get", "pseudolife-memory"]:
            return self.claude_get
        if name == "codex" and rest[:3] == ["mcp", "get", "pseudolife-memory"]:
            return self.codex_get
        if name == "pipx" and rest[:2] == ["list", "--json"]:
            return self.pipx_list
        if name == "pipx" and rest[:2] == ["install", "--force"]:
            return self.pipx_install
        if rest[:3] == ["-m", "pip", "install"]:
            return self.pip_install
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
    tools = {"claude": str(tmp_path / f"claude{EXE}"), "codex": str(tmp_path / f"codex{EXE}"),
             "pipx": str(tmp_path / f"pipx{EXE}")}
    fake.tools = tools
    monkeypatch.setattr(uc, "which", lambda name: tools.get(name))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    return fake


# ── the shim ────────────────────────────────────────────────────────────────

def _claude_stdio(command: str) -> str:
    return (f"pseudolife-memory:\n  Scope: User config\n  Status: ✔ Connected\n  Type: stdio\n"
            f"  Command: {command}\n  Args:\n  Environment:\n    PSEUDOLIFE_MCP_NO_SPAWN=1\n")


def _codex_stdio(command: str, args: str) -> str:
    return (f"pseudolife-memory\n  enabled: true\n  transport: stdio\n  command: {command}\n"
            f"  args: {args}\n  cwd: {Path(command).parent.parent}\n")


def test_editable_checkout_shim_is_reported_not_reinstalled(cli, tmp_path):
    """The code is live from the checkout; the metadata refresh needs every
    session closed (the launcher is in use), so it is named, not run."""
    launcher = ROOT / ".venv" / "Scripts" / f"pseudolife-mcp{EXE}"
    cli.claude_get = (0, _claude_stdio(str(launcher)))
    result = uc.update_shim(ROOT)
    assert result["state"] == "editable"
    assert "pip install -e" in result["detail"]
    assert not any(c[1:3] == ["-m", "pip"] or c[1:2] == ["install"] for c in cli.calls)


def test_pipx_managed_shim_is_reinstalled_from_the_checkout(cli, tmp_path):
    cli.claude_get = (0, _claude_stdio(str(tmp_path / "pipx-venv" / "bin" / "pseudolife-mcp")))
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {}}}))
    result = uc.update_shim(ROOT)
    assert result["state"] == "reinstalled:pipx"
    assert [cli.tools["pipx"], "install", "--force", str(ROOT)] in cli.calls


def test_runtime_venv_shim_is_upgraded_through_its_own_interpreter(cli, tmp_path):
    """Codex registers `<runtime>/Scripts/python -m pseudolife_memory.cli`;
    that interpreter's pip upgrades the package in place."""
    python = tmp_path / "runtime" / "Scripts" / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    result = uc.update_shim(ROOT)
    assert result["state"] == "reinstalled:pip"
    assert [str(python), "-m", "pip", "install", "--upgrade", str(ROOT)] in cli.calls


def test_launcher_beside_an_interpreter_is_upgraded_through_that_interpreter(cli, tmp_path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    result = uc.update_shim(ROOT)
    assert result["state"] == "reinstalled:pip"
    assert [str(scripts / f"python{EXE}"), "-m", "pip", "install", "--upgrade", str(ROOT)] in cli.calls


def test_shim_upgrade_failure_is_reported_with_the_command(cli, tmp_path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    cli.pip_install = (1, "ERROR: [WinError 32] The process cannot access the file")
    result = uc.update_shim(ROOT)
    assert result["state"] == "failed"
    assert "pip install --upgrade" in result["detail"]
    assert "close" in result["detail"].lower()


def test_custom_registration_is_left_alone_and_named(cli, tmp_path):
    cli.claude_get = (0, "transport: stdio\ncommand: /opt/custom/bridge\nargs: -m private_bridge\n")
    result = uc.update_shim(ROOT)
    assert result["state"] == "unmanaged"
    assert "/opt/custom/bridge" in result["detail"]
    # Lookups only (registrations, pipx inventory): nothing installed.
    assert not any(c[1:3] == ["-m", "pip"] or c[1:2] == ["install"] for c in cli.calls)


def test_two_registrations_of_one_shim_upgrade_it_once(cli, tmp_path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    launcher = scripts / f"pseudolife-mcp{EXE}"
    cli.claude_get = (0, _claude_stdio(str(launcher)))
    cli.codex_get = (0, _codex_stdio(str(scripts / f"python{EXE}"), "-m pseudolife_memory.cli"))
    result = uc.update_shim(ROOT)
    assert result["state"] == "reinstalled:pip"
    assert sum(1 for c in cli.calls if c[1:3] == ["-m", "pip"]) == 1


def test_no_registration_anywhere_is_reported(cli):
    assert uc.update_shim(ROOT)["state"] == "not-registered"


def test_pip_user_launcher_is_upgraded_through_its_owning_interpreter(cli, tmp_path):
    """The installers' non-pipx path is `pip install --user`: the launcher
    lands in the user scripts directory with no interpreter beside it
    (reviewer finding, 2026-09-21). The interpreter whose user scheme owns
    that directory is the one to upgrade through."""
    scripts = tmp_path / "AppData" / "Roaming" / "Python" / "Python312" / "Scripts"
    scripts.mkdir(parents=True)
    cli.tools["python"] = str(tmp_path / f"python{EXE}")
    cli.user_scripts = scripts
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    result = uc.update_shim(ROOT)
    assert result["state"] == "reinstalled:pip-user"
    assert [cli.tools["python"], "-m", "pip", "install", "--user", "--upgrade", str(ROOT)] in cli.calls


def test_a_same_named_venv_elsewhere_does_not_claim_the_launcher(cli, tmp_path):
    """A pipx venv named pseudolife-mcp elsewhere must not make the helper
    `pipx install --force` over an unrelated registration. (The test's name
    stays clear of the word pipx: it names tmp_path, and a pipx tree is
    recognised by that substring.)"""
    scripts = tmp_path / "somewhere" / "bin"
    scripts.mkdir(parents=True)
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(tmp_path / "pipx" / "venvs" / "pseudolife-mcp" / "bin" / "pseudolife-mcp")}]}}}}}))
    result = uc.update_shim(ROOT)
    assert result["state"] == "unmanaged"
    assert not any(c[1:3] == ["install", "--force"] for c in cli.calls)
    # The same listing does claim a launcher it names.
    owned = tmp_path / "pipx" / "venvs" / "pseudolife-mcp" / "bin" / f"pseudolife-mcp{EXE}"
    owned.parent.mkdir(parents=True)
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(owned)}]}}}}}))
    cli.claude_get = (0, _claude_stdio(str(owned)))
    assert uc.update_shim(ROOT)["state"] == "reinstalled:pipx"


def test_versioned_python_registration_is_recognised(cli, tmp_path):
    python = tmp_path / "rt" / "bin" / "python3.12"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    assert uc.update_shim(ROOT)["state"] == "reinstalled:pip"


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

def test_codex_hooks_current_when_the_checkout_digest_dir_exists(cli, tmp_path):
    hooks_root = cli.home / "codex" / "pseudolife" / "hooks"
    (hooks_root / uc.codex_bundle_digest(ROOT)).mkdir(parents=True)
    assert uc.check_codex_hooks(ROOT)["state"] == "current"


def test_codex_hooks_stale_names_the_setup_command(cli, tmp_path):
    hooks_root = cli.home / "codex" / "pseudolife" / "hooks"
    (hooks_root / ("0" * 20)).mkdir(parents=True)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "stale"
    assert "setup-codex-hooks.py" in result["detail"]


def test_codex_hooks_not_configured_without_a_hooks_root(cli):
    assert uc.check_codex_hooks(ROOT)["state"] == "not-configured"


def _codex_plugin_config(cli):
    codex_home = cli.home / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        '[plugins."pseudolife-memory@pseudolife-mcp"]\nenabled = true\n', encoding="utf-8")
    return codex_home


def test_codex_plugin_hooks_current_when_its_clone_matches_the_checkout(cli):
    """The maintainer's Codex runs the plugin, not manual copies; its
    marketplace clone is what to compare (found live, 2026-09-21)."""
    codex_home = _codex_plugin_config(cli)
    clone = codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin"
    shutil.copytree(ROOT / "plugin", clone)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "current" and "plugin" in result["detail"]


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
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    assert uc.main(["--repo", str(ROOT)]) == 0
    out = capsys.readouterr().out
    assert "[x] Shim" in out and "[x] Plugin" in out and "Codex hooks" in out
    cli.pip_install = (1, "boom")
    assert uc.main(["--repo", str(ROOT)]) == 1
    assert "[!] Shim" in capsys.readouterr().out


def test_main_only_runs_the_selected_steps(cli, tmp_path, capsys):
    _plugin_fixture(cli, tmp_path, differ=False)
    assert uc.main(["--repo", str(ROOT), "--only", "plugin"]) == 0
    out = capsys.readouterr().out
    assert "Plugin" in out and "Shim" not in out
    assert not any(c[1:3] == ["mcp", "get"] for c in cli.calls)


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
