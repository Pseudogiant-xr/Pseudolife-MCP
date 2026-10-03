"""The update refreshes Codex's plugin-managed hooks itself when their
normalized definitions remain equivalent.

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
approved definition is not knowingly upgraded here: its approval stays a
consented step.
If the branch moves between inspection and the fetch, read-back reports
the changed definition as stale with approval steps.
The marketplace serves its branch, which can differ from the checkout and
from the release just deployed, so before upgrading the update reads the
branch's hooks.json from a private blob-less clone in a temporary
directory (never Codex's own clone) and upgrades only when its normalized
definitions match the installed ones. Raw clamp-only changes still refresh.

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


def _edit_manifest(tree: Path, edit) -> None:
    path = tree / "hooks" / "hooks.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _timeout(tree: Path, seconds, event: str = "SessionEnd") -> None:
    _edit_manifest(tree, lambda manifest: manifest["hooks"][event][0]["hooks"][0].update(timeout=seconds))


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


@pytest.mark.parametrize("response", ["deep-json", "raw-surrogate"])
def test_fetched_branch_parser_fails_closed(cli, monkeypatch, response):
    codex_home = _installed(cli)
    before = (codex_home / "config.toml").read_bytes()
    codex = FakeCodex(cli, monkeypatch)
    if response == "deep-json":
        codex.remote_hooks = ('{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"ok","unknown":'
                              + '[' * 2000 + '0' + ']' * 2000 + '}]}]}}')
    else:
        codex.remote_hooks = '{"hooks":{},"description":"' + "\ud800" + '"}'
    # Installed/reference definitions are valid and match, so inspection must
    # reach the fetched response instead of exiting stale at initial eligibility.
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert len(codex.git_calls) == 2 and "could not read" in result["detail"]
    assert (codex_home / "config.toml").read_bytes() == before


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
        if kw.get("text") and not kw.get("encoding"):
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


def test_invalid_utf8_branch_is_not_replaced_into_an_approved_definition(cli, monkeypatch, tmp_path):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, lambda m: m["hooks"]["SessionEnd"][0]["hooks"][0].update(unknownField="\ufffd"))
    manifest = json.loads((reference / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    invalid = json.dumps(manifest, ensure_ascii=False).encode("utf-8").replace(b"\xef\xbf\xbd", b"\xff")
    assert b"\xff" in invalid
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")

    def run(argv, **kwargs):
        if "show" in argv:
            # A real Python subprocess emits the malformed bytes; git/Codex
            # remain fake. Replacing 0xff would match the approved U+FFFD value.
            return RUN_CLI([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({invalid!r})"], **kwargs)
        return codex(argv, **kwargs)

    monkeypatch.setattr(uc, "run_cli", run)
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert "could not read" in result["detail"]


def test_run_cli_preserves_replacement_decoding_for_other_calls():
    code, out = RUN_CLI([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"], encoding="utf-8")
    assert code == 0 and out == "\ufffd"


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_run_cli_strict_utf8_decoding_reports_a_failure():
    code, out = RUN_CLI([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"],
                       encoding="utf-8", errors="strict")
    assert code == 1 and "UnicodeDecodeError" in out


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


# Codex b741e480 normalizes SessionEnd/Interrupt before hashing their definitions.
def test_clamped_timeout_is_current_without_refresh(cli, monkeypatch):
    codex_home = _installed(cli, older=None)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _timeout(tree, 10)
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "current" and codex.calls == []


@pytest.mark.parametrize("older", [None, "session-end.sh"])
def test_clamped_timeout_changes_refresh_without_reapproval(cli, monkeypatch, older):
    codex_home = _installed(cli, older=older)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _timeout(tree, 10)
    before = (codex_home / "config.toml").read_bytes()
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "refreshed" and len(codex.calls) == 1
    assert "hooks.json" in result["changed_files"]
    assert uc.codex_reapproval_text(result) == ""
    assert (codex_home / "config.toml").read_bytes() == before
    assert uc.changed_hook_files(_cache(codex_home) / "hooks", ROOT / "plugin" / "hooks") == []


def test_branch_and_readback_accept_an_equivalent_clamped_timeout(cli, monkeypatch, tmp_path):
    codex_home = _installed(cli)
    served = tmp_path / "served"
    shutil.copytree(ROOT / "plugin", served)
    _timeout(served, 10)
    codex = FakeCodex(cli, monkeypatch, offers=served)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "refreshed" and len(codex.calls) == 1
    assert uc.codex_reapproval_text(result) == ""
    assert uc.check_codex_hooks(ROOT)["state"] == "current"
    # Raw changes remain visible, even when they do not require approval.
    assert uc.changed_hook_files(_cache(codex_home) / "hooks", ROOT / "plugin" / "hooks") == ["hooks.json"]


def test_clamped_timeout_with_nothing_newer_does_not_claim_a_refresh(cli, monkeypatch):
    codex_home = _installed(cli, older=None)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _timeout(tree, 10)
    codex = FakeCodex(cli, monkeypatch, offers=None)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and len(codex.calls) == 1
    assert "nothing newer" in result["detail"]
    assert uc.codex_reapproval_text(result) == ""


def test_interrupt_uses_the_same_timeout_cap(cli, monkeypatch, tmp_path):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")

    def add_interrupt(manifest):
        manifest["hooks"]["Interrupt"] = json.loads(json.dumps(manifest["hooks"]["SessionEnd"]))

    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, add_interrupt)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _timeout(tree, 10, "Interrupt")
    with (codex_home / "config.toml").open("a", encoding="utf-8") as config:
        config.write('\n[hooks.state."pseudolife-memory@pseudolife-mcp:hooks/hooks.json:interrupt:0:0"]\n'
                     'trusted_hash = "already-approved"\n')
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "refreshed" and len(codex.calls) == 1
    assert uc.codex_reapproval_text(result) == ""


@pytest.mark.parametrize("field,value", [
    ("command", "echo changed"), ("commandWindows", "echo changed"),
    ("async", True), ("statusMessage", "changed"),
    ("additionalContextLimit", 7), ("unknownField", 1),
    ("matcher", "changed"), ("timeout", 2), ("SessionStart.timeout", 10),
])
def test_clamping_never_hides_other_definition_changes(cli, monkeypatch, tmp_path, field, value):
    codex_home = _installed(cli)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _timeout(tree, 10)
    served = tmp_path / "served"
    shutil.copytree(ROOT / "plugin", served)

    def change(manifest):
        if field == "SessionStart.timeout":
            manifest["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = value
        elif field == "matcher":
            manifest["hooks"]["SessionEnd"][0]["matcher"] = value
        else:
            manifest["hooks"]["SessionEnd"][0]["hooks"][0][field] = value

    _edit_manifest(served, change)
    codex = FakeCodex(cli, monkeypatch, offers=served)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "behind" and codex.calls == []
    assert "hooks.json" in result["detail"] and "approv" in result["detail"]


def test_unknown_field_boolean_and_integer_remain_distinct(cli, monkeypatch, tmp_path):
    codex_home = _installed(cli)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, lambda m: m["hooks"]["SessionEnd"][0]["hooks"][0].update(unknownField=True))
    served = tmp_path / "served"
    shutil.copytree(ROOT / "plugin", served)
    _edit_manifest(served, lambda m: m["hooks"]["SessionEnd"][0]["hooks"][0].update(unknownField=1))
    codex = FakeCodex(cli, monkeypatch, offers=served)
    # Compare against the installed definitions, isolating the branch consent check.
    result = uc._upgrade_codex_plugin(codex_home, ROOT, None, ["session-end.sh"])
    assert result["state"] == "behind" and codex.calls == []


@pytest.mark.parametrize("field,value", [
    ("timeout", True), ("timeout", "3"), ("timeout", 3.0), ("timeout", -1),
    ("timeout", 2 ** 64), ("command", 1), ("async", "true"),
    ("statusMessage", 1), ("matcher", 1), ("hooks", None), ("type", "unknown"),
])
def test_identically_malformed_definitions_are_not_auto_refreshed(cli, monkeypatch, tmp_path, field, value):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")

    def invalidate(manifest):
        group = manifest["hooks"]["SessionEnd"][0]
        if field in ("matcher", "hooks"):
            group[field] = value
        else:
            group["hooks"][0][field] = value

    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, invalidate)
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "stale" and codex.calls == []


@pytest.mark.parametrize("manifest", [
    b"not JSON", b"[]", b'{"hooks": []}', b'{"hooks": {"SessionEnd": null}}',
    b'{"hooks": {"SessionEnd": [{"hooks": [7]}]}}',
    b'{"description": 1, "hooks": {}}', b'{"hooks": {}, "unknown": NaN}',
    b'{"hooks": {"SessionEnd": [], "SessionEnd": []}}',
])
def test_malformed_manifest_structure_is_not_auto_refreshed(cli, monkeypatch, tmp_path, manifest):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        (tree / "hooks" / "hooks.json").write_bytes(manifest)
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "stale" and codex.calls == []


@pytest.mark.parametrize("field,value", [
    pytest.param("command", "\ud800", id="command-high-surrogate"),
    pytest.param("command", "\udc00", id="command-low-surrogate"),
    pytest.param("matcher", "\ud800", id="matcher-surrogate"),
    pytest.param("statusMessage", "\udc00", id="status-surrogate"),
    pytest.param("unknownField", {"nested": ["\ud800"]}, id="unknown-nested-value-surrogate"),
    pytest.param("unknownField", {"\udc00": "valid value"}, id="unknown-nested-key-surrogate"),
])
def test_lone_surrogates_are_not_auto_refreshed(cli, monkeypatch, tmp_path, field, value):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")

    def invalidate(manifest):
        group = manifest["hooks"]["SessionEnd"][0]
        if field == "matcher":
            group[field] = value
        else:
            group["hooks"][0][field] = value

    # The invalid bytes are identical: raw equality must not authorize an upgrade.
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, invalidate)
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "stale" and codex.calls == []


def test_deeply_nested_manifest_parse_fails_closed(cli, monkeypatch, tmp_path):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    manifest = (b'{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"ok","unknown":'
                + b'[' * 2000 + b'0' + b']' * 2000 + b'}]}]}}')
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        (tree / "hooks" / "hooks.json").write_bytes(manifest)
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "stale" and codex.calls == []


def test_deeply_nested_manifest_canonicalization_fails_closed(monkeypatch):
    data = uc._normalised(ROOT / "plugin" / "hooks" / "hooks.json")
    manifest = json.loads(data)
    nested = 0
    for _ in range(2000):
        nested = [nested]
    manifest["hooks"]["SessionEnd"][0]["hooks"][0]["unknownField"] = nested
    # Provide an already-decoded deep value so conservative validation does
    # not depend on the decoder's recursion limit.
    monkeypatch.setattr(uc.json, "loads", lambda *args, **kwargs: manifest)
    assert uc._codex_hook_manifest(data) is None


@pytest.mark.parametrize("invalid", ["deep-json", "escaped-surrogate"])
def test_approval_position_manifest_parser_fails_closed(cli, invalid):
    codex_home = _installed(cli, older=None)
    if invalid == "deep-json":
        manifest = (b'{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"ok","unknown":'
                    + b'[' * 2000 + b'0' + b']' * 2000 + b'}]}]}}')
    else:
        manifest = b'{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"\\ud800"}]}]}}'
    hooks = _cache(codex_home) / "hooks"
    (hooks / "hooks.json").write_bytes(manifest)
    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert uc._unapproved_plugin_handlers(hooks, config_text) is None


@pytest.mark.parametrize("invalid", ["deep-json", "escaped-surrogate", "invalid-utf8"])
def test_invalid_approval_manifest_cannot_report_current_without_a_reference(cli, monkeypatch, invalid):
    codex_home = _installed(cli, older=None)
    if invalid == "deep-json":
        manifest = (b'{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"ok","unknown":'
                    + b'[' * 2000 + b'0' + b']' * 2000 + b'}]}]}}')
    else:
        command = b"\\ud800" if invalid == "escaped-surrogate" else b"\xff"
        manifest = b'{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"' + command + b'"}]}]}}'
    (_cache(codex_home) / "hooks" / "hooks.json").write_bytes(manifest)
    monkeypatch.setattr(uc, "_daemon_scripts_dir", lambda digest: None)
    codex = FakeCodex(cli, monkeypatch)
    result = uc.check_codex_hooks(None, daemon_digest=uc.hooks_digest(ROOT / "plugin" / "hooks"))
    assert result["state"] == "unknown" and codex.calls == []
    assert "hooks.json" in result["detail"]


@pytest.mark.parametrize("depth,state", [(121, "refreshed"), (122, "stale")])
def test_manifest_nesting_matches_the_pinned_serde_boundary(cli, monkeypatch, tmp_path, depth, state):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    nested = 0
    for _ in range(depth):
        nested = [nested]
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, lambda m: m["hooks"]["SessionEnd"][0]["hooks"][0].update(unknownField=nested))
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    # Six enclosing manifest containers plus the unknown arrays: serde_json
    # 1.0.149 accepts 127 total, and rejects 128 (disposable pinned-version proof).
    assert result["state"] == state
    assert len(codex.calls) == (1 if state == "refreshed" else 0)


@pytest.mark.parametrize("error", [RecursionError, UnicodeError])
def test_manifest_encoder_exceptions_fail_closed(monkeypatch, error):
    data = uc._normalised(ROOT / "plugin" / "hooks" / "hooks.json")

    def failed_encoder(*args, **kwargs):
        raise error("encoder could not represent the manifest")

    monkeypatch.setattr(uc.json, "dumps", failed_encoder)
    assert uc._codex_hook_manifest(data) is None


@pytest.mark.parametrize("number,state", [("1e300", "refreshed"), ("1e400", "stale")])
def test_unknown_field_exponent_overflow_is_not_an_approved_definition(cli, monkeypatch, tmp_path, number, state):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, lambda m: m["hooks"]["SessionEnd"][0]["hooks"][0].update(unknownField={"nested": 12345}))
        path = tree / "hooks" / "hooks.json"
        path.write_bytes(path.read_bytes().replace(b"12345", number.encode("ascii")))
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == state
    assert len(codex.calls) == (1 if state == "refreshed" else 0)


@pytest.mark.parametrize("digits,state", [(300, "refreshed"), (400, "stale")])
def test_unknown_field_integer_range_matches_pinned_serde(cli, monkeypatch, tmp_path, digits, state):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, lambda m: m["hooks"]["SessionEnd"][0]["hooks"][0].update(unknownField=int("9" * digits)))
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == state
    assert len(codex.calls) == (1 if state == "refreshed" else 0)


def test_finite_large_integer_and_float_definitions_remain_distinct():
    manifest = json.loads(uc._normalised(ROOT / "plugin" / "hooks" / "hooks.json"))
    handler = manifest["hooks"]["SessionEnd"][0]["hooks"][0]
    handler["unknownField"] = 10 ** 300
    integer = json.dumps(manifest).encode("utf-8")
    handler["unknownField"] = 1e300
    floating = json.dumps(manifest).encode("utf-8")
    assert not uc._same_codex_hook_definitions(integer, floating)


@pytest.mark.parametrize("offered_timeout,state", [("0", "refreshed"), ("-0", "behind")])
def test_signed_zero_timeout_is_not_an_approved_branch(cli, monkeypatch, tmp_path, offered_timeout, state):
    codex_home = _installed(cli)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")
    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _timeout(tree, 0)
    codex = FakeCodex(cli, monkeypatch, offers=reference / "plugin")
    codex.remote_hooks = (reference / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8").replace(
        '"timeout": 0', f'"timeout": {offered_timeout}', 1)
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == state
    assert len(codex.calls) == (1 if state == "refreshed" else 0)


def test_valid_unicode_pairs_preserve_clamp_only_refresh(cli, monkeypatch, tmp_path):
    codex_home = _installed(cli, older=None)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "plugin", reference / "plugin")

    def add_unicode(manifest):
        handler = manifest["hooks"]["SessionEnd"][0]["hooks"][0]
        handler["command"] += " # \U0001f642"
        handler["unknownField"] = {"\U0001f642": ["caf\u00e9", "\U0001f642"]}

    for tree in (reference / "plugin", _clone(codex_home), _cache(codex_home)):
        _edit_manifest(tree, add_unicode)
    for tree in (_clone(codex_home), _cache(codex_home)):
        _timeout(tree, 10)
    served = tmp_path / "served"
    shutil.copytree(reference / "plugin", served)
    path = served / "hooks" / "hooks.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert b"\\ud83d\\ude42" in (reference / "plugin" / "hooks" / "hooks.json").read_bytes()
    codex = FakeCodex(cli, monkeypatch, offers=served)
    result = uc.check_codex_hooks(reference, refresh=True)
    assert result["state"] == "refreshed" and len(codex.calls) == 1
    assert uc.codex_reapproval_text(result) == ""
    assert uc.check_codex_hooks(reference)["state"] == "current"
