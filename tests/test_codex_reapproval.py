"""The Codex re-approval steps: printed by every update path when and only
when Codex's hook copy differs from the current scripts.

Codex trusts hooks by hash and asks again when a script changes, so no
update can finish this for the user. Until 2026-09-29 the update printed
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
                  {"state": "unknown"}):
        assert uc.codex_reapproval_text(codex) == ""


def test_the_complete_steps_name_the_changed_files_the_approval_and_the_check():
    text = uc.codex_reapproval_text({"state": "stale", "changed_files": ["session-start.sh", "stop-wake.sh"]})
    assert text.startswith("Codex: its hook copy needs re-approval (changed: session-start.sh, stop-wake.sh)")
    assert "/hooks" in text
    assert "python ops/setup-codex-hooks.py --source plugin --trust yes" in text
    assert "--codex-hook-trust yes" in text and "-CodexHookTrust yes" in text
    assert "without the memory briefing" in text
    assert uc.CODEX_REAPPROVAL_VERIFY in text and "codex_hooks = current" in text
    assert text.count("\n") == 3                                  # one head line, three numbered steps


def test_the_text_says_what_it_knows_when_files_cannot_be_named():
    assert "new handler positions" in uc.codex_reapproval_text({"state": "needs-approval", "changed_files": []})
    assert "differs from the current scripts" in uc.codex_reapproval_text({"state": "stale", "changed_files": None})
    assert "no marketplace clone" in uc.codex_reapproval_text({"state": "plugin-managed"})


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


def test_check_names_the_files_that_differ_from_the_checkout(cli):
    _clone(cli, change="session-end.sh")
    result = uc.check_codex_hooks(ROOT)
    assert result["state"] == "stale" and result["changed_files"] == ["session-end.sh"]
    assert "session-end.sh" in uc.codex_reapproval_text(result)


def test_check_names_the_files_through_the_plugin_cache_without_a_checkout(cli):
    """In release mode there is no checkout: the daemon's scripts are read
    from the Claude plugin cache when its digest is the daemon's."""
    _clone(cli, change="user-prompt-submit.sh")
    plugins = cli.home / ".claude" / "plugins"
    cache = plugins / "cache" / "pseudolife-memory"
    shutil.copytree(ROOT / "plugin", cache)
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {
        uc.PLUGIN_ID: [{"version": "0.15.0", "installPath": str(cache)}]}}), encoding="utf-8")
    digest = hooks_digest(ROOT / "plugin" / "hooks")
    result = uc.check_codex_hooks(None, daemon_digest=digest)
    assert result["state"] == "stale" and result["changed_files"] == ["user-prompt-submit.sh"]
    # a cache that is not the daemon's scripts names nothing
    (cache / "hooks" / "session-end.sh").write_bytes(b"# different\n")
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
    _clone(cli, change="stop-wake.sh")
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    out = capsys.readouterr().out
    assert "[!] Codex hooks" in out
    assert out.index("[!] Codex hooks") < out.index("re-approval (changed: stop-wake.sh)")
    shutil.rmtree(cli.home / "codex" / ".tmp")
    _clone(cli)
    assert uc.main(["--repo", str(ROOT), "--only", "codex"]) == 0
    assert "re-approval" not in capsys.readouterr().out


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
