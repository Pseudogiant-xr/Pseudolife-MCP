"""ops/shim_autostart.py: the extractor shims start from ops/.env, not from
a command line baked into a scheduled task or a systemd unit.

Changing the model, prompt file, port or CLI path used to mean re-registering
the Windows task from an elevated PowerShell (the values were in the task's
action) — on 2026-09-28 the maintainer needed a Start-menu elevated shell
for exactly that. The task and the unit now run ``shim_autostart.py run
<kind>`` and read ``ops/.env`` at every start; a change is an edit plus
``restart``. The installers' flags keep working: ``config`` writes them
into the file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("shim_autostart", ROOT / "ops" / "shim_autostart.py")
sa = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sa
SPEC.loader.exec_module(sa)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout shape: the shim scripts, the prompt, an ops/ with the example env."""
    root = tmp_path / "checkout"
    (root / "evals" / "prompts").mkdir(parents=True)
    (root / "ops").mkdir()
    (root / "evals" / "claude_shim.py").write_text("# shim\n", encoding="utf-8")
    (root / "evals" / "codex_shim.py").write_text("# shim\n", encoding="utf-8")
    (root / "evals" / "prompts" / "sonnet_extractor_v5.md").write_text("prompt\n", encoding="utf-8")
    (root / "ops" / ".env.example").write_text("# example\n#POSTGRES_PASSWORD=change-me\n", encoding="utf-8")
    monkeypatch.setattr(sa, "default_host", lambda: "127.0.0.1")
    monkeypatch.setattr(sa.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(sa.sys, "executable", str(tmp_path / "py" / "python"))
    return root


# ── ops/.env ────────────────────────────────────────────────────────────────

def test_env_file_parsing_accepts_export_quotes_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# c\nexport A=1\nB="two words"\nC=\'x\'\nD=plain # trailing\n\nbad line\n9X=no\nE=\n',
                    encoding="utf-8")
    assert sa.read_env(path) == {"A": "1", "B": "two words", "C": "x", "D": "plain", "E": ""}
    assert sa.read_env(tmp_path / "missing") == {}


def test_settings_come_from_defaults_then_env_then_flags(repo):
    defaults = sa.resolve("claude", repo)
    assert defaults["model"] == "claude-opus-5-5" and defaults["port"] == "8082"
    assert defaults["prompt_file"] == str(repo / "evals" / "prompts" / "sonnet_extractor_v5.md")
    assert defaults["python"] == sa.sys.executable and defaults["host"] == "127.0.0.1"
    assert "cli" not in defaults              # a CLI on PATH is the shim's own lookup, never passed as --cli
    (repo / "ops" / ".env").write_text(
        "PSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-sonnet-5\nPSEUDOLIFE_CLAUDE_SHIM_PORT=8090\n"
        "PSEUDOLIFE_CLAUDE_SHIM_CLI=/opt/claude\nPSEUDOLIFE_CODEX_SHIM_MODEL=gpt-6-sol\n", encoding="utf-8")
    from_env = sa.resolve("claude", repo)
    assert from_env["model"] == "claude-sonnet-5" and from_env["port"] == "8090" and from_env["cli"] == "/opt/claude"
    assert sa.resolve("codex", repo)["model"] == "gpt-6-sol" and sa.resolve("codex", repo)["port"] == "8086"
    flagged = sa.resolve("claude", repo, {"model": "claude-fable-5", "port": 9000, "prompt_file": "evals/prompts/x.md"})
    assert flagged["model"] == "claude-fable-5" and flagged["port"] == "9000"
    assert flagged["prompt_file"] == str(repo / "evals" / "prompts" / "x.md")     # relative to the checkout
    absolute = sa.resolve("claude", repo, {"prompt_file": str(repo / "elsewhere.md")})
    assert absolute["prompt_file"] == str(repo / "elsewhere.md")


def test_the_runners_defaults_are_the_installers_defaults():
    """One default per value: the model-list test pins the installers' own
    lines, this pins the runner to them."""
    ps = (ROOT / "ops" / "install-shim-autostart.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "ops" / "install-shim-autostart.sh").read_text(encoding="utf-8")
    assert re.search(r'\[string\]\$Model = "([^"]+)"', ps).group(1) == sa.KINDS["claude"]["defaults"]["model"]
    assert re.search(r'(?m)^MODEL="([^"]+)"$', sh).group(1) == sa.KINDS["claude"]["defaults"]["model"]
    assert re.search(r'\[int\]\$Port = (\d+)', ps).group(1) == sa.KINDS["claude"]["defaults"]["port"]
    assert re.search(r'(?m)^PORT=(\d+)$', sh).group(1) == sa.KINDS["claude"]["defaults"]["port"]
    prompt = sa.KINDS["claude"]["defaults"]["prompt_file"]
    assert prompt.replace("/", "\\") in ps and prompt in sh
    cps = (ROOT / "ops" / "install-codex-shim-autostart.ps1").read_text(encoding="utf-8")
    csh = (ROOT / "ops" / "install-codex-shim-autostart.sh").read_text(encoding="utf-8")
    assert re.search(r'\[string\]\$Model = "([^"]+)"', cps).group(1) == sa.KINDS["codex"]["defaults"]["model"]
    assert re.search(r'(?m)^MODEL="([^"]+)"$', csh).group(1) == sa.KINDS["codex"]["defaults"]["model"]
    assert re.search(r'\[int\]\$HealthTtl = (\d+)', cps).group(1) == sa.KINDS["codex"]["defaults"]["health_ttl"]
    assert re.search(r'(?m)^HEALTH_TTL=(\d+)$', csh).group(1) == sa.KINDS["codex"]["defaults"]["health_ttl"]


def test_the_shim_command_lines(repo):
    claude = sa.shim_argv("claude", repo, sa.resolve("claude", repo, {"cli": "/opt/claude"}))
    assert claude == [sa.sys.executable, str(repo / "evals" / "claude_shim.py"), "--host", "127.0.0.1",
                      "--port", "8082", "--model", "claude-opus-5-5",
                      "--system-prompt-file", str(repo / "evals" / "prompts" / "sonnet_extractor_v5.md"),
                      "--cli", "/opt/claude"]
    codex = sa.shim_argv("codex", repo, sa.resolve("codex", repo))
    assert codex == [sa.sys.executable, str(repo / "evals" / "codex_shim.py"), "--host", "127.0.0.1",
                     "--port", "8086", "--model", "gpt-5.6-terra", "--health-ttl", "1800"]
    assert "--system-prompt-file" not in codex        # the codex shim runs the production prompt


def test_config_writes_a_managed_block_and_merges_later_edits(repo):
    path, moved = sa.write_config(repo, "claude", {"model": "claude-sonnet-5", "port": "8090"})
    assert path == repo / "ops" / ".env" and moved == []
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# example\n#POSTGRES_PASSWORD=change-me\n")        # scaffolded from the example
    assert sa.BLOCK_BEGIN in text and sa.BLOCK_END in text
    assert sa.read_env(path)["PSEUDOLIFE_CLAUDE_SHIM_MODEL"] == "claude-sonnet-5"
    # the user's own lines outside the block stay; a second config keeps the other kind's keys
    path.write_text("POSTGRES_PASSWORD=mine\n" + text, encoding="utf-8")
    sa.write_config(repo, "codex", {"model": "gpt-6-sol", "health_ttl": "600"})
    sa.write_config(repo, "claude", {"port": "8091"})
    values = sa.read_env(path)
    assert values["POSTGRES_PASSWORD"] == "mine"
    # only the given key changes: the model written before survives a port-only config
    assert values["PSEUDOLIFE_CLAUDE_SHIM_PORT"] == "8091" and values["PSEUDOLIFE_CLAUDE_SHIM_MODEL"] == "claude-sonnet-5"
    assert values["PSEUDOLIFE_CODEX_SHIM_MODEL"] == "gpt-6-sol" and values["PSEUDOLIFE_CODEX_SHIM_HEALTH_TTL"] == "600"
    assert path.read_text(encoding="utf-8").count(sa.BLOCK_BEGIN) == 1
    # a value with spaces is quoted and read back; so is one with quotes and backslashes
    sa.write_config(repo, "claude", {"cli": r"C:\Program Files\claude\claude.exe"})
    assert sa.read_env(path)["PSEUDOLIFE_CLAUDE_SHIM_CLI"] == r"C:\Program Files\claude\claude.exe"
    assert "'C:\\Program Files\\claude\\claude.exe'" in path.read_text(encoding="utf-8")
    odd = 'C:\\it\'s "here"\\'
    sa.write_config(repo, "claude", {"cli": odd})
    assert sa.read_env(path)["PSEUDOLIFE_CLAUDE_SHIM_CLI"] == odd


def test_config_moves_a_key_written_outside_the_block_into_it(repo):
    """The block sits at the end of the file and is read last, so a
    hand-written line above it would be shadowed silently; config moves
    it in, keeping its value unless the same key is given."""
    path = repo / "ops" / ".env"
    path.write_text("PSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-sonnet-5\nPSEUDOLIFE_CLAUDE_SHIM_PORT=8090\n"
                    "#PSEUDOLIFE_CLAUDE_SHIM_HOST=127.0.0.1\nPSEUDOLIFE_CODEX_SHIM_MODEL=gpt-6-sol\n",
                    encoding="utf-8")
    _, moved = sa.write_config(repo, "claude", {"port": "8091"})
    assert moved == ["PSEUDOLIFE_CLAUDE_SHIM_MODEL", "PSEUDOLIFE_CLAUDE_SHIM_PORT"]
    text = path.read_text(encoding="utf-8")
    before, after = text.split(sa.BLOCK_BEGIN)
    assert "PSEUDOLIFE_CLAUDE_SHIM_MODEL" not in before and "#PSEUDOLIFE_CLAUDE_SHIM_HOST" in before
    assert "PSEUDOLIFE_CODEX_SHIM_MODEL=gpt-6-sol" in before          # the other kind's line is not touched
    assert "PSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-sonnet-5" in after and "PSEUDOLIFE_CLAUDE_SHIM_PORT=8091" in after
    assert sa.resolve("claude", repo)["model"] == "claude-sonnet-5"


def test_check_names_what_would_stop_a_start(repo):
    settings = sa.resolve("claude", repo, {"prompt_file": "evals/prompts/missing.md", "cli": "/nowhere/claude"})
    problems = sa.check("claude", repo, settings)
    assert any("prompt file not found" in p for p in problems)
    assert any("CLI not found" in p for p in problems)
    assert any("interpreter not found" in p for p in problems)
    with pytest.raises(ValueError, match="PSEUDOLIFE_CLAUDE_SHIM_PORT must be a number"):
        sa.resolve("claude", repo, {"port": "eighty"})


def test_the_restart_plan_needs_no_elevation(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "state_dir", lambda: tmp_path / "state")
    settings = {"port": "8082", "model": "x", "host": "127.0.0.1"}
    plan = sa.restart_plan("claude", settings, tmp_path)
    if os.name == "nt":
        assert plan[0][0] == "powershell.exe" and "claude_shim" in plan[0][-1] and "(8082)" in plan[0][-1]
        assert plan[1] == ["<start detached>"]                 # started here, not through the scheduler
        assert not any("schtasks" in " ".join(c) or "Register-ScheduledTask" in " ".join(c) for c in plan)
    elif sa.shutil.which("systemctl"):
        assert plan == [["systemctl", "--user", "restart", "pseudolife-sonnet-shim.service"]]
    else:
        assert plan[0][:2] == ["pkill", "-f"] and plan[1] == ["<start detached>"]


def test_restart_stops_the_shim_on_the_port_it_was_last_started_on(tmp_path, monkeypatch):
    """A port change in ops/.env must not leave the old shim serving the
    old port: the pid file from the last start names it."""
    monkeypatch.setattr(sa, "state_dir", lambda: tmp_path / "state")
    repo = tmp_path / "checkout"
    sa._write_pid("claude", repo, 4242, "8090")
    assert sa.previous_start("claude", repo) == (4242, "8090")
    assert sa.previous_start("claude", tmp_path / "other-checkout") is None      # per checkout
    plan = sa.restart_plan("claude", {"port": "8082", "model": "x", "host": "127.0.0.1"}, repo)
    if os.name == "nt":
        stop = plan[0][-1]
        assert "(8082|8090)" in stop and "@(4242)" in stop and "claude_shim" in stop
    elif not sa.shutil.which("systemctl"):
        assert "(8082|8090)" in plan[0][-1]
    sa.pid_file("claude", repo).write_text("garbage\n", encoding="utf-8")
    assert sa.previous_start("claude", repo) is None


def test_the_shim_does_not_inherit_a_claude_code_sessions_environment():
    """A restart from a Claude Code shell must not hand the shim (and every
    CLI call it makes) that session's identity; the scheduled task starts
    it with the logon environment, which has none of these."""
    env = {"PATH": "/usr/bin", "HOME": "/home/u", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "abc",
           "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_PID": "7", "CLAUDE_EFFORT": "high",
           "CLAUDE_AGENT_SDK_VERSION": "1", "ANTHROPIC_API_KEY": "kept-on-purpose", "PSEUDOLIFE_MCP_TOKEN": "t"}
    child = sa.child_environment(env)
    assert child == {"PATH": "/usr/bin", "HOME": "/home/u", "ANTHROPIC_API_KEY": "kept-on-purpose",
                     "PSEUDOLIFE_MCP_TOKEN": "t"}


def test_the_shim_starts_under_a_hidden_console_not_detached():
    """CREATE_NO_WINDOW is ignored beside DETACHED_PROCESS, and a console
    program started from a console-less process opens a visible console:
    the blank Windows Terminal tab of 2026-07-12."""
    detached = getattr(sa.subprocess, "DETACHED_PROCESS", 0x8)
    no_window = getattr(sa.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    flags = sa._creation_flags(False)
    if os.name == "nt":
        assert flags & no_window and not flags & detached
        assert sa._creation_flags(True) & getattr(sa.subprocess, "CREATE_BREAKAWAY_FROM_JOB")
    else:
        assert flags == 0


def test_registration_note_says_when_the_task_still_carries_the_values(monkeypatch):
    monkeypatch.setattr(sa, "_registered_command", lambda kind: "python.exe evals/claude_shim.py --model x --port 8082")
    note = sa.registration_note("claude")
    assert "still carries the model" in note and "ops/install-shim-autostart" in note
    monkeypatch.setattr(sa, "_registered_command", lambda kind: "... ops/shim_autostart.py run claude ...")
    assert sa.registration_note("claude") == ""
    monkeypatch.setattr(sa, "_registered_command",
                        lambda kind: "<Settings><Enabled>false</Enabled></Settings> ... ops/shim_autostart.py run claude")
    if os.name == "nt":
        assert "is disabled" in sa.registration_note("claude")
    else:
        assert sa.registration_note("claude") == ""
    monkeypatch.setattr(sa, "_registered_command", lambda kind: None)
    note = sa.registration_note("codex")
    if os.name == "nt" or sa.shutil.which("systemctl"):
        assert "not registered" in note and "ops/install-codex-shim-autostart" in note
    else:
        assert note == ""


# ── the script itself ───────────────────────────────────────────────────────

def _run_script(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "ops" / "shim_autostart.py"), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


def test_run_dry_run_prints_the_resolved_command(tmp_path):
    root = tmp_path / "checkout"
    (root / "evals" / "prompts").mkdir(parents=True)
    (root / "ops").mkdir()
    (root / "evals" / "claude_shim.py").write_text("# shim\n", encoding="utf-8")
    (root / "evals" / "prompts" / "sonnet_extractor_v5.md").write_text("prompt\n", encoding="utf-8")
    (root / "ops" / ".env").write_text("PSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-sonnet-5\n", encoding="utf-8")
    proc = _run_script(root, "run", "claude", "--dry-run", "--port", "8099", "--python", sys.executable)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["problems"] == []
    argv = report["argv"]
    assert argv[0] == sys.executable and argv[1] == str(root / "evals" / "claude_shim.py")
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"        # from ops/.env
    assert argv[argv.index("--port") + 1] == "8099"                    # the flag wins
    proc = _run_script(root, "run", "claude", "--dry-run", "--prompt-file", "evals/prompts/nope.md",
                       "--python", sys.executable)
    assert proc.returncode == 1 and "prompt file not found" in proc.stdout
    proc = _run_script(root, "config", "claude", "--model", "claude-fable-5")
    assert proc.returncode == 0 and "wrote model" in proc.stdout
    proc = _run_script(root, "show", "claude", "--json")
    assert json.loads(proc.stdout)["settings"]["model"] == "claude-fable-5"
    proc = _run_script(root, "restart", "claude", "--dry-run")
    assert proc.returncode == 0 and proc.stdout.strip()


def test_a_detached_run_writes_a_failed_start_into_the_log(tmp_path):
    """The scheduled task has no console anyone reads: a mistyped port or a
    missing prompt file must show in the shim's log, not vanish."""
    root = tmp_path / "checkout"
    (root / "evals" / "prompts").mkdir(parents=True)
    (root / "ops").mkdir()
    (root / "evals" / "claude_shim.py").write_text("# shim\n", encoding="utf-8")
    log = tmp_path / "logs" / "claude-shim.log"
    (root / "ops" / ".env").write_text(f"PSEUDOLIFE_CLAUDE_SHIM_LOG={log.as_posix()}\n"
                                       "PSEUDOLIFE_CLAUDE_SHIM_PROMPT_FILE=evals/prompts/nope.md\n", encoding="utf-8")
    proc = _run_script(root, "run", "claude", "--python", sys.executable)
    assert proc.returncode == 1 and "prompt file not found" in proc.stderr
    assert "prompt file not found" in log.read_text(encoding="utf-8")
    proc = _run_script(root, "run", "claude", "--python", sys.executable, "--port", "80x")
    assert proc.returncode == 2                                         # argparse: --port takes an int
    (root / "ops" / ".env").write_text("PSEUDOLIFE_CLAUDE_SHIM_PORT=eighty\n", encoding="utf-8")
    proc = _run_script(root, "run", "claude", "--dry-run")
    assert proc.returncode == 1 and "PSEUDOLIFE_CLAUDE_SHIM_PORT must be a number" in proc.stderr


# ── the installers register the runner, not a baked command line ────────────

def test_the_task_and_the_unit_run_the_runner_with_no_baked_values():
    ps = (ROOT / "ops" / "install-shim-autostart.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "ops" / "install-shim-autostart.sh").read_text(encoding="utf-8")
    cps = (ROOT / "ops" / "install-codex-shim-autostart.ps1").read_text(encoding="utf-8")
    csh = (ROOT / "ops" / "install-codex-shim-autostart.sh").read_text(encoding="utf-8")
    # the registered action names the runner and the kind, and no model/port/prompt
    for text, kind in ((ps, "claude"), (cps, "codex")):
        assert "`$psi.FileName = '$($PythonExe -replace \"'\", \"''\")'" in text
        assert ("`$psi.Arguments = '$((\"`\"$repo\\ops\\shim_autostart.py`\" run " + kind
                + "\") -replace \"'\", \"''\")'") in text
        assert "`$psi.CreateNoWindow = `$true" in text
        assert "$innerCmd" not in text
    assert "ExecStart=$PYTHON_EXE $repo/ops/shim_autostart.py run claude --foreground" in sh
    assert "ExecStart=$PYTHON_EXE $repo/ops/shim_autostart.py run codex --foreground" in csh
    for text in (ps, cps):
        spawner = text.split("$inner = @\"", 1)[1].split("\"@", 1)[0]
        assert "--model" not in spawner and "--port" not in spawner and "--system-prompt-file" not in spawner
    # the flags are written into ops/.env before the task or unit is registered
    for text, kind in ((ps, "claude"), (cps, "codex")):
        assert '(Join-Path $repo "ops\\shim_autostart.py") ' in text
        config_call = f"config {kind}" if f"config {kind}" in text else f'"config", "{kind}"'
        assert config_call in text
        assert text.index(config_call) < text.index("Register-ScheduledTask -TaskName $taskName")
    for text, kind in ((sh, "claude"), (csh, "codex")):
        assert f'"$repo/ops/shim_autostart.py" config {kind}' in text
        assert text.index(f"config {kind}") < text.index("systemctl --user daemon-reload")
    # and every installer says how to change a value later
    for text in (ps, sh, cps, csh):
        assert "shim_autostart.py restart" in text


def test_restart_refuses_a_registration_that_still_carries_its_values_unless_forced(repo, monkeypatch, capsys):
    """An install registered before this runner keeps its model, interpreter
    and log on the task's or unit's command line. ``restart`` used to stop
    that shim and start one from ops/.env — the defaults, for an install
    that never wrote the block — and only then print the note (review,
    2026-09-29). It refuses now, naming the installer to run once; --force
    restarts from ops/.env anyway."""
    monkeypatch.setattr(sa, "_registered_command",
                        lambda kind: "python.exe evals/claude_shim.py --model claude-sonnet-5 --port 8082")
    monkeypatch.setattr(sa, "check", lambda kind, repo, settings: [])
    restarts = []
    monkeypatch.setattr(sa, "restart", lambda kind, repo, settings, *, dry_run: restarts.append(kind) or 0)
    assert sa.main(["--repo", str(repo), "restart", "claude"]) == 1
    err = capsys.readouterr().err
    assert restarts == []
    assert "still carries the model" in err and "--force" in err
    assert sa.main(["--repo", str(repo), "restart", "claude", "--force"]) == 0
    assert restarts == ["claude"]
    # a registration that runs this runner needs no flag
    monkeypatch.setattr(sa, "_registered_command", lambda kind: "... ops/shim_autostart.py run claude ...")
    assert sa.main(["--repo", str(repo), "restart", "claude"]) == 0
    assert restarts == ["claude", "claude"]
    # --dry-run only prints the plan; it never refuses
    monkeypatch.setattr(sa, "_registered_command",
                        lambda kind: "python.exe evals/claude_shim.py --model claude-sonnet-5 --port 8082")
    assert sa.main(["--repo", str(repo), "restart", "claude", "--dry-run"]) == 0
