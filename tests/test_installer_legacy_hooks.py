"""Upgrading to the Claude Code plugin removes the hooks an earlier install
wrote to ~/.claude/settings.json, with consent.

Since 2026-09-21 the installers add the pseudolife-memory plugin, and section
9 stops writing settings.json hooks once the plugin is recorded. Nothing
removed the ones an earlier install had written, so an upgraded user got
every session-start context twice and the discipline line twice per turn.
``install-hook --remove-legacy`` removes exactly the command strings the
installers shipped (never a substring match: that would delete a user's own
compound command), only while the plugin is installed and enabled for every
project, after a timestamped backup. The installers ask first, or act on
``--claude-legacy-hooks``. These tests run install-hook directly against
disposable settings files, and the installers' consent block extracted
between its markers.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest

from tests.test_installer_existing_upgrade import (
    _bash_fixture_path, _bash_variants, _between, _fixture_env,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"


def _line(pattern: str, rel: str) -> str:
    return re.search(pattern, (ROOT / rel).read_text(encoding="utf-8"), re.M)[1]


DISCIPLINE = _line(r'^DISCIPLINE_LINE="(.*)"$', "ops/install-hook.sh")
COORDINATION = _line(r'^COORDINATION_LINE="(.*)"$', "ops/install-hook.sh")
# The discipline line as shipped 2026-08-28 .. 2026-09-05 (a13de5e1), before
# "with used_ids" was added. Installs from that window still carry it.
OLD_DISCIPLINE = (
    "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, "
    "or a PR -> memory_search + memory_lesson_search the target area FIRST, "
    "then compare memory against the files and correct drift both ways (fix "
    "stale memory via memory_fact_set + memory_outcome; treat memory-vs-file "
    "mismatches as review findings). Status or in-progress questions -> "
    "memory_search (include sources: status) before or alongside git. "
    "Starting work in a new area -> memory_search + memory_lesson_search "
    "first. Launching or finishing long-running work -> memory_store a status "
    "entry. Outcome landed -> memory_outcome.")

BRIEFING = "pseudolife-mcp briefing --hook-json"
DOCKER_BRIEFING = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
# The coordination check-in: an unconditional echo from 2026-09-24, then
# (#367, 2026-09-25) the briefing command with --coordination.
COORD_CMD = f"echo '{COORDINATION}'"
GATED_COORD = BRIEFING + " --coordination"
DOCKER_GATED_COORD = DOCKER_BRIEFING + " --coordination"
UPS_CMD = f"echo '{DISCIPLINE}'"
OLD_UPS_CMD = f"echo '{OLD_DISCIPLINE}'"
# Since 2026-09-26 the per-turn hook is the memory-change note, not the echo.
# The Docker tier's needs -i: the hook's session id arrives on stdin.
PROMPT_CMD = "pseudolife-mcp prompt-hook"
DOCKER_PROMPT_CMD = "docker exec -i pseudolife-mcp-daemon pseudolife-mcp prompt-hook"
EPISODE_START = "pseudolife-mcp episode-start"
EPISODE_END = "pseudolife-mcp episode-end"


def _hook(command: str, **extra) -> dict:
    return {"type": "command", "command": command, **extra}


USER_START = _hook("echo user-start")
USER_SHARED = _hook("echo user-shared")
USER_PROMPT = _hook("echo user-prompt")
USER_TEXT = "bär <x> & 'y' -> z"


def _upgraded_settings(ups: str = UPS_CMD, coord: str = COORD_CMD) -> dict:
    """What pre-plugin installs left behind, beside the user's own hooks."""
    return {
        "model": "opus",
        "env": {"NOTE": USER_TEXT},
        "enabledPlugins": {PLUGIN_ID: True, "other@market": True},
        "hooks": {
            "SessionStart": [
                {"hooks": [USER_START]},
                {"hooks": [_hook(DOCKER_BRIEFING)]},
                {"hooks": [_hook(coord)]},
                # The first Windows install-hook also wrote shell=bash, and a
                # user may have moved our hook into a group of their own.
                {"matcher": "startup",
                 "hooks": [_hook(EPISODE_START, shell="bash"), USER_SHARED]},
                # Already empty before we came: not ours to drop.
                {"matcher": "resume", "hooks": []},
            ],
            "UserPromptSubmit": [{"hooks": [USER_PROMPT]}, {"hooks": [_hook(ups)]}],
            "SessionEnd": [{"hooks": [_hook(EPISODE_END)]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [_hook("echo pre")]}],
        },
    }


def _cleaned(settings: dict) -> dict:
    """The same settings once the five installer-written entries are gone:
    user hooks and groups stay, emptied groups go, and an emptied event
    stays as an empty list (as install-hook's episode clean-up leaves it)."""
    out = json.loads(json.dumps(settings))
    out["hooks"] = {
        "SessionStart": [{"hooks": [USER_START]},
                         {"matcher": "startup", "hooks": [USER_SHARED]},
                         {"matcher": "resume", "hooks": []}],
        "UserPromptSubmit": [{"hooks": [USER_PROMPT]}],
        "SessionEnd": [],
        "PreToolUse": settings["hooks"]["PreToolUse"],
    }
    return out


def _write_home(home: Path, settings: dict | None, *, record: str | None = "user",
                newline: str = "\n") -> Path:
    claude = home / ".claude"
    (claude / "plugins").mkdir(parents=True, exist_ok=True)
    if record:
        entry = {"scope": record, "version": "0.15.0", "gitCommitSha": "0" * 40}
        if record != "user":
            entry["projectPath"] = str(home / "work")
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {PLUGIN_ID: [entry]}}, indent=2), encoding="utf-8")
    path = claude / "settings.json"
    if settings is not None:
        text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak-*"))


def _commands(settings: dict) -> list[str]:
    return [h.get("command") for groups in settings.get("hooks", {}).values()
            for g in groups for h in g.get("hooks", [])]


def _variants():
    return [("bash", b) for b in _bash_variants()] + [("pwsh", None)]


VARIANTS = _variants()
IDS = [v[0] if v[1] is None else Path(v[1]).parent.name for v in VARIANTS]


def _pwsh() -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    return pwsh


def _env(variant, tmp_path: Path) -> dict[str, str]:
    return _fixture_env(tmp_path / f"{variant[0]}-env")


def _q(text: str) -> str:
    return text.replace("'", "'\\''")


def _bash_prelude(bash: str, env: dict[str, str]) -> str:
    # install-hook.sh edits JSON with python; the fixture PATH carries only
    # the interpreter running this suite.
    python_dir = _bash_fixture_path(bash, Path(sys.executable).parent, env)
    return f"PATH='{_q(python_dir)}:/usr/bin:/bin'\nexport PATH\n"


def _run_hook(variant, env: dict[str, str], settings: Path | None, *,
              mode: str = "remove", client: str = "claude",
              command: str | None = None) -> subprocess.CompletedProcess[bytes]:
    kind, bash = variant
    if kind == "bash":
        hook = _bash_fixture_path(bash, ROOT / "ops" / "install-hook.sh", env)
        target = "" if settings is None else _bash_fixture_path(bash, settings, env)
        args = {"remove": "--remove-legacy", "dry-run": "--remove-legacy --dry-run",
                "install": ""}[mode]
        tail = f" '{_q(command)}'" if command else ""
        script = (_bash_prelude(bash, env)
                  + f"bash '{_q(hook)}' --client {client} {args} '{_q(target)}'{tail}\n")
        return subprocess.run([bash], input=script.encode(), capture_output=True,
                              check=False, timeout=60, env=env)
    args = [_pwsh(), "-NoProfile", "-NonInteractive", "-File",
            str(ROOT / "ops" / "install-hook.ps1"), "-Client", client]
    args += {"remove": ["-RemoveLegacy"], "dry-run": ["-RemoveLegacy", "-DryRun"],
             "install": []}[mode]
    if settings is not None:
        args += ["-SettingsPath", str(settings)]
    if command:
        args += ["-Command", command]
    return subprocess.run(args, capture_output=True, check=False, timeout=60, env=env)


def _out(proc) -> str:
    return proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")


# ── install-hook --remove-legacy ────────────────────────────────────────────

@pytest.mark.parametrize("ups, coord", [
    (UPS_CMD, COORD_CMD), (OLD_UPS_CMD, COORD_CMD),
    (UPS_CMD, DOCKER_GATED_COORD), (UPS_CMD, GATED_COORD),
    (PROMPT_CMD, GATED_COORD), (DOCKER_PROMPT_CMD, DOCKER_GATED_COORD),
], ids=["echo-checkin", "pre-2026-09-05-discipline", "gated-checkin-docker", "gated-checkin",
        "prompt-hook", "prompt-hook-docker"])
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_removes_exactly_the_shipped_entries_after_a_backup(variant, ups, coord, tmp_path):
    env = _env(variant, tmp_path)
    settings = _upgraded_settings(ups, coord)
    path = _write_home(Path(env["HOME"]), settings)
    original = path.read_bytes()
    proc = _run_hook(variant, env, path)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert json.loads(path.read_text(encoding="utf-8")) == _cleaned(settings)
    backups = _backups(path)
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert "Removed 5" in out and str(backups[0].name) in out


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_rewrite_keeps_the_users_text_and_line_endings(variant, newline, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), _upgraded_settings(), newline=newline)
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 0, _out(proc)
    raw = path.read_bytes()
    assert DOCKER_BRIEFING not in raw.decode("utf-8")
    assert raw.endswith(newline.encode())
    if newline == "\n":
        assert b"\r" not in raw
    else:
        assert raw.count(b"\n") == raw.count(b"\r\n")
    # Rewritten JSON, not re-escaped: the user's text reads as they wrote it.
    assert USER_TEXT in raw.decode("utf-8")
    # Claude Code writes settings.json as 2-space JSON; in that format only
    # the removed entries change, byte for byte.
    expected = json.dumps(_cleaned(_upgraded_settings()), indent=2, ensure_ascii=False) + "\n"
    assert raw.decode("utf-8").replace("\r\n", "\n") == expected


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_dry_run_lists_the_entries_and_writes_nothing(variant, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), _upgraded_settings())
    original = path.read_bytes()
    proc = _run_hook(variant, env, path, mode="dry-run")
    out = _out(proc)
    assert proc.returncode == 0, out
    assert path.read_bytes() == original and _backups(path) == []
    for needle in (DOCKER_BRIEFING, "Pseudolife coordination:", "mid-session discipline",
                   EPISODE_START, EPISODE_END):
        assert needle in out
    assert "user-start" not in out and "user-shared" not in out


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_lookalikes_are_reported_and_never_removed(variant, tmp_path):
    lookalikes = [
        _hook(DOCKER_BRIEFING + " 2>/dev/null || true"),                  # edited copy
        _hook("bash -c 'pseudolife-mcp briefing --hook-json; echo mine'"),  # compound
        _hook("pseudolife-mcp briefing"),        # hand-copied docs snippet
        _hook(UPS_CMD),                          # right line, wrong event
        _hook(BRIEFING, commandWindows="pwsh -File my-briefing.ps1"),     # own override
    ]
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), {
        "enabledPlugins": {PLUGIN_ID: True},
        "hooks": {"SessionStart": [{"hooks": [h]} for h in lookalikes]}})
    original = path.read_bytes()
    proc = _run_hook(variant, env, path)
    out = _out(proc)
    assert proc.returncode == 3, out
    assert path.read_bytes() == original and _backups(path) == []
    assert "by hand" in out
    assert sum(line.startswith("  SessionStart: ") for line in out.splitlines()) == 5


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_settings_without_installer_hooks_are_not_rewritten(variant, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), {
        "enabledPlugins": {PLUGIN_ID: True},
        "hooks": {"SessionStart": [{"hooks": [USER_START]}]}})
    original = path.read_bytes()
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 3, _out(proc)
    assert path.read_bytes() == original and _backups(path) == []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_missing_settings_file_is_not_created(variant, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), None)
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 3, _out(proc)
    assert not path.exists()


@pytest.mark.parametrize("gate", ["no-record", "project-scope", "disabled", "not-enabled"])
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_hooks_stay_unless_the_plugin_runs_for_every_project(variant, gate, tmp_path):
    """A recorded plugin is not necessarily a running one: a project-scoped
    install or a disabled plugin leaves these hooks as the only ones that
    run, so removing them would silently end the briefing."""
    env = _env(variant, tmp_path)
    settings = _upgraded_settings()
    if gate == "disabled":
        settings["enabledPlugins"][PLUGIN_ID] = False
    if gate == "not-enabled":
        del settings["enabledPlugins"][PLUGIN_ID]
    record = {"no-record": None, "project-scope": "project"}.get(gate, "user")
    path = _write_home(Path(env["HOME"]), settings, record=record)
    original = path.read_bytes()
    for mode in ("dry-run", "remove"):
        proc = _run_hook(variant, env, path, mode=mode)
        assert proc.returncode == 4, _out(proc)
        assert "enabled for all projects" in _out(proc)
    assert path.read_bytes() == original and _backups(path) == []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_unreadable_settings_are_an_error_not_a_rewrite(variant, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), None)
    path.write_text('{"hooks": {', encoding="utf-8")
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 1, _out(proc)
    assert "could not read" in _out(proc).lower()
    assert path.read_text(encoding="utf-8") == '{"hooks": {' and _backups(path) == []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_non_string_commands_are_kept_not_a_crash(variant, tmp_path):
    """A list or object where a command string belongs is not ours: it is
    kept, and the entries beside it are still removed (reviewer finding,
    2026-09-25: the bash set lookup raised on unhashable values)."""
    listed = {"type": "command", "command": ["bash", "-c", "x"]}
    windows_list = _hook(BRIEFING, commandWindows=["pwsh", "-File", "x.ps1"])
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), {
        "enabledPlugins": {PLUGIN_ID: True},
        "hooks": {"SessionStart": [{"hooks": [_hook(DOCKER_BRIEFING), listed]},
                                   {"hooks": [windows_list]}]}})
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 0, _out(proc)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"] == [{"hooks": [listed]}, {"hooks": [windows_list]}]


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_duplicate_keys_are_refused_not_rewritten(variant, tmp_path):
    """Python keeps the last duplicate and .NET refuses the object, so a
    rewrite would drop the other value in one shell and fail in the other.
    Both refuse, and the file stays as the user wrote it."""
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), None)
    text = json.dumps(_upgraded_settings(), indent=2).replace(
        '"model": "opus",', '"model": "opus",\n  "model": "sonnet",', 1)
    path.write_text(text, encoding="utf-8")
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 1, _out(proc)
    assert "could not read" in _out(proc).lower()
    assert path.read_text(encoding="utf-8") == text and _backups(path) == []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_text_outside_the_basic_plane_keeps_its_value(variant, tmp_path):
    """The rewrite never changes a value. bash also keeps an emoji
    literal; .NET's encoder writes it as a \\u surrogate pair, which is the
    same JSON string."""
    env = _env(variant, tmp_path)
    settings = _upgraded_settings()
    settings["statusLine"] = {"type": "command", "command": "echo '\U0001F9E0 memory'"}
    path = _write_home(Path(env["HOME"]), settings)
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 0, _out(proc)
    raw = path.read_text(encoding="utf-8")
    assert json.loads(raw) == _cleaned(settings)
    if variant[0] == "bash":
        assert "\U0001F9E0" in raw


@pytest.mark.skipif(sys.platform != "win32",
                    reason="a read-only destination blocks a rename only on Windows")
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_a_failed_write_leaves_the_file_and_no_temp_behind(variant, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), _upgraded_settings())
    original = path.read_bytes()
    path.chmod(stat.S_IREAD)
    try:
        proc = _run_hook(variant, env, path)
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "could not write" in out.lower()
    assert path.read_bytes() == original
    assert list(path.parent.glob("*.tmp-pseudolife*")) == []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_a_symlinked_settings_file_stays_a_symlink(variant, tmp_path):
    """Dotfile setups link ~/.claude/settings.json into a repository;
    install mode writes through the link, and so does the removal."""
    env = _env(variant, tmp_path)
    home = Path(env["HOME"])
    path = _write_home(home, None)
    real = home / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps(_upgraded_settings(), indent=2) + "\n", encoding="utf-8")
    try:
        path.symlink_to(real)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 0, _out(proc)
    assert path.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8")) == _cleaned(_upgraded_settings())


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_codex_hooks_are_left_to_the_codex_setup_helper(variant, tmp_path):
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), _upgraded_settings())
    original = path.read_bytes()
    proc = _run_hook(variant, env, path, client="codex")
    assert proc.returncode == 2, _out(proc)
    assert "setup-codex-hooks.py" in _out(proc)
    assert path.read_bytes() == original


@pytest.mark.parametrize("command", [BRIEFING, DOCKER_BRIEFING], ids=["default", "installer"])
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_everything_install_mode_writes_is_removable(variant, command, tmp_path):
    """Round trip through the real install path, called the way the
    installers call it: whatever install-hook writes today, --remove-legacy
    removes, so a reworded line can never strand new installs."""
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]), {
        "enabledPlugins": {PLUGIN_ID: True},
        "hooks": {"SessionStart": [{"hooks": [USER_START]}]}})
    for _ in range(2):
        proc = _run_hook(variant, env, None, mode="install", command=command)
        assert proc.returncode == 0, _out(proc)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert len(_commands(written)) == 4, written
    assert command in _commands(written)
    proc = _run_hook(variant, env, path)
    assert proc.returncode == 0, _out(proc)
    assert "Removed 3" in _out(proc)
    assert _commands(json.loads(path.read_text(encoding="utf-8"))) == ["echo user-start"]


# ── install mode: the per-turn memory-change hook ───────────────────────────

def _settings_path(env: dict[str, str], client: str) -> Path:
    home = Path(env["HOME"])
    return home / ".codex" / "hooks.json" if client == "codex" else home / ".claude" / "settings.json"


def _ups(settings: dict) -> list[dict]:
    return [h for g in settings["hooks"].get("UserPromptSubmit", []) for h in g.get("hooks", [])]


@pytest.mark.parametrize("command, prompt", [(BRIEFING, PROMPT_CMD),
                                             (DOCKER_BRIEFING, DOCKER_PROMPT_CMD)],
                         ids=["default", "installer"])
@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_install_writes_the_memory_change_hook_not_the_static_line(
        variant, client, command, prompt, tmp_path):
    """Plugin-less installs get the plugin's per-turn memory-change note
    (maintainer decision 2026-09-26) instead of a 614-character line on
    every turn. The Docker tier runs it in the daemon container, fed the
    hook's stdin by ``docker exec -i``."""
    env = _env(variant, tmp_path)
    path = _settings_path(env, client)
    for _ in range(2):
        proc = _run_hook(variant, env, path, mode="install", client=client, command=command)
        assert proc.returncode == 0, _out(proc)
    written = json.loads(path.read_text(encoding="utf-8"))
    ups = _ups(written)
    assert [h["command"] for h in ups] == [prompt], ups
    assert "mid-session discipline" not in path.read_text(encoding="utf-8")
    if variant[0] == "pwsh" and client == "codex":
        assert ups[0]["commandWindows"] == prompt and ups[0]["timeout"] == 5


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_install_replaces_the_shipped_static_lines_exactly_and_once(variant, tmp_path):
    """Re-running an installer migrates what earlier versions wrote: the
    current line, the pre-2026-09-05 one, and Codex's PowerShell pair.
    A user's own hook that merely mentions the phrase stays."""
    env = _env(variant, tmp_path)
    lookalike = _hook("echo 'my own mid-session discipline reminder'")
    codex_pair = _hook(UPS_CMD, commandWindows=f"Write-Output '{DISCIPLINE}'", timeout=5)
    # A platform override of the user's own makes the entry theirs.
    customized = _hook(UPS_CMD, commandWindows="Write-Output 'my own override'")
    path = _write_home(Path(env["HOME"]), {"hooks": {
        "SessionStart": [{"hooks": [_hook(DOCKER_BRIEFING)]},
                         {"hooks": [_hook(DOCKER_GATED_COORD)]}],
        "UserPromptSubmit": [{"hooks": [USER_PROMPT, _hook(UPS_CMD)]},
                             {"hooks": [_hook(OLD_UPS_CMD)]},
                             {"hooks": [codex_pair, lookalike, customized]}]}}, record=None)
    proc = _run_hook(variant, env, path, mode="install", command=DOCKER_BRIEFING)
    assert proc.returncode == 0, _out(proc)
    first = json.loads(path.read_text(encoding="utf-8"))
    assert [h["command"] for h in _ups(first)] == [
        USER_PROMPT["command"], lookalike["command"], UPS_CMD, DOCKER_PROMPT_CMD]
    assert _ups(first)[2]["commandWindows"] == customized["commandWindows"]
    proc = _run_hook(variant, env, path, mode="install", command=DOCKER_BRIEFING)
    assert proc.returncode == 0, _out(proc)
    assert json.loads(path.read_text(encoding="utf-8")) == first


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_install_replaces_the_checkin_echo_only_where_both_commands_are_ours(variant, tmp_path):
    """The unconditional check-in echo follows the same exact-pair rule in
    both installers: a Codex entry whose commandWindows is the user's own
    is theirs (2026-09-26 review of the PowerShell copy)."""
    env = _env(variant, tmp_path)
    codex_pair = _hook(COORD_CMD, commandWindows=f"Write-Output '{COORDINATION}'", timeout=5)
    customized = _hook(COORD_CMD, commandWindows="Write-Output 'my own override'")
    path = _write_home(Path(env["HOME"]), {"hooks": {"SessionStart": [
        {"hooks": [_hook(COORD_CMD)]}, {"hooks": [codex_pair, customized]}]}}, record=None)
    proc = _run_hook(variant, env, path, mode="install", command=DOCKER_BRIEFING)
    assert proc.returncode == 0, _out(proc)
    starts = [h for g in json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
              for h in g["hooks"]]
    assert [h for h in starts if h["command"] == COORD_CMD] == [customized]
    assert DOCKER_GATED_COORD in [h["command"] for h in starts]


# ── installer consent block (section 9) ─────────────────────────────────────

def _run_installer_block(variant, tmp_path: Path, *, flag: str = "ask",
                         answer: str | None = None,
                         settings: dict | None = None, record: str | None = "user",
                         raw: str | None = None):
    """Run the consent block with the real install-hook. ``answer`` stands
    in for a terminal reply; None means no terminal (an unattended run)."""
    kind, bash = variant
    env = _env(variant, tmp_path)
    path = _write_home(Path(env["HOME"]),
                       _upgraded_settings() if settings is None else settings,
                       record=record)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    if kind == "bash":
        block = _between("ops/install.sh", "# >>> claude legacy hooks >>>",
                         "# <<< claude legacy hooks <<<")
        override = "" if answer is None else (
            "claude_legacy_answer() { printf 'ASKED\\n' >&2; "
            f"printf '%s' '{_q(answer)}'; }}")
        repo = _bash_fixture_path(bash, ROOT, env)
        # The installer's own strict mode: an abort in the block fails here.
        # One ordered stream, so "listed before asked" is observable.
        script = f"""set -euo pipefail
exec 2>&1
{_bash_prelude(bash, env)}repo='{_q(repo)}'
CLAUDE_LEGACY_HOOKS='{flag}'
step() {{ printf 'STEP: %s\\n' "$*"; }}
{block}
{override}
cleanup_claude_legacy_hooks
printf 'STATE=%s\\n' "$LEGACY_CLAUDE"
printf 'LINE=%s\\n' "$(describe_legacy_hooks "$LEGACY_CLAUDE")"
"""
        proc = subprocess.run([bash], input=script.encode(), capture_output=True,
                              check=False, timeout=60, env=env)
    else:
        pwsh = _pwsh()
        block = _between("ops/install.ps1", "# >>> claude legacy hooks >>>",
                         "# <<< claude legacy hooks <<<")
        override = "" if answer is None else (
            "function Read-ClaudeLegacyAnswer { Write-Host 'ASKED'; "
            f"return '{answer}' }}")
        repo = str(ROOT).replace("'", "''")
        script = f"""$ErrorActionPreference = 'Stop'
$repo = '{repo}'
$interactive = $false
$ClaudeLegacyHooks = '{flag}'
function Step($message) {{ Write-Output "STEP: $message" }}
{block}
{override}
Invoke-ClaudeLegacyHookCleanup
Write-Output "STATE=$($script:legacyClaude)"
Write-Output ("LINE=" + (Describe-LegacyHooks $script:legacyClaude))
"""
        runner = tmp_path / "run-legacy.ps1"
        runner.write_text(script, encoding="utf-8")
        proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                              capture_output=True, check=False, timeout=90, env=env)
    out = _out(proc)
    assert proc.returncode == 0, out
    state = next(l[len("STATE="):] for l in out.splitlines() if l.startswith("STATE="))
    line = next(l[len("LINE="):] for l in out.splitlines() if l.startswith("LINE="))
    return state, line, out, path


def _flag(variant) -> str:
    return "--claude-legacy-hooks remove" if variant[0] == "bash" else "-ClaudeLegacyHooks remove"


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_remove_flag_removes_without_asking(variant, tmp_path):
    state, line, out, path = _run_installer_block(variant, tmp_path, flag="remove",
                                                  answer="n")
    assert state == "removed"
    assert "ASKED" not in out
    assert json.loads(path.read_text(encoding="utf-8")) == _cleaned(_upgraded_settings())
    backup = _backups(path)[0]
    assert line.startswith("[x] Old hooks") and backup.name in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_keep_flag_keeps_without_asking_and_says_how_to_remove(variant, tmp_path):
    state, line, out, path = _run_installer_block(variant, tmp_path, flag="keep", answer="y")
    assert state == "kept"
    assert "ASKED" not in out
    assert json.loads(path.read_text(encoding="utf-8")) == _upgraded_settings()
    assert line.startswith("[!] Old hooks") and _flag(variant) in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_unattended_ask_keeps_them_and_names_the_flag(variant, tmp_path):
    state, line, out, path = _run_installer_block(variant, tmp_path)
    assert state == "kept"
    assert json.loads(path.read_text(encoding="utf-8")) == _upgraded_settings()
    assert _backups(path) == []
    assert _flag(variant) in out and _flag(variant) in line


@pytest.mark.parametrize("answer, removed", [("y", True), ("YES", True), ("", False), ("n", False)],
                         ids=["y", "YES", "enter", "n"])
@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_interactive_ask_needs_an_explicit_yes(variant, answer, removed, tmp_path):
    state, _, out, path = _run_installer_block(variant, tmp_path, answer=answer)
    assert "ASKED" in out
    # The user sees what would go before answering.
    assert out.index(DOCKER_BRIEFING) < out.index("ASKED")
    assert state == ("removed" if removed else "kept")
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == (_cleaned(_upgraded_settings()) if removed else _upgraded_settings())


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_nothing_to_remove_asks_nothing_and_adds_no_ladder_line(variant, tmp_path):
    state, line, out, _ = _run_installer_block(
        variant, tmp_path, answer="y",
        settings={"enabledPlugins": {PLUGIN_ID: True},
                  "hooks": {"SessionStart": [{"hooks": [USER_START]}]}})
    assert state == "" and line == ""
    assert "ASKED" not in out


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_inactive_plugin_keeps_the_hooks_and_says_why(variant, tmp_path):
    settings = _upgraded_settings()
    settings["enabledPlugins"][PLUGIN_ID] = False
    state, line, out, path = _run_installer_block(variant, tmp_path, flag="remove",
                                                  answer="y", settings=settings)
    assert state == "inactive"
    assert "ASKED" not in out
    assert json.loads(path.read_text(encoding="utf-8")) == settings
    assert line.startswith("[-] Old hooks") and "all projects" in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_unreadable_settings_are_reported_not_fatal(variant, tmp_path):
    state, line, _, path = _run_installer_block(variant, tmp_path, flag="remove",
                                                raw='{"hooks": {')
    assert state == "error"
    assert path.read_text(encoding="utf-8") == '{"hooks": {'
    assert line.startswith("[!] Old hooks") and "plugin/README.md" in line
    # The check may have run and only the write failed: say what is known.
    assert "not removed" in line and "not checked" not in line


# ── wiring ──────────────────────────────────────────────────────────────────

def test_both_installers_expose_the_flag_and_offer_cleanup_only_under_the_plugin():
    sh = (ROOT / "ops/install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    assert "--claude-legacy-hooks ask|remove|keep" in sh.split("# <<< usage <<<")[0]
    assert "\nCLAUDE_LEGACY_HOOKS=ask\n" in sh
    assert '--claude-legacy-hooks) CLAUDE_LEGACY_HOOKS="$2"; shift 2 ;;' in sh
    assert '[ValidateSet("ask", "remove", "keep")]\n    [string]$ClaudeLegacyHooks = "ask"' in ps1
    # Without the plugin these hooks are the only ones that run, so only the
    # plugin-owned branch of section 9 may call the cleanup.
    sh_branch = sh.split('if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then\n        HOOK_CLAUDE=plugin', 1)[1].split("continue", 1)[0]
    ps_branch = ps1.split('if (($selectedClient -eq "claude") -and $claudePluginInstalled) {\n        $hookState["claude"] = "plugin"', 1)[1].split("continue", 1)[0]
    assert "cleanup_claude_legacy_hooks" in sh_branch
    assert "Invoke-ClaudeLegacyHookCleanup" in ps_branch
    assert sh.count("\n        cleanup_claude_legacy_hooks\n") == 1
    assert ps1.count("\n        Invoke-ClaudeLegacyHookCleanup\n") == 1
    assert 'describe_legacy_hooks "$LEGACY_CLAUDE"' in sh.split("# ── 13.")[1]
    assert "Describe-LegacyHooks $script:legacyClaude" in ps1.split("# -- 13.")[1]


def test_the_installers_briefing_command_is_in_the_removable_set():
    """install-hook carries the installers' briefing command as a literal;
    changing it in one place without the other would strand every install."""
    assert _line(r'^briefing_command="(.*)"$', "ops/install.sh") == DOCKER_BRIEFING
    assert _line(r'^\$briefingCommand = "(.*)"$', "ops/install.ps1") == DOCKER_BRIEFING
    for rel in ("ops/install-hook.sh", "ops/install-hook.ps1"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert f'"{DOCKER_BRIEFING}"' in text or f"'{DOCKER_BRIEFING}'" in text, rel
