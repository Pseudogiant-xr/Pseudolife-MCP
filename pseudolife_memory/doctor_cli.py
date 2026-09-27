"""Read-only runtime diagnostics: never spawn a daemon or call a bank tool."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys


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


def _read_toml(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(text)


def _claude_code_wake(health_enabled: bool | None) -> dict:
    """Claude Code's wake path is the plugin's Stop hook, which reads the
    ``env`` block of settings.json (Claude Code passes it to the processes it
    starts) over the launching environment."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path.home()
    registration = Path(config_dir) / ".claude.json" if config_dir else home / ".claude.json"
    config_dir = Path(config_dir) if config_dir else home / ".claude"
    registered = False
    try:
        if registration.is_file():
            text = registration.read_text(encoding="utf-8")
            json.loads(text)
            registered = _CODEX_SERVER in text
        installed = config_dir / "plugins" / "installed_plugins.json"
        if not registered and installed.is_file():
            registered = _CODEX_SERVER in installed.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {"registered": "unknown (unreadable ~/.claude.json)"}
    if not registered:
        return {"registered": False}
    env: dict = {}
    settings = config_dir / "settings.json"
    try:
        if settings.is_file():
            block = json.loads(settings.read_text(encoding="utf-8")).get("env")
            env = block if isinstance(block, dict) else {}
    except (OSError, ValueError):
        return {"registered": True, "stop_hook": "unknown (unreadable settings.json)"}

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
    health = None
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
    report["wake"] = _wake_report(health if isinstance(health, dict) else None)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
