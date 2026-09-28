"""Read-only runtime diagnostics: never spawn a daemon or call a bank tool."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys

# Where Claude Code looks for bash.exe on Windows when CLAUDE_CODE_GIT_BASH_PATH
# is not set, before the `git` on PATH (code.claude.com/docs/en/troubleshoot-install.md).
GIT_BASH_DEFAULTS = (r"C:\Program Files\Git\bin\bash.exe",
                     r"C:\Program Files (x86)\Git\bin\bash.exe")
_BASH_NAMES = {"bash.exe", "sh.exe", "bash", "sh"}
# The WSL launcher lives in System32; the Store build's alias in WindowsApps.
_WSL_LAUNCHER_DIRS = {"system32", "windowsapps"}


def _windows() -> bool:
    return os.name == "nt"


def claude_settings_env(env, *, home: Path | None = None) -> dict:
    """The string entries of the ``env`` block in Claude Code's user settings
    (``$CLAUDE_CONFIG_DIR/settings.json``, else ``~/.claude/settings.json``),
    which Claude Code applies to its own environment and where its docs put
    ``CLAUDE_CODE_GIT_BASH_PATH``. ``{}`` when the file is absent or not a
    JSON object with an ``env`` object."""
    base = (Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR")
            else (home or Path.home()) / ".claude")
    try:
        settings = json.loads((base / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    block = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(block, dict):
        return {}
    return {key: value for key, value in block.items()
            if isinstance(key, str) and isinstance(value, str)}


def find_git_bash(env, *, defaults=GIT_BASH_DEFAULTS, which=shutil.which) -> str | None:
    """The bash.exe Claude Code would run hook commands with, by its rules:
    ``CLAUDE_CODE_GIT_BASH_PATH`` when it names an existing bash or sh
    binary, then the default Git for Windows install directories, then
    ``bin\\bash.exe`` two directories up from the ``git`` on PATH (Git's
    ``cmd\\git.exe``, the one its installer puts on PATH; a
    ``mingw64\\bin\\git.exe`` does not resolve, in Claude Code either).
    ``None`` when none exists: Claude Code then runs hooks in PowerShell,
    where the plugin's Bash commands fail."""
    configured = env.get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured:
        path = Path(configured)
        if path.name.lower() in _BASH_NAMES and path.is_file():
            return configured
    for candidate in defaults:
        if Path(candidate).is_file():
            return candidate
    git = which("git")
    if git:
        candidate = Path(git).parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def git_bash_report(env, *, defaults=GIT_BASH_DEFAULTS, which=shutil.which) -> dict:
    """The Windows half of the report: the Git Bash Claude Code would use,
    what a bare ``bash`` on PATH runs, and recovery text when either is
    wrong. ``bash`` resolving to the WSL launcher does not affect Claude
    Code, which never looks bash up on PATH, but breaks any tool that does
    (the 2026-09-12 Codex hook failures ran plugin scripts under WSL).
    ``CLAUDE_CODE_GIT_BASH_PATH`` from the Claude Code settings env block
    wins over this process's environment, as it does inside Claude Code."""
    configured = claude_settings_env(env).get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured:
        env = {**env, "CLAUDE_CODE_GIT_BASH_PATH": configured}
    git_bash = find_git_bash(env, defaults=defaults, which=which)
    bash_on_path = which("bash")
    is_wsl_launcher = bool(bash_on_path
                           and Path(bash_on_path).parent.name.lower() in _WSL_LAUNCHER_DIRS)
    report = {"git_bash": git_bash, "bash_on_path": bash_on_path,
              "bash_on_path_is_wsl_launcher": is_wsl_launcher}
    if git_bash is None:
        report["git_bash_recovery"] = (
            "Claude Code runs the plugin's hook commands through Git Bash and found "
            "none: install Git for Windows (the default location, or put its cmd "
            "directory on PATH), or set CLAUDE_CODE_GIT_BASH_PATH to its bin\\bash.exe "
            "in the env block of ~/.claude/settings.json; then restart Claude Code. "
            "Codex hooks run in PowerShell and are unaffected.")
    elif is_wsl_launcher:
        report["git_bash_recovery"] = (
            f"`bash` on PATH is the WSL launcher ({bash_on_path}). Claude Code still "
            f"runs the plugin hooks through {git_bash}, but a bare `bash` in a "
            "terminal or another tool opens WSL, which cannot read the plugin's "
            "Windows paths: put Git's bin directory ahead of System32 on PATH, or "
            "call that bash.exe by its full path.")
    return report


async def _handshake() -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "pseudolife_memory.cli"],
        env={**os.environ, "PSEUDOLIFE_MCP_NO_SPAWN": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            result = await client.initialize()
            manifest = (await client.list_tools()).tools
            return {
                "instructions_present": bool(result.instructions),
                "tool_count": len(manifest),
                "tools_missing_annotations": [t.name for t in manifest if t.annotations is None],
            }


_OFF = {"0", "false", "no", "off"}
_YES = {"1", "true", "yes", "on"}
_CODEX_SERVER = "pseudolife-memory"
_PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"


def _read_toml(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(text)


def _claude_code_wake(health_enabled: bool | None) -> dict:
    """Claude Code's wake path is the plugin's Stop hook: the plugin must be
    installed (``plugins/installed_plugins.json``) and not disabled in
    settings.json ``enabledPlugins``; an MCP registration alone, from the
    installer or ``ops/install-hook.*``, has no Stop hook. The hook reads the
    ``env`` block of settings.json (Claude Code passes it to the processes it
    starts) over the launching environment."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path.home()
    registration = Path(config_dir) / ".claude.json" if config_dir else home / ".claude.json"
    config_dir = Path(config_dir) if config_dir else home / ".claude"
    installed = config_dir / "plugins" / "installed_plugins.json"
    registered = plugin_installed = False
    try:
        if registration.is_file():
            text = registration.read_text(encoding="utf-8")
            json.loads(text)
            registered = _CODEX_SERVER in text
        if installed.is_file():
            record = json.loads(installed.read_text(encoding="utf-8"))
            plugins = record.get("plugins", record) if isinstance(record, dict) else {}
            plugin_installed = isinstance(plugins, dict) and bool(plugins.get(_PLUGIN_ID))
    except (OSError, ValueError):
        return {"registered": "unknown (unreadable ~/.claude.json)"}
    if not (registered or plugin_installed):
        return {"registered": False}
    settings_data: dict = {}
    settings = config_dir / "settings.json"
    try:
        if settings.is_file():
            loaded = json.loads(settings.read_text(encoding="utf-8"))
            settings_data = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {"registered": True, "stop_hook": "unknown (unreadable settings.json)"}
    env = settings_data.get("env") if isinstance(settings_data.get("env"), dict) else {}
    enabled = settings_data.get("enabledPlugins")
    if not plugin_installed:
        return {"registered": True, "stop_hook": "off (plugin not installed)"}
    if isinstance(enabled, dict) and enabled.get(_PLUGIN_ID) is False:
        return {"registered": True, "stop_hook": "off (plugin disabled)"}

    def effective(key):
        return str(env.get(key, os.environ.get(key, "")) or "").strip()

    return {"registered": True,
            "stop_hook": _wake_state(health_enabled, effective, "PSEUDOLIFE_AGENT_WAKE_HOOK")}


def _codex_wake(health_enabled: bool | None) -> dict:
    """Codex's wake path is the shim's doorbell, which reads its MCP server's
    ``env`` table, the ``env_vars`` Codex forwards from the launching
    environment, and finds ``codex`` on PATH or in the desktop app's bin
    directory."""
    from pseudolife_memory.codex_doorbell import resolve_codex_command

    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    if not config.is_file():
        return {"registered": False}
    try:
        server = (_read_toml(config.read_text(encoding="utf-8"))
                  .get("mcp_servers", {}).get(_CODEX_SERVER))
    except ModuleNotFoundError:
        return {"registered": "unknown (reading config.toml needs Python 3.11, or tomli)"}
    except (OSError, ValueError):
        return {"registered": "unknown (unreadable config.toml)"}
    if not isinstance(server, dict):
        return {"registered": False}
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    forwarded = server.get("env_vars") if isinstance(server.get("env_vars"), list) else []

    def effective(key):
        if key in env:
            return str(env[key] or "").strip()
        return str(os.environ.get(key, "") or "").strip() if key in forwarded else ""

    state = _wake_state(health_enabled, effective, "PSEUDOLIFE_CODEX_DOORBELL")
    # The shim arms a Codex doorbell only as the codex writer, and its board
    # registry needs a bearer (orchestrator review of #434, 2026-09-28).
    if state == "on" and effective("PSEUDOLIFE_WRITER_ID").lower() != "codex":
        state = "off (PSEUDOLIFE_WRITER_ID is not codex)"
    if state == "on" and not (effective("PSEUDOLIFE_MCP_TOKEN")
                              or effective("PSEUDOLIFE_MCP_TOKEN_FILE")):
        state = "off (no bearer token)"
    if state == "on":
        lookup = {**os.environ, **{k: str(v) for k, v in env.items()}}
        if resolve_codex_command(lookup) is None:
            state = "off (no codex CLI)"
    return {"registered": True, "doorbell": state}


def _wake_state(health_enabled: bool | None, effective, flag: str) -> str:
    if health_enabled is False:
        return "off (coordination disabled on the daemon)"
    master = effective("PSEUDOLIFE_AGENT_COORDINATION")
    if master and master.lower() not in _YES:
        return f"off (PSEUDOLIFE_AGENT_COORDINATION={master})"
    value = effective(flag)
    if value and value.lower() not in _YES and (
            flag != "PSEUDOLIFE_AGENT_WAKE_HOOK" or value.lower() in _OFF):
        return f"off ({flag}={value})"
    return "on"


def _wake_report(health: dict | None) -> dict:
    """Wake on by default (2026-09-28): each registered client's wake path
    and the caps the daemon applies, so an operator can see who rings."""
    coordination = (health or {}).get("coordination")
    enabled = coordination.get("enabled") if isinstance(coordination, dict) else None
    if health is None:
        caps = "unknown (daemon unreachable)"
    elif not isinstance(coordination, dict) or not isinstance(coordination.get("wake"), dict):
        caps = "unknown (the daemon does not report them; update it)"
    else:
        caps = coordination["wake"]
    report = {}
    for name, probe in (("claude_code", _claude_code_wake), ("codex", _codex_wake)):
        try:
            report[name] = probe(enabled)
        except Exception as exc:  # noqa: BLE001 - a client's config never hides the report
            report[name] = {"registered": f"unknown ({type(exc).__name__})"}
    report["caps"] = caps
    return report


def _board_line(timeout: float) -> str:
    """The agent board's status for the credential this shim would send."""
    from pseudolife_memory.board_status import board_status
    from pseudolife_memory.credentials import CredentialProvider
    from pseudolife_memory.daemon_url import _daemon_url

    try:
        token = CredentialProvider.from_environment().snapshot().token
    except Exception:  # noqa: BLE001 - never serialize credential errors
        return "off - the configured token file is missing, unsafe or malformed"
    try:
        url = _daemon_url()
    except (Exception, SystemExit):  # noqa: BLE001 - an invalid URL is reported elsewhere
        return "off - PSEUDOLIFE_MCP_DAEMON_URL is not a usable daemon URL"
    return board_status(url, token, timeout=timeout)[1]


def run_doctor() -> None:
    from pseudolife_memory.shim import _daemon_url, _require_mcp_sdk_v2, probe_health

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20,
                        help="total MCP handshake budget in seconds (default: 20)")
    args = parser.parse_args(sys.argv[2:])
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    report = {"ok": False, "interpreter": sys.executable,
              "source": str(Path(__file__).resolve().parent)}
    for package in ("pseudolife-mcp", "mcp"):
        try:
            report[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report[package] = "not installed"
    report["board"] = _board_line(min(args.timeout, 2))
    health = None
    if _windows():
        report.update(git_bash_report(os.environ))
    try:
        _require_mcp_sdk_v2()
        health = probe_health(_daemon_url(), timeout=min(args.timeout, 2))
        report["daemon_status"] = health.get("status") if health else "unreachable"
        if not health or health.get("status") != "ok":
            report["error"] = "DaemonUnavailable"
            report["recovery"] = "Start the intended daemon, then retry; doctor never starts one."
        else:
            report["daemon_version"] = health.get("version") or "unknown"
            report.update(asyncio.run(asyncio.wait_for(_handshake(), timeout=args.timeout)))
            report["ok"] = bool(report["instructions_present"] and report["tool_count"]
                                and not report["tools_missing_annotations"])
            if not report["ok"]:
                report["recovery"] = "Check shim stderr and daemon MCP access, then compare daemon and shim versions; update the component missing instructions or annotations and reconnect."
            # Both halves were already in the report; the comparison used to
            # be left to the reader (2026-09-21).
            installed = report["pseudolife-mcp"]
            if (report["daemon_version"] != "unknown" and installed != "not installed"
                    and report["daemon_version"] != installed):
                report["ok"] = False
                report["version_mismatch"] = True
                report["recovery"] = (
                    f"The shim is pseudolife-mcp {report['pseudolife-mcp']} but the daemon "
                    f"is {report['daemon_version']}. Run pseudolife-mcp update --clients-only "
                    f"--tag {report['daemon_version']} (the daemon's release as a new shim "
                    f"runtime beside the running one; from a checkout: python "
                    f"ops/update_clients.py --only shim; no session has to close), or update "
                    f"the daemon with pseudolife-mcp update; then start a new session and retry.")
    except TimeoutError:
        report["error"] = "TimeoutError"
        report["recovery"] = "Check daemon health and MCP access; if startup is slow, retry doctor with a larger --timeout budget."
    except (Exception, SystemExit) as exc:
        # Do not serialize transport exceptions: they can contain auth headers
        # or URL credentials. The exception type plus recovery is sufficient.
        report["error"] = type(exc).__name__
        report["recovery"] = "Check daemon health and the exact registered interpreter. Run that interpreter with -m pip check and -m pip show pseudolife-mcp mcp; reinstall there if stale, then retry."
    report["wake"] = _wake_report(health if isinstance(health, dict) else None)
    # No Git Bash fails the report whatever the daemon said: the shim works,
    # the Claude Code plugin hooks do not. An earlier error keeps its name
    # and recovery; the Git Bash advice is in git_bash_recovery either way.
    if "git_bash" in report and report["git_bash"] is None:
        report["ok"] = False
        if "error" not in report:
            report["error"] = "GitBashMissing"
            report["recovery"] = report["git_bash_recovery"]
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
