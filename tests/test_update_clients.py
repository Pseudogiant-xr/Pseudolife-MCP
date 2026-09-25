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
REAL_RUN_CLI = uc.run_cli                                   # the fixture fakes both
REAL_LIST_PROCESSES = getattr(uc, "list_processes", None)

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
        self.pip_effect = None   # called with argv before pip's answer, to act on the fake runtime
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
        if name == "claude" and rest[:3] == ["mcp", "get", "pseudolife-memory"]:
            return self.claude_get
        if name == "codex" and rest[:3] == ["mcp", "get", "pseudolife-memory"]:
            return self.codex_get
        if name == "pipx" and rest[:2] == ["list", "--json"]:
            return self.pipx_list
        if name == "pipx" and rest[:2] == ["install", "--force"]:
            return self.pipx_install
        if rest[:3] == ["-m", "pip", "install"]:
            if self.pip_effect:
                self.pip_effect(argv)
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
    monkeypatch.setattr(uc, "_install_kinds", {})  # one probe memo per test, as per run
    tools = {"claude": str(tmp_path / f"claude{EXE}"), "codex": str(tmp_path / f"codex{EXE}"),
             "pipx": str(tmp_path / f"pipx{EXE}")}
    fake.tools = tools
    monkeypatch.setattr(uc, "which", lambda name: tools.get(name))
    # This machine's process table is not the test's: nothing runs from the
    # fixture runtimes unless a test says so.
    monkeypatch.setattr(uc, "list_processes", lambda: fake.processes, raising=False)
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


def test_an_editable_install_anywhere_is_named_never_pip_upgraded(cli, tmp_path):
    """The first real -All run (2026-09-21) pip-upgraded the maintainer's
    checkout venv because --repo pointed at a deploy worktree, so the
    launcher was not under <repo>/.venv. Editable-ness is asked of the
    interpreter, not inferred from a path."""
    scripts = tmp_path / "elsewhere" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / f"python{EXE}"
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "editable"
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    result = uc.update_shim(tmp_path / "some-deploy-worktree")
    assert result["state"] == "editable"
    assert "never pip-upgraded" in result["detail"] and "pip install -e" in result["detail"]
    assert not any(c[1:3] == ["-m", "pip"] for c in cli.calls)


def test_an_editable_codex_runtime_is_named_too(cli, tmp_path):
    python = tmp_path / "rt" / "Scripts" / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "editable"
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    assert uc.update_shim(ROOT)["state"] == "editable"
    assert not any(c[1:3] == ["-m", "pip"] for c in cli.calls)


def test_a_missing_package_in_a_venv_is_installed_fresh(cli, tmp_path):
    python = tmp_path / "rt" / "Scripts" / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "missing"
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    assert uc.update_shim(ROOT)["state"] == "reinstalled:pip"


def test_a_noisy_probe_answer_still_reads_as_editable(cli, tmp_path):
    """``run_cli`` appends stderr after stdout, so a warning emitted by a
    .pth or sitecustomize lands after the JSON line. Taking the last line
    made that parse fail and fall through to the pip upgrade the guard
    exists to prevent (reviewer finding, 2026-09-21)."""
    python = tmp_path / "rt" / "Scripts" / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "noisy-editable"
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    assert uc.install_kind(python)[0] == "editable"
    assert uc.update_shim(ROOT)["state"] == "editable"
    assert not any(c[1:3] == ["-m", "pip"] for c in cli.calls)


def test_a_probe_that_did_not_answer_is_left_alone_not_pip_upgraded(cli, tmp_path):
    """"The probe failed" and "the package is absent" are different facts;
    only the second licenses a pip install. An interpreter the helper
    cannot classify is named for the maintainer, never upgraded."""
    python = tmp_path / "rt" / "Scripts" / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    cli.install_kinds[str(python).lower()] = "crashed"
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    kind, reason = uc.install_kind(python)
    assert kind == "unknown" and "RuntimeError: boom" in reason
    result = uc.update_shim(ROOT)
    assert result["state"] == "unknown"
    assert str(python) in result["detail"] and "RuntimeError: boom" in result["detail"]
    assert not any(c[1:3] == ["-m", "pip"] for c in cli.calls)


def test_the_install_kind_probe_runs_under_a_real_interpreter():
    """The fakes hand-copy the probe's output contract; this pins the
    probe itself, so a typo or a scheme lookup that raises on some
    platform cannot ship green and degrade live to "unknown"."""
    proc = subprocess.run([sys.executable, "-c", uc._INSTALL_KIND_PROBE], capture_output=True,
                          text=True, timeout=60, cwd=str(Path(uc.tempfile.gettempdir())))
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    report = json.loads(lines[0])
    # The interpreter's own purelib comes first and exists; the user-site
    # entry that follows is reported even when nothing was ever installed there.
    assert report["libs"] and Path(report["libs"][0]).is_dir()
    assert all(Path(lib).is_absolute() for lib in report["libs"])
    # The suite imports pseudolife_memory, so this interpreter has it: the
    # answer is a real classification, never the fall-through.
    kind, detail = uc.install_kind(Path(sys.executable))
    assert kind in ("editable", "site"), (kind, detail)


def test_install_kind_probe_runs_from_a_neutral_directory(cli, tmp_path):
    """`python -c` puts the working directory first on sys.path; run from
    the checkout, every interpreter would import the checkout's package
    and read as editable (seen live, 2026-09-21)."""
    python = tmp_path / "rt" / "Scripts" / f"python{EXE}"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    uc.install_kind(python)
    probe_kwargs = [kw for call, kw in zip(cli.calls, cli.run_kwargs) if "package_dir" in " ".join(call)]
    assert probe_kwargs and probe_kwargs[0].get("cwd")
    assert not Path(probe_kwargs[0]["cwd"]).resolve().is_relative_to(ROOT.resolve())


def test_failure_detail_quotes_pips_error_line_not_its_notice(cli, tmp_path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / f"python{EXE}").write_text("", encoding="utf-8")
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    cli.pip_install = (1, "Collecting x\nWARNING: Error parsing dependencies of some-pkg: bad version\n"
                          "ERROR: Could not install packages due to an OSError: "
                          "[WinError 32] in use\n\n[notice] A new release of pip is available\n"
                          "[notice] To update, run: pip install --upgrade pip")
    result = uc.update_shim(ROOT)
    assert result["state"] == "failed"
    assert "WinError 32" in result["detail"] and "[notice]" not in result["detail"]
    assert "Error parsing" not in result["detail"]


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


# ── a runtime in use is never left without its package ──────────────────────
#
# 2026-09-25 20:27: `update.ps1 -All` ran `<runtime>\Scripts\python -m pip
# install --upgrade <checkout>` while ~36 sessions ran that runtime's
# launcher. pip 24.0 stashes what it uninstalls in sorted order, so it renamed
# Lib\site-packages\pseudolife_memory and its dist-info to `~`-prefixed
# siblings, then raised WinError 32 moving the running Scripts\pseudolife-mcp.exe.
# That raise is inside uninstall(), outside the try that rolls a failed
# install back, so nothing was put back: the runtime had no package of its
# own and imports fell through to another copy on sys.path.

DIST_INFO = "pseudolife_mcp-0.15.0.dist-info"
WINERROR_32 = ("ERROR: Could not install packages due to an OSError: [WinError 32] The process cannot "
               "access the file because it is being used by another process: "
               "'C:\\rt\\Scripts\\pseudolife-mcp.exe' -> 'C:\\Temp\\pip-uninstall-x\\pseudolife-mcp.exe'")


def _runtime(tmp_path, name: str = "rt") -> Path:
    """A venv-shaped shim runtime with the package installed in it; the fake
    install-kind probe reports ``<root>/Lib/site-packages`` as its library."""
    root = tmp_path / name
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("include-system-site-packages = true\n", encoding="utf-8")
    for exe in ("python", "pseudolife-mcp"):
        (scripts / f"{exe}{EXE}").write_text("", encoding="utf-8")
    site = root / "Lib" / "site-packages"
    (site / "pseudolife_memory").mkdir(parents=True)
    (site / "pseudolife_memory" / "__init__.py").write_text("RELEASE = 'installed'\n", encoding="utf-8")
    (site / DIST_INFO).mkdir()
    (site / DIST_INFO / "METADATA").write_text("Name: pseudolife-mcp\n", encoding="utf-8")
    return root


def _site(root: Path) -> Path:
    return root / "Lib" / "site-packages"


def _pip24_stash_then_fail(site: Path):
    """What pip 24.0 did on 2026-09-25: rename each wholly-removed directory
    to the first free `~` name (AdjacentTempDirectory), in sorted order, then
    fail on the running launcher before anything is rolled back."""
    def effect(argv):
        for name in sorted([DIST_INFO, "pseudolife_memory"]):
            stash = "~" + name[1:]
            if (site / stash).exists():
                stash = "~-" + name[2:]
            os.rename(site / name, site / stash)
    return effect


def _sessions(root: Path, claude: int = 0, codex: int = 0, base: str = "C:/Python311/python.exe"):
    """Process rows for sessions running from ``root``: a Claude session is
    launcher -> venv redirector -> base interpreter; Codex registers
    ``<root>/Scripts/python -m ...``, so its tree starts at the redirector."""
    rows, pid = [], 1000
    for i in range(claude + codex):
        client = 50 + i
        if i < claude:
            rows.append((pid, client, str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
            parent, pid = pid, pid + 1
        else:
            parent = client
        rows.append((pid, parent, str(root / "Scripts" / f"python{EXE}")))
        rows.append((pid + 1, pid, base))
        pid += 2
    return rows


def _pip_calls(cli):
    return [c for c in cli.calls if c[1:3] == ["-m", "pip"]]


def test_a_runtime_that_sessions_are_running_is_not_pip_upgraded(cli, tmp_path):
    root = _runtime(tmp_path)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    cli.processes = _sessions(root, claude=2, codex=1) + [(7, 1, "C:/Windows/explorer.exe")]
    result = uc.update_shim(ROOT)
    assert result["state"] == "in-use"
    assert not _pip_calls(cli)
    # One count per session tree, not per process (a Claude session is a pair).
    assert "3 sessions" in result["detail"]
    assert "--only shim" in result["detail"]
    assert (_site(root) / "pseudolife_memory" / "__init__.py").is_file()
    assert uc._marker(result["state"]) == "[!]"


def test_a_pipx_shim_in_use_is_not_force_reinstalled(cli, tmp_path):
    """`pipx install --force` removes the venv with rmtree(ignore_errors=True)
    before rebuilding it: with the launcher running, site-packages is deleted
    outright and there is no stash to put back, so only the check before it
    protects a pipx runtime."""
    launcher = tmp_path / "pipx-bin" / f"pseudolife-mcp{EXE}"
    launcher.parent.mkdir()
    cli.claude_get = (0, _claude_stdio(str(launcher)))
    cli.pipx_list = (0, json.dumps({"venvs": {"pseudolife-mcp": {"metadata": {"main_package": {
        "app_paths": [{"__Path__": str(launcher)}]}}}}}))
    cli.processes = [(1000, 50, str(launcher))]
    result = uc.update_shim(ROOT)
    assert result["state"] == "in-use" and "1 session" in result["detail"]
    assert not any(c[1:3] == ["install", "--force"] for c in cli.calls)


def test_a_pip_user_launcher_in_use_is_not_upgraded(cli, tmp_path):
    """The user scripts directory holds other tools' launchers too: only the
    registered launcher itself counts."""
    scripts = tmp_path / "AppData" / "Roaming" / "Python" / "Python312" / "Scripts"
    scripts.mkdir(parents=True)
    cli.tools["python"] = str(tmp_path / f"python{EXE}")
    cli.user_scripts = scripts
    cli.claude_get = (0, _claude_stdio(str(scripts / f"pseudolife-mcp{EXE}")))
    cli.processes = [(1000, 50, str(scripts / f"other-tool{EXE}"))]
    assert uc.update_shim(ROOT)["state"] == "reinstalled:pip-user"
    cli.calls.clear()
    uc._install_kinds.clear()
    cli.processes.append((1001, 51, str(scripts / f"pseudolife-mcp{EXE}")))
    assert uc.update_shim(ROOT)["state"] == "in-use"
    assert not _pip_calls(cli)


def test_processes_elsewhere_and_the_helper_itself_do_not_hold_the_runtime(cli, tmp_path):
    root = _runtime(tmp_path)
    cli.codex_get = (0, _codex_stdio(str(root / "Scripts" / f"python{EXE}"), "-m pseudolife_memory.cli"))
    # A sibling whose name starts with the runtime's is not inside it, and the
    # helper run from the runtime's own interpreter must not block itself.
    cli.processes = (_sessions(tmp_path / "rt-other", claude=1)
                     + [(os.getpid(), os.getppid(), str(root / "Scripts" / f"python{EXE}")),
                        (os.getppid(), 1, str(root / "Scripts" / f"python{EXE}"))])
    assert uc.update_shim(ROOT)["state"] == "reinstalled:pip"
    assert len(_pip_calls(cli)) == 1


def test_a_process_list_that_cannot_be_read_blocks_the_upgrade(cli, tmp_path, monkeypatch):
    """Not knowing who runs the runtime is not "nobody does"."""
    root = _runtime(tmp_path)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))

    def unreadable():
        raise OSError("CreateToolhelp32Snapshot failed")

    monkeypatch.setattr(uc, "list_processes", unreadable)
    result = uc.update_shim(ROOT)
    assert result["state"] == "unknown" and "CreateToolhelp32Snapshot" in result["detail"]
    assert not _pip_calls(cli)


def test_a_platform_without_the_check_upgrades_as_before(cli, tmp_path, monkeypatch):
    """POSIX renames and unlinks open files, so pip cannot strand a runtime
    there by an in-use file; the process list is Windows-only."""
    root = _runtime(tmp_path)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    monkeypatch.setattr(uc, "list_processes", lambda: None)
    assert uc.update_shim(ROOT)["state"] == "reinstalled:pip"


def test_a_pip_that_stashes_then_fails_is_put_back(cli, tmp_path):
    """The safety net behind the check: a session can start between the
    check and pip's stash. What this run moved aside is renamed back."""
    root = _runtime(tmp_path)
    site = _site(root)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    cli.pip_effect = _pip24_stash_then_fail(site)
    cli.pip_install = (1, WINERROR_32)
    result = uc.update_shim(ROOT)
    assert result["state"] == "failed"
    assert (site / "pseudolife_memory" / "__init__.py").read_text(encoding="utf-8") == "RELEASE = 'installed'\n"
    assert (site / DIST_INFO / "METADATA").is_file()
    assert not [p.name for p in site.iterdir() if p.name.startswith("~")]
    assert "restored" in result["detail"] and "WinError 32" in result["detail"]


def test_an_older_leftover_stash_is_not_mistaken_for_this_runs(cli, tmp_path):
    """With `~seudolife_memory` already taken by an earlier failed run, pip
    picks the next name; restoring the old leftover would roll the runtime
    back to whatever that run stranded."""
    root = _runtime(tmp_path)
    site = _site(root)
    (site / "~seudolife_memory").mkdir()
    (site / "~seudolife_memory" / "__init__.py").write_text("RELEASE = 'older'\n", encoding="utf-8")
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    cli.pip_effect = _pip24_stash_then_fail(site)
    cli.pip_install = (1, WINERROR_32)
    assert uc.update_shim(ROOT)["state"] == "failed"
    assert (site / "pseudolife_memory" / "__init__.py").read_text(encoding="utf-8") == "RELEASE = 'installed'\n"
    assert (site / "~seudolife_memory" / "__init__.py").read_text(encoding="utf-8") == "RELEASE = 'older'\n"
    assert not (site / "~-eudolife_memory").exists()


def test_a_vanished_package_with_no_stash_to_restore_is_named(cli, tmp_path):
    """Moved somewhere this helper cannot see (a temp dir): say plainly that
    the runtime has lost its package and name it, instead of "retry"."""
    root = _runtime(tmp_path)
    site = _site(root)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    cli.pip_effect = lambda argv: shutil.move(str(site / "pseudolife_memory"), str(tmp_path / "elsewhere"))
    cli.pip_install = (1, WINERROR_32)
    result = uc.update_shim(ROOT)
    assert result["state"] == "failed"
    assert str(site / "pseudolife_memory") in result["detail"]
    assert "no package of its own" in result["detail"]


def test_a_pip_failure_that_moved_nothing_says_the_runtime_is_intact(cli, tmp_path):
    root = _runtime(tmp_path)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    cli.pip_install = (1, "ERROR: No matching distribution found for mcp>=2.1")
    result = uc.update_shim(ROOT)
    assert result["state"] == "failed"
    assert "still imports its own" in result["detail"]


@pytest.mark.parametrize("stash, original, expected", [
    ("~seudolife_memory", "pseudolife_memory", True),
    ("~-eudolife_memory", "pseudolife_memory", True),
    ("~~-udolife_memory", "pseudolife_memory", True),
    ("~pseudolife_memory", "pseudolife_memory", True),      # pip's longer fallback names
    ("~seudolife_mcp-0.15.0.dist-info", DIST_INFO, True),
    ("~seudolife_memory", "pseudolife_memory_extra", False),
    ("~ydantic", "pseudolife_memory", False),
    ("~xeudolife_memory", "pseudolife_memory", False),      # 'x' is not a pip stash character
    ("pseudolife_memory", "pseudolife_memory", False),
])
def test_stash_names_follow_pips_adjacent_temp_directory(stash, original, expected):
    assert uc._stash_of(stash, original) is expected


def test_main_exits_non_zero_when_the_shim_was_skipped_as_in_use(cli, tmp_path, capsys):
    root = _runtime(tmp_path)
    cli.claude_get = (0, _claude_stdio(str(root / "Scripts" / f"pseudolife-mcp{EXE}")))
    cli.processes = _sessions(root, claude=1)
    assert uc.main(["--repo", str(ROOT), "--only", "shim"]) == 1
    assert "[!] Shim" in capsys.readouterr().out


# ── against a real interpreter ──────────────────────────────────────────────

_STUB_PIP = '''\
"""Stands in for pip 24.0 failing the way it did on 2026-09-25: stash in
sorted order with AdjacentTempDirectory's first free name, then raise on
the running launcher before any rollback."""
import os, sys, sysconfig
site = sysconfig.get_paths()["purelib"]
for name in sorted(n for n in os.listdir(site)
                   if n == "pseudolife_memory" or n.startswith("pseudolife_mcp-")):
    os.rename(os.path.join(site, name), os.path.join(site, "~" + name[1:]))
print("ERROR: Could not install packages due to an OSError: [WinError 32] The process cannot "
      "access the file because it is being used by another process")
sys.exit(1)
'''


def _real_runtime(tmp_path) -> tuple[Path, Path]:
    """A real venv with a stand-in package (and, for the stash test, a stub
    pip) in its own site-packages; returns ``(root, interpreter)``."""
    root = tmp_path / "real-rt"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True, timeout=180)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = Path(subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                               capture_output=True, text=True, check=True, timeout=60).stdout.strip())
    (site / "pseudolife_memory").mkdir()
    (site / "pseudolife_memory" / "__init__.py").write_text("", encoding="utf-8")
    (site / DIST_INFO).mkdir()
    (site / DIST_INFO / "METADATA").write_text("Name: pseudolife-mcp\n", encoding="utf-8")
    return root, python


def _imports_from(python: Path) -> str:
    proc = subprocess.run([str(python), "-I", "-c", "import pseudolife_memory; print(pseudolife_memory.__file__)"],
                          capture_output=True, text=True, timeout=60, cwd=str(Path(uc.tempfile.gettempdir())))
    return proc.stdout.strip() if proc.returncode == 0 else f"import failed: {proc.stderr.strip()[-200:]}"


def _through_real(cli, python: Path, monkeypatch):
    """Commands of the real runtime run for real; the client CLIs stay fake.
    A PYTHONPATH naming a checkout (how worktree runs pin their code) would
    reach the runtime's probe and make it read as editable."""
    monkeypatch.delenv("PYTHONPATH", raising=False)

    def run(argv, **kw):
        if Path(str(argv[0])) == python:
            cli.calls.append([str(a) for a in argv])
            return REAL_RUN_CLI(argv, **kw)
        return cli(argv, **kw)
    return run


def test_a_real_runtime_still_imports_its_own_package_after_a_stash_then_fail(cli, tmp_path, monkeypatch):
    root, python = _real_runtime(tmp_path)
    site = Path(_imports_from(python)).parent.parent
    (site / "pip").mkdir()
    (site / "pip" / "__init__.py").write_text("", encoding="utf-8")
    (site / "pip" / "__main__.py").write_text(_STUB_PIP, encoding="utf-8")
    monkeypatch.setattr(uc, "run_cli", _through_real(cli, python, monkeypatch))
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    result = uc.update_shim(ROOT)
    assert _pip_calls(cli), "the stub pip never ran"
    assert result["state"] == "failed" and "WinError 32" in result["detail"]
    imported = Path(_imports_from(python))
    assert imported.is_file() and imported.resolve().is_relative_to(root.resolve()), imported
    assert not [p.name for p in site.iterdir() if p.name.startswith("~")]


@pytest.mark.skipif(os.name != "nt", reason="the in-use check reads the Windows process table")
def test_a_real_session_holding_the_runtime_blocks_the_upgrade(cli, tmp_path, monkeypatch):
    """Nothing faked but the client CLIs: a process started from the
    runtime's own interpreter (what every Codex session is) must be seen."""
    root, python = _real_runtime(tmp_path)
    monkeypatch.setattr(uc, "run_cli", _through_real(cli, python, monkeypatch))
    monkeypatch.setattr(uc, "list_processes", REAL_LIST_PROCESSES)
    cli.codex_get = (0, _codex_stdio(str(python), "-m pseudolife_memory.cli"))
    session = subprocess.Popen([str(python), "-c", "import time; time.sleep(120)"])
    try:
        own = [row for row in uc.list_processes() if row[0] == os.getpid()]
        assert own and own[0][1] == os.getppid() and Path(own[0][2]).is_file()
        result = uc.update_shim(ROOT)
    finally:
        session.kill()
        session.wait(timeout=30)
    assert result["state"] == "in-use" and "1 session" in result["detail"], result
    assert not _pip_calls(cli)


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


@pytest.mark.parametrize("state", ['"not a table"', "[1, 2]"])
def test_codex_plugin_approval_check_survives_a_malformed_config(cli, state):
    """A report that crashes on one odd config value loses the whole ladder."""
    codex_home = _codex_plugin_config(cli)
    with open(codex_home / "config.toml", "a", encoding="utf-8") as config:
        config.write(f"\n[hooks]\nstate = {state}\n")
    shutil.copytree(ROOT / "plugin", codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin")
    assert uc.check_codex_hooks(ROOT)["state"] in ("current", "needs-approval")
