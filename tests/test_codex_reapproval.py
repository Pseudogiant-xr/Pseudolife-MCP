"""The Codex re-approval steps: printed by every update path when and only
when Codex would ask to approve PseudoLife's hooks again.

Codex approves a hook by its definition (the command, timeout, async and
statusMessage), not by the script it runs (measured on Codex 0.158.0,
2026-09-30). A plugin copy whose scripts changed but whose hooks.json did
not is only behind: its approvals carry over. Manual copies run through a
launcher whose commands never change, and the update refreshes them itself.
Until 2026-09-29 the update printed
one generic line for any non-current state; now it prints the complete,
copy-pasteable steps (which files changed, the ``/hooks`` approval or
``--trust yes`` for unattended installs, what is off until then, and the
``doctor`` line that verifies it), and nothing at all when the hooks did
not change.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pseudolife_memory import client_updates as uc, update_cli as up  # noqa: E402
from pseudolife_memory.plugin_hooks import hooks_digest  # noqa: E402
from tests.test_update_clients import _codex_plugin_config, _plugin_handler_keys, cli  # noqa: E402,F401
from tests.test_update_cli import World, _project, clients, world  # noqa: E402,F401


def _clone(cli, *, change: str | None = None) -> Path:
    codex_home = _codex_plugin_config(cli, _plugin_handler_keys())
    clone = codex_home / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin"
    shutil.copytree(ROOT / "plugin", clone)
    if change:
        hook = clone / "hooks" / change
        hook.write_bytes(hook.read_bytes() + b"\n# older\n")
    return clone


# ── the text ────────────────────────────────────────────────────────────────

def test_nothing_is_said_when_the_hooks_did_not_change():
    for codex in (None, {}, {"state": "current"}, {"state": "not-configured"}, {"state": "bundle-present"},
                  {"state": "unknown"}, {"state": "plugin-managed"}):      # no clone at all is not "changed"
        assert uc.codex_reapproval_text(codex) == ""


def test_the_complete_steps_name_the_changed_files_the_approval_and_the_check():
    text = uc.codex_reapproval_text({"state": "stale", "changed_files": ["session-start.sh", "stop-wake.sh"]})
    assert text.startswith("Codex: its hook copy needs re-approval (changed: session-start.sh, stop-wake.sh)")
    assert "/hooks" in text
    assert "python ops/setup-codex-hooks.py --source plugin --trust yes" in text
    assert "--codex-hook-trust yes" in text and "-CodexHookTrust yes" in text
    assert "without the memory briefing" in text and "Stop-hook park gate" in text
    assert uc.CODEX_REAPPROVAL_VERIFY in text and "codex_hooks = current" in text
    assert "does not pull one" in text                             # setup-codex-hooks approves; Codex refreshes
    assert text.count("\n") == 3                                  # one head line, three numbered steps


def test_manual_copies_get_the_one_time_switch_to_the_launcher():
    text = uc.codex_reapproval_text({"state": "stale", "source": "manual"})
    assert "--source manual" in text and "--source plugin" not in text and "/hooks" not in text
    assert "last approval" in text and "codex_hooks = bundle-present" in text


def test_the_text_says_what_it_knows_when_files_cannot_be_named():
    assert "new handler positions" in uc.codex_reapproval_text({"state": "needs-approval", "changed_files": []})
    assert "differs from the current scripts" in uc.codex_reapproval_text({"state": "stale", "changed_files": None})


# ── which files changed ─────────────────────────────────────────────────────

def test_changed_hook_files_compares_content_crlf_insensitively(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    shutil.copytree(ROOT / "plugin" / "hooks", a)
    shutil.copytree(ROOT / "plugin" / "hooks", b)
    assert uc.changed_hook_files(a, b) == []
    (a / "session-end.sh").write_bytes((a / "session-end.sh").read_bytes().replace(b"\n", b"\r\n"))
    assert uc.changed_hook_files(a, b) == []                      # line endings are not a change
    (a / "stop-wake.sh").write_bytes(b"# other\n")
    (b / "hooks.json").unlink()
    assert uc.changed_hook_files(a, b) == ["stop-wake.sh", "hooks.json"]
    assert uc.changed_hook_files(a, tmp_path / "missing") is None


def test_a_changed_hooks_json_needs_approval_and_is_named(cli):
    _clone(cli, change="hooks.json")
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "stale" and result["changed_files"] == ["hooks.json"]
    assert "hooks.json" in uc.codex_reapproval_text(result)


def test_changed_scripts_alone_are_behind_and_keep_their_approval(cli):
    """Codex does not hash the scripts: a plugin copy whose scripts changed
    and whose hooks.json did not needs a plugin update, never an approval."""
    _clone(cli, change="session-end.sh")
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "behind" and result["changed_files"] == ["session-end.sh"]
    assert uc.codex_reapproval_text(result) == ""
    assert uc._marker(result["state"]) != "[!]"
    assert "approvals carry over" in result["detail"]


def test_check_names_the_files_through_the_plugin_cache_without_a_checkout(cli):
    """In release mode there is no checkout: the daemon's scripts are read
    from the Claude plugin cache when its digest is the daemon's."""
    _clone(cli, change="hooks.json")
    plugins = cli.home / ".claude" / "plugins"
    cache = plugins / "cache" / "pseudolife-memory"
    shutil.copytree(ROOT / "plugin", cache)
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {
        uc.PLUGIN_ID: [{"version": "0.15.0", "installPath": str(cache)}]}}), encoding="utf-8")
    digest = hooks_digest(ROOT / "plugin" / "hooks")
    result = uc.check_codex_hooks(None, daemon_digest=digest)
    assert result["state"] == "stale" and result["changed_files"] == ["hooks.json"]
    # a cache that is not the daemon's scripts names nothing, so with older
    # scripts in the clone whether hooks.json changed is unknown: the
    # approval steps stay
    (cache / "hooks" / "session-end.sh").write_bytes(b"# different\n")
    clone_hook = cli.home / "codex" / ".tmp" / "marketplaces" / "pseudolife-mcp" / "plugin" / "hooks" / "session-end.sh"
    clone_hook.write_bytes(clone_hook.read_bytes() + b"\n# older\n")
    result = uc.check_codex_hooks(None, daemon_digest=digest)
    assert result["state"] == "stale" and result["changed_files"] is None


def test_a_current_clone_carries_no_changed_files(cli):
    _clone(cli)
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "current" and "changed_files" not in result
    assert uc.codex_reapproval_text(result) == ""


# ── every update path ───────────────────────────────────────────────────────

def test_update_prints_the_steps_only_when_the_copy_differs(world, tmp_path, monkeypatch, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    codex = {"state": "current", "detail": "ok"}
    monkeypatch.setattr(uc, "run_steps", lambda steps, **kw: {
        "shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "current:0.15.1", "detail": "ok"},
        "codex": codex, "ok": True})
    assert up.main(["--health-delay-ms", "1"]) == 0
    out = capsys.readouterr().out
    assert "re-approval" not in out and "/hooks" not in out
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    codex.update({"state": "stale", "changed_files": ["session-start.sh"]})
    assert up.main(["--health-delay-ms", "1", "--reinstall"]) == 0
    out = capsys.readouterr().out
    assert "re-approval (changed: session-start.sh)" in out and "/hooks" in out and "--trust yes" in out
    assert "codex_hooks = current" in out


def test_the_json_report_carries_the_steps(world, tmp_path, monkeypatch, capsys):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    monkeypatch.setattr(uc, "run_steps", lambda steps, **kw: {
        "shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "current:0.15.1", "detail": "ok"},
        "codex": {"state": "needs-approval", "changed_files": []}, "ok": True})
    assert up.main(["--health-delay-ms", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["codex_reapproval"].startswith("Codex: its hook copy needs re-approval (new handler positions")


def test_update_clients_prints_the_steps_after_the_ladder(cli, capsys):
    _clone(cli, change="hooks.json")
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    out = capsys.readouterr().out
    assert "[!] Codex hooks" in out
    assert out.index("[!] Codex hooks") < out.index("re-approval (changed: hooks.json)")
    shutil.rmtree(cli.home / "codex" / ".tmp")
    _clone(cli, change="stop-wake.sh")
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    out = capsys.readouterr().out
    assert "re-approval" not in out and "[!] Codex hooks" not in out
    shutil.rmtree(cli.home / "codex" / ".tmp")
    _clone(cli)
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    assert "re-approval" not in capsys.readouterr().out


def test_a_daemon_only_update_says_when_the_hooks_changed(world, tmp_path, monkeypatch, capsys):
    """ops/update.ps1 without -All (and update --daemon-only) moves no
    client: when the daemon's scripts changed, one line says the client
    side and the Codex steps are still to do; nothing when they did not."""
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0", "hooks_digest": "a" * 64},
                    {"status": "ok", "version": "0.15.1", "hooks_digest": "b" * 64}]
    assert up.main(["--daemon-only", "--health-delay-ms", "1"]) == 0
    out = capsys.readouterr().out
    assert "hook scripts changed with this update and the client side was not moved" in out
    assert "--clients-only --tag" in out
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0", "hooks_digest": "a" * 64},
                    {"status": "ok", "version": "0.15.1", "hooks_digest": "a" * 64}]
    assert up.main(["--daemon-only", "--health-delay-ms", "1"]) == 0
    assert "hook scripts changed" not in capsys.readouterr().out


def test_the_result_file_gets_the_codex_steps_beside_it(world, tmp_path, monkeypatch):
    _project(world, tmp_path)
    world.health = [{"status": "ok", "version": "0.15.0"}, {"status": "ok", "version": "0.15.1"}]
    monkeypatch.setattr(uc, "run_steps", lambda steps, **kw: {
        "shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "current:0.15.1", "detail": "ok"},
        "codex": {"state": "stale", "changed_files": ["stop-wake.sh"], "detail": "stale"}, "ok": True})
    result = tmp_path / "state" / "update-clients.0.15.1.result"
    assert up.main(["--clients-only", "--tag", "0.15.1", "--result-file", str(result)]) == 0
    assert result.read_text(encoding="utf-8").strip() == "0"
    steps = result.with_suffix(".codex").read_text(encoding="utf-8")
    assert steps.startswith("Codex: its hook copy needs re-approval (changed: stop-wake.sh)")
    monkeypatch.setattr(uc, "run_steps", lambda steps, **kw: {
        "shim": {"state": "installed:0.15.1", "detail": "ok"}, "plugin": {"state": "current:0.15.1", "detail": "ok"},
        "codex": {"state": "current", "detail": "ok"}, "ok": True})
    world.health = [{"status": "ok", "version": "0.15.1"}]
    assert up.main(["--clients-only", "--tag", "0.15.1", "--result-file", str(result)]) == 0
    assert not result.with_suffix(".codex").exists()             # the steps file goes when nothing is due


def test_doctor_reports_the_codex_hooks_line(monkeypatch, capsys):
    from tests.test_version_handshake import _run_doctor
    from pseudolife_memory import doctor_cli, __version__

    monkeypatch.setattr(doctor_cli, "_board_line", lambda timeout: "off - test")
    monkeypatch.setattr(uc, "check_codex_hooks",
                        lambda repo, daemon_digest=None: {"state": "stale", "changed_files": ["session-start.sh"]})
    _, report = _run_doctor(monkeypatch, capsys, {"status": "ok", "version": __version__, "hooks_digest": "d" * 64})
    assert report["codex_hooks"] == "stale (changed: session-start.sh)"
    monkeypatch.setattr(uc, "check_codex_hooks", lambda repo, daemon_digest=None: {"state": "current"})
    _, report = _run_doctor(monkeypatch, capsys, {"status": "ok", "version": __version__})
    assert report["codex_hooks"] == "current"


# ── manual copies behind the launcher ───────────────────────────────────────

def _setup_module():
    return uc._load_from_checkout(ROOT, "ops/setup-codex-hooks.py", "codex_hook_setup_reapproval")


def _older_scripts(tmp_path: Path) -> Path:
    older = tmp_path / "older-hooks"
    shutil.copytree(ROOT / "plugin" / "hooks", older)
    (older / "session-end.sh").write_bytes((older / "session-end.sh").read_bytes() + b"\n# older\n")
    return older


def test_the_update_refreshes_launcher_copies_without_an_approval(cli, tmp_path, capsys):
    """A Codex user without the plugin: the update moves the manual copy to
    the checkout's scripts itself, and the approved commands stay as they
    were, so there is nothing to approve."""
    setup = _setup_module()
    codex_home = cli.home / "codex"
    setup.install_manual(codex_home, {"backups": []}, scripts=_older_scripts(tmp_path))
    approved = (codex_home / "hooks.json").read_bytes()
    checkout = setup.bundle_digest(setup.bundle_bytes(ROOT / "plugin" / "hooks"))
    pointer = codex_home / "pseudolife" / "hooks" / "current"
    # reading does not write: doctor and a plain check see the copy as behind
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "behind" and pointer.read_text(encoding="utf-8").strip() != checkout
    assert uc.check_codex_hooks(None)["state"] == "bundle-present"
    # the update step refreshes
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    out = capsys.readouterr().out
    assert "[x] Codex hooks" in out and "re-approval" not in out
    assert pointer.read_text(encoding="utf-8").strip() == checkout
    assert (codex_home / "hooks.json").read_bytes() == approved
    assert uc.check_codex_hooks(ROOT)["state"] == "current"


def test_bundle_named_manual_copies_get_the_one_time_switch(cli, tmp_path):
    """Copies from before the launcher name a bundle in their commands; the
    update cannot move them without an approval, so it says so once."""
    setup = _setup_module()
    codex_home = cli.home / "codex"
    older = _older_scripts(tmp_path)
    bundle = codex_home / "pseudolife" / "hooks" / setup.bundle_digest(setup.bundle_bytes(older))
    for name, data in setup.bundle_bytes(older).items():
        setup.atomic_write(bundle / name, data)
    legacy = setup.manual_definitions(bundle)
    (codex_home / "hooks.json").write_text(json.dumps({"hooks": {
        event: [{"hooks": [legacy[role]]} for role in setup.MANUAL_ROLES[key]]
        for key, event in setup.EVENTS.items()}}), encoding="utf-8")
    result = uc.run_steps(["codex"], repo=ROOT, source=str(ROOT))["codex"]
    assert result["state"] == "stale" and result["source"] == "manual"
    assert "last approval" in uc.codex_reapproval_text(result)


def test_a_current_pointer_is_verified_before_it_reads_current(cli, tmp_path):
    """Review finding: with `current` already the checkout's bundle the
    check returned current without verifying anything."""
    setup = _setup_module()
    codex_home = cli.home / "codex"
    setup.install_manual(codex_home, {"backups": []})
    (codex_home / "pseudolife" / "hooks" / "run.sh").write_bytes(b"echo tampered\n")
    assert uc.check_codex_hooks(ROOT)["state"] == "failed"


def test_a_refresh_that_cannot_write_is_a_failed_step_not_a_crash(cli, tmp_path, monkeypatch):
    setup = _setup_module()
    codex_home = cli.home / "codex"
    setup.install_manual(codex_home, {"backups": []}, scripts=_older_scripts(tmp_path))
    real = uc._load_from_checkout

    def loaded(repo, relative, name):
        module = real(repo, relative, name)
        if relative == "ops/setup-codex-hooks.py":
            def refuse(*a, **k):
                raise PermissionError("locked")
            module.refresh_manual = refuse
        return module
    monkeypatch.setattr(uc, "_load_from_checkout", loaded)
    result = uc.check_codex_hooks(ROOT, refresh=True)
    assert result["state"] == "failed" and "locked" in result["detail"]


def test_the_codex_home_is_resolved_as_setup_resolves_it(cli, tmp_path, monkeypatch):
    """Setup writes hooks.json commands under the resolved Codex home; a
    relative or linked CODEX_HOME must name the same one here, or a
    launcher install reads as stale forever (review finding)."""
    setup = _setup_module()
    codex_home = (cli.home / "codex").resolve()
    setup.install_manual(codex_home, {"backups": []})
    monkeypatch.chdir(cli.home)
    monkeypatch.setenv("CODEX_HOME", "codex")
    assert uc.check_codex_hooks(ROOT)["state"] == "current"


def test_an_older_checkout_without_the_launcher_reads_as_before(cli, tmp_path, monkeypatch):
    """A newer client_updates beside an older checkout's setup script (a
    rollback) must not crash on the missing launcher functions."""
    setup = _setup_module()
    codex_home = cli.home / "codex"
    setup.install_manual(codex_home, {"backups": []})
    real = uc._load_from_checkout

    def older(repo, relative, name):
        module = real(repo, relative, name)
        if relative == "ops/setup-codex-hooks.py":
            del module.launcher_installed
        return module
    monkeypatch.setattr(uc, "_load_from_checkout", older)
    assert uc.check_codex_hooks(ROOT, refresh=True)["state"] in ("bundle-present", "stale")
