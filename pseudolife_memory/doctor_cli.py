"""Runtime diagnostics; read-only unless --disposable-proof is explicitly selected."""
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
        env={**os.environ, "PSEUDOLIFE_MCP_NO_SPAWN": "1",
             "PSEUDOLIFE_AGENT_COORDINATION": "0"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            result = await client.initialize()
            manifest = (await client.list_tools()).tools
            return {
                "instructions_present": bool(result.instructions),
                "tool_count": len(manifest),
                "tools_missing_annotations": [t.name for t in manifest if t.annotations is None],
                "coordination_tools_present": all(name in {t.name for t in manifest}
                                                  for name in ("memory_agents", "memory_message")),
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


_TOKEN_KEYS = ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE")
_URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"


def _registration_env_blocks(env):
    """``(label, env block)`` for this server's Claude Code registration
    (``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``), then its
    Codex one (``$CODEX_HOME/config.toml``, else ``~/.codex/config.toml``);
    an absent or unreadable file yields nothing."""
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    claude = Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
    codex = Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    for label, path, parse, table in (
            ("Claude Code", claude, json.loads, "mcpServers"),
            ("Codex", codex, _read_toml, "mcp_servers")):
        try:
            data = parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, ModuleNotFoundError):
            continue
        servers = data.get(table) if isinstance(data, dict) else None
        server = servers.get(_CODEX_SERVER) if isinstance(servers, dict) else None
        block = server.get("env") if isinstance(server, dict) else None
        if isinstance(block, dict):
            yield f"{label} registration ({path})", block


def registration_credentials(env) -> tuple[dict, str | None]:
    """The credential doctor should use when run outside a client: ``({},
    "environment")`` when ``env`` already has ``PSEUDOLIFE_MCP_TOKEN`` or
    ``PSEUDOLIFE_MCP_TOKEN_FILE``; else the token keys (and the daemon URL,
    when ``env`` has none) from the first registration whose env block
    carries a token, with a label naming it; ``({}, None)`` when none does.
    A plain shell has neither key, and without them the handshake's shim
    exits on its missing-credential line (2026-09-29)."""
    if env.get("PSEUDOLIFE_MCP_TOKEN") or "PSEUDOLIFE_MCP_TOKEN_FILE" in env:
        return {}, "environment"
    for label, block in _registration_env_blocks(env):
        found = {key: block[key] for key in _TOKEN_KEYS
                 if isinstance(block.get(key), str) and block[key]}
        if not found:
            continue
        if not env.get(_URL_KEY) and isinstance(block.get(_URL_KEY), str) and block[_URL_KEY]:
            found[_URL_KEY] = block[_URL_KEY]
        return found, label
    return {}, None


_BEARER_MISSING = (
    "The daemon requires bearer authentication (/health reports auth=true) and doctor "
    "found no credential: neither PSEUDOLIFE_MCP_TOKEN_FILE nor PSEUDOLIFE_MCP_TOKEN is "
    "set in this shell, and no Claude Code (~/.claude.json) or Codex (config.toml) "
    "registration of pseudolife-memory carries one in its env block. Set "
    "PSEUDOLIFE_MCP_TOKEN_FILE=<path to a private file holding the token> (or "
    "PSEUDOLIFE_MCP_TOKEN=<the token>) and retry; to fix a client, re-run "
    "ops/install.* --client <client> with PSEUDOLIFE_MCP_TOKEN set.")


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
        value = master.lower() if master.lower() in _OFF else "invalid"
        return f"off (PSEUDOLIFE_AGENT_COORDINATION={value})"
    value = effective(flag)
    if value and value.lower() not in _YES and (
            flag != "PSEUDOLIFE_AGENT_WAKE_HOOK" or value.lower() in _OFF):
        value = value.lower() if value.lower() in _OFF else "invalid"
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
        caps = {key: value for key, value in coordination["wake"].items()
                if key in {"per_recipient_per_hour", "urgent_per_sender_per_hour", "nightly_total",
                           "fan_out_stagger_seconds", "active_seconds",
                           "authority_per_sender_per_hour"}
                and type(value) is int and value >= 0}
    report = {}
    for name, probe in (("claude_code", _claude_code_wake), ("codex", _codex_wake)):
        try:
            report[name] = probe(enabled)
        except Exception as exc:  # noqa: BLE001 - a client's config never hides the report
            report[name] = {"registered": f"unknown ({type(exc).__name__})"}
    report["caps"] = caps
    return report


def _codex_hooks_line(health: dict) -> str:
    """Whether Codex's hook copy is the daemon's scripts: the line the
    re-approval steps say to verify (``codex_hooks = current``)."""
    from pseudolife_memory.client_updates import check_codex_hooks

    try:
        result = check_codex_hooks(None, daemon_digest=health.get("hooks_digest"))
    except Exception as exc:  # noqa: BLE001 - doctor reports, never fails on this
        return f"unknown ({type(exc).__name__})"
    changed = result.get("changed_files")
    return result["state"] + (f" (changed: {', '.join(changed)})" if changed else "")


def _board_probe(timeout: float) -> dict:
    """The agent board's status for the credential this shim would send."""
    from pseudolife_memory.board_status import board_probe
    from pseudolife_memory.credentials import CredentialProvider
    from pseudolife_memory.daemon_url import _daemon_url

    try:
        token = CredentialProvider.from_environment().snapshot().token
    except Exception:  # noqa: BLE001 - never serialize credential errors
        return {"state": "invalid_credential_file", "line": "off - the configured token file is missing, unsafe or malformed"}
    try:
        url = _daemon_url()
    except (Exception, SystemExit):  # noqa: BLE001 - an invalid URL is reported elsewhere
        return {"state": "invalid_url", "line": "off - PSEUDOLIFE_MCP_DAEMON_URL is not a usable daemon URL"}
    return board_probe(url, token, timeout=timeout)


def _board_line(timeout: float) -> str:
    return _board_probe(timeout)["line"]


# --- maintainer passkeys ------------------------------------------------------
# Read through the daemon's GET /api/maintainer with the board probe's bearer:
# doctor has no database connection of its own. The daemon checks the config
# with MaintainerConfig.problem() and names the rule it broke
# (``config_problem``), so doctor and daemon never disagree on it.

MAINTAINER_FIX = (
    "Run `pseudolife-mcp maintainer setup` on the daemon host. By hand: "
    "in the daemon's config.yaml (file-only), set coordination.maintainer.rp_id to the "
    "lower-case host name the Console is served at and coordination.maintainer.origin to "
    "exactly https://<rp_id> or https://<rp_id>:<port> (for local use only, rp_id localhost "
    "with origin http://localhost:<port>). Restart the daemon, then open the Console at "
    "that origin.")
MAINTAINER_NOTE = (
    "Maintainer authority on this host also rests on the database password and on a "
    "shell on the daemon host: either can reset the passkeys and enrol another. See the "
    "guide's \"Maintainer messages and roles from the Console\".")


def maintainer_probe(url: str, token: str | None, *, timeout: float = 2.0) -> dict:
    """Whether maintainer passkeys are on, off, or configured in a way the
    daemon refuses; ``recovery`` only for the last, which is a failure."""
    import urllib.error
    import urllib.request
    from pseudolife_memory.daemon_url import _NoRedirectHandler

    if not token:
        return {"state": "not_checked",
                "line": "not checked - no bearer token (a tokenless daemon refuses passkeys)"}
    request = urllib.request.Request(url.rstrip("/") + "/api/maintainer")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            status, body = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(65536)
    except Exception:  # noqa: BLE001 - any failure is reported plainly
        return {"state": "not_checked", "line": "not checked - daemon unreachable"}
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        answer = {}
    if status == 200:
        keys = answer.get("passkeys") if isinstance(answer.get("passkeys"), list) else []
        active = sum(1 for k in keys if isinstance(k, dict) and k.get("state") == "active")
        rp_id, origin = answer.get("rp_id"), answer.get("origin")
        return {"state": "on", "rp_id": rp_id, "origin": origin, "active_keys": active,
                "line": f"on - rp_id {rp_id}, origin {origin}, {active} active key(s)",
                "note": MAINTAINER_NOTE}
    if status in (404, 405):
        return {"state": "unsupported",
                "line": "not available - this daemon predates maintainer passkeys"}
    if status == 409 and answer.get("error") == "maintainer_https_required":
        problem = answer.get("config_problem")
        if problem == "unset":
            return {"state": "off", "line": "off - coordination.maintainer is not configured "
                                            "(pseudolife-mcp maintainer setup)"}
        if isinstance(problem, str) and problem:
            return {"state": "invalid", "line": f"invalid - {problem}",
                    "recovery": MAINTAINER_FIX}
        return {"state": "off_or_invalid", "line": (
            "off - not configured, or configured in a way the daemon refuses (this daemon "
            "does not say which; its log names the rule)")}
    code = answer.get("error") if isinstance(answer.get("error"), str) else ""
    return {"state": "not_checked",
            "line": f"not checked - HTTP {status}" + (f" {code}" if code else "")}


def _maintainer_probe(timeout: float) -> dict:
    """``maintainer_probe`` with the credential and daemon URL the board
    probe uses."""
    from pseudolife_memory.credentials import CredentialProvider
    from pseudolife_memory.daemon_url import _daemon_url

    try:
        token = CredentialProvider.from_environment().snapshot().token
        url = _daemon_url()
    except (Exception, SystemExit):  # noqa: BLE001 - never serialize credential errors
        return {"state": "not_checked", "line": "not checked - no usable credential or URL"}
    return maintainer_probe(url, token, timeout=timeout)


async def probe_registration(url, token, state_path, timeout, *, client=None) -> str:
    """Verify a saved instance by nonce proof without sending its credential.

    Context's explicit read-only mode never initializes identity or storage.
    Never receive, attach, renew, recover or rewrite saved state.
    """
    import httpx
    import hashlib
    import hmac
    import uuid
    from pseudolife_memory.coordination_identity import read_legacy

    if not state_path.exists():
        return "missing_registration"
    try:
        state = read_legacy(state_path, url)
    except Exception:  # noqa: BLE001 - state errors can carry credentials
        return "invalid_state"
    if not token:
        return "missing_bearer"
    if client is None:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as owned:
            return await probe_registration(url, token, state_path, timeout, client=owned)
    try:
        nonce = uuid.uuid4().hex
        response = await client.post(url + "/api/coordination/context",
            json={"agent_id": state["agent_id"], "nonce": nonce, "read_only": True},
            headers={"Authorization": "Bearer " + token}, timeout=timeout, follow_redirects=False)
        if response.status_code == 200:
            value = response.json()
            if not isinstance(value, dict) or not all(isinstance(value.get(key), str)
                                                      for key in ("bank_id", "principal", "proof")):
                return "unsupported_capability"
            if state.get("version") == 2 and any(state.get(key) != value[key]
                                                  for key in ("bank_id", "principal")):
                return "bank_identity_mismatch"
            if state.get("version") not in (None, 2):
                return "invalid_state"
            message = json.dumps(["pseudolife-context-v1", value["bank_id"], value["principal"],
                state["agent_id"], nonce], separators=(",", ":"), ensure_ascii=True).encode("ascii")
            expected = hmac.new(hashlib.sha256(state["credential"].encode()).digest(), message, hashlib.sha256).hexdigest()
            return "authenticated" if hmac.compare_digest(value["proof"], expected) else "invalid_credential"
        if response.status_code == 401:
            return "unauthorized"
        if response.status_code in (404, 405):
            return "unsupported_capability"
        if response.status_code in (400, 403):
            value = response.json()
            code = value.get("error") if isinstance(value, dict) else None
            return {"instance_not_found": "missing_registration", "invalid_credential": "invalid_credential",
                    "principal_not_allowed": "principal_not_allowed",
                    "unexpected_parameter": "unsupported_capability"}.get(code, "refused")
        return "unavailable" if response.status_code >= 500 else "refused"
    except httpx.TransportError:
        return "unavailable"
    except (ValueError, TypeError):
        return "unsupported_capability"


def coordination_snapshot(health, board, wake, host, registration, *, tools_present=None) -> dict:
    reachable = isinstance(health, dict) or board.get("state") in {
        "on", "unauthorized", "disabled", "authentication_required", "principal_not_allowed",
        "coordination_requires_postgres", "unsupported_capability", "transport_refused", "refused"}
    auth = "admitted" if board.get("state") == "on" else board.get("state", "unknown")
    if not reachable:
        auth = "offline"
    selected = wake.get("codex" if host == "codex" else "claude_code", {})
    configured = selected.get("doorbell" if host == "codex" else "stop_hook")
    wake_state = "unsupported" if host in ("generic", "claude-desktop") else (
        "configured" if configured == "on" else "unavailable")
    result = {"daemon": "reachable" if reachable else "offline",
              "health": "ok" if isinstance(health, dict) and health.get("status") == "ok" else "not_verified",
              "authentication": auth,
              "registration": "unsupported_host" if host == "claude-desktop" else registration or "not_checked",
              "transport": {"coordination_tools": "advertised" if tools_present is True else
                            "unsupported_capability" if tools_present is False else "not_checked"},
              "host": host, "wake": {"state": wake_state, "evidence": "configuration_only"},
              "delivery": "unverified", "next": []}
    if not reachable:
        result["next"].append("Start the intended daemon and retry; HTTP health does not prove mail delivery.")
    elif auth != "admitted":
        result["next"].append("Check the board reason and credential source; repair authentication or daemon coordination settings.")
    elif registration in ("invalid_state", "invalid_credential", "bank_identity_mismatch"):
        result["next"].append("Preserve the saved instance file; verify its bank and principal and the client's credentials before explicit recovery.")
    elif registration == "unsupported_capability" or tools_present is False:
        result["next"].append("Update the daemon and shim together; this diagnostic requires coordination tools and read-only context support.")
    elif registration != "authenticated" and host != "claude-desktop":
        result["next"].append("Start or reconnect the client, make a memory call, then use --agent-state with its private saved instance file to check registration.")
    if host == "claude-desktop":
        result["next"].append("Claude Desktop shares its MCP process across conversations; use a supported session host for a mailbox.")
    elif wake_state == "unsupported":
        result["next"].append("This host has no verified idle wake path; pull mail on the next turn.")
    else:
        result["next"].append("Configured wake is not delivery evidence; verify enqueue, hint/ring, receive and explicit ack in a disposable bank.")
    result["transport"]["mailbox_pull"] = "authenticated" if registration == "authenticated" else "not_verified"
    return result


def run_doctor() -> None:
    from pseudolife_memory.shim import _daemon_url, _require_mcp_sdk_v2, probe_health

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20,
                        help="MCP handshake or async fixture-proof budget in seconds (default: 20); excludes fixture setup and cleanup")
    parser.add_argument("--host", choices=("codex", "claude-code", "claude-desktop", "generic"),
                        default="generic", help="host whose configured wake path to report (default: generic)")
    parser.add_argument("--agent-state", type=Path,
                        help="explicit private saved instance file for a read-only registration check; never repaired")
    parser.add_argument("--disposable-proof", action="store_true",
                        help="write a tagged mail proof in a newly created disposable fixture bank; requires explicit PSEUDOLIFE_TEST_DATABASE_URL, never saved client settings")
    args = parser.parse_args(sys.argv[2:])
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.disposable_proof:
        if args.agent_state is not None:
            parser.error("--disposable-proof cannot use a saved --agent-state")
        dsn = os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL")
        if not dsn:
            print(json.dumps({"ok": False, "error": "ExplicitDisposableDatabaseRequired",
                              "recovery": "Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used."}))
            raise SystemExit(2)
        from pseudolife_memory.coordination_proof import FixtureCleanupError, run_disposable_proof
        try:
            proof = run_disposable_proof(dsn, timeout=args.timeout)
        except FixtureCleanupError as exc:
            print(json.dumps(exc.report()))
            raise SystemExit(1) from None
        except Exception as exc:  # noqa: BLE001 - database errors can contain credentials
            print(json.dumps({"ok": False, "error": type(exc).__name__,
                              "recovery": "Check the disposable fixture server and CREATE/DROP DATABASE permission; no host delivery has been verified."}))
            raise SystemExit(1) from None
        print(json.dumps(proof, indent=2))
        raise SystemExit(0)
    report = {"ok": False, "interpreter": sys.executable,
              "source": str(Path(__file__).resolve().parent)}
    for package in ("pseudolife-mcp", "mcp"):
        try:
            report[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report[package] = "not installed"
    # Before anything reads the environment: the board line, the daemon URL
    # and the handshake's shim all take the credential from it.
    overrides, credential_source = registration_credentials(os.environ)
    os.environ.update(overrides)
    report["credential_source"] = credential_source or "none"
    board = _board_probe(min(args.timeout, 2))
    report["board"] = board["line"]
    # The maintainer routes need the board admitted: probed only then.
    report["maintainer_passkeys"] = (
        _maintainer_probe(min(args.timeout, 2)) if board["state"] == "on" else
        {"state": "not_checked", "line": "not checked - the board is off for this token"})
    health = None
    if _windows():
        report.update(git_bash_report(os.environ))
    try:
        health = probe_health(_daemon_url(), timeout=min(args.timeout, 2))
        report["daemon_status"] = health.get("status") if health else "unreachable"
        if not health or health.get("status") != "ok":
            if board["state"] == "unauthorized":
                report["error"] = "BearerRejected"
                report["recovery"] = "The endpoint is reachable but rejects this bearer; verify the credential source and daemon authentication configuration, then reconnect."
            else:
                report["error"] = "DaemonUnavailable"
                report["recovery"] = "Start the intended daemon, then retry; doctor never starts one."
        elif health.get("auth") and credential_source is None:
            # The handshake's shim would exit on its own missing-credential
            # line, which reaches doctor as an opaque ExceptionGroup.
            report["error"] = "BearerMissing"
            report["recovery"] = _BEARER_MISSING
        else:
            report["daemon_version"] = health.get("version") or "unknown"
            report["codex_hooks"] = _codex_hooks_line(health)
            _require_mcp_sdk_v2()
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
    registration = None
    if args.agent_state is not None and args.host != "claude-desktop" and board["state"] == "on":
        from pseudolife_memory.credentials import CredentialProvider
        try:
            token = CredentialProvider.from_environment().snapshot().token
            registration = asyncio.run(probe_registration(
                _daemon_url(), token, args.agent_state, min(args.timeout, 2)))
        except (Exception, SystemExit):  # noqa: BLE001 - never expose credential diagnostics
            registration = "unavailable"
    report["coordination"] = coordination_snapshot(health, board, report["wake"], args.host,
        registration, tools_present=report.get("coordination_tools_present"))
    if args.agent_state is not None and report["coordination"]["registration"] != "authenticated":
        report["ok"] = False
    # No Git Bash fails the report whatever the daemon said: the shim works,
    # the Claude Code plugin hooks do not. An earlier error keeps its name
    # and recovery; the Git Bash advice is in git_bash_recovery either way.
    if "git_bash" in report and report["git_bash"] is None:
        report["ok"] = False
        if "error" not in report:
            report["error"] = "GitBashMissing"
            report["recovery"] = report["git_bash_recovery"]
    # Passkeys configured in a way the daemon refuses fail the report; off
    # is information.
    if report["maintainer_passkeys"]["state"] == "invalid":
        report["ok"] = False
        if "error" not in report:
            report["error"] = "MaintainerPasskeysInvalid"
            report["recovery"] = report["maintainer_passkeys"]["recovery"]
    # Which `pseudolife-mcp` a terminal runs, beside the launcher (informational).
    report["path_resolution"] = path_resolution()
    from pseudolife_memory.tunnel_cli import saved_tunnel_diagnostics
    tunnels = saved_tunnel_diagnostics()
    if tunnels:
        report["tunnels"] = tunnels
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


# --- which `pseudolife-mcp` a terminal runs -----------------------------------
# 2026-09-29: every registration named the launcher, while `pseudolife-mcp` on
# PATH was still pipx's copy of the old package, so `pseudolife-mcp update`
# ran old code.

def path_resolution(which=shutil.which) -> dict:
    """What ``pseudolife-mcp`` resolves to on this process's PATH, the
    launcher path (``None`` when no launcher is installed), and a warning
    when a launcher exists and the name does not reach it."""
    from pseudolife_memory import runtimes
    found = which("pseudolife-mcp")
    try:
        launcher = runtimes.default_layout().launcher
    except ValueError:
        launcher = None
    report = {"on_path": found, "launcher": str(launcher) if launcher and launcher.is_file() else None}
    if report["launcher"] and not (found and os.path.normcase(os.path.realpath(found))
                                   == os.path.normcase(os.path.realpath(report["launcher"]))):
        report["warning"] = (f"`pseudolife-mcp` on PATH is {found or 'not found'}, not the launcher "
                             f"{report['launcher']}, so a terminal runs another install. Run "
                             f"\"{report['launcher']}\" update --clients-only to point the name at the "
                             "launcher, then open a new terminal")
    return report
