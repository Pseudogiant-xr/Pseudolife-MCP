#!/usr/bin/env python3
"""Install, approve, and verify only PseudoLife's Codex lifecycle hooks.

Uses Codex's JSON-RPC config writer and runtime-generated trust hashes. No
third-party Python packages, external model requests, or hook-trust bypass are needed.
"""
from __future__ import annotations

import argparse
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
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
EVENTS = {"sessionStart": "SessionStart", "userPromptSubmit": "UserPromptSubmit",
          "sessionEnd": "SessionEnd"}
SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh", "session-end.sh")
RECOVERY = "Open Codex /hooks to review PseudoLife hooks; rerun setup after correcting the reported problem."


class SetupError(Exception):
    """A safe, user-facing error (never a raw transport error)."""


def backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(path.name + ".bak-pseudolife-" + stamp)
    shutil.copy2(path, target)
    return str(target)


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
        if self.proc.stdout:
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


def bundle_bytes(directory):
    return {name: (directory / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode()
            for name in SCRIPTS}


def bundle_digest(files):
    digest = hashlib.sha256()
    for name in SCRIPTS:
        digest.update(name.encode() + b"\0" + files[name] + b"\0")
    return digest.hexdigest()[:20]


def manual_definitions(directory):
    mapping = {"SessionStart": "session-start.sh", "UserPromptSubmit": "user-prompt-submit.sh",
               "SessionEnd": "session-end.sh"}
    # Literal single quotes protect $, backticks, and spaces in native paths.
    ps = str(directory / "lifecycle.ps1").replace("'", "''")
    return {event: {"type": "command", "command": "bash " + shlex.quote(str(directory / script)),
                    "commandWindows": f"pwsh -NoProfile -File '{ps}' -Event {event}",
                    "timeout": {"SessionStart": 15, "UserPromptSubmit": 5, "SessionEnd": 3}[event]}
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
        command = hook.get("command")
        event = EVENTS.get(hook.get("eventName"))
        if event and command in (definitions[event]["command"], definitions[event]["commandWindows"]):
            return True
    return False


def legacy_commands():
    # Only shipped legacy commands are migratable; substring matches would
    # silently remove or approve arbitrary user code.
    line = re.search(r'\$disciplineLine = "(.*)"',
                     (ROOT / "plugin/hooks/lifecycle.ps1").read_text(encoding="utf-8"))[1]
    return {"pseudolife-mcp briefing --hook-json",
            "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json",
            f"echo '{line}'", f"Write-Output '{line}'"}


def is_legacy(hook, home):
    return (hook.get("source") != "plugin"
            and Path(hook.get("sourcePath", "")).resolve() == (home / "hooks.json").resolve()
            and hook.get("command") in legacy_commands())


def select_hooks(hooks, home, source):
    plugin = [h for h in hooks if h.get("pluginId") == PLUGIN_ID]
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
    if len(hooks) != 3 or {h["eventName"] for h in hooks} != set(EVENTS):
        raise SetupError("The enabled PseudoLife plugin is missing or has unexpected hooks; update it and retry.")
    expected = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    for h in hooks:
        path = Path(h["sourcePath"])
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))["hooks"]
            files_match = bundle_bytes(path.parent) == bundle_bytes(ROOT / "plugin/hooks")
        except (OSError, ValueError):
            files_match = False
            actual = None
        if actual != expected or not files_match or h.get("handlerType") != "command":
            raise SetupError("Installed PseudoLife hooks differ from this installer. Update them together or review manually in /hooks.")


def vet_manual(hooks, home, complete=True):
    if complete and (len(hooks) != 3 or {h["eventName"] for h in hooks} != set(EVENTS)):
        raise SetupError("The manual PseudoLife hook set is incomplete; rerun setup with approval to repair it.")
    directories = []
    for directory in (home / "pseudolife/hooks").glob("*"):
        definitions = manual_definitions(directory)
        if all(h.get("command") in (definitions[EVENTS[h["eventName"]]]["command"],
                                     definitions[EVENTS[h["eventName"]]]["commandWindows"]) for h in hooks):
            directories.append(directory)
    if len(directories) != 1:
        raise SetupError("Manual PseudoLife hooks reference mixed or unknown script bundles; review /hooks.")
    try:
        matches = bundle_digest(bundle_bytes(directories[0])) == directories[0].name
    except (OSError, UnicodeError):
        matches = False
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
            for d in manual_definitions(directory).values():
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
        if event in definitions:
            groups.append({"hooks": [definitions[event]]})
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


def daemon_request(path):
    url = os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:8765").rstrip("/")
    headers = {}
    if os.environ.get("PSEUDOLIFE_MCP_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["PSEUDOLIFE_MCP_TOKEN"]
    with urlopen(Request(url + path, headers=headers), timeout=3) as response:
        return json.load(response)


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
        if len(own) != 3 or any(not h["enabled"] or h["trustStatus"] != "trusted" for h in own):
            raise SetupError("PseudoLife hooks are not all enabled and trusted in a fresh Codex runtime.")
        if any(h["enabled"] and h["trustStatus"] == "trusted" for h in active if h["key"] not in own_keys):
            raise SetupError("Cannot isolate PseudoLife's verification from other trusted hooks.")
        thread = client.rpc("thread/start", {"cwd": str(cwd), "ephemeral": True,
                             "approvalPolicy": "never", "sandbox": "read-only",
                             "baseInstructions": "Local hook verification only."})["thread"]["id"]
        client.rpc("turn/start", {"threadId": thread, "input": [{"type": "text",
                   "text": "Local hook verification.", "text_elements": []}]})
        deadline = time.monotonic() + 25
        completed = {}
        while time.monotonic() < deadline:
            completed = {e["params"]["run"]["eventName"]: e["params"]["run"]
                         for e in client.events if e.get("method") == "hook/completed"}
            if all(event in completed for event in ("sessionStart", "userPromptSubmit")):
                break
            client.receive(deadline - time.monotonic())
        for event, text in (("sessionStart", "Session episode:"),
                            ("userPromptSubmit", "memory_lesson_search")):
            run = completed.get(event, {})
            if run.get("status") != "completed" or not any(text in e.get("text", "") for e in run.get("entries", [])):
                raise SetupError(f"{EVENTS[event]} did not return the expected memory context. Check daemon access and /hooks.")
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
        print("Approve PseudoLife's three current hook scripts (briefing, reminders, cleanup) "
              "to run outside the sandbox? [y/N] ", end="", file=sys.stderr, flush=True)
        approved = sys.stdin.readline().strip().lower() in ("y", "yes")
        return approved, args.instructions == "append"
    print("PseudoLife memory setup:\n"
          "  1. Enable automatic briefings, reminders, and session cleanup (recommended).\n"
          "     Approves only PseudoLife's current three scripts to run outside the sandbox;\n"
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
                        if len(selected) != 3 or {h["eventName"] for h in selected} != set(EVENTS):
                            raise SetupError("Codex did not discover the three installed PseudoLife hooks.")
                        if source == "manual":
                            vet_manual(selected, home)
                        else:
                            vet_plugin(selected)
                        trust_hooks(client, config, selected, home, report)
                if obsolete and not approved:
                    raise SetupError("Duplicate PseudoLife hooks need migration. Rerun setup with approval or remove duplicates in /hooks.")
                if len(selected) != 3:
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
                        report["verified"] = verify(executable, home, cwd, config, hooks, selected)
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
