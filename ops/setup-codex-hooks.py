#!/usr/bin/env python3
"""Install, approve, and verify only PseudoLife's Codex lifecycle hooks.

Uses Codex's JSON-RPC config writer and runtime-generated trust hashes. No
third-party Python packages, external model requests, or hook-trust bypass are needed.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Setup runs before the host shim is installed or upgraded. Prefer this
# checkout's standard-library credential helper over an older installed copy.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pseudolife_memory.credentials import (
    CredentialError,
    CredentialProvider,
    _write_token_file,
)


PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
EVENTS = {"sessionStart": "SessionStart", "userPromptSubmit": "UserPromptSubmit",
          "sessionEnd": "SessionEnd"}
# MemoryPolicy is the separate memory-policy SessionStart output (the full
# memory-loop block when the daemon's memory_policy variant asks for it).
MANUAL_ROLES = {"sessionStart": ("SessionStart", "MemoryPolicy", "CoordinationStart"),
                "userPromptSubmit": ("UserPromptSubmit", "CoordinationPrompt"),
                "sessionEnd": ("SessionEnd",)}
# The plugin's hooks.json also carries Claude Code's opt-in Stop wake hook,
# which Codex lists too. In Codex it is a no-op (lifecycle.ps1 -Event Stop
# exits at once; stop-wake.sh exits unless Claude Code started it), approved
# with the three lifecycle hooks. Optional: Codex before 0.148 skips async
# hooks outside SessionEnd and lists three. Manual installs keep EVENTS.
PLUGIN_EVENTS = {**EVENTS, "stop": "Stop"}
# A manual bundle copies SCRIPTS; it has no Stop hook, so no stop-wake.sh.
SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh",
           "coordination-start.sh", "coordination-prompt.sh", "session-end.sh")
LEGACY_SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh", "session-end.sh")
PLUGIN_SCRIPTS = SCRIPTS + ("stop-wake.sh",)
RECOVERY = "Open Codex /hooks to review PseudoLife hooks; rerun setup after correcting the reported problem."


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


urlopen = build_opener(_NoRedirect).open


class SetupError(Exception):
    """A safe, user-facing error (never a raw transport error)."""


def backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(path.name + ".bak-pseudolife-" + stamp)
    shutil.copy2(path, target)
    return str(target)


def private_backup(path: Path) -> str | None:
    if not path.exists():
        return None
    from pseudolife_memory.coordination_adapter import _open_state

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(path.name + ".bak-pseudolife-credentials-" + stamp)
    fd = _open_state(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "wb") as stream:
        stream.write(path.read_bytes())
    return str(target)


def _private_json(path: Path, updates):
    from pseudolife_memory.coordination_adapter import _open_state

    current = {}
    if path.exists() or path.is_symlink():
        try:
            fd = _open_state(path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(16385)
            if len(data) > 16384:
                raise ValueError
            current = json.loads(data)
            if not isinstance(current, dict):
                raise ValueError
        except (OSError, ValueError, UnicodeError) as error:
            raise SetupError(
                "The managed Codex connection file is unsafe or malformed; "
                "repair or remove it before retrying.") from error
    updated = dict(updates)
    data = (json.dumps(updated, indent=2) + "\n").encode()
    if updated == current:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = private_backup(path)
    temporary = path.with_name(".connection-" + uuid.uuid4().hex + ".tmp")
    fd = _open_state(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return saved


def _validated_daemon_url(url):
    if not isinstance(url, str):
        raise SetupError("The configured memory daemon URL is invalid.")
    try:
        parsed = urlsplit(url)
        parsed.port
    except (TypeError, ValueError) as error:
        raise SetupError("The configured memory daemon URL is invalid.") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(character.isspace() or ord(character) < 0x20 for character in url)):
        raise SetupError("The configured memory daemon URL is invalid.")
    return urlunsplit((parsed.scheme, parsed.netloc.rstrip("/"), "", "", ""))


def _daemon_url(server):
    env = server.get("env") or {}
    forwarded = set(server.get("env_vars") or ())
    if "PSEUDOLIFE_MCP_DAEMON_URL" in env:
        url = env["PSEUDOLIFE_MCP_DAEMON_URL"]
    elif ("PSEUDOLIFE_MCP_DAEMON_URL" in forwarded
          and "PSEUDOLIFE_MCP_DAEMON_URL" in os.environ):
        url = os.environ["PSEUDOLIFE_MCP_DAEMON_URL"]
    elif not server:
        url = os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL")
    else:
        url = None
    if url is None:
        url = "http://127.0.0.1:8765"
    return _validated_daemon_url(url)


def installer_credential_valid(url, token):
    """Authenticate a one-shot installer credential without exposing details."""
    try:
        token = CredentialProvider(token=token).snapshot().token
        request = Request(
            url + "/api/episodes?limit=1",
            headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=3) as response:
            response.read(1)
            return getattr(response, "status", 200) == 200
    except Exception:
        return False


def _connection_values(url, target=None):
    def encoded(value):
        text = "" if value is None else str(value)
        return base64.b64encode(text.encode("utf-8")).decode("ascii")
    return {"version": 1, "daemon_url": encoded(url), "token_file": encoded(target)}


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pseudolife-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def dotted(*parts: str) -> str:
    return ".".join(json.dumps(p) for p in parts)


def toml_value(value):
    """CLI -c values are TOML, while config RPC values are JSON.

    Override entire tables for hook/plugin keys containing dots: the CLI's
    dotted-path splitter does not implement the config RPC's quoted keys.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k, ensure_ascii=False) + " = " + toml_value(v)
                                for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def resolve_codex() -> str:
    explicit = os.environ.get("CODEX_CLI_PATH")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
        candidates = list(root.glob("*/codex.exe"))
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise SetupError("Codex CLI is unavailable; install or update Codex, then rerun setup.")


class Codex:
    """Bounded stdio JSON-RPC client; child stderr never enters user reports."""

    def __init__(self, executable: str, home: Path, cwd: Path, overrides=None):
        args = [executable, "app-server", "--stdio"]
        for key, value in (overrides or {}).items():
            args += ["-c", key + "=" + toml_value(value)]
        env = dict(os.environ, CODEX_HOME=str(home))
        self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8")
        self.messages = queue.Queue()
        self.events = []
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.rpc("initialize", {"clientInfo": {"name": "pseudolife_hook_setup", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def send(self, value):
        self.proc.stdin.write(json.dumps(value) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout):
        try:
            item = self.messages.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            raise SetupError("Codex did not respond within the setup timeout.") from None
        if item is None:
            raise SetupError("Codex exited before hook setup completed.")
        self.events.append(item)
        return item

    def rpc(self, method, params, timeout=20):
        self.counter += 1
        ident = self.counter
        self.send({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.receive(deadline - time.monotonic())
            if item.get("id") == ident:
                if "error" in item:
                    raise SetupError(f"Codex rejected {method}; update Codex or use /hooks for manual review.")
                return item["result"]
        raise SetupError("Codex did not respond within the setup timeout.")

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        # A hook Codex killed can outlive it holding an inherited copy of its
        # stdout (seen on Windows under load: a PowerShell hook stuck mid-exit
        # for minutes), so the reader never sees EOF. Closing the pipe under
        # that blocked read would wait on the hook; the daemon reader ends
        # with this process instead.
        if self.proc.stdout and not self.reader.is_alive():
            self.proc.stdout.close()


@contextmanager
def codex(executable, home, cwd, overrides=None):
    client = Codex(executable, home, cwd, overrides)
    try:
        yield client
    finally:
        client.close()


def inventory(client, cwd):
    config = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    result = client.rpc("hooks/list", {"cwds": [str(cwd)]})
    entries = result.get("data", [])
    if len(entries) != 1 or entries[0].get("errors"):
        raise SetupError("Codex could not load its hook configuration.")
    hooks = entries[0].get("hooks")
    if not isinstance(hooks, list):
        raise SetupError("This Codex hook-list schema is unsupported; use /hooks.")
    for h in hooks:
        if not all(k in h for k in ("key", "currentHash", "eventName", "enabled", "trustStatus", "sourcePath")):
            raise SetupError("This Codex hook-list schema is unsupported; use /hooks.")
    return config, hooks


def bundle_bytes(directory, names=SCRIPTS):
    return {name: (directory / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode()
            for name in names}


def complete_set(hooks, source):
    """Memory and coordination each have independent start and prompt hooks,
    and the memory-policy block has a start hook of its own."""
    from collections import Counter
    counts = Counter(h["eventName"] for h in hooks)
    required = Counter({event: len(roles) for event, roles in MANUAL_ROLES.items()})
    if source == "plugin" and counts.get("stop") == 1:
        del counts["stop"]
    return (counts == required
            and len({h.get("command") for h in hooks}) == len(hooks)
            and len({h.get("key") for h in hooks}) == len(hooks))


def bundle_digest(files, names=SCRIPTS):
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + files[name] + b"\0")
    return digest.hexdigest()[:20]


def manual_definitions(directory, codex_marker=True):
    mapping = {"SessionStart": "session-start.sh", "MemoryPolicy": "session-start.sh",
               "UserPromptSubmit": "user-prompt-submit.sh",
               "CoordinationStart": "coordination-start.sh", "CoordinationPrompt": "coordination-prompt.sh",
               "SessionEnd": "session-end.sh"}
    arguments = {"MemoryPolicy": " memory-policy"}
    # Literal single quotes protect $, backticks, and spaces in native paths.
    ps = str(directory / "lifecycle.ps1").replace("'", "''")
    bash_prefix = "env PSEUDOLIFE_CODEX_HOOK=1 bash " if codex_marker else "bash "
    return {event: {"type": "command",
                    "command": bash_prefix + shlex.quote(str(directory / script)) + arguments.get(event, ""),
                    "commandWindows": f"pwsh -NoProfile -File '{ps}' -Event {event}",
                    "timeout": {"SessionStart": 15, "MemoryPolicy": 15, "UserPromptSubmit": 5,
                                "CoordinationStart": 5, "CoordinationPrompt": 5,
                                "SessionEnd": 3}[event]}
            for event, script in mapping.items()}


def owned_manual(hook, home):
    """Recognize our immutable installed bundles by an exact generated command."""
    if hook.get("source") == "plugin":
        return False
    source = Path(hook.get("sourcePath", "")).resolve()
    if source != (home / "hooks.json").resolve():
        return False
    for directory in (home / "pseudolife/hooks").glob("*"):
        if not re.fullmatch(r"[a-f0-9]{20}", directory.name):
            continue
        definitions = manual_definitions(directory)
        legacy_definitions = manual_definitions(directory, codex_marker=False)
        command = hook.get("command")
        if any(command in (definitions[role]["command"], definitions[role]["commandWindows"],
                           legacy_definitions[role]["command"])
               for role in MANUAL_ROLES.get(hook.get("eventName"), ())):
            return True
    return False


def legacy_commands():
    # Only shipped legacy commands are migratable; substring matches would
    # silently remove or approve arbitrary user code. The discipline line is
    # the one install-hook writes (the plugin's prompt hook stopped carrying
    # it on 2026-09-26, when it became the memory-change note).
    install_hook = (ROOT / "ops/install-hook.ps1").read_text(encoding="utf-8")
    line = re.search(r'\$disciplineLine = "(.*)"', install_hook)[1]
    coordination = re.search(r'\$coordinationLine = "(.*)"', install_hook)[1]
    briefings = {"pseudolife-mcp briefing --hook-json",
                 "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"}
    # The installers' daemon-gated check-in (2026-09-25) and the
    # unconditional echo it replaced.
    return {*briefings, *(command + " --coordination" for command in briefings),
            f"echo '{line}'", f"Write-Output '{line}'",
            f"echo '{coordination}'", f"Write-Output '{coordination}'"}


def is_legacy(hook, home):
    return (hook.get("source") != "plugin"
            and Path(hook.get("sourcePath", "")).resolve() == (home / "hooks.json").resolve()
            and hook.get("command") in legacy_commands())


def select_hooks(hooks, home, source):
    # A Stop entry the user disabled in /hooks is a no-op there anyway: keep
    # that choice instead of refusing setup over it.
    plugin = [h for h in hooks if h.get("pluginId") == PLUGIN_ID
              and (h["enabled"] or h["eventName"] != "stop")]
    manual = [h for h in hooks if owned_manual(h, home)]
    legacy = [h for h in hooks if is_legacy(h, home)]
    other_legacy = [h for h in hooks if h not in legacy and h not in plugin
                    and h.get("command") in legacy_commands()]
    if other_legacy:
        raise SetupError("Existing PseudoLife hooks use another configuration source; review them in /hooks before migrating.")
    chosen = "plugin" if source == "auto" and plugin else "manual" if source == "auto" else source
    if chosen == "manual" and plugin:
        raise SetupError("The PseudoLife plugin already owns hooks. Use auto or plugin to avoid duplicate execution.")
    if any(not h["enabled"] for h in plugin + manual + legacy):
        raise SetupError("A PseudoLife hook is disabled. Re-enable it in /hooks if desired; setup preserves disabled hooks.")
    return chosen, plugin if chosen == "plugin" else manual, manual + legacy if chosen == "plugin" else legacy


def vet_plugin(hooks):
    if not complete_set(hooks, "plugin"):
        raise SetupError("The enabled PseudoLife plugin is missing or has unexpected hooks; update it and retry.")
    expected = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    roles = {event: [handler for group in groups for handler in group["hooks"]]
             for event, groups in expected.items()}
    seen = {event: set() for event in roles}
    for h in hooks:
        path = Path(h["sourcePath"])
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))["hooks"]
            files_match = (bundle_bytes(path.parent, PLUGIN_SCRIPTS)
                           == bundle_bytes(ROOT / "plugin/hooks", PLUGIN_SCRIPTS))
        except (OSError, ValueError):
            files_match = False
            actual = None
        if actual != expected or not files_match or h.get("handlerType") != "command":
            raise SetupError("Installed PseudoLife hooks differ from this installer. Update them together or review manually in /hooks.")
        event = PLUGIN_EVENTS[h["eventName"]]
        roots = (str(path.parent.parent), path.parent.parent.as_posix())
        matches = []
        for index, handler in enumerate(roles[event]):
            commands = set()
            for field in ("command", "commandWindows"):
                command = handler[field]
                commands.add(command)
                for root in roots:
                    commands.add(command.replace("${CLAUDE_PLUGIN_ROOT}", root)
                                 .replace("$env:CLAUDE_PLUGIN_ROOT", root))
            if h.get("command") in commands:
                matches.append(index)
        if len(matches) != 1 or matches[0] in seen[event]:
            raise SetupError("Installed PseudoLife hooks differ from this installer. Update them together or review manually in /hooks.")
        seen[event].add(matches[0])
    if any(len(seen[event]) != len(roles[event]) for event in seen if event != "Stop" or seen[event]):
        raise SetupError("Installed PseudoLife hooks differ from this installer. Update them together or review manually in /hooks.")


def vet_manual(hooks, home, complete=True):
    if complete and not complete_set(hooks, "manual"):
        raise SetupError("The manual PseudoLife hook set is incomplete; rerun setup with approval to repair it.")
    directories = []
    for directory in (home / "pseudolife/hooks").glob("*"):
        current = manual_definitions(directory)
        legacy = manual_definitions(directory, codex_marker=False)
        if all(any(h.get("command") in (current[role]["command"], current[role]["commandWindows"],
                                        legacy[role]["command"])
                   for role in MANUAL_ROLES.get(h.get("eventName"), ()))
               for h in hooks):
            directories.append(directory)
    if len(directories) != 1:
        raise SetupError("Manual PseudoLife hooks reference mixed or unknown script bundles; review /hooks.")
    if complete:
        current = manual_definitions(directories[0])
        for event, roles in MANUAL_ROLES.items():
            seen = [h.get("command") for h in hooks if h.get("eventName") == event]
            if len(seen) != len(roles) or any(
                    not any(command in (current[role]["command"], current[role]["commandWindows"])
                            for command in seen)
                    for role in roles):
                raise SetupError("The manual PseudoLife hook roles are incomplete; rerun setup with approval to repair them.")
    try:
        matches = bundle_digest(bundle_bytes(directories[0])) == directories[0].name
    except (OSError, UnicodeError):
        matches = False
    if not matches and not complete:
        try:
            # Only an approved upgrade may accept the exact pre-split bundle.
            # Its directory name still authenticates all four original bytes.
            legacy = list(directories[0].iterdir())
            if ({p.name for p in legacy} == set(LEGACY_SCRIPTS)
                    and all(p.is_file() and not p.is_symlink() for p in legacy)):
                matches = (bundle_digest(bundle_bytes(directories[0], LEGACY_SCRIPTS),
                                         LEGACY_SCRIPTS) == directories[0].name)
        except (OSError, UnicodeError):
            pass
    if not matches:
        raise SetupError("An installed PseudoLife script was modified; restore or review it before running verification.")


def install_manual(home, report, plugin=False):
    path = home / "hooks.json"
    original = path.read_bytes() if path.exists() else None
    obj = json.loads(original) if original else {}
    hooks = obj.setdefault("hooks", {})
    known = legacy_commands()
    for directory in (home / "pseudolife/hooks").glob("*"):
        if re.fullmatch(r"[a-f0-9]{20}", directory.name):
            for marker in (True, False):
                for d in manual_definitions(directory, codex_marker=marker).values():
                    known.update((d["command"], d["commandWindows"]))
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for h in group.get("hooks", []):
                commands = [h.get("command"), h.get("commandWindows")]
                if any(c in known for c in commands) and any(c and c not in known for c in commands):
                    raise SetupError("A PseudoLife hook has a custom platform command. Review it in /hooks before migration; existing hooks were preserved.")
    definitions = {}
    if not plugin:
        files = bundle_bytes(ROOT / "plugin/hooks")
        directory = home / "pseudolife/hooks" / bundle_digest(files)
        for name, data in files.items():
            destination = directory / name
            if destination.exists() and destination.read_bytes() != data:
                raise SetupError("An installed PseudoLife script was modified; restore or review it before rerunning setup.")
            if not destination.exists():
                atomic_write(destination, data)
        definitions = manual_definitions(directory)
        for definition in definitions.values():
            known.update((definition["command"], definition["commandWindows"]))
    for event in EVENTS.values():
        groups = []
        for group in hooks.get(event, []):
            # An unknown platform override is user customization, even when
            # its other command still matches our legacy installer exactly.
            kept = [h for h in group.get("hooks", [])
                    if h.get("type") != "command" or h.get("command") not in known
                    or (h.get("commandWindows") and h["commandWindows"] not in known)]
            if kept:
                groups.append({**group, "hooks": kept})
        for name in MANUAL_ROLES[next(k for k, v in EVENTS.items() if v == event)]:
            if name in definitions:
                groups.append({"hooks": [definitions[name]]})
        hooks[event] = groups
    data = (json.dumps(obj, indent=2) + "\n").encode()
    # Compare parsed values so formatting changes alone never create backups.
    if original is not None and json.loads(original) == obj:
        return
    saved = backup(path)
    if saved:
        report["backups"].append(saved)
    atomic_write(path, data)


def trust_hooks(client, config, hooks, home, report):
    edits = []
    for h in hooks:
        if h["trustStatus"] == "trusted":
            continue
        if h.get("isManaged") or h["trustStatus"] not in ("untrusted", "modified"):
            raise SetupError("Codex policy or an unsupported trust state prevents automatic approval; use /hooks.")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", h["currentHash"]):
            raise SetupError("Unsupported Codex hook hash format; use /hooks.")
        edits.append({"keyPath": dotted("hooks", "state", h["key"], "trusted_hash"),
                      "value": h["currentHash"], "mergeStrategy": "replace"})
    if not edits:
        return
    path = (home / "config.toml").resolve()
    layers = [l for l in config.get("layers", [])
              if l["name"].get("type") == "user" and not l["name"].get("profile")
              and Path(l["name"]["file"]).resolve() == path]
    if len(layers) != 1 or not layers[0].get("version"):
        raise SetupError("Codex did not expose a versioned user configuration; use /hooks for approval.")
    saved = backup(path)
    if saved:
        report["backups"].append(saved)
    client.rpc("config/batchWrite", {"edits": edits, "filePath": str(path),
                                    "expectedVersion": layers[0]["version"]})


def _user_config_layer(config, home):
    path = (home / "config.toml").resolve()
    layers = [layer for layer in config.get("layers", [])
              if layer.get("name", {}).get("type") == "user"
              and not layer["name"].get("profile")
              and Path(layer["name"].get("file", "")).resolve() == path]
    if len(layers) != 1 or not layers[0].get("version"):
        raise SetupError("Codex did not expose a versioned user configuration; credential settings were not changed.")
    return path, layers[0]["version"], layers[0].get("config") or {}


def configure_credential_file(client, home, cwd, config=None,
                              installer_connection=None):
    """Bootstrap a private token file and migrate an existing Codex MCP env."""
    config = config or client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    effective_server = config.get("config", {}).get("mcp_servers", {}).get(
        "pseudolife-memory") or {}
    user_layers = [layer for layer in config.get("layers", [])
                   if layer.get("name", {}).get("type") == "user"
                   and not layer["name"].get("profile")
                   and Path(layer["name"].get("file", "")).resolve()
                   == (home / "config.toml").resolve()]
    user_config = user_layers[0].get("config") if len(user_layers) == 1 else {}
    server = (user_config or {}).get("mcp_servers", {}).get(
        "pseudolife-memory") or {}
    env = dict(server.get("env") or {})
    forwarded = set(server.get("env_vars") or ())
    effective_env = dict(effective_server.get("env") or {})
    effective_forwarded = set(effective_server.get("env_vars") or ())
    source_path = None
    literal = None
    selected_daemon_url = None
    if "PSEUDOLIFE_MCP_TOKEN_FILE" in env:
        source_path = env["PSEUDOLIFE_MCP_TOKEN_FILE"]
    elif "PSEUDOLIFE_MCP_TOKEN" in env:
        literal = env["PSEUDOLIFE_MCP_TOKEN"]
    elif ("PSEUDOLIFE_MCP_TOKEN_FILE" in forwarded
          and "PSEUDOLIFE_MCP_TOKEN_FILE" in os.environ):
        source_path = os.environ["PSEUDOLIFE_MCP_TOKEN_FILE"]
    elif ("PSEUDOLIFE_MCP_TOKEN" in forwarded
          and "PSEUDOLIFE_MCP_TOKEN" in os.environ):
        literal = os.environ["PSEUDOLIFE_MCP_TOKEN"]
    if source_path is None and not literal and installer_connection is not None:
        if (any(key in effective_env for key in (
                "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKEN"))
                or any(key in effective_forwarded for key in (
                    "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKEN"))):
            raise SetupError(
                "Credential settings come from another Codex configuration layer; "
                "the installer did not replace them.")
        try:
            installer_url, installer_token = installer_connection
            installer_url = _validated_daemon_url(installer_url)
            if installer_token is not None:
                CredentialProvider(token=installer_token).snapshot()
        except (CredentialError, SetupError, TypeError, ValueError) as error:
            raise SetupError(
                "The installer-provided daemon credential is invalid.") from error
        if effective_server:
            user_url = _daemon_url(server)
            effective_url = _daemon_url(effective_server)
            if installer_url != user_url or installer_url != effective_url:
                raise SetupError(
                    "The installer daemon URL does not match the configured Codex server.")
        if (installer_token is not None
                and not installer_credential_valid(installer_url, installer_token)):
            raise SetupError(
                "The installer credential was not accepted by the configured daemon.")
        literal = installer_token or None
        selected_daemon_url = installer_url
    try:
        if source_path is not None:
            snapshot = CredentialProvider(path=source_path).snapshot()
            target = Path(os.path.abspath(Path(source_path).expanduser()))
        else:
            token = literal or None
            if token is None:
                result = {"credential_file_configured": False,
                          "credential_file_path": "",
                          "daemon_url": selected_daemon_url,
                          "connection_configured": selected_daemon_url is not None,
                          "migrated_literal": False, "backup": None,
                          "connection_backup": None}
                if selected_daemon_url is not None:
                    result["connection_backup"] = _private_json(
                        home / "pseudolife" / "connection.json",
                        _connection_values(selected_daemon_url))
                return result
            target = (home / "pseudolife" / "token").resolve()
            if target.exists() or target.is_symlink():
                current = CredentialProvider(path=target).snapshot()
                if current.token != token:
                    _write_token_file(target, token)
            else:
                _write_token_file(target, token)
            snapshot = CredentialProvider(path=target).snapshot()
            if snapshot.token != token:
                raise CredentialError("credential file validation failed")
    except (CredentialError, OSError, UnicodeError) as error:
        raise SetupError(
            "The configured credential file is missing, unsafe, or malformed; "
            "repair it or remove PSEUDOLIFE_MCP_TOKEN_FILE before retrying.") from error

    result = {"credential_file_configured": True,
              "credential_file_path": str(target),
              "daemon_url": selected_daemon_url or _daemon_url(
                  server if effective_server else {}),
              "connection_configured": True,
              "migrated_literal": ("PSEUDOLIFE_MCP_TOKEN" in env
                                    or selected_daemon_url is not None),
              "backup": None,
              "connection_backup": None}
    if not effective_server:
        result["connection_backup"] = _private_json(
            home / "pseudolife" / "connection.json",
            _connection_values(result["daemon_url"], target))
        return result
    new_env = dict(env)
    new_env["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(target)
    new_env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    if new_env == env:
        result["connection_backup"] = _private_json(
            home / "pseudolife" / "connection.json",
            _connection_values(result["daemon_url"], target))
        return result
    path, version, _ = _user_config_layer(config, home)
    result["backup"] = private_backup(path)
    client.rpc("config/batchWrite", {
        "edits": [{"keyPath": dotted("mcp_servers", "pseudolife-memory", "env"),
                   "value": new_env, "mergeStrategy": "replace"}],
        "filePath": str(path),
        "expectedVersion": version,
    })
    current = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    actual = current.get("config", {}).get("mcp_servers", {}).get(
        "pseudolife-memory", {}).get("env", {})
    if (actual.get("PSEUDOLIFE_MCP_TOKEN_FILE") != str(target)
            or "PSEUDOLIFE_MCP_TOKEN" in actual
            or any(actual.get(key) != value for key, value in new_env.items())):
        raise SetupError(
            "Codex saved credential settings but its effective configuration differs; "
            "check project or managed overrides before reconnecting.")
    result["connection_backup"] = _private_json(
        home / "pseudolife" / "connection.json",
        _connection_values(result["daemon_url"], target))
    return result


@contextmanager
def credential_environment(path, daemon_url):
    before_file = os.environ.get("PSEUDOLIFE_MCP_TOKEN_FILE")
    before_token = os.environ.get("PSEUDOLIFE_MCP_TOKEN")
    before_url = os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL")
    try:
        if path is not None:
            if path:
                os.environ["PSEUDOLIFE_MCP_TOKEN_FILE"] = path
            else:
                os.environ.pop("PSEUDOLIFE_MCP_TOKEN_FILE", None)
            os.environ.pop("PSEUDOLIFE_MCP_TOKEN", None)
        if daemon_url:
            os.environ["PSEUDOLIFE_MCP_DAEMON_URL"] = daemon_url
        yield
    finally:
        if before_file is None:
            os.environ.pop("PSEUDOLIFE_MCP_TOKEN_FILE", None)
        else:
            os.environ["PSEUDOLIFE_MCP_TOKEN_FILE"] = before_file
        if before_token is None:
            os.environ.pop("PSEUDOLIFE_MCP_TOKEN", None)
        else:
            os.environ["PSEUDOLIFE_MCP_TOKEN"] = before_token
        if before_url is None:
            os.environ.pop("PSEUDOLIFE_MCP_DAEMON_URL", None)
        else:
            os.environ["PSEUDOLIFE_MCP_DAEMON_URL"] = before_url


def daemon_request(path, *, text=False):
    url = os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:8765").rstrip("/")
    headers = {}
    try:
        token = CredentialProvider.from_environment().snapshot().token
    except CredentialError as error:
        raise SetupError(
            "The configured credential file is missing, unsafe, or malformed.") from error
    if token:
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url + path, headers=headers), timeout=3) as response:
        return response.read().decode("utf-8") if text else json.load(response)


def board_checkin_expected():
    """Whether CoordinationStart should print the board check-in here: the
    daemon serves it only where this credential can use the board, and a
    client opt-out asks for none (2026-09-25)."""
    setting = os.environ.get("PSEUDOLIFE_AGENT_COORDINATION", "").strip().lower()
    if setting and setting not in {"1", "true", "yes", "on"}:
        return False
    try:
        return bool(daemon_request("/api/hook/coordination-start", text=True).strip())
    except Exception:
        return False


def episode_open(thread_id):
    # Exact session lookup is bounded by the recently opened episode's place in
    # the response. More than 100 simultaneous starts degrades to not-ready.
    return any(e.get("session_key") == thread_id
               for e in daemon_request("/api/episodes?limit=100")["episodes"])


def wait_for_daemon(timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if daemon_request("/health").get("status") == "ok":
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise SetupError("The memory daemon is not ready. Start it, check its URL and authentication, then rerun setup.")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def verify(executable, home, cwd, config, hooks, selected):
    wait_for_daemon()
    effective = config["config"]
    overrides = {"model_provider": "pseudolife_hook_fixture", "model": "fixture-model",
                 "model_providers.pseudolife_hook_fixture.name": "Local hook verification",
                 "model_providers.pseudolife_hook_fixture.base_url": "http://127.0.0.1:1/v1",
                 "model_providers.pseudolife_hook_fixture.wire_api": "responses",
                 "model_providers.pseudolife_hook_fixture.requires_openai_auth": False,
                 "model_providers.pseudolife_hook_fixture.request_max_retries": 0,
                 "model_providers.pseudolife_hook_fixture.stream_max_retries": 0,
                 "analytics.enabled": False, "notify": []}
    overrides["mcp_servers"] = {name: {"enabled": False} for name in effective.get("mcp_servers", {})}
    overrides["plugins"] = {name: {"enabled": False} for name in effective.get("plugins", {}) if name != PLUGIN_ID}
    own_keys = {h["key"] for h in selected}
    states = {}
    for h in hooks:
        if h["key"] not in own_keys:
            if h.get("isManaged"):
                raise SetupError("Managed hooks also apply here; automatic probe is unavailable. Review /hooks and verify in a normal session.")
            states[h["key"]] = {"enabled": False}
    overrides["hooks.state"] = states
    with codex(executable, home, cwd, overrides) as client:
        isolated_config, active = inventory(client, cwd)
        if any(server.get("enabled", True) for server in isolated_config["config"].get("mcp_servers", {}).values()):
            raise SetupError("Cannot isolate hook verification from configured MCP servers.")
        own = [h for h in active if h["key"] in own_keys]
        if len(own) != len(selected) or any(not h["enabled"] or h["trustStatus"] != "trusted" for h in own):
            raise SetupError("PseudoLife hooks are not all enabled and trusted in a fresh Codex runtime.")
        if any(h["enabled"] and h["trustStatus"] == "trusted" for h in active if h["key"] not in own_keys):
            raise SetupError("Cannot isolate PseudoLife's verification from other trusted hooks.")
        thread = client.rpc("thread/start", {"cwd": str(cwd), "ephemeral": True,
                             "approvalPolicy": "never", "sandbox": "read-only",
                             "baseInstructions": "Local hook verification only."})["thread"]["id"]
        client.rpc("turn/start", {"threadId": thread, "input": [{"type": "text",
                   "text": "Local hook verification.", "text_elements": []}]})
        deadline = time.monotonic() + 25
        completed = []
        expected = {event: sum(h["eventName"] == event for h in selected)
                    for event in ("sessionStart", "userPromptSubmit")}
        while time.monotonic() < deadline:
            completed = [e["params"]["run"] for e in client.events
                         if e.get("method") == "hook/completed"]
            if all(sum(run.get("eventName") == event for run in completed) >= expected[event]
                   for event in expected):
                break
            client.receive(deadline - time.monotonic())
        # The prompt hook prints only when memory changed, and a session's
        # first turn is a silent baseline, so its proof is the cursor it
        # saves after an authorized answer from the daemon (2026-09-26).
        from pseudolife_memory.coordination_identity import default_digest_dir
        mark = default_digest_dir() / (hashlib.sha256(thread.encode("utf-8")).hexdigest() + ".mark")
        for event, text in (("sessionStart", "Session episode:"),
                            ("userPromptSubmit", None)):
            runs = [run for run in completed if run.get("eventName") == event]
            memory = (mark.is_file() if text is None else any(
                text in entry.get("text", "") for run in runs for entry in run.get("entries", [])))
            if len(runs) != expected[event] or any(run.get("status") != "completed" for run in runs) or not memory:
                raise SetupError(f"{EVENTS[event]} did not return the expected memory context "
                                 f"({len(runs)} completed events, memory={memory}). Check daemon access and /hooks.")
        mark.unlink(missing_ok=True)
        if board_checkin_expected() and not any(
                "memory_agents(action=list)" in entry.get("text", "")
                for run in completed if run.get("eventName") == "sessionStart"
                for entry in run.get("entries", [])):
            raise SetupError("CoordinationStart did not return board setup guidance. Check /hooks.")
        if not episode_open(thread):
            raise SetupError("SessionStart did not open a verifiable memory episode. Check daemon access.")
    if episode_open(thread):
        raise SetupError("SessionEnd did not close the verification episode. Check daemon access and /hooks.")
    return {"session_start": True, "user_prompt_submit": True, "session_end": True}


def standing_instructions(home, choice, fallback_allowed, ready, report):
    # Follow Codex's first-nonempty override precedence, preserving user text.
    override = home / "AGENTS.override.md"
    path = override if override.is_file() and override.read_text(encoding="utf-8").strip() else home / "AGENTS.md"
    old = path.read_bytes() if path.exists() else b""
    if all(marker in old for marker in (b"## Memory", b"pseudolife-memory", b"RECALL", b"CAPTURE", b"REFLECT")):
        return "present"
    if choice == "skip":
        return "skipped"
    if choice == "auto" and ready:
        # Verified hooks serve a compact memory core, not this block; the
        # append stays optional (--instructions append). The state name is
        # read by both installers, which print what it means.
        return "covered-by-hooks"
    if choice != "append" and not fallback_allowed:
        return "skipped"
    data = (ROOT / "examples/CLAUDE.memory.md").read_bytes()
    saved = backup(path)
    if saved:
        report["backups"].append(saved)
    atomic_write(path, old + (b"\n\n" if old else b"") + data)
    report["instructions_path"] = str(path)
    return "appended"


def consent(args):
    if args.source == "skip":
        return False, args.instructions == "append"
    if args.trust == "yes":
        return True, True
    if args.trust == "no" or args.non_interactive or not sys.stdin.isatty():
        return False, args.instructions == "append"
    if args.instructions != "auto":
        print("Approve PseudoLife's current hook scripts (briefing, reminders, cleanup) "
              "to run outside the sandbox? [y/N] ", end="", file=sys.stderr, flush=True)
        approved = sys.stdin.readline().strip().lower() in ("y", "yes")
        return approved, args.instructions == "append"
    print("PseudoLife memory setup:\n"
          "  1. Enable automatic briefings, reminders, and session cleanup (recommended).\n"
          "     Approves only PseudoLife's current hook scripts to run outside the sandbox;\n"
          "     adds standing memory instructions if verification fails.\n"
          "  2. Standing memory instructions only.\n"
          "  3. Skip both.\nChoose [1/2/3, default 1]: ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if not answer:  # EOF is not approval.
        return False, False
    answer = answer.strip()
    if answer in ("", "1"):
        return True, True
    if answer == "2":
        args.source = "skip"
        return False, True
    args.source = "skip"
    args.instructions = "skip"
    return False, False


def setup(args):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    report = {"source": "skip" if args.source == "auto" else args.source, "status": "pending", "instructions": "skipped",
              "recovery": None, "backups": []}
    approved, fallback_allowed = consent(args)
    if args.source != "auto":
        report["source"] = args.source
    if args.source == "skip":
        report["status"] = "skipped"
    else:
        try:
            executable = resolve_codex()
            with tempfile.TemporaryDirectory(prefix="pseudolife-hook-check-") as temporary:
                cwd = Path(temporary)
                with codex(executable, home, cwd) as client:
                    config, hooks = inventory(client, cwd)
                    credential = configure_credential_file(client, home, cwd, config)
                    report.update({key: value for key, value in credential.items()
                                   if key not in {"backup", "connection_backup"}})
                    if credential["backup"]:
                        report["backups"].append(credential["backup"])
                    if credential.get("connection_backup"):
                        report["backups"].append(credential["connection_backup"])
                    if credential["credential_file_configured"]:
                        config, hooks = inventory(client, cwd)
                if config["config"].get("features", {}).get("hooks") is False:
                    raise SetupError("Codex hooks are disabled in configuration; setup preserves that choice.")
                if args.source == "auto" and config["config"].get("plugins", {}).get(PLUGIN_ID, {}).get("enabled") is False:
                    raise SetupError("The PseudoLife plugin is disabled. Re-enable it if desired, or explicitly select manual hooks.")
                source, selected, obsolete = select_hooks(hooks, home, args.source)
                report["source"] = source
                if source == "plugin":
                    vet_plugin(selected)
                elif selected:
                    vet_manual(selected, home, complete=not approved)
                if approved:
                    if source == "manual" or obsolete:
                        install_manual(home, report, plugin=source == "plugin")
                    with codex(executable, home, cwd) as client:
                        config, hooks = inventory(client, cwd)
                        _, selected, _ = select_hooks(hooks, home, source)
                        if not complete_set(selected, source):
                            raise SetupError("Codex did not discover the installed PseudoLife hooks.")
                        if source == "manual":
                            vet_manual(selected, home)
                        else:
                            vet_plugin(selected)
                        trust_hooks(client, config, selected, home, report)
                if obsolete and not approved:
                    raise SetupError("Duplicate PseudoLife hooks need migration. Rerun setup with approval or remove duplicates in /hooks.")
                if not complete_set(selected, source):
                    report["recovery"] = "Hooks are not installed. Rerun setup interactively or pass --trust yes; --instructions append enables the fallback."
                else:
                    with codex(executable, home, cwd) as client:
                        config, hooks = inventory(client, cwd)
                        _, selected, _ = select_hooks(hooks, home, source)
                    if source == "manual":
                        vet_manual(selected, home)
                    else:
                        vet_plugin(selected)
                    if any(h["trustStatus"] != "trusted" for h in selected):
                        report["recovery"] = "PseudoLife hooks await approval. Rerun setup interactively or review them in Codex /hooks."
                    else:
                        with credential_environment(
                                report.get("credential_file_path"),
                                report.get("daemon_url")):
                            report["verified"] = verify(
                                executable, home, cwd, config, hooks, selected)
                        report["status"] = "ready"
        except SetupError as exc:
            report.update(status="unavailable", recovery=str(exc))
        except Exception as exc:
            # Never serialize raw exceptions: URLs, RPC errors, and subprocess
            # output may carry credentials or private memory content.
            report.update(status="unavailable", recovery=f"Hook setup failed ({type(exc).__name__}). {RECOVERY}")
    report["instructions"] = standing_instructions(home, args.instructions, fallback_allowed,
                                                    report["status"] == "ready", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("auto", "manual", "plugin", "skip"), default="auto")
    parser.add_argument("--trust", choices=("ask", "yes", "no"), default="ask")
    parser.add_argument("--instructions", choices=("auto", "append", "skip"), default="auto")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    try:
        report = setup(args)
    except Exception as exc:
        report = {"source": "skip" if args.source == "auto" else args.source, "status": "unavailable", "instructions": "skipped",
                  "recovery": f"Setup could not write fallback instructions ({type(exc).__name__}); check file permissions."}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ready" or report["instructions"] in ("present", "appended") else 1


if __name__ == "__main__":
    sys.exit(main())
