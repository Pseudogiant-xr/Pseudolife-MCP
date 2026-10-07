"""Doctor failure reports must retain safe, actionable recovery advice."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pseudolife_memory import doctor_cli, shim

_CREDENTIAL_KEYS = ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE",
                    "PSEUDOLIFE_MCP_DAEMON_URL")


@pytest.fixture(autouse=True)
def _isolated_client_config(tmp_path, monkeypatch):
    """No test reads the host's real client registrations, and the
    credential keys doctor may copy from a registration into os.environ
    are restored afterwards (setenv first, so delenv records them)."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-codex"))
    for key in _CREDENTIAL_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


# --- Git Bash on Windows -----------------------------------------------------
#
# Claude Code runs the plugin's Bash hook commands through Git Bash on
# Windows, found by its own rules, not by `bash` on PATH: CLAUDE_CODE_GIT_BASH_PATH,
# then the default Git for Windows install directories, then the `git` on
# PATH's own bin\bash.exe (code.claude.com/docs/en/troubleshoot-install.md;
# verified on 2.1.280 on 2026-09-27 with every Git directory stripped from
# PATH). Without one, hooks fall back to PowerShell, where every plugin hook
# command fails, so doctor fails loudly. `bash` on PATH being the WSL launcher
# (System32 or the Store alias) does not affect Claude Code and is a warning.

def _git_layout(tmp_path):
    for name in ("cmd/git.exe", "bin/bash.exe", "mingw64/bin/git.exe"):
        path = tmp_path / "Git" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return tmp_path / "Git"


def test_git_bash_is_found_the_way_claude_code_finds_it(tmp_path):
    git = _git_layout(tmp_path)
    defaults = (str(tmp_path / "absent/Git/bin/bash.exe"),)
    # 1. CLAUDE_CODE_GIT_BASH_PATH, when it names an existing bash/sh binary.
    env = {"CLAUDE_CODE_GIT_BASH_PATH": str(git / "bin/bash.exe")}
    assert doctor_cli.find_git_bash(env, defaults=defaults, which=lambda _: None) == str(git / "bin/bash.exe")
    for bad in (str(git / "git-bash.exe"), str(tmp_path / "missing/bash.exe")):
        assert doctor_cli.find_git_bash({"CLAUDE_CODE_GIT_BASH_PATH": bad},
                                        defaults=defaults, which=lambda _: None) is None
    # 2. The default install directories.
    assert doctor_cli.find_git_bash({}, defaults=(str(git / "bin/bash.exe"),),
                                    which=lambda _: None) == str(git / "bin/bash.exe")
    # 3. bin\bash.exe two directories up from the git on PATH: Claude Code
    # joins the git path with `..`, `..`, `bin`, `bash.exe`, so Git's cmd\
    # (the directory the installer puts on PATH) resolves and mingw64\bin\
    # does not.
    found = doctor_cli.find_git_bash({}, defaults=defaults,
                                     which=lambda name: str(git / "cmd/git.exe") if name == "git" else None)
    assert Path(found) == git / "bin/bash.exe"
    assert doctor_cli.find_git_bash({}, defaults=defaults,
                                    which=lambda name: str(git / "mingw64/bin/git.exe") if name == "git" else None) is None
    assert doctor_cli.find_git_bash({}, defaults=defaults, which=lambda _: None) is None


def test_git_bash_path_is_read_from_the_claude_settings_env_block(tmp_path):
    """The docs (and this module's own recovery text) put
    CLAUDE_CODE_GIT_BASH_PATH in the env block of ~/.claude/settings.json,
    which Claude Code applies to its own environment; doctor runs outside it
    (orchestrator review of #429, 2026-09-28)."""
    git = _git_layout(tmp_path)
    config = tmp_path / "claude-config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps(
        {"env": {"CLAUDE_CODE_GIT_BASH_PATH": str(git / "bin/bash.exe"), "OTHER": 1}}), encoding="utf-8")
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    assert doctor_cli.claude_settings_env(env) == {"CLAUDE_CODE_GIT_BASH_PATH": str(git / "bin/bash.exe")}
    report = doctor_cli.git_bash_report(env, defaults=(), which=lambda _: None)
    assert report["git_bash"] == str(git / "bin/bash.exe") and "git_bash_recovery" not in report
    # The settings block wins over the process environment, as in Claude Code.
    report = doctor_cli.git_bash_report({**env, "CLAUDE_CODE_GIT_BASH_PATH": str(tmp_path / "missing/bash.exe")},
                                        defaults=(), which=lambda _: None)
    assert report["git_bash"] == str(git / "bin/bash.exe")
    for broken in ("{not json", "[]", '{"env": ["x"]}', '{"env": {"CLAUDE_CODE_GIT_BASH_PATH": 3}}'):
        (config / "settings.json").write_text(broken, encoding="utf-8")
        assert doctor_cli.claude_settings_env(env) == {}
    (config / "settings.json").unlink()
    assert doctor_cli.claude_settings_env(env) == {}
    # Without CLAUDE_CONFIG_DIR the user settings live under ~/.claude.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/settings.json").write_text(json.dumps({"env": {"A": "b"}}), encoding="utf-8")
    assert doctor_cli.claude_settings_env({}, home=home) == {"A": "b"}


def test_git_bash_report_flags_the_wsl_launcher_on_path(tmp_path):
    git = _git_layout(tmp_path)
    system32 = tmp_path / "Windows/System32/bash.exe"
    system32.parent.mkdir(parents=True)
    system32.write_bytes(b"")
    which = {"git": str(git / "cmd/git.exe"), "bash": str(system32)}.get
    # An empty config dir: never the real ~/.claude/settings.json of the host.
    no_settings = {"CLAUDE_CONFIG_DIR": str(tmp_path / "no-claude-config")}
    report = doctor_cli.git_bash_report(no_settings, defaults=(), which=which)
    assert Path(report["git_bash"]) == git / "bin/bash.exe"
    assert report["bash_on_path"] == str(system32)
    assert report["bash_on_path_is_wsl_launcher"] is True
    assert "Claude Code" in report["git_bash_recovery"] and "PATH" in report["git_bash_recovery"]
    clean = doctor_cli.git_bash_report(no_settings, defaults=(), which={"git": str(git / "cmd/git.exe"),
                                                                "bash": str(git / "bin/bash.exe")}.get)
    assert clean["bash_on_path_is_wsl_launcher"] is False and "git_bash_recovery" not in clean


def test_doctor_fails_loudly_without_git_bash_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: {"status": "ok", "version": "1"})
    monkeypatch.setattr(doctor_cli, "_handshake", AsyncMock(return_value={
        "instructions_present": True, "tool_count": 3, "tools_missing_annotations": []}))
    monkeypatch.setattr(doctor_cli, "_windows", lambda: True)
    monkeypatch.setattr(doctor_cli, "git_bash_report", lambda env: {
        "git_bash": None, "bash_on_path": None, "bash_on_path_is_wsl_launcher": False,
        "git_bash_recovery": "Install Git for Windows."})
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert exit_info.value.code == 1
    assert report["ok"] is False and report["error"] == "GitBashMissing"
    assert report["git_bash"] is None and "Git for Windows" in report["recovery"]


def test_doctor_reports_git_bash_only_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: None)
    monkeypatch.setattr(doctor_cli, "_windows", lambda: False)
    with pytest.raises(SystemExit):
        doctor_cli.run_doctor()
    assert "git_bash" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("failure,hint", [
    ("unreachable", "Start the intended daemon"),
    ("timeout", "--timeout"),
    ("transport", "exact registered interpreter"),
])
def test_doctor_reports_specific_safe_recovery(monkeypatch, capsys, failure, hint):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw:
                        None if failure == "unreachable" else {"status": "ok"})
    handshake = AsyncMock(side_effect=TimeoutError() if failure == "timeout"
                          else RuntimeError("fixture-secret-must-not-leak"))
    monkeypatch.setattr(doctor_cli, "_handshake", handshake)
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is False
    assert hint in report["recovery"]
    assert "fixture-secret-must-not-leak" not in output
    if failure == "unreachable":
        handshake.assert_not_called()


# --- The credential a shell lacks --------------------------------------------
#
# Run from a plain shell, doctor used to read the credential only from its own
# environment. On a Docker-tier host with bearer auth the handshake's shim then
# exited on its missing-credential line and doctor reported ExceptionGroup with
# advice to reinstall the interpreter (2026-09-29), although the Claude Code or
# Codex registration it had just read carries PSEUDOLIFE_MCP_TOKEN_FILE.

def _claude_registration(config_dir: Path, env: dict) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": "shim", "args": [], "env": env}}}), encoding="utf-8")
    return path


def _doctor_with_auth(monkeypatch, handshake):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(doctor_cli, "_windows", lambda: False)
    monkeypatch.setattr(doctor_cli, "_board_probe", lambda timeout: {"state": "disabled", "line": "off"})
    monkeypatch.setattr(doctor_cli, "_codex_hooks_line", lambda health: "current")
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: {
        "status": "ok", "auth": True, "version": doctor_cli.importlib.metadata.version("pseudolife-mcp")})
    monkeypatch.setattr(doctor_cli, "_handshake", handshake)


def test_registration_supplies_the_credential_the_shell_lacks(tmp_path):
    token_file = tmp_path / "token"
    config = tmp_path / "claude"
    path = _claude_registration(config, {
        "PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file),
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9999",
        "PSEUDOLIFE_WRITER_ID": "claude-code"})
    overrides, source = doctor_cli.registration_credentials(
        {"CLAUDE_CONFIG_DIR": str(config), "CODEX_HOME": str(tmp_path / "none")})
    assert overrides == {"PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file),
                         "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9999"}
    assert "Claude Code" in source and str(path) in source
    # A daemon URL the shell already sets is the shell's.
    overrides, _ = doctor_cli.registration_credentials(
        {"CLAUDE_CONFIG_DIR": str(config), "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:1"})
    assert overrides == {"PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file)}
    # A credential in the shell wins; no registration is consulted.
    assert doctor_cli.registration_credentials(
        {"CLAUDE_CONFIG_DIR": str(config), "PSEUDOLIFE_MCP_TOKEN": "t"}) == ({}, "environment")


def test_codex_registration_supplies_the_credential_when_claude_code_has_none(tmp_path):
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        '[mcp_servers.pseudolife-memory]\ncommand = "shim"\n'
        '[mcp_servers.pseudolife-memory.env]\nPSEUDOLIFE_MCP_TOKEN_FILE = "/tokens/codex"\n',
        encoding="utf-8")
    # A Claude Code registration without a credential (HTTP, or an older
    # install) does not stop the search.
    _claude_registration(tmp_path / "claude", {"PSEUDOLIFE_WRITER_ID": "claude-code"})
    overrides, source = doctor_cli.registration_credentials(
        {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude"), "CODEX_HOME": str(codex)})
    assert overrides == {"PSEUDOLIFE_MCP_TOKEN_FILE": "/tokens/codex"}
    assert "Codex" in source and "config.toml" in source
    assert doctor_cli.registration_credentials(
        {"CLAUDE_CONFIG_DIR": str(tmp_path / "x"), "CODEX_HOME": str(tmp_path / "y")}) == ({}, None)


def test_doctor_hands_the_registration_credential_to_the_handshake(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "token"
    _claude_registration(tmp_path / "isolated-claude", {"PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file)})
    seen = {}

    async def handshake():
        seen["token_file"] = doctor_cli.os.environ.get("PSEUDOLIFE_MCP_TOKEN_FILE")
        return {"instructions_present": True, "tool_count": 3, "tools_missing_annotations": []}

    _doctor_with_auth(monkeypatch, handshake)
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert seen["token_file"] == str(token_file)
    assert "Claude Code" in report["credential_source"]
    assert exit_info.value.code == 0 and report["ok"] is True


def test_doctor_names_the_missing_bearer_not_the_interpreter(monkeypatch, capsys):
    handshake = AsyncMock(side_effect=RuntimeError("unhandled errors in a TaskGroup"))
    _doctor_with_auth(monkeypatch, handshake)
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert exit_info.value.code == 1 and report["ok"] is False
    assert report["error"] == "BearerMissing" and report["credential_source"] == "none"
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in report["recovery"]
    assert "PSEUDOLIFE_MCP_TOKEN" in report["recovery"]
    assert "ExceptionGroup" not in output and "reinstall" not in report["recovery"]
    handshake.assert_not_called()

# --- which `pseudolife-mcp` a terminal runs ------------------------------------
#
# 2026-09-29: after every registration moved to the launcher, `pseudolife-mcp`
# on PATH was still pipx's copy of the old package. doctor names both and
# warns when they differ.

def _launcher_layout(tmp_path, monkeypatch):
    name = "pseudolife-mcp.exe" if doctor_cli._windows() else "pseudolife-mcp"
    launcher = tmp_path / "bin" / name
    launcher.parent.mkdir()
    launcher.write_text("launcher", encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "runtimes"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(launcher))
    return launcher


def test_path_resolution_warns_when_the_name_runs_something_else(tmp_path, monkeypatch):
    launcher = _launcher_layout(tmp_path, monkeypatch)
    old = str(tmp_path / "pipx" / "bin" / "pseudolife-mcp")
    report = doctor_cli.path_resolution(which=lambda name: old)
    assert report["on_path"] == old and report["launcher"] == str(launcher)
    assert old in report["warning"] and str(launcher) in report["warning"]
    same = doctor_cli.path_resolution(which=lambda name: str(launcher))
    assert same["on_path"] == str(launcher) and "warning" not in same
    missing = doctor_cli.path_resolution(which=lambda name: None)
    assert missing["on_path"] is None and str(launcher) in missing["warning"]


def test_path_resolution_is_quiet_without_a_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_SHIM_RUNTIMES", str(tmp_path / "runtimes"))
    monkeypatch.setenv("PSEUDOLIFE_SHIM_LAUNCHER", str(tmp_path / "bin" / (
        "pseudolife-mcp.exe" if doctor_cli._windows() else "pseudolife-mcp")))
    report = doctor_cli.path_resolution(which=lambda name: "/usr/bin/pseudolife-mcp")
    assert report["launcher"] is None and "warning" not in report


def test_doctor_reports_path_resolution(tmp_path, monkeypatch, capsys):
    _launcher_layout(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: None)
    monkeypatch.setattr(doctor_cli, "_windows", lambda: False)
    with pytest.raises(SystemExit):
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert set(report["path_resolution"]) >= {"on_path", "launcher"}


# --- maintainer passkeys ------------------------------------------------------
#
# Read through the daemon's own GET /api/maintainer with the bearer the board
# probe uses; doctor opens no database connection. Off is information, a
# configuration the daemon refuses is a failure with the fix.

class _MaintainerDaemon:
    def __init__(self, status: int, body: dict):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.seen = []
        daemon = self
        raw = json.dumps(body).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's naming
                daemon.seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


_KEY = {"credential_id": "k", "state": "active", "active_from": 1.0}


def test_maintainer_passkeys_on_reports_rp_id_origin_and_active_keys():
    body = {"available": True, "rp_id": "box.example", "origin": "https://box.example:8443",
            "passkeys": [_KEY, {**_KEY, "state": "revoked"}, {**_KEY, "state": "pending"}]}
    with _MaintainerDaemon(200, body) as daemon:
        out = doctor_cli.maintainer_probe(daemon.url, "fixture-token", timeout=2)
    assert daemon.seen == [("/api/maintainer", "Bearer fixture-token")]
    assert out["state"] == "on" and out["active_keys"] == 1
    assert (out["rp_id"], out["origin"]) == ("box.example", "https://box.example:8443")
    assert out["line"] == "on - rp_id box.example, origin https://box.example:8443, 1 active key(s)"
    assert "database password" in out["note"]
    assert "Maintainer messages and roles from the Console" in out["note"]


def test_maintainer_passkeys_unset_is_information_not_failure():
    body = {"error": "maintainer_https_required", "config_problem": "unset"}
    with _MaintainerDaemon(409, body) as daemon:
        out = doctor_cli.maintainer_probe(daemon.url, "fixture-token", timeout=2)
    assert out["state"] == "off" and "recovery" not in out
    assert "pseudolife-mcp maintainer setup" in out["line"]      # the way to turn it on


def test_a_maintainer_config_the_daemon_refuses_is_a_failure_with_the_fix():
    body = {"error": "maintainer_https_required",
            "config_problem": "plain http is allowed for rp_id localhost only"}
    with _MaintainerDaemon(409, body) as daemon:
        out = doctor_cli.maintainer_probe(daemon.url, "fixture-token", timeout=2)
    assert out["state"] == "invalid" and "plain http" in out["line"]
    assert out["recovery"] == doctor_cli.MAINTAINER_FIX
    assert "coordination.maintainer.origin" in doctor_cli.MAINTAINER_FIX
    assert doctor_cli.MAINTAINER_FIX.startswith("Run `pseudolife-mcp maintainer setup`")


@pytest.mark.parametrize("status,body,state", [
    (404, {"error": "not_found"}, "unsupported"),
    (409, {"error": "maintainer_https_required"}, "off_or_invalid"),   # an older daemon
    (503, {"error": "coordination_unavailable"}, "not_checked"),
])
def test_other_maintainer_answers_are_reported_plainly(status, body, state):
    with _MaintainerDaemon(status, body) as daemon:
        out = doctor_cli.maintainer_probe(daemon.url, "fixture-token", timeout=2)
    assert out["state"] == state and "recovery" not in out
    assert doctor_cli.maintainer_probe(daemon.url, None, timeout=2)["state"] == "not_checked"


def test_doctor_fails_on_a_maintainer_config_the_daemon_refuses(monkeypatch, capsys):
    async def handshake():
        return {"instructions_present": True, "tool_count": 3, "tools_missing_annotations": []}

    _doctor_with_auth(monkeypatch, handshake)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-token")
    monkeypatch.setattr(doctor_cli, "_board_probe", lambda timeout: {"state": "on", "line": "on"})
    probe = {"state": "invalid", "line": "invalid - origin must be https",
             "recovery": "fix it"}
    monkeypatch.setattr(doctor_cli, "_maintainer_probe", lambda timeout: probe)
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert report["maintainer_passkeys"] == probe
    assert exit_info.value.code == 1 and report["ok"] is False
    assert report["error"] == "MaintainerPasskeysInvalid" and report["recovery"] == "fix it"
    # Off is information: the report stays ok.
    monkeypatch.setattr(doctor_cli, "_maintainer_probe",
                        lambda timeout: {"state": "off", "line": "off"})
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    assert exit_info.value.code == 0


def test_doctor_skips_the_maintainer_check_while_the_board_is_off(monkeypatch, capsys):
    async def handshake():
        return {"instructions_present": True, "tool_count": 3, "tools_missing_annotations": []}

    _doctor_with_auth(monkeypatch, handshake)
    monkeypatch.setattr(doctor_cli, "_maintainer_probe",
                        lambda timeout: pytest.fail("probed with the board off"))
    with pytest.raises(SystemExit):
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert report["maintainer_passkeys"]["state"] == "not_checked"
