"""Manual Codex hook copies behind one stable launcher: approved once,
refreshed without asking again.

Codex approves a hook by its definition (the command, timeout, async and
statusMessage), not by the script the command runs: measured on Codex
0.158.0 on 2026-09-30, editing a script left ``currentHash`` unchanged and
editing the command changed it. Manual copies used to be content-addressed
directories named in the command itself, so every script change was a new
command, and every Codex user without the plugin approved the hooks again
after every update. The commands now name a launcher (``run.sh`` /
``run.ps1``) beside the bundles, which runs the bundle ``current`` names.
Bundles stay content-addressed and verified; a refresh writes the new
bundle, checks it and moves ``current``, leaving the approved commands byte
for byte alone.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_hook_setup_launcher", ROOT / "ops/setup-codex-hooks.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def report():
    return {"backups": []}


def _scripts(where: Path, tweak: str | None = None) -> Path:
    """A copy of the checkout's hook scripts; ``tweak`` changes one script."""
    shutil.copytree(ROOT / "plugin" / "hooks", where)
    if tweak:
        hook = where / "session-end.sh"
        hook.write_bytes(hook.read_bytes() + f"\n# {tweak}\n".encode())
    return where


def _ours(home: Path) -> list[dict]:
    """Our handlers in ``hooks.json``, shaped the way Codex lists them."""
    data = json.loads((home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    listed = []
    for key, event in setup.EVENTS.items():
        for g, group in enumerate(data.get(event, [])):
            for h, handler in enumerate(group["hooks"]):
                if "pseudolife" in handler.get("command", "") + handler.get("commandWindows", ""):
                    listed.append({"eventName": key, "command": handler["command"],
                                   "commandWindows": handler["commandWindows"], "timeout": handler["timeout"],
                                   "key": f"{home / 'hooks.json'}:{key}:{g}:{h}", "source": "user",
                                   "sourcePath": str(home / "hooks.json")})
    return listed


def _hooks_root(home: Path) -> Path:
    return home / "pseudolife" / "hooks"


def _current(home: Path) -> str:
    return (_hooks_root(home) / "current").read_text(encoding="utf-8").strip()


def test_manual_hooks_name_the_launcher_not_a_bundle(tmp_path):
    setup.install_manual(tmp_path, report())
    root = _hooks_root(tmp_path)
    current = _current(tmp_path)
    assert re.fullmatch(r"[a-f0-9]{20}", current)
    assert current == setup.bundle_digest(setup.bundle_bytes(ROOT / "plugin" / "hooks"))
    ours = _ours(tmp_path)
    assert len(ours) == sum(len(roles) for roles in setup.MANUAL_ROLES.values())
    for handler in ours:
        assert current not in handler["command"] and current not in handler["commandWindows"]
        assert "run.sh" in handler["command"] and "run.ps1" in handler["commandWindows"]
        assert handler["command"].startswith("env PSEUDOLIFE_CODEX_HOOK=1 bash ")
    for name, data in setup.LAUNCHERS.items():
        assert (root / name).read_bytes() == data
    # Codex lists the platform's command: POSIX the bash one, Windows the pwsh one.
    setup.vet_manual(ours, tmp_path)
    setup.vet_manual([dict(h, command=h["commandWindows"]) for h in ours], tmp_path)


def test_a_refresh_moves_the_bundle_and_leaves_the_approved_commands_alone(tmp_path):
    """The whole point: new scripts, the same commands, so Codex keeps its
    approval. The superseded bundle goes once nothing points at it."""
    setup.install_manual(tmp_path, report())
    approved = (tmp_path / "hooks.json").read_bytes()
    old = _current(tmp_path)
    newer = _scripts(tmp_path / "src", tweak="a later release")
    result = setup.refresh_manual(tmp_path, newer)
    new = _current(tmp_path)
    assert result["state"] == "refreshed" and result["bundle"] == new != old
    assert new == setup.bundle_digest(setup.bundle_bytes(newer))
    assert (tmp_path / "hooks.json").read_bytes() == approved
    assert not (_hooks_root(tmp_path) / old).exists()
    setup.vet_manual(_ours(tmp_path), tmp_path)
    assert setup.refresh_manual(tmp_path, newer)["state"] == "current"


def test_a_refresh_needs_the_launcher_install(tmp_path):
    """Copies whose commands still name a bundle cannot move without a new
    approval: the refresh says to run setup once more instead."""
    bundle = _hooks_root(tmp_path) / setup.bundle_digest(setup.bundle_bytes(ROOT / "plugin" / "hooks"))
    for name, data in setup.bundle_bytes(ROOT / "plugin" / "hooks").items():
        setup.atomic_write(bundle / name, data)
    legacy = setup.manual_definitions(bundle)
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": {
        event: [{"hooks": [legacy[role]]} for role in setup.MANUAL_ROLES[key]]
        for key, event in setup.EVENTS.items()}}), encoding="utf-8")
    assert not setup.launcher_installed(tmp_path)
    with pytest.raises(setup.SetupError, match="setup-codex-hooks.py"):
        setup.refresh_manual(tmp_path, _scripts(tmp_path / "src", tweak="newer"))
    assert not (_hooks_root(tmp_path) / "current").exists()


def test_setup_moves_bundle_named_commands_to_the_launcher(tmp_path):
    """The one last approval: setup rewrites the old bundle-named commands to
    the launcher's and keeps unrelated hooks. The old bundle stays: a Codex
    session started before may still run the commands that name it."""
    old_scripts = _scripts(tmp_path / "old", tweak="an older release")
    bundle = _hooks_root(tmp_path) / setup.bundle_digest(setup.bundle_bytes(old_scripts))
    for name, data in setup.bundle_bytes(old_scripts).items():
        setup.atomic_write(bundle / name, data)
    legacy = setup.manual_definitions(bundle)
    other = {"hooks": [{"type": "command", "command": "echo unrelated"}]}
    hooks = {event: [{"hooks": [legacy[role]]} for role in setup.MANUAL_ROLES[key]]
             for key, event in setup.EVENTS.items()}
    hooks["SessionStart"].insert(0, other)
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    listed = [h for h in _ours(tmp_path)]
    assert all(setup.owned_manual(dict(h), tmp_path) for h in listed)
    setup.install_manual(tmp_path, report())
    data = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert data["SessionStart"][0] == other
    commands = [h["command"] for groups in data.values() for g in groups for h in g["hooks"]]
    assert not any(bundle.name in c for c in commands)
    assert len(_ours(tmp_path)) == sum(len(roles) for roles in setup.MANUAL_ROLES.values())
    assert setup.launcher_installed(tmp_path)
    assert all(setup.owned_manual(dict(h), tmp_path) for h in _ours(tmp_path))
    assert bundle.is_dir() and setup.bundle_digest(setup.bundle_bytes(bundle)) == bundle.name
    # later refreshes leave it too, and prune only what the launcher ran
    setup.refresh_manual(tmp_path, _scripts(tmp_path / "newer", tweak="newer"))
    setup.refresh_manual(tmp_path, _scripts(tmp_path / "newest", tweak="newest"))
    names = sorted(p.name for p in _hooks_root(tmp_path).iterdir() if p.is_dir())
    assert names == sorted([bundle.name, _current(tmp_path)])


@pytest.mark.parametrize("damage", ["launcher", "bundle", "pointer", "missing-bundle"])
def test_verification_refuses_a_changed_launcher_bundle_or_pointer(tmp_path, damage):
    """What runs is still exactly what setup wrote: the launchers byte for
    byte, the bundle by its content-derived name, the pointer by shape."""
    setup.install_manual(tmp_path, report())
    root = _hooks_root(tmp_path)
    current = _current(tmp_path)
    if damage == "launcher":
        (root / "run.sh").write_text("echo changed\n", encoding="utf-8")
    elif damage == "bundle":
        (root / current / "session-start.sh").write_text("echo changed\n", encoding="utf-8")
    elif damage == "pointer":
        (root / "current").write_text("../../elsewhere\n", encoding="utf-8")
    else:
        shutil.rmtree(root / current)
    with pytest.raises(setup.SetupError):
        setup.vet_manual(_ours(tmp_path), tmp_path)
    with pytest.raises(setup.SetupError):
        setup.refresh_manual(tmp_path, _scripts(tmp_path / "src", tweak="newer"))


def test_an_old_bundle_the_checkout_matches_is_never_pruned(tmp_path):
    """Review finding 2026-09-30: when the old bundle-named install already
    ran the checkout's scripts, migration pointed at that very directory
    and recorded it as a launcher bundle, so the next refresh deleted what
    a still-running session's commands name. Only a bundle a call created
    is ever recorded."""
    files = setup.bundle_bytes(ROOT / "plugin" / "hooks")
    bundle = _hooks_root(tmp_path) / setup.bundle_digest(files)
    for name, data in files.items():
        setup.atomic_write(bundle / name, data)
    legacy = setup.manual_definitions(bundle)
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": {
        event: [{"hooks": [legacy[role]]} for role in setup.MANUAL_ROLES[key]]
        for key, event in setup.EVENTS.items()}}), encoding="utf-8")
    setup.install_manual(tmp_path, report())
    assert _current(tmp_path) == bundle.name
    setup.refresh_manual(tmp_path, _scripts(tmp_path / "newer", tweak="newer"))
    assert bundle.is_dir() and setup.bundle_digest(setup.bundle_bytes(bundle)) == bundle.name


@pytest.mark.parametrize("damage", ["launcher", "pointer", "missing-pointer"])
def test_an_approved_setup_repairs_the_launcher_and_pointer(tmp_path, damage):
    """Setup with approval vets before it installs (complete=False); a
    broken launcher or pointer must not stop the step that rewrites them.
    A modified bundle still does (test_verification_refuses_...)."""
    setup.install_manual(tmp_path, report())
    root = _hooks_root(tmp_path)
    if damage == "launcher":
        (root / "run.ps1").write_text("# edited\n", encoding="utf-8")
    elif damage == "pointer":
        (root / "current").write_text("not-a-bundle\n", encoding="utf-8")
    else:
        (root / "current").unlink()
    setup.vet_manual(_ours(tmp_path), tmp_path, complete=False)
    setup.install_manual(tmp_path, report())
    setup.vet_manual(_ours(tmp_path), tmp_path)


def test_a_launcher_from_an_earlier_release_is_rewritten_by_a_refresh(tmp_path, monkeypatch):
    """The first release that edits run.sh or run.ps1 must not strand every
    install: a launcher some release shipped is replaced by a refresh (Codex
    does not hash it); only one no release shipped is refused."""
    setup.install_manual(tmp_path, report())
    root = _hooks_root(tmp_path)
    shipped_before = b"# an earlier release's run.sh\n"
    (root / "run.sh").write_bytes(shipped_before)
    monkeypatch.setitem(setup.PREVIOUS_LAUNCHERS, "run.sh",
                        setup.PREVIOUS_LAUNCHERS.get("run.sh", frozenset())
                        | {hashlib.sha256(shipped_before).hexdigest()})
    setup.refresh_manual(tmp_path, _scripts(tmp_path / "newer", tweak="newer"))
    assert (root / "run.sh").read_bytes() == setup.LAUNCHERS["run.sh"]


def test_every_shipped_launcher_is_known_to_later_releases():
    """Changing a launcher? Add the previous bytes' sha256 to
    PREVIOUS_LAUNCHERS in ops/setup-codex-hooks.py and the new ones here,
    or every existing manual install stops refreshing."""
    assert {name: hashlib.sha256(data).hexdigest() for name, data in setup.LAUNCHERS.items()} == {
        "run.sh": "23dc357bbddc1b4687f9ddfe54f8591b03fbc6e1b19bd41c3aeb2834b0f0a1ce",
        "run.ps1": "0ff3ff20c091588aa5b8bf2d2f6859c0ade5a922ba83f9ac8b824415f09d2bc7",
    }


# ── the launchers themselves ───────────────────────────────────────────────

def _bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    for candidate in ([Path(git).parents[1] / "bin/bash.exe", Path(git).parents[2] / "bin/bash.exe"] if git else []):
        if candidate.is_file():
            return str(candidate)
    return None


def _fake_bundle(home: Path, name: str = "b" * 20) -> Path:
    root = _hooks_root(home)
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "session-start.sh").write_bytes(
        b'printf "args=%s stdin=%s codex=%s dir=%s" "$*" "$(cat)" "$PSEUDOLIFE_CODEX_HOOK" '
        b'"$(basename "$(dirname "$0")")"\n')
    (bundle / "lifecycle.ps1").write_text(
        "param([string]$Event)\n$in = [Console]::In.ReadToEnd()\n"
        "Write-Output \"event=$Event stdin=$in dir=$(Split-Path -Leaf $PSScriptRoot)\"\nexit 3\n",
        encoding="utf-8")
    for launcher, data in setup.LAUNCHERS.items():
        (root / launcher).write_bytes(data)
    (root / "current").write_text(name + "\n", encoding="utf-8")
    return root


def test_the_bash_launcher_runs_the_current_bundle_with_its_arguments_and_input(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("Bash is not installed")
    root = _fake_bundle(tmp_path)
    run = subprocess.run([bash, (root / "run.sh").as_posix(), "session-start.sh", "memory-policy"],
                         input=b'{"session_id":"s"}', capture_output=True, timeout=60,
                         env=dict(os.environ, PSEUDOLIFE_CODEX_HOOK="1"))
    assert run.returncode == 0, run.stderr
    assert run.stdout.decode() == 'args=memory-policy stdin={"session_id":"s"} codex=1 dir=' + "b" * 20
    # a pointer that is not a bundle name, or a script that is not a hook,
    # runs nothing and says so: a silent exit would drop the briefing unseen
    for pointer, script in (("../x", "session-start.sh"), ("b" * 20, "../../evil.sh")):
        (root / "current").write_text(pointer, encoding="utf-8")
        run = subprocess.run([bash, (root / "run.sh").as_posix(), script], input=b"",
                             capture_output=True, timeout=60)
        assert run.returncode == 1 and run.stdout == b"" and b"PseudoLife" in run.stderr


def test_the_powershell_launcher_runs_the_current_bundle_with_its_event_input_and_exit_code(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    root = _fake_bundle(tmp_path)
    run = subprocess.run([pwsh, "-NoProfile", "-File", str(root / "run.ps1"), "-Event", "SessionStart"],
                         input=b'{"session_id":"s"}', capture_output=True, timeout=60)
    assert run.returncode == 3, run.stderr
    assert run.stdout.decode().strip() == 'event=SessionStart stdin={"session_id":"s"} dir=' + "b" * 20
    (root / "current").write_text("..\\x", encoding="utf-8")
    run = subprocess.run([pwsh, "-NoProfile", "-File", str(root / "run.ps1"), "-Event", "SessionStart"],
                         input=b"", capture_output=True, timeout=60)
    assert run.returncode == 1 and run.stdout.strip() == b"" and b"PseudoLife" in run.stderr


def test_real_codex_keeps_trusting_the_manual_hooks_across_a_refresh(tmp_path, monkeypatch):
    """The claim itself, against the Codex on this machine: approve the
    manual hooks once, refresh them to other scripts, and Codex still lists
    every one as trusted, with the same hash. Only ``hooks/list`` and a
    config write in a throwaway Codex home; no model, no thread."""
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("Codex is not installed")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    setup.install_manual(home, report(), scripts=_scripts(tmp_path / "older", tweak="older"))
    with setup.codex(codex, home, tmp_path) as client:
        config, hooks = setup.inventory(client, tmp_path)
        _, ours, _ = setup.select_hooks(hooks, home, "manual")
        assert setup.complete_set(ours, "manual")
        setup.trust_hooks(client, config, ours, home, report())
    with setup.codex(codex, home, tmp_path) as client:
        _, hooks = setup.inventory(client, tmp_path)
    before = {h["key"]: (h["trustStatus"], h["currentHash"]) for h in setup.select_hooks(hooks, home, "manual")[1]}
    assert before and all(status == "trusted" for status, _ in before.values())
    old = _current(home)
    assert setup.refresh_manual(home, _scripts(tmp_path / "newer", tweak="newer"))["state"] == "refreshed"
    assert _current(home) != old
    with setup.codex(codex, home, tmp_path) as client:
        _, hooks = setup.inventory(client, tmp_path)
    ours = setup.select_hooks(hooks, home, "manual")[1]
    after = {h["key"]: (h["trustStatus"], h["currentHash"]) for h in ours}
    assert after == before
    setup.vet_manual(ours, home)


# ── the plugin's hooks.json: raw definition review guard ────────────────────

# sha256 over raw plugin handler fields, in order. Codex hashes normalized
# definitions: SessionEnd/Interrupt timeouts normalize before hashing, so a
# clamp-only 10 -> 3 change leaves trust unchanged (openai/codex b741e480,
# discovery.rs, confirmed 2026-10-03). Scripts are not hashed. When a raw
# definition changes, update this review pin, check Codex's normalized trust
# behavior and describe it in the CHANGELOG; approve only if Codex asks.
CODEX_APPROVED_HOOKS = "639c4367cc121ce9d7fb755b0a514559949ab41b5803c4ba0f5b7bc10332b265"


@pytest.mark.parametrize("event, cap", [
    ("UserPromptSubmit", None), ("SessionStart", None), ("SessionEnd", 3),
    ("Stop", None), ("SubagentStart", None), ("SubagentStop", None), ("PreToolUse", None),
])
def test_plugin_handlers_fit_codex_event_timeout_caps(event, cap):
    """Codex clamps only SessionEnd/Interrupt, not the other events.

    Confirmed 2026-10-03 in openai/codex b741e480, hooks/src/engine/
    discovery.rs normalize_command_hook: other events use the configured
    value (600 seconds by default), with no upper cap.
    """
    manifest = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    for group in manifest[event]:
        for handler in group["hooks"]:
            assert handler["timeout"] >= 1
            if cap is not None:
                assert handler["timeout"] <= cap


def _codex_approved_fields() -> list:
    manifest = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    return [[event, g, h, group.get("matcher"),
             handler.get("command"), handler.get("commandWindows"), handler.get("timeout"),
             handler.get("async"), handler.get("statusMessage")]
            for event, groups in manifest.items()
            for g, group in enumerate(groups)
            for h, handler in enumerate(group["hooks"])]


def test_the_plugins_codex_approved_hook_definitions_are_pinned():
    digest = hashlib.sha256(json.dumps(_codex_approved_fields(), sort_keys=True).encode()).hexdigest()
    assert digest == CODEX_APPROVED_HOOKS, (
        "plugin/hooks/hooks.json changed raw handler fields (command, commandWindows, timeout, async, "
        "statusMessage, matcher) or added a handler. Codex hashes normalized definitions, so a raw change "
        "may preserve trust. Prefer putting behavior in a script, which Codex does not hash. If needed, "
        f"set CODEX_APPROVED_HOOKS = {digest!r}, check Codex's normalized trust behavior and describe it "
        "in the CHANGELOG; approve only if Codex asks.")
