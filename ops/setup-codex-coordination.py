#!/usr/bin/env python3
"""Opt an existing Codex stdio shim into addressed messaging with per-task identity."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SPEC = importlib.util.spec_from_file_location(
    "pseudolife_hook_setup", Path(__file__).with_name("setup-codex-hooks.py"))
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)
SetupError = hooks.SetupError
SERVER = "pseudolife-memory"
ENABLED_ENV = {"PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
               "PSEUDOLIFE_AGENT_COORDINATION": "1", "PSEUDOLIFE_AGENT_WAKE": "0"}
RUNTIME_DEFAULTS = {"startup_timeout_sec": 240.0, "tool_timeout_sec": 240.0,
                    "required": True}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


urlopen = build_opener(_NoRedirect).open


def probe(url, token):
    """Pass the bearer/allowlist gate without creating an agent or renewing a lease."""
    try:
        url = hooks._validated_daemon_url(url)
        if not token:
            return False
        request = Request(url + "/api/coordination/agents", data=b"{}",
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3):
                return False  # Disabled coordination returns 200 without checking identity.
        except HTTPError as error:
            with error:
                return (error.code == 401 and json.loads(error.read(4096)).get("error")
                        == "instance_authentication_required")
    except Exception:
        return False  # Never expose a credential-bearing URL or transport exception.


def private_backup(path):
    if not path.exists():
        return None
    # Config may already contain bearer credentials. Secure the destination
    # before copying bytes, including a protected owner-only DACL on Windows.
    from pseudolife_memory.coordination_adapter import _open_state
    target = path.with_name(path.name + ".bak-pseudolife-coordination-"
                            + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))
    fd = _open_state(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "wb") as stream:
        stream.write(path.read_bytes())
    return str(target)


def configure(client, home, cwd, action):
    result = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    server = result.get("config", {}).get("mcp_servers", {}).get(SERVER, {})
    if not server or not server.get("command") or server.get("url"):
        raise SetupError("Register the Pseudolife stdio shim in Codex before running coordination setup.")
    command = Path(server["command"]).name.lower()
    args = server.get("args") or []
    supported = ((command in {"pseudolife-mcp", "pseudolife-mcp.exe"} and args in ([], ["shim"]))
                 or (command.startswith("python") and args in
                     (["-m", "pseudolife_memory.cli"], ["-m", "pseudolife_memory.cli", "shim"])))
    if not supported:
        raise SetupError("Use the ordinary Pseudolife stdio shim; this configured command is unsupported.")
    env = server.get("env") or {}
    forwarded = server.get("env_vars") or []

    def effective(key):
        return env.get(key, os.environ.get(key) if key in forwarded else None)

    token_file = effective("PSEUDOLIFE_MCP_TOKEN_FILE")
    token_file_configured = (
        "PSEUDOLIFE_MCP_TOKEN_FILE" in env
        or ("PSEUDOLIFE_MCP_TOKEN_FILE" in forwarded
            and "PSEUDOLIFE_MCP_TOKEN_FILE" in os.environ))
    if token_file_configured:
        try:
            token = hooks.CredentialProvider(path=token_file).snapshot().token
        except hooks.CredentialError as error:
            raise SetupError(
                "The configured credential file is missing, unsafe, or malformed; "
                "repair it before retrying.") from error
    else:
        token = effective("PSEUDOLIFE_MCP_TOKEN")
    fixed_state = effective("PSEUDOLIFE_AGENT_STATE")
    daemon_ready = False if action == "disable" else probe(
        effective("PSEUDOLIFE_MCP_DAEMON_URL") or "http://127.0.0.1:8765", token)
    enabled = str(effective("PSEUDOLIFE_AGENT_COORDINATION") or "").lower() in {"1", "true", "yes", "on"}
    codex_writer = str(effective("PSEUDOLIFE_WRITER_ID") or "").strip().lower() == "codex"
    ready = enabled and daemon_ready and not fixed_state and codex_writer and server.get("enabled", True)
    report = {"status": "ready" if ready else "needs-configuration",
              "bearer_configured": bool(token), "daemon_ready": daemon_ready,
              "coordination_enabled": enabled, "identity_source": "MCP _meta.threadId",
              "fixed_state_configured": bool(fixed_state), "backup": None}
    if action == "check":
        return report
    if action == "enable":
        if server.get("enabled") is False:
            raise SetupError("Enable the Pseudolife MCP server in Codex first; coordination setup leaves its transport settings unchanged.")
        if fixed_state:
            raise SetupError("Remove PSEUDOLIFE_AGENT_STATE from Codex; each task needs its own saved identity.")
        if not token:
            raise SetupError("Configure a bearer token in this MCP server's env or explicitly forwarded env_vars first.")
        if not daemon_ready:
            raise SetupError("Enable daemon coordination and allow this bearer principal, then retry; setup never changes the bank.")
    values = dict(ENABLED_ENV) if action == "enable" else {"PSEUDOLIFE_AGENT_COORDINATION": "0"}
    if action == "enable" and not effective("PSEUDOLIFE_AGENT_STATE_DIR"):
        # Codex does not necessarily forward its own CODEX_HOME to MCP children.
        values["PSEUDOLIFE_AGENT_STATE_DIR"] = str(home / "pseudolife" / "agents")
    edits = [{"keyPath": hooks.dotted("mcp_servers", SERVER, "env", key),
              "value": value, "mergeStrategy": "replace"}
             for key, value in values.items() if env.get(key) != value]
    if edits:
        path = (home / "config.toml").resolve()
        layers = [layer for layer in result.get("layers", [])
                  if layer.get("name", {}).get("type") == "user"
                  and not layer["name"].get("profile")
                  and Path(layer["name"].get("file", "")).resolve() == path]
        if len(layers) != 1 or not layers[0].get("version"):
            raise SetupError("Codex did not expose a versioned user configuration; no settings were changed.")
        report["backup"] = private_backup(path)
        client.rpc("config/batchWrite", {"edits": edits, "filePath": str(path),
                                        "expectedVersion": layers[0]["version"]})
        current = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
        actual = current.get("config", {}).get("mcp_servers", {}).get(SERVER, {}).get("env", {})
        if any(actual.get(key) != value for key, value in values.items()):
            raise SetupError("Codex saved settings but its effective configuration differs; check project or managed overrides before reconnecting.")
    report.update(status="enabled" if action == "enable" else "disabled",
                  coordination_enabled=action == "enable", wake="pull-only")
    return report


def configure_runtime_defaults(client, home, cwd):
    """Add first-run safety defaults without changing explicit settings."""
    result = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    server = result.get("config", {}).get("mcp_servers", {}).get(SERVER)
    if not server:
        raise SetupError(
            "The Pseudolife MCP server is not registered; runtime defaults were not changed.")
    # Codex's typed config API represents an omitted optional field as null.
    # False and zero remain explicit user choices and must not be replaced.
    missing = {key: value for key, value in RUNTIME_DEFAULTS.items()
               if server.get(key) is None}
    report = {"status": "ready",
              "runtime_defaults": "configured" if missing else "preserved",
              "backup": None}
    if not missing:
        return report
    path = (home / "config.toml").resolve()
    layers = [layer for layer in result.get("layers", [])
              if layer.get("name", {}).get("type") == "user"
              and not layer["name"].get("profile")
              and Path(layer["name"].get("file", "")).resolve() == path]
    if len(layers) != 1 or not layers[0].get("version"):
        raise SetupError(
            "Codex did not expose a versioned user configuration; runtime defaults were not changed.")
    user_server = (layers[0].get("config") or {}).get("mcp_servers", {}).get(SERVER)
    if not isinstance(user_server, dict):
        raise SetupError(
            "The Pseudolife registration is not in the versioned user configuration; "
            "runtime defaults were not changed.")
    updated_server = dict(user_server)
    updated_server.update(missing)
    expected = {key: server[key] if server.get(key) is not None else value
                for key, value in RUNTIME_DEFAULTS.items()}
    report["backup"] = private_backup(path)
    client.rpc("config/batchWrite", {
        "edits": [{"keyPath": hooks.dotted("mcp_servers", SERVER),
                   "value": updated_server, "mergeStrategy": "replace"}],
        "filePath": str(path),
        "expectedVersion": layers[0]["version"],
    })
    current = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    actual = current.get("config", {}).get("mcp_servers", {}).get(SERVER, {})
    if any(key not in actual or actual[key] != value
           for key, value in expected.items()):
        raise SetupError(
            "Codex saved runtime defaults but its effective configuration differs; "
            "check project or managed overrides before reconnecting.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true", help="enable pull messaging after checking daemon access")
    group.add_argument("--disable", action="store_true", help="disable registration for future Codex connections")
    group.add_argument("--check", action="store_true", help="check configuration without changing it (default)")
    group.add_argument("--credentials", action="store_true",
                       help="bootstrap and configure a rotatable Codex credential file")
    group.add_argument("--runtime-defaults", action="store_true",
                       help=argparse.SUPPRESS)
    parser.add_argument("--installer-token-stdin", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--installer-daemon-url", help=argparse.SUPPRESS)
    args = parser.parse_args()
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    action = ("enable" if args.enable else "disable" if args.disable else
              "credentials" if args.credentials else
              "runtime-defaults" if args.runtime_defaults else "check")
    try:
        installer_connection = None
        if args.installer_token_stdin:
            if action != "credentials" or not args.installer_daemon_url:
                raise SetupError(
                    "Installer credential input requires credential setup and a daemon URL.")
            data = sys.stdin.buffer.read(4097)
            if len(data) > 4096:
                raise SetupError("The installer credential input is invalid.")
            try:
                token = data.decode("utf-8").rstrip("\r\n")
                hooks.CredentialProvider(token=token).snapshot()
            except (UnicodeError, hooks.CredentialError) as error:
                raise SetupError("The installer credential input is invalid.") from error
            installer_connection = (args.installer_daemon_url, token)
        elif args.installer_daemon_url:
            if action != "credentials":
                raise SetupError("Installer daemon input requires credential setup.")
            installer_connection = (args.installer_daemon_url, None)
        with tempfile.TemporaryDirectory(prefix="pseudolife-codex-config-") as temporary:
            neutral = Path(temporary)
            with hooks.codex(hooks.resolve_codex(), home, neutral) as client:
                if action == "credentials":
                    report = hooks.configure_credential_file(
                        client, home, neutral,
                        installer_connection=installer_connection)
                    report["status"] = (
                        "ready" if report["credential_file_configured"] else "tokenless")
                elif action == "runtime-defaults":
                    report = configure_runtime_defaults(client, home, neutral)
                else:
                    report = configure(client, home, neutral, action)
    except SetupError as error:
        report = {"status": "needs-configuration", "recovery": str(error)}
    except Exception:
        report = {"status": "needs-configuration", "recovery": "Check the Codex runtime and configuration; no transport details are displayed."}
    print(json.dumps(report, indent=2))
    return 1 if report["status"] == "needs-configuration" else 0


if __name__ == "__main__":
    raise SystemExit(main())
