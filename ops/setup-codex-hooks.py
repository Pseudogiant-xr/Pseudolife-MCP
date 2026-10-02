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
import re
import shlex
import shutil
import sys
import tempfile
import time
from urllib.request import Request

# Setup runs before the host shim is installed or upgraded. Prefer this
# checkout's standard-library credential helper over an older installed copy.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pseudolife_memory.credentials import (
    CredentialError,
    CredentialProvider,
    _write_token_file,
)
# The Codex connection writer (config env, connection.json, token copy) lives
# in the package, so that `pseudolife-mcp connect` can run it from a release
# install too.
from pseudolife_memory import codex_connection as _connection  # noqa: E402
from pseudolife_memory.codex_connection import (  # noqa: E402,F401
    SERVER,
    Codex,
    SetupError,
    _connection_values,
    _daemon_url,
    _private_json,
    _user_config_layer,
    _validated_daemon_url,
    dotted,
    installer_credential_valid,
    private_backup,
    resolve_codex,
    toml_value,
    urlopen,
)


PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
EVENTS = {"sessionStart": "SessionStart", "userPromptSubmit": "UserPromptSubmit",
          "sessionEnd": "SessionEnd"}
# MemoryPolicy is the separate memory-policy SessionStart output (the full
# memory-loop block when the daemon's memory_policy variant asks for it).
MANUAL_ROLES = {"sessionStart": ("SessionStart", "MemoryPolicy", "CoordinationStart"),
                "userPromptSubmit": ("UserPromptSubmit", "CoordinationPrompt"),
                "sessionEnd": ("SessionEnd",)}
# The plugin's hooks.json also carries Claude Code's Stop wake hook (on by
# default since 2026-09-28), which Codex lists too. In Codex it runs only the
# park gate (v49; lifecycle.ps1 -Event Stop on Windows, stop-wake.sh in Codex
# context elsewhere), never the wake, approved with the three lifecycle
# hooks. Optional: Codex before 0.148 skips async hooks outside SessionEnd
# and lists three. Manual installs keep EVENTS.
# SubagentStop runs the same park gate for a Codex child thread, keyed by the
# child's agent_id (its MCP threadId, probed on Codex 0.158.0 2026-09-29); in
# Claude Code the entry does nothing. Optional too: a Codex that does not
# list it still reaches ready.
# The PreToolUse entry is Claude Code's subagent board guard
# (subagent-board-guard.sh). Codex 0.158.0 lists it (pre_tool_use:0:0 in
# hooks/list, 2026-09-30), but a Codex child has a board address of its own,
# so in Codex it allows every call (lifecycle.ps1 -Event SubagentBoardGuard
# on Windows, the script's Codex check elsewhere). Approved with the rest,
# like Stop, and optional the same way.
# SubagentStart, and a second SubagentStop entry (schema v50), carry Claude
# Code's subagent liveness hook (subagent-board.sh), a no-op in Codex
# (lifecycle.ps1 -Event SubagentBoardStart|SubagentBoardStop exits at once;
# the bash script exits in Codex context): the shim links Codex's native
# subagents to their parent instead. Async, so a Codex that skips async
# hooks lists only the park gate's SubagentStop entry.
PLUGIN_EVENTS = {**EVENTS, "stop": "Stop", "subagentStart": "SubagentStart",
                 "subagentStop": "SubagentStop", "preToolUse": "PreToolUse"}
# Each optional event and how many plugin handlers it has in hooks.json
# (tests/test_codex_hook_setup_edges.py pins this against the manifest). A
# Codex may list none of them, or any of them; each listed one must still be
# a distinct handler of the shipped manifest.
OPTIONAL_PLUGIN_EVENTS = {"stop": 1, "subagentStart": 1, "subagentStop": 2, "preToolUse": 1}
# A manual bundle copies SCRIPTS; it has no Stop, PreToolUse or subagent
# hooks, so no stop-wake.sh, subagent-board-guard.sh or subagent-board.sh.
SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh",
           "coordination-start.sh", "coordination-prompt.sh", "session-end.sh")
LEGACY_SCRIPTS = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh", "session-end.sh")
PLUGIN_SCRIPTS = SCRIPTS + ("stop-wake.sh", "subagent-board-guard.sh", "subagent-board.sh")
RECOVERY = "Open Codex /hooks to review PseudoLife hooks; rerun setup after correcting the reported problem."

# Manual copies run through a launcher (2026-09-30). Codex approves a hook by
# its definition (command, timeout, async, statusMessage), not by the script
# the command runs: on Codex 0.158.0 editing a script left `currentHash`
# unchanged and editing the command changed it. The commands used to name a
# content-addressed bundle directory, so every script change was a new
# command and a new approval. They now name run.sh / run.ps1 beside the
# bundles, which run the bundle `current` names; bundles stay
# content-addressed and are verified before `current` moves to one, and the
# launchers are verified byte for byte.
POINTER = "current"
LAUNCHERS = {
    "run.sh": b"""#!/usr/bin/env bash
# PseudoLife's Codex hooks: runs the script named by $1 from the bundle that
# ./current names. Codex approves the command naming this file, not the
# scripts it runs, so a new bundle needs no new approval. Written and
# verified byte for byte by ops/setup-codex-hooks.py.
here=$(dirname "$0")
fail() { printf 'PseudoLife hooks: %s; rerun: python ops/setup-codex-hooks.py\\n' "$1" >&2; exit 1; }
bundle=$(head -c 64 "$here/current" 2>/dev/null | tr -d '\\r\\n ')
case "$bundle" in *[!0-9a-f]*|'') fail "$here/current names no script bundle" ;; esac
[ "${#bundle}" -eq 20 ] || fail "$here/current names no script bundle"
case "${1:-}" in
    session-start.sh|user-prompt-submit.sh|coordination-start.sh|coordination-prompt.sh|session-end.sh) ;;
    *) fail "no hook script named ${1:-(none)}" ;;
esac
script=$1
shift
exec bash "$here/$bundle/$script" "$@"
""",
    "run.ps1": b"""# PseudoLife's Codex hooks: runs lifecycle.ps1 from the bundle that
# .\\current names. Codex approves the command naming this file, not the
# scripts it runs, so a new bundle needs no new approval. Written and
# verified byte for byte by ops/setup-codex-hooks.py.
param([string]$Event)
$bundle = ''
try { $bundle = ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'current'))).Trim() } catch {}
if ($bundle -cnotmatch '^[0-9a-f]{20}$') {
    [Console]::Error.WriteLine("PseudoLife hooks: $PSScriptRoot\\current names no script bundle; rerun: python ops/setup-codex-hooks.py")
    exit 1
}
$global:LASTEXITCODE = 0
& (Join-Path (Join-Path $PSScriptRoot $bundle) 'lifecycle.ps1') -Event $Event
exit $LASTEXITCODE
""",
}
# The sha256 of every launcher an earlier release shipped, by name. A
# refresh replaces one of these with LAUNCHERS (Codex does not hash it), so
# editing a launcher never strands an install; one no release shipped is
# refused as modified. Changing LAUNCHERS? Add the old bytes' sha256 here
# (tests/test_codex_hook_launcher.py pins the current ones).
PREVIOUS_LAUNCHERS = {"run.sh": frozenset(), "run.ps1": frozenset()}


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
        # On Windows a replace is refused while another process holds the
        # target for a moment: a hook reading `current`, or a scanner
        # opening a file just written (a live setup run met it 2026-09-30).
        for _ in range(40):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                time.sleep(0.05)
        else:
            os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def codex(executable, home, cwd, overrides=None):
    """Codex's app-server, built from this module's ``Codex`` name (a test
    tripwire replaces it to prove a code path never launches Codex)."""
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
    if source == "plugin":
        for event, most in OPTIONAL_PLUGIN_EVENTS.items():
            if 1 <= counts.get(event, 0) <= most:
                del counts[event]
    return (counts == required
            and len({h.get("command") for h in hooks}) == len(hooks)
            and len({h.get("key") for h in hooks}) == len(hooks))


def bundle_digest(files, names=SCRIPTS):
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + files[name] + b"\0")
    return digest.hexdigest()[:20]


_MANUAL_SCRIPTS = {"SessionStart": "session-start.sh", "MemoryPolicy": "session-start.sh",
                   "UserPromptSubmit": "user-prompt-submit.sh",
                   "CoordinationStart": "coordination-start.sh", "CoordinationPrompt": "coordination-prompt.sh",
                   "SessionEnd": "session-end.sh"}
_MANUAL_TIMEOUTS = {"SessionStart": 15, "MemoryPolicy": 15, "UserPromptSubmit": 5,
                    "CoordinationStart": 5, "CoordinationPrompt": 5, "SessionEnd": 3}


def _definitions(bash_target, ps_path, codex_marker):
    """``bash_target(script)`` is the bash command's script and arguments."""
    # Literal single quotes protect $, backticks, and spaces in native paths.
    ps = str(ps_path).replace("'", "''")
    bash_prefix = "env PSEUDOLIFE_CODEX_HOOK=1 bash " if codex_marker else "bash "
    arguments = {"MemoryPolicy": " memory-policy"}
    return {event: {"type": "command",
                    "command": bash_prefix + bash_target(script) + arguments.get(event, ""),
                    "commandWindows": f"pwsh -NoProfile -File '{ps}' -Event {event}",
                    "timeout": _MANUAL_TIMEOUTS[event]}
            for event, script in _MANUAL_SCRIPTS.items()}


def manual_definitions(directory, codex_marker=True):
    """The definitions naming one bundle directory directly: what setup wrote
    before the launcher, recognized so that setup can replace them."""
    return _definitions(lambda script: shlex.quote(str(directory / script)),
                        directory / "lifecycle.ps1", codex_marker)


def hooks_root(home):
    return Path(home) / "pseudolife" / "hooks"


def launcher_definitions(home, codex_marker=True):
    """The manual hooks' definitions through the launchers. They name no
    bundle, so a refresh leaves them, and Codex's approval, as they are."""
    root = hooks_root(home)
    return _definitions(lambda script: shlex.quote(str(root / "run.sh")) + " " + script,
                        root / "run.ps1", codex_marker)


def launcher_installed(home):
    """Whether ``hooks.json`` runs every manual role through the launcher."""
    try:
        hooks = json.loads((Path(home) / "hooks.json").read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError, AttributeError):
        return False
    commands = {h.get("command") for groups in (hooks.values() if isinstance(hooks, dict) else [])
                if isinstance(groups, list) for g in groups if isinstance(g, dict)
                for h in g.get("hooks", []) if isinstance(h, dict)}
    return all(d["command"] in commands for d in launcher_definitions(home).values())


def _pointer(home):
    try:
        name = (hooks_root(home) / POINTER).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return name if re.fullmatch(r"[a-f0-9]{20}", name) else None


def _launcher_shipped(data, name):
    """This release's launcher, or one an earlier release shipped."""
    return data == LAUNCHERS[name] or hashlib.sha256(data).hexdigest() in PREVIOUS_LAUNCHERS.get(name, ())


def _vet_launcher(home, repairable=False):
    """The launchers are ones a release shipped, ``current`` names a bundle,
    and that bundle's scripts still digest to its name. ``repairable``
    (setup with approval, which rewrites the launchers and the pointer next)
    checks only the bundle, and only when the pointer names one: a modified
    bundle is refused either way."""
    root = hooks_root(home)
    if not repairable:
        for name in LAUNCHERS:
            try:
                intact = _launcher_shipped((root / name).read_bytes(), name)
            except OSError:
                intact = False
            if not intact:
                raise SetupError("A PseudoLife hook launcher is missing or was modified; rerun setup with "
                                 "approval to repair it: python ops/setup-codex-hooks.py --source manual --trust ask")
    name = _pointer(home)
    if name is None:
        if repairable:
            return None
        raise SetupError("The PseudoLife hook pointer is missing or malformed; rerun setup with approval to "
                         "repair it: python ops/setup-codex-hooks.py --source manual --trust ask")
    try:
        matches = bundle_digest(bundle_bytes(root / name)) == name
    except (OSError, UnicodeError):
        matches = False
    if not matches and not (repairable and not (root / name).exists()):
        raise SetupError("An installed PseudoLife script was modified; restore or review it before running verification.")
    return root / name


def _write_bundle(root, files):
    """Write ``files`` as their content-addressed bundle. A bundle this call
    writes into is recorded as a launcher bundle, and so may be pruned
    later; one that already held every file is not: it may be a bundle from
    before the launcher that a running session's commands still name."""
    directory = root / bundle_digest(files)
    wrote = False
    for name, data in files.items():
        destination = directory / name
        if destination.exists() and destination.read_bytes() != data:
            raise SetupError("An installed PseudoLife script was modified; restore or review it before rerunning setup.")
        if not destination.exists():
            atomic_write(destination, data)
            wrote = True
    launched = _launched(root)
    if wrote and directory.name not in launched:
        atomic_write(root / LAUNCHED, "".join(n + "\n" for n in launched + [directory.name]).encode())
    return directory


def _write_launchers(root):
    for name, data in LAUNCHERS.items():
        path = root / name
        if not path.exists() or path.read_bytes() != data:
            atomic_write(path, data)


# The bundles written since the launcher, one name per line. Only these are
# ever removed: every launcher run reads `current` afresh, while a bundle from
# before the launcher is named in the commands a still-running Codex session
# may have loaded, so it stays where it is.
LAUNCHED = "launched"


def _launched(root):
    try:
        lines = (root / LAUNCHED).read_text(encoding="utf-8").split()
    except (OSError, UnicodeError):
        return []
    return [n for n in lines if re.fullmatch(r"[a-f0-9]{20}", n)]


def _point(root, name):
    atomic_write(root / POINTER, (name + "\n").encode())


def _prune(root, keep):
    """Remove launcher bundles other than ``keep``. A hook still running from
    one can hold a file on Windows; what cannot go now goes on a later run."""
    kept = []
    for name in _launched(root):
        if name != keep:
            shutil.rmtree(root / name, ignore_errors=True)
        if (root / name).exists():
            kept.append(name)
    atomic_write(root / LAUNCHED, "".join(n + "\n" for n in kept).encode())


def refresh_manual(home, scripts=None):
    """Move launcher-run manual copies to ``scripts`` (the checkout's by
    default) without touching the approved commands: write the new bundle,
    verify it, point ``current`` at it, bring the launchers to this
    release's, then drop the old bundle. No consent is asked because nothing
    Codex approves changes."""
    home = Path(home)
    if not launcher_installed(home):
        raise SetupError("These manual hooks name a script bundle directly; approve them once more to switch "
                         "to the launcher: python ops/setup-codex-hooks.py --source manual --trust ask")
    root = hooks_root(home)
    _vet_launcher(home)
    files = bundle_bytes(Path(scripts) if scripts else ROOT / "plugin/hooks")
    name = bundle_digest(files)
    previous = _pointer(home)
    if name != previous:
        directory = _write_bundle(root, files)
        if bundle_digest(bundle_bytes(directory)) != name:
            raise SetupError("The new PseudoLife script bundle did not verify; the current one stays.")
        _point(root, name)
    _write_launchers(root)
    _prune(root, name)
    return {"state": "current" if name == previous else "refreshed", "bundle": name, "previous": previous}


def owned_manual(hook, home):
    """Recognize our immutable installed bundles by an exact generated command."""
    if hook.get("source") == "plugin":
        return False
    source = Path(hook.get("sourcePath", "")).resolve()
    if source != (home / "hooks.json").resolve():
        return False
    launcher = launcher_definitions(home)
    if any(hook.get("command") in (launcher[role]["command"], launcher[role]["commandWindows"])
           for role in MANUAL_ROLES.get(hook.get("eventName"), ())):
        return True
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
    # the static echo install-hook wrote until 2026-09-26, when it (and the
    # plugin's prompt hook) became the memory-change note, prompt-hook.
    install_hook = (ROOT / "ops/install-hook.ps1").read_text(encoding="utf-8")
    line = re.search(r'\$disciplineLine = "(.*)"', install_hook)[1]
    coordination = re.search(r'\$coordinationLine = "(.*)"', install_hook)[1]
    briefings = {"pseudolife-mcp briefing --hook-json",
                 "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"}
    prompts = {"pseudolife-mcp prompt-hook",
               "docker exec -i pseudolife-mcp-daemon pseudolife-mcp prompt-hook"}
    # The installers' daemon-gated check-in (2026-09-25) and the
    # unconditional echo it replaced.
    return {*briefings, *(command + " --coordination" for command in briefings), *prompts,
            f"echo '{line}'", f"Write-Output '{line}'",
            f"echo '{coordination}'", f"Write-Output '{coordination}'"}


def is_legacy(hook, home):
    return (hook.get("source") != "plugin"
            and Path(hook.get("sourcePath", "")).resolve() == (home / "hooks.json").resolve()
            and hook.get("command") in legacy_commands())


def select_hooks(hooks, home, source):
    # A Stop or SubagentStop park-gate entry the user disabled in /hooks
    # gives up only the park gate, and a PreToolUse or subagent liveness one
    # is a no-op in Codex: keep that choice instead of refusing setup over it.
    plugin = [h for h in hooks if h.get("pluginId") == PLUGIN_ID
              and (h["enabled"] or h["eventName"] not in OPTIONAL_PLUGIN_EVENTS)]
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
    optional = {PLUGIN_EVENTS[event] for event in OPTIONAL_PLUGIN_EVENTS}
    # Every lifecycle role must be listed; an optional event may list any of
    # its handlers (a Codex that skips async hooks lists only SubagentStop's
    # park gate), each already matched to a distinct role above.
    if any(len(seen[event]) != len(roles[event]) for event in seen if event not in optional):
        raise SetupError("Installed PseudoLife hooks differ from this installer. Update them together or review manually in /hooks.")


def _roles_complete(hooks, definitions):
    for event, roles in MANUAL_ROLES.items():
        seen = [h.get("command") for h in hooks if h.get("eventName") == event]
        if len(seen) != len(roles) or any(
                not any(command in (definitions[role]["command"], definitions[role]["commandWindows"])
                        for command in seen)
                for role in roles):
            raise SetupError("The manual PseudoLife hook roles are incomplete; rerun setup with approval to repair them.")


def vet_manual(hooks, home, complete=True):
    if complete and not complete_set(hooks, "manual"):
        raise SetupError("The manual PseudoLife hook set is incomplete; rerun setup with approval to repair it.")
    launcher = launcher_definitions(home)
    if hooks and all(any(h.get("command") in (launcher[role]["command"], launcher[role]["commandWindows"])
                         for role in MANUAL_ROLES.get(h.get("eventName"), ()))
                     for h in hooks):
        _vet_launcher(home, repairable=not complete)
        if complete:
            _roles_complete(hooks, launcher)
        return
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
        _roles_complete(hooks, manual_definitions(directories[0]))
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


def install_manual(home, report, plugin=False, scripts=None):
    """Write the manual hooks: the scripts as a content-addressed bundle, the
    launchers, ``current`` pointing at the bundle, and ``hooks.json``
    handlers that run the launchers. Handlers setup wrote before (naming a
    bundle directly, or through the launcher) are replaced; anything else
    is kept. ``scripts`` defaults to the checkout's."""
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
    for d in launcher_definitions(home).values():
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
    current = None
    if not plugin:
        root = hooks_root(home)
        current = _write_bundle(root, bundle_bytes(Path(scripts) if scripts else ROOT / "plugin/hooks")).name
        _write_launchers(root)
        _point(root, current)
        definitions = launcher_definitions(home)
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
    if original is None or json.loads(original) != obj:
        saved = backup(path)
        if saved:
            report["backups"].append(saved)
        atomic_write(path, data)
    if current:
        # Only now does nothing name the older bundles.
        _prune(hooks_root(home), current)


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


# Board mail's tool. Hook setup's consent also covers approving it
# (maintainer decision 2026-10-02): a board ring wakes a thread only for mail
# it needs, and a woken thread that must ask before memory_message stalls on
# the prompt. Codex approves per tool, so this approves sends too; it
# approves nothing else. The server is the user-registered pseudolife-memory
# for every hook source: the plugin ships hooks only, no MCP server.
MAILBOX_TOOL = "memory_message"


def _user_layer(config, home):
    path = (home / "config.toml").resolve()
    layers = [l for l in config.get("layers", [])
              if l.get("name", {}).get("type") == "user" and not l["name"].get("profile")
              and Path(l["name"].get("file", "")).resolve() == path]
    return layers[0] if len(layers) == 1 else None


def _mailbox_mode(config, home):
    """The approval_mode Codex holds for memory_message on PseudoLife's
    server, and that server's table in the user configuration. The effective
    value counts first, so a choice another layer makes is kept too; the
    user layer's is read where Codex does not report the tool table."""
    def mode(server):
        tools = server.get("tools") if isinstance(server, dict) else None
        tool = tools.get(MAILBOX_TOOL) if isinstance(tools, dict) else None
        return tool.get("approval_mode") if isinstance(tool, dict) else None
    effective = (config.get("config", {}).get("mcp_servers") or {}).get(SERVER)
    layer = _user_layer(config, home)
    user = ((layer or {}).get("config") or {}).get("mcp_servers") or {}
    user = user.get(SERVER) if isinstance(user, dict) else None
    current = mode(effective)
    return (mode(user) if current is None else current), user


def mailbox_approval(config, home, report, client=None, cwd=None):
    """Record in ``report`` whether memory_message is approved, approving it
    when consent was given (``client``) and nothing is set. States: "set",
    "already" ("approve" was set), "kept-explicit" (another value stays),
    "declined" (no consent) and "unavailable" (the reason in
    ``mailbox_approval_detail``). A failure here never fails hook setup."""
    try:
        if client is not None:
            # The hook trust write just before this moved the version.
            config = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
        current, server = _mailbox_mode(config, home)
        if current == "approve":
            state = "already"
        elif current is not None:
            state = "kept-explicit"
            report["mailbox_approval_detail"] = "approval_mode = " + toml_value(current)
        elif client is None:
            state = "declined"
        else:
            # Writing the key without the server's own table would leave
            # Codex a server with no command.
            if not isinstance(server, dict) or not server:
                raise SetupError("Codex's user configuration has no pseudolife-memory server.")
            layer = _user_layer(config, home)
            if not layer or not layer.get("version"):
                raise SetupError("Codex did not expose a versioned user configuration.")
            path = (home / "config.toml").resolve()
            saved = backup(path)
            if saved:
                report["backups"].append(saved)
            client.rpc("config/batchWrite", {
                "edits": [{"keyPath": dotted("mcp_servers", SERVER, "tools", MAILBOX_TOOL, "approval_mode"),
                           "value": "approve", "mergeStrategy": "replace"}],
                "filePath": str(path), "expectedVersion": layer["version"]})
            after = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
            if _mailbox_mode(after, home)[0] != "approve":
                raise SetupError("Codex saved the approval but its effective configuration differs.")
            state = "set"
    except SetupError as exc:
        state = "unavailable"
        report["mailbox_approval_detail"] = str(exc)
    report["mailbox_approval"] = state
    return state


def mailbox_notice(state, detail=None):
    """The end-of-setup line for ready hooks: what happened to the
    memory_message approval, and the choice still open where none was set."""
    table = f"[mcp_servers.{SERVER}.tools.{MAILBOX_TOOL}]"
    see = "\nSee docs/guide/configuration.md (Experimental agent coordination)."
    if state == "set":
        return ('Hooks ready; setup approved memory_message (approval_mode = "approve" under '
                f'{table} in Codex config.toml), so a thread woken by board mail can receive, '
                'ack and send without an approval prompt. No other tool was approved.' + see)
    if state == "already":
        return ('Hooks ready; memory_message is already approved (approval_mode = "approve"), '
                'so a thread woken by board mail can receive, ack and send without an '
                'approval prompt.' + see)
    if state == "kept-explicit":
        lead = f"Hooks ready; setup kept your memory_message {detail}. "
    elif state == "unavailable":
        lead = f"Hooks ready; setup could not approve memory_message: {detail} "
    else:
        lead = "Hooks ready; setup leaves tool approvals unchanged. "
    return (lead + 'For unattended receive, ack and send, choose approval_mode = "approve" '
            f'under {table} in Codex config.toml. '
            'Without that approval, a woken thread can stall on an approval prompt.' + see)


def configure_credential_file(client, home, cwd, config=None,
                              installer_connection=None):
    """Bootstrap a private token file and migrate an existing Codex MCP env
    (``codex_connection.configure_credential_file``, checking an installer
    credential with this module's ``installer_credential_valid``)."""
    return _connection.configure_credential_file(
        client, home, cwd, config, installer_connection,
        credential_valid=lambda url, token: installer_credential_valid(url, token))


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
            raise SetupError("The agent-board check-in hook (CoordinationStart) returned no guidance. Check /hooks.")
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
        print("Approve PseudoLife's hooks (briefing, reminders, cleanup, agent-board check-in, "
              "new-mail hint) to run outside the sandbox, including the scripts later PseudoLife "
              "updates install, and its memory_message tool (board mail: receive, ack, send) "
              "so a thread woken by mail does not stall on an approval prompt? [y/N] ",
              end="", file=sys.stderr, flush=True)
        approved = sys.stdin.readline().strip().lower() in ("y", "yes")
        return approved, args.instructions == "append"
    print("PseudoLife memory setup:\n"
          "  1. Enable automatic briefings, reminders, and session cleanup (recommended).\n"
          "     Where the agent board is on, also an agent-board check-in at session start\n"
          "     and a new-mail hint when a peer's message is waiting.\n"
          "     Approves only PseudoLife's hooks to run outside the sandbox, including the\n"
          "     scripts later PseudoLife updates install, and its memory_message tool\n"
          "     (board mail: receive, ack, send) so a thread woken by mail does not stall\n"
          "     on an approval prompt; adds standing memory instructions if verification fails.\n"
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
                        mailbox_approval(config, home, report, client, cwd)
                else:
                    mailbox_approval(config, home, report)
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
                        report["mailbox_approval_notice"] = mailbox_notice(
                            report["mailbox_approval"], report.get("mailbox_approval_detail"))
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
    if report["status"] == "ready":
        print(report.get("mailbox_approval_notice", ""), file=sys.stderr)
    return 0 if report["status"] == "ready" or report["instructions"] in ("present", "appended") else 1


if __name__ == "__main__":
    sys.exit(main())
