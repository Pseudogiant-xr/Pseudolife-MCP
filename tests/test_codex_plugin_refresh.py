"""The update refreshes Codex's plugin-managed hooks itself when only the
scripts changed.

Codex approves a hook by its definition in hooks.json, not by the script it
runs (measured on Codex 0.158.0, 2026-09-30), so a plugin copy whose scripts
are older but whose hooks.json is current needs no approval, only the newer
files. Until 2026-10-03 the update only reported that copy as behind and
told the maintainer to click update in Codex's plugin manager. Now the
update step runs Codex's own ``codex plugin marketplace upgrade
pseudolife-mcp``, which (measured on Codex 0.160.0, 2026-10-03, in a
throwaway CODEX_HOME) fetches the git marketplace and replaces both its
clone and the installed copy Codex runs hooks from
(``plugins/cache/pseudolife-mcp/pseudolife-memory/local``), leaving
config.toml and every hook's approval hash as they were. A changed
hooks.json is not knowingly upgraded here: its approval stays a consented step.
If the branch moves between inspection and the fetch, read-back reports
the changed definition as stale with approval steps.
The marketplace serves its branch, which can differ from the checkout and
from the release just deployed, so before upgrading the update reads the
branch's hooks.json from a private blob-less clone in a temporary
directory (never Codex's own clone) and upgrades only when it matches the
installed one.

The fake Codex CLI below plays that upgrade against a fake CODEX_HOME; the
real ~/.codex is never read or written.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import client_updates as uc, codex_doorbell  # noqa: E402
from tests.test_update_clients import _codex_plugin_config, _plugin_handler_keys, cli  # noqa: E402,F401

EXE = ".exe" if os.name == "nt" else ""
UPGRADE = ["plugin", "marketplace", "upgrade", "pseudolife-mcp", "--json"]
SOURCE = "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"
RUN_CLI = uc.run_cli
GIT_EXECUTABLE = uc._git_executable
SYSTEM_GIT = shutil.which("git")


@pytest.fixture(autouse=True)
def _no_configured_codex(monkeypatch):
    # A maintainer's configured executable is not part of a fake client home.
    monkeypatch.delenv("PSEUDOLIFE_CODEX_BIN", raising=False)


def _clone(codex_home: Path) -> Path:
    return codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin"


def _cache(codex_home: Path) -> Path:
    return codex_home / "plugins" / "cache" / "pseudolife-mcp" / "pseudolife-memory" / "local"


def _older(tree: Path, name: str = "session-end.sh") -> None:
    hook = tree / "hooks" / name
    hook.write_bytes(hook.read_bytes() + b"\n# older\n")


def _redefined(tree: Path) -> None:
    """A real definition change: one handler's timeout."""
    manifest = tree / "hooks" / "hooks.json"
    text = manifest.read_bytes()
    assert b'"timeout": 5' in text
    manifest.write_bytes(text.replace(b'"timeout": 5', b'"timeout": 6', 1))


def _installed(cli, *, older: str | None = "session-end.sh") -> Path:
    """An approved Codex home whose clone and installed copy hold the
    checkout's plugin, with ``older`` changed in both."""
    codex_home = _codex_plugin_config(cli, _plugin_handler_keys())
    with open(codex_home / "config.toml", "a", encoding="utf-8") as config:
        config.write(f'\n[marketplaces.pseudolife-mcp]\nsource_type = "git"\nsource = "{SOURCE}"\n')
    for tree in (_clone(codex_home), _cache(codex_home)):
        shutil.copytree(ROOT / "plugin", tree)
        if older:
            _older(tree, older)
    return codex_home


class FakeCodex:
    """``codex plugin marketplace upgrade <name> --json`` as measured on
    Codex 0.160.0: when the marketplace offers something newer it replaces
    the clone and the installed ``local`` copy and lists the clone's root
    under ``upgradedRoots``; with nothing newer it lists none and touches
    nothing. ``git clone`` / ``git show`` answer the branch's hooks.json
    (``remote_hooks``, else the served tree's). Every other call goes to the
    shared fake."""

    def __init__(self, cli, monkeypatch, *, offers: Path | None = ROOT / "plugin"):
        self.cli = cli
        self.offers = offers                 # the plugin tree the marketplace serves; None = nothing newer
        self.answer: tuple[int, str] | None = None
        self.calls: list[tuple[list[str], dict]] = []
        self.git_calls: list[list[str]] = []
        self.git_kwargs: list[dict] = []
        self.remote_hooks: str | None = None     # the branch's hooks.json, when not the served tree's
        self.exe = cli.tools["codex"]
        cli.tools["git"] = str(Path(cli.tools["codex"]).with_name(f"git{EXE}"))
        monkeypatch.setattr(uc, "_git_executable", lambda: cli.tools.get("git"))
        monkeypatch.setattr(codex_doorbell, "resolve_codex_command",
                            lambda environ=None: [self.exe] if self.exe else None)
        monkeypatch.setattr(uc, "run_cli", self)

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        name = Path(argv[0]).name.lower().removesuffix(".exe")
        if name == "git":
            return self._git(argv, **kw)
        if name != "codex":
            return self.cli(argv, **kw)
        self.calls.append((argv, kw))
        if self.answer is not None:
            return self.answer
        if argv[1:] != UPGRADE:
            return 91, f"unexpected call {argv}"
        selected_home = (kw.get("env") or {}).get("CODEX_HOME")
        if not selected_home:
            return 92, "the fake upgrade requires an explicit CODEX_HOME"
        codex_home = Path(selected_home)
        clone = _clone(codex_home)
        upgraded = []
        if self.offers is not None and uc.tree_differs(self.offers, clone):
            for tree in (clone, _cache(codex_home)):
                shutil.rmtree(tree, ignore_errors=True)
                shutil.copytree(self.offers, tree)
            upgraded = [str(clone.parent)]
        report = {"selectedMarketplaces": ["pseudolife-mcp"], "upgradedRoots": upgraded, "errors": []}
        # stdout first, then stderr: run_cli joins them in that order, and
        # Codex warns on stderr (seen under a temp-dir CODEX_HOME).
        return 0, json.dumps(report, indent=2) + "\nWARNING: proceeding, even though we could not create PATH aliases\n"

    def _git(self, argv, **kw):
        self.git_calls.append(argv)
        self.git_kwargs.append(kw)
        if "clone" in argv:
            Path(argv[-1]).mkdir(parents=True)
            return 0, ""
        if "show" in argv:
            if self.remote_hooks is not None:
                return 0, self.remote_hooks
            served = self.offers or _cache(self.cli.home / "codex")
            return 0, (served / "hooks" / "hooks.json").read_text(encoding="utf-8")
        return 91, f"unexpected call {argv}"


# ── the automatic refresh ───────────────────────────────────────────────────

def test_the_update_upgrades_the_marketplace_when_only_scripts_changed(cli, monkeypatch):
    codex_home = _installed(cli)
    config = (codex_home / "config.toml").read_bytes()
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "refreshed" and result["source"] == "plugin"
    assert result["changed_files"] == ["session-end.sh"]
    assert "approvals carry over" in result["detail"]
    [(argv, kw)] = codex.calls
    assert argv == [codex.exe] + UPGRADE
    assert Path(kw["env"]["CODEX_HOME"]) == codex_home.resolve()   # the home that was inspected
    assert uc.changed_hook_files(_cache(codex_home) / "hooks", ROOT / "plugin" / "hooks") == []
    assert (codex_home / "config.toml").read_bytes() == config      # approvals and trust untouched
    assert uc.codex_reapproval_text(result) == ""
    assert uc._marker(result["state"]) == "[x]"


def test_a_plain_check_never_upgrades(cli, monkeypatch):
    """doctor and the daemon-only path read; only the update step refreshes."""
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "behind" and codex.calls == []


def test_a_changed_hooks_json_is_never_upgraded_automatically(cli, monkeypatch):
    _installed(cli, older="hooks.json")
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "stale" and result["changed_files"] == ["hooks.json"]
    assert codex.calls == []
    assert "hooks.json" in uc.codex_reapproval_text(result)


def test_codex_not_installed_or_no_plugin_runs_nothing(cli, monkeypatch):
    codex = FakeCodex(cli, monkeypatch)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "not-configured"
    _codex_plugin_config(cli)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "plugin-managed"
    assert codex.calls == []


def test_a_current_copy_runs_nothing(cli, monkeypatch):
    _installed(cli, older=None)
    codex = FakeCodex(cli, monkeypatch)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "current"
    assert codex.calls == []


# ── honest when the automatic path is unavailable ───────────────────────────

def test_no_codex_cli_stays_behind_and_says_why(cli, monkeypatch):
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    codex.exe = None
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert "no Codex CLI" in result["detail"]
    assert "codex plugin marketplace upgrade pseudolife-mcp" in result["detail"]
    assert "plugin manager" in result["detail"]


def test_the_desktop_apps_standalone_cli_is_found_when_nothing_is_on_path(cli, monkeypatch):
    """A desktop-only Codex keeps its CLI in the Codex home's standalone
    package (seen on Windows, Codex 0.160.0, 2026-10-03:
    packages/standalone/current/bin/codex.exe)."""
    codex_home = _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    codex.exe = None
    standalone = codex_home / "packages" / "standalone" / "current" / "bin" / f"codex{EXE}"
    standalone.parent.mkdir(parents=True)
    standalone.write_bytes(b"fake codex")
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "refreshed"
    assert codex.calls[0][0][0] == str(standalone.resolve())


@pytest.mark.parametrize("answer, said", [
    ((1, "Error: git fetch failed: could not resolve host github.com"), "could not resolve host"),
    ((124, "TimeoutExpired: timed out"), "timed out"),
    ((0, json.dumps({"selectedMarketplaces": ["pseudolife-mcp"], "upgradedRoots": [],
                     "errors": [{"marketplaceName": "pseudolife-mcp",
                                 "message": "rename failed: access is denied"}]})), "access is denied"),
])
def test_a_failed_upgrade_stays_behind_with_codexs_words(cli, monkeypatch, answer, said):
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    codex.answer = answer
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and said in result["detail"]
    assert "plugin manager" in result["detail"]
    assert "approvals carry over" in result["detail"]
    assert "inspected" in result["detail"]
    assert uc._marker(result["state"]) == "[-]"


def test_nothing_newer_in_the_marketplace_stays_behind_and_says_so(cli, monkeypatch):
    """A checkout ahead of the published master: the marketplace has
    nothing newer than Codex's clone, so the copy stays behind."""
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch, offers=None)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and len(codex.calls) == 1
    assert "nothing newer" in result["detail"]
    assert result["changed_files"] == ["session-end.sh"]


def test_an_upgrade_to_a_branch_that_still_differs_stays_behind(cli, monkeypatch, tmp_path):
    _installed(cli)
    served = tmp_path / "served"
    shutil.copytree(ROOT / "plugin", served)
    _older(served, "stop-wake.sh")
    FakeCodex(cli, monkeypatch, offers=served)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and result["changed_files"] == ["stop-wake.sh"]
    assert "upgraded its copy to the marketplace's branch" in result["detail"]
    assert "the checkout's scripts" in result["detail"]


def test_release_mode_refreshes_against_the_daemons_scripts(cli, monkeypatch):
    """``pseudolife-mcp update`` has no checkout: the daemon's scripts are
    read from the Claude plugin cache when its digest is the daemon's."""
    _installed(cli)
    plugins = cli.home / ".claude" / "plugins"
    cache = plugins / "cache" / "pseudolife-memory"
    shutil.copytree(ROOT / "plugin", cache)
    (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {
        uc.PLUGIN_ID: [{"version": "0.15.0", "installPath": str(cache)}]}}), encoding="utf-8")
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(None, daemon_digest=uc.hooks_digest(ROOT / "plugin" / "hooks"), refresh=True)
    assert result["state"] == "refreshed" and len(codex.calls) == 1


def test_a_branch_that_changes_hooks_json_is_not_upgraded(cli, monkeypatch, tmp_path):
    """The marketplace serves master, which can carry a hooks.json the
    checkout or the deployed release does not: the upgrade would take it in
    without consent, so it does not run, and the step says what is needed."""
    codex_home = _installed(cli)
    before = (_cache(codex_home) / "hooks" / "session-end.sh").read_bytes()
    served = tmp_path / "served"
    shutil.copytree(ROOT / "plugin", served)
    _redefined(served)
    codex = FakeCodex(cli, monkeypatch, offers=served)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert "hooks.json" in result["detail"] and "approv" in result["detail"]
    assert "approvals carry over" not in result["detail"]
    assert "/hooks" in result["detail"] and "plugin manager" in result["detail"]
    assert (_cache(codex_home) / "hooks" / "session-end.sh").read_bytes() == before


def test_the_branch_is_read_in_a_private_clone_never_codexs(cli, monkeypatch):
    codex_home = _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "refreshed"
    clone, show = codex.git_calls
    assert clone[1:7] == ["clone", "--quiet", "--depth", "1", "--filter=blob:none", "--no-checkout"]
    assert clone[-2] == SOURCE
    assert show[-2:] == ["show", "HEAD:plugin/hooks/hooks.json"] and show[2] == clone[-1]
    private = Path(clone[-1]).resolve()
    assert codex_home.resolve() not in private.parents and not private.exists()   # removed afterwards


def test_the_read_never_waits_on_a_prompt(cli, monkeypatch):
    """No terminal, SSH or Git Credential Manager prompt can hold the
    update: a source that wants credentials fails and stays behind."""
    _installed(cli)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    codex = FakeCodex(cli, monkeypatch)
    uc.check_codex_hooks(ROOT, refresh=True)
    for kw in codex.git_kwargs:
        env = kw["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_a_pinned_ref_is_the_branch_read(cli, monkeypatch):
    """``codex plugin marketplace add --ref`` writes ``ref`` to config.toml
    and ``ref_name`` to the clone's install record (Codex 0.160.0,
    2026-10-03)."""
    codex_home = _installed(cli)
    config = codex_home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + 'ref = "stable"\n', encoding="utf-8")
    codex = FakeCodex(cli, monkeypatch)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "refreshed"
    clone = codex.git_calls[0]
    assert clone[clone.index("--branch") + 1] == "stable" and clone[-2] == SOURCE


def test_the_install_record_names_the_source_when_the_config_cannot(cli, monkeypatch):
    """Python 3.10 has no tomllib, and a config may carry no source: the
    clone's install record (written by Codex's upgrade) names it then."""
    codex_home = _installed(cli)
    config = codex_home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").split("\n[marketplaces.")[0], encoding="utf-8")
    (_clone(codex_home).parent / ".codex-marketplace-install.json").write_text(json.dumps({
        "source_type": "git", "source": SOURCE, "ref_name": "master", "sparse_paths": [],
        "revision": "0" * 40}), encoding="utf-8")
    codex = FakeCodex(cli, monkeypatch)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "refreshed"
    clone = codex.git_calls[0]
    assert clone[clone.index("--branch") + 1] == "master" and clone[-2] == SOURCE


def test_the_source_is_read_without_tomllib(cli, monkeypatch):
    """pyproject allows Python 3.10, which has no tomllib: the marketplace
    table is still read from config.toml."""
    _installed(cli)
    monkeypatch.setitem(sys.modules, "tomllib", None)        # import tomllib raises, as on 3.10
    codex = FakeCodex(cli, monkeypatch)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] == "refreshed"
    assert codex.git_calls[0][-2] == SOURCE


def test_a_branch_that_cannot_be_read_is_not_upgraded(cli, monkeypatch):
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    codex.remote_hooks = "fatal: repository not found"
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert "could not read" in result["detail"] and "plugin manager" in result["detail"]


def test_no_git_or_no_marketplace_source_is_not_upgraded(cli, monkeypatch):
    codex_home = _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    git = cli.tools["git"]
    cli.tools["git"] = None
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == [] and "git" in result["detail"]
    cli.tools["git"] = git
    config = codex_home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").split("\n[marketplaces.")[0], encoding="utf-8")
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == [] and "source" in result["detail"]


def test_a_hooks_json_that_lands_anyway_asks_for_the_approval(cli, monkeypatch, tmp_path):
    """The branch can move between the read and Codex's fetch: a changed
    hooks.json that lands anyway is reported with the approval steps."""
    _installed(cli)
    served = tmp_path / "served"
    shutil.copytree(ROOT / "plugin", served)
    _redefined(served)
    codex = FakeCodex(cli, monkeypatch, offers=served)
    codex.remote_hooks = (ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "stale" and "hooks.json" in result["changed_files"]
    assert "hooks.json" in uc.codex_reapproval_text(result)
    assert "approvals carry over" not in result["detail"]


# ── the copy Codex runs ─────────────────────────────────────────────────────

def test_the_installed_copy_is_compared_not_the_clone(cli):
    """Codex runs a plugin's hooks from its installed copy (hooks/list
    sourcePath, Codex 0.160.0, 2026-10-03), not from the marketplace clone."""
    codex_home = _installed(cli, older=None)
    _older(_cache(codex_home))
    assert uc.check_codex_hooks(ROOT)["state"] == "behind"
    shutil.rmtree(_cache(codex_home))
    shutil.copytree(ROOT / "plugin", _cache(codex_home))
    _older(_clone(codex_home))
    assert uc.check_codex_hooks(ROOT)["state"] == "current"


def test_the_ladder_shows_the_refresh(cli, monkeypatch, capsys):
    _installed(cli)
    FakeCodex(cli, monkeypatch)
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    out = capsys.readouterr().out
    assert "[x] Codex hooks    refreshed" in out
    assert "re-approval" not in out


@pytest.mark.parametrize("unavailable", ["inspection", "codex", "git", "source", "branch"])
def test_uninspected_marketplace_never_promises_approval_carryover(cli, monkeypatch, unavailable):
    codex_home = _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    if unavailable == "codex":
        codex.exe = None
    elif unavailable == "git":
        cli.tools["git"] = None
    elif unavailable == "source":
        config = codex_home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").split("\n[marketplaces.")[0], encoding="utf-8")
    elif unavailable == "branch":
        codex.remote_hooks = "fatal: repository not found"
    result = uc.check_codex_hooks(ROOT, refresh=unavailable != "inspection")
    assert result["state"] == "behind" and codex.calls == []
    assert "approvals carry over" not in result["detail"]
    assert "may change hooks.json" in result["detail"] and "approval" in result["detail"]


def test_missing_configured_codex_never_uses_the_standalone_copy(cli, monkeypatch):
    codex_home = _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    codex.exe = None
    standalone = codex_home / "packages" / "standalone" / "current" / "bin" / f"codex{EXE}"
    standalone.parent.mkdir(parents=True)
    standalone.write_bytes(b"fake codex")
    monkeypatch.setenv("PSEUDOLIFE_CODEX_BIN", str(cli.home / "missing-codex"))
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert "PSEUDOLIFE_CODEX_BIN" in result["detail"] and "setup error" in result["detail"]


def test_git_clone_separates_a_source_from_options(cli, monkeypatch):
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    uc.check_codex_hooks(ROOT, refresh=True)
    assert codex.git_calls[0][-3:] == ["--", SOURCE, codex.git_calls[0][-1]]


@pytest.mark.parametrize("entry", ["", ".", "relative-bin"])
def test_git_never_runs_from_cwd_or_a_relative_path(cli, monkeypatch, tmp_path, entry):
    _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    planted = tmp_path / entry / f"git{EXE}"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"fake git")
    planted.chmod(0o755)
    cli.tools["git"] = str(planted.resolve())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", entry)
    # Exercise real resolution instead of the fake CLI's lookup seam.
    monkeypatch.setattr(uc, "_git_executable", GIT_EXECUTABLE)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.git_calls == [] and codex.calls == []


def test_branch_manifest_is_decoded_as_utf8_under_a_legacy_locale(cli, monkeypatch):
    codex_home = _installed(cli)
    real_run_cli = RUN_CLI
    real_subprocess_run = uc.subprocess.run
    codex = FakeCodex(cli, monkeypatch)
    manifest = json.dumps({"hooks": {}, "description": "caf\u00e9"}, ensure_ascii=False).encode("utf-8")

    def legacy_locale(argv, **kw):
        if not kw.get("encoding"):
            kw["encoding"] = "cp1252"
        return real_subprocess_run(argv, **kw)

    def run(argv, **kw):
        if "show" in argv:
            return real_run_cli([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({manifest!r})"], **kw)
        return codex(argv, **kw)

    monkeypatch.setattr(uc.subprocess, "run", legacy_locale)
    monkeypatch.setattr(uc, "run_cli", run)
    offered, why = uc._marketplace_hooks_json(codex_home)
    assert offered == manifest and why == ""


def test_upgrade_uses_the_inspected_home_when_codex_home_is_unset(cli, monkeypatch):
    configured = _installed(cli)
    inspected = cli.home / ".codex"
    configured.rename(inspected)
    monkeypatch.delenv("CODEX_HOME")
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    # Inspect only the home field, so a failed assertion cannot print the
    # subprocess's whole inherited environment.
    received_home = codex.calls[0][1]["env"].get("CODEX_HOME")
    assert received_home == str(inspected.resolve())
    assert result["state"] == "refreshed"
    assert len(codex.calls) == 1


@pytest.mark.parametrize("git_kind", ["fake", "system"])
def test_git_is_found_in_an_absolute_path_directory(cli, monkeypatch, tmp_path, git_kind):
    codex_home = _installed(cli)
    codex = FakeCodex(cli, monkeypatch)
    if git_kind == "system":
        if SYSTEM_GIT is None:
            pytest.skip("no system git installed; fake absolute PATH remains covered")
        executable = Path(SYSTEM_GIT).resolve()
    else:
        executable = tmp_path / "absolute-bin" / f"git{EXE}"
        executable.parent.mkdir()
        executable.write_bytes(b"fake git")
        executable.chmod(0o755)
    cwd = tmp_path / "working-directory"
    cwd.mkdir()
    (cwd / f"git{EXE}").write_bytes(b"planted git")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(uc, "_git_executable", GIT_EXECUTABLE)
    offered, why = uc._marketplace_hooks_json(codex_home)
    assert offered == uc._normalised(ROOT / "plugin" / "hooks" / "hooks.json") and why == ""
    assert Path(codex.git_calls[0][0]).resolve() == executable
