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
                    f"is {report['daemon_version']}. Reinstall the shim from the daemon's "
                    f"checkout in the registered interpreter (re-run the installer, or "
                    f"pipx install --force <checkout>), or redeploy the daemon "
                    f"(ops/update.ps1 / ops/update.sh); then retry.")
    except TimeoutError:
        report["error"] = "TimeoutError"
        report["recovery"] = "Check daemon health and MCP access; if startup is slow, retry doctor with a larger --timeout budget."
    except (Exception, SystemExit) as exc:
        # Do not serialize transport exceptions: they can contain auth headers
        # or URL credentials. The exception type plus recovery is sufficient.
        report["error"] = type(exc).__name__
        report["recovery"] = "Check daemon health and the exact registered interpreter. Run that interpreter with -m pip check and -m pip show pseudolife-mcp mcp; reinstall there if stale, then retry."
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
