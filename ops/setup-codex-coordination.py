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
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SPEC = importlib.util.spec_from_file_location(
    "pseudolife_hook_setup", Path(__file__).with_name("setup-codex-hooks.py"))
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)
SetupError = hooks.SetupError
SERVER = "pseudolife-memory"
ENABLED_ENV = {"PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
               "PSEUDOLIFE_AGENT_COORDINATION": "1", "PSEUDOLIFE_AGENT_WAKE": "0"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


urlopen = build_opener(_NoRedirect).open


def probe(url, token):
    """Pass the bearer/allowlist gate without creating an agent or renewing a lease."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not token):
            return False
        request = Request(url.rstrip("/") + "/api/coordination/agents", data=b"{}",
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3):
                return False  # Disabled coordination returns 200 without checking identity.
        except HTTPError as error:
            with error:
                return (error.code == 400 and json.loads(error.read(4096)).get("error")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true", help="enable pull messaging after checking daemon access")
    group.add_argument("--disable", action="store_true", help="disable registration for future Codex connections")
    group.add_argument("--check", action="store_true", help="check configuration without changing it (default)")
    args = parser.parse_args()
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    action = "enable" if args.enable else "disable" if args.disable else "check"
    try:
        with hooks.codex(hooks.resolve_codex(), home, Path.cwd()) as client:
            report = configure(client, home, Path.cwd(), action)
    except SetupError as error:
        report = {"status": "needs-configuration", "recovery": str(error)}
    except Exception:
        report = {"status": "needs-configuration", "recovery": "Check the Codex runtime and configuration; no transport details are displayed."}
    print(json.dumps(report, indent=2))
    return 1 if report["status"] == "needs-configuration" else 0


if __name__ == "__main__":
    raise SystemExit(main())
