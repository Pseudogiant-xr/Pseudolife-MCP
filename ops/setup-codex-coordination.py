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
# PSEUDOLIFE_AGENT_WAKE is deliberately absent: unset already means pull-only
# in the shim, and writing "0" here reset live delivery for anyone who had
# opted in and later re-ran --enable.
ENABLED_ENV = {"PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
               "PSEUDOLIFE_AGENT_COORDINATION": "1"}
TRUTHY = {"1", "true", "yes", "on"}
# Token-map principals join the board only when listed (2026-09-25 decision).
PRINCIPAL_REASON = ("principal not allowed on the board (add 'codex' to "
                    "coordination.allowed_principals in config.yaml)")
RUNTIME_DEFAULTS = {"startup_timeout_sec": 240.0, "tool_timeout_sec": 240.0,
                    "required": True}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


urlopen = build_opener(_NoRedirect).open


def probe(url, token):
    """Pass the bearer/allowlist gate without creating an agent or renewing a lease.

    Returns ``ready``, ``principal_not_allowed``, ``disabled`` or ``unavailable``."""
    try:
        url = hooks._validated_daemon_url(url)
        if not token:
            return "unavailable"
        request = Request(url + "/api/coordination/agents", data=b"{}",
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3) as response:
                # Disabled coordination returns 200 without checking identity.
                disabled = json.loads(response.read(4096)) == {"enabled": False}
                return "disabled" if disabled else "unavailable"
        except HTTPError as error:
            with error:
                code = json.loads(error.read(4096)).get("error")
            if (error.code, code) == (401, "instance_authentication_required"):
                return "ready"
            if (error.code, code) == (403, "principal_not_allowed"):
                return "principal_not_allowed"
            return "unavailable"
    except Exception:
        return "unavailable"  # Never expose a credential-bearing URL or transport exception.


def served(url, token):
    """Whether the daemon serves the board check-in to this bearer: the question
    the shim asks at startup when PSEUDOLIFE_AGENT_COORDINATION is unset."""
    try:
        url = hooks._validated_daemon_url(url)
        if not token:
            return False
        request = Request(url + "/api/hook/coordination-start",
                          headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=3) as response:
            return bool(response.read(65536).strip())
    except Exception:
        return False


def codex_cli(lookup):
    """The codex CLI the shim's doorbell would run, from the merged view of the
    server's ``env`` over the launching environment (as doctor looks)."""
    from pseudolife_memory.codex_doorbell import resolve_codex_command
    return resolve_codex_command(lookup)


def wake_path(effective, env, board, board_reason, bank_token):
    """``(path, reason)`` for how board mail reaches this Codex shim: ``live``
    (the app-server bridge), ``doorbell`` (``codex queue``, on by default
    since 2026-09-28) or ``pull-only``, read the way ``pseudolife_memory.shim``
    reads the switches and ``pseudolife-mcp doctor`` reports them.
    ``bank_token`` is the bearer the shim would send, from the token file
    when one is configured, which is what it compares the bridge's with."""
    def value(key):
        return str(effective(key) or "").strip()

    if not board:
        return "pull-only", board_reason
    if value("PSEUDOLIFE_WRITER_ID").lower() != "codex":
        return "pull-only", "PSEUDOLIFE_WRITER_ID is not codex"
    if value("PSEUDOLIFE_AGENT_STATE"):
        return "pull-only", ("PSEUDOLIFE_AGENT_STATE is set, which turns Codex "
                             "coordination off")
    if not bank_token:
        return "pull-only", "no bearer token"  # the board registry needs one
    bridge_token = value("PSEUDOLIFE_CODEX_SERVER_TOKEN")
    if (value("PSEUDOLIFE_AGENT_WAKE").lower() in TRUTHY
            and value("PSEUDOLIFE_CODEX_SERVER_URL") and bridge_token
            and bridge_token != bank_token):
        return "live", "PSEUDOLIFE_AGENT_WAKE with the app-server bridge"
    doorbell = value("PSEUDOLIFE_CODEX_DOORBELL")
    if doorbell and doorbell.lower() not in TRUTHY:
        return "pull-only", f"PSEUDOLIFE_CODEX_DOORBELL={doorbell}"
    lookup = {**os.environ, **{key: str(item) for key, item in env.items()}}
    if codex_cli(lookup) is None:
        return "pull-only", ("PSEUDOLIFE_CODEX_BIN is not an absolute path to an "
                             "existing file" if value("PSEUDOLIFE_CODEX_BIN")
                             else "no codex CLI")
    return "doorbell", ("PSEUDOLIFE_CODEX_DOORBELL is on by default and a codex "
                        "CLI was found" if not doorbell
                        else f"PSEUDOLIFE_CODEX_DOORBELL={doorbell} and a codex CLI was found")


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
    url = effective("PSEUDOLIFE_MCP_DAEMON_URL") or "http://127.0.0.1:8765"
    access = "unavailable" if action == "disable" else probe(url, token)
    daemon_ready = access == "ready"
    # The board is on by default: an unset value is a working configuration
    # when the daemon serves the board to this bearer, which the shim asks
    # at startup. An explicit opt-in skips that question, as the shim does.
    setting = str(effective("PSEUDOLIFE_AGENT_COORDINATION") or "").strip().lower()
    mode = ("explicit" if setting in TRUTHY else "disabled" if setting
            else "default-on")
    board = daemon_ready and (mode == "explicit" or (
        mode == "default-on" and action == "check" and served(url, token)))
    codex_writer = str(effective("PSEUDOLIFE_WRITER_ID") or "").strip().lower() == "codex"
    ready = board and not fixed_state and codex_writer and server.get("enabled", True)
    report = {"status": f"ready ({mode})" if ready else "needs-configuration",
              "bearer_configured": bool(token), "daemon_ready": daemon_ready,
              "coordination_enabled": mode != "disabled", "coordination_mode": mode,
              "identity_source": "MCP _meta.threadId",
              "fixed_state_configured": bool(fixed_state), "backup": None}
    if access == "principal_not_allowed":
        report["reason"] = PRINCIPAL_REASON
    if action == "check":
        if mode == "disabled":
            board_reason = f"PSEUDOLIFE_AGENT_COORDINATION={setting}"
        elif not token:
            board_reason = "no bearer token"
        elif access == "principal_not_allowed":
            board_reason = PRINCIPAL_REASON
        elif access == "disabled":
            board_reason = "coordination disabled on the daemon"
        elif not daemon_ready:
            board_reason = "the daemon is unreachable or refuses this bearer"
        else:
            board_reason = "the daemon does not serve the board to this bearer"
        report["wake"], report["wake_reason"] = wake_path(effective, env, board, board_reason, token)
        return report
    if action == "enable":
        if server.get("enabled") is False:
            raise SetupError("Enable the Pseudolife MCP server in Codex first; coordination setup leaves its transport settings unchanged.")
        if fixed_state:
            raise SetupError("Remove PSEUDOLIFE_AGENT_STATE from Codex; each task needs its own saved identity.")
        if not token:
            raise SetupError("Configure a bearer token in this MCP server's env or explicitly forwarded env_vars first.")
        if access == "principal_not_allowed":
            raise SetupError(f"The daemon reports: {PRINCIPAL_REASON}; setup never changes the bank.")
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
    # The wake path after this write: live delivery needs the app-server
    # bridge (docs/guide/configuration.md, "Optional Codex live delivery"),
    # and without it the shim's default is the doorbell, not pull.
    written = {**env, **values}

    def effective_after(key):
        return written.get(key, effective(key))

    if action == "enable":
        wake, wake_reason = wake_path(effective_after, written, True, "", token)
    else:
        wake, wake_reason = "pull-only", "PSEUDOLIFE_AGENT_COORDINATION=0"
    report.update(status="enabled" if action == "enable" else "disabled",
                  coordination_enabled=action == "enable",
                  coordination_mode="explicit" if action == "enable" else "disabled",
                  wake=wake, wake_reason=wake_reason)
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
