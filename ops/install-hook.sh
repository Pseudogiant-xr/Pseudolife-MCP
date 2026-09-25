#!/usr/bin/env bash
# Idempotently add the Pseudolife-MCP session-start briefing to Claude Code or
# Codex SessionStart hooks, ALONGSIDE (never replacing) existing hooks.
# Bash port of ops/install-hook.ps1 for Linux/macOS hosts.
#
#   ops/install-hook.sh
#   ops/install-hook.sh --client codex
#   ops/install-hook.sh /path/to/settings.json
#   ops/install-hook.sh --remove-legacy [--dry-run] [/path/to/settings.json]
#
# Backs up settings.json first; re-running is a no-op once installed. Uses
# python3 (no jq dependency) for the JSON edit.
#
# --remove-legacy removes the Claude Code hooks this script and the installers
# wrote, once the pseudolife-memory plugin provides them. It removes only
# exact installer-written commands, only while the plugin is installed and
# enabled for all projects, and backs up settings.json first. --dry-run lists
# them without writing. Exit: 0 found (dry run) or removed, 3 none found,
# 4 plugin not active for all projects, 1 error, 2 usage.
set -euo pipefail

CLIENT=claude
REMOVE_LEGACY=""
DRY_RUN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --client) CLIENT="${2:-}"; shift 2 ;;
    --remove-legacy) REMOVE_LEGACY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) break ;;
  esac
done
case "$CLIENT" in claude|codex) ;; *)
  echo "invalid --client '$CLIENT' (claude|codex)" >&2; exit 2 ;;
esac
if [ -n "$REMOVE_LEGACY" ] && [ "$CLIENT" != claude ]; then
  echo "--remove-legacy is for Claude Code; ops/setup-codex-hooks.py migrates Codex hooks." >&2
  exit 2
fi
if [ -n "$DRY_RUN" ] && [ -z "$REMOVE_LEGACY" ]; then
  echo "--dry-run applies to --remove-legacy only" >&2; exit 2
fi
if [ "$CLIENT" = codex ]; then
  default_settings="$HOME/.codex/hooks.json"
  instruction_file=AGENTS.md
else
  default_settings="$HOME/.claude/settings.json"
  instruction_file=CLAUDE.md
fi
SETTINGS_PATH="${1:-$default_settings}"
COMMAND="${2:-pseudolife-mcp briefing --hook-json}"

# Every-turn memory-discipline line (UserPromptSubmit), both clients.
# Codex requires review and trust before newly installed hooks run. Static echo
# (no daemon call): the one-shot session-start briefing loses salience over
# a long session; this keeps the loop — including recall-before-review —
# mechanical. Keep the line free of quote characters (it nests in JSON+sh).
DISCIPLINE_LINE="Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."
UPS_COMMAND="echo '$DISCIPLINE_LINE'"
# Board check-in, separate from the daemon-backed memory briefing. The daemon
# serves it only where this bearer can use the board, so a board that is off
# costs no failed tool call at every session start. COORDINATION_LINE is the
# unconditional echo older installers wrote: kept to replace it here, and
# ops/setup-codex-hooks.py migrates the PowerShell copy of it.
COORDINATION_LINE="Pseudolife coordination: at the first task and on resume, use memory_agents(action=list), then memory_agents(action=update, project=<project>, task=<task>, status=<status>) to show scope. Use memory_message(action=receive); read each full message and memory_message(action=ack, message_id=<id>) after reading. On a pending-message hint, receive again. If unavailable, report that and continue independently."
case "$COMMAND" in
  *"pseudolife-mcp briefing"*) COORDINATION_COMMAND="$COMMAND --coordination" ;;
  *) COORDINATION_COMMAND="pseudolife-mcp briefing --hook-json --coordination" ;;
esac

# Prefer python3 but accept python (verified runnable — Windows ships a
# python3 Store stub that "exists" yet exits with an install nag).
PYBIN=""
for c in python3 python; do
  if "$c" -c "" >/dev/null 2>&1; then PYBIN="$c"; break; fi
done
[ -n "$PYBIN" ] || { echo "python3 is required" >&2; exit 1; }

if [ -n "$REMOVE_LEGACY" ]; then
  # The 2026-08-28..09-05 discipline line ended "-> memory_outcome." Derived,
  # so this file keeps one copy of the line (test_plugin_packaging.py).
  LEGACY_UPS_COMMAND="echo '${DISCIPLINE_LINE% with used_ids.}.'"
  rc=0
  SETTINGS_PATH="$SETTINGS_PATH" DRY_RUN="$DRY_RUN" UPS_COMMAND="$UPS_COMMAND" \
    LEGACY_UPS_COMMAND="$LEGACY_UPS_COMMAND" COORDINATION_LINE="$COORDINATION_LINE" \
    "$PYBIN" - <<'PY' || rc=$?
import json, os, shutil, stat, sys, tempfile, time

sys.stdout.reconfigure(errors="replace")
path = os.environ["SETTINGS_PATH"]
plugin_id = "pseudolife-memory@pseudolife-mcp"
# Exact commands this script and the installers ever wrote, by event. A
# substring match would also delete a user's own command that merely
# mentions one (ops/setup-codex-hooks.py applies the same rule). The
# check-in was an unconditional echo on 2026-09-24, then the briefing
# command with --coordination (COORDINATION_COMMAND above).
briefings = ("pseudolife-mcp briefing --hook-json",
             "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json")
shipped = {
    "SessionStart": {
        *briefings,
        *(f"{b} --coordination" for b in briefings),
        f"echo '{os.environ['COORDINATION_LINE']}'",
        "pseudolife-mcp episode-start",
    },
    "UserPromptSubmit": {os.environ["UPS_COMMAND"], os.environ["LEGACY_UPS_COMMAND"]},
    "SessionEnd": {"pseudolife-mcp episode-end"},
}
needles = ("pseudolife-mcp briefing", "mid-session discipline", "Pseudolife coordination:",
           "pseudolife-mcp episode-start", "pseudolife-mcp episode-end")


def is_shipped(event, hook):
    # Only strings can be ours; a list or object there is someone else's.
    known = shipped.get(event, ())
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = hook.get("command")
    windows = hook.get("commandWindows", command)
    return (isinstance(command, str) and command in known
            and isinstance(windows, str) and windows in known)


def unique_keys(pairs):
    # A duplicate would be silently dropped on rewrite (.NET refuses the
    # object outright): leave such a file to the user.
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def show(event, command):
    command = " ".join(command.split())
    return f"  {event}: {command if len(command) <= 100 else command[:97] + '...'}"


def plugin_active(settings):
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict) or enabled.get(plugin_id) is not True:
        return False
    record = os.path.join(os.path.dirname(os.path.abspath(path)),
                          "plugins", "installed_plugins.json")
    try:
        with open(record, encoding="utf-8-sig") as f:
            data = json.load(f)
        records = data["plugins"][plugin_id]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    records = records if isinstance(records, list) else [records]
    # A project-scoped install runs in that project only.
    return any(isinstance(r, dict) and r.get("scope", "user") == "user" for r in records)


if not os.path.exists(path):
    print(f"No settings file at {path} - nothing to remove.")
    sys.exit(3)
try:
    with open(path, "rb") as f:
        raw = f.read()
    obj = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys)
except (OSError, ValueError) as exc:
    print(f"Could not read {path}: {exc}", file=sys.stderr)
    sys.exit(1)
hooks = obj.get("hooks") if isinstance(obj, dict) else None
hooks = hooks if isinstance(hooks, dict) else {}

found, lookalikes = [], []
for event, groups in hooks.items():
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        for hook in entries if isinstance(entries, list) else []:
            command = hook.get("command") if isinstance(hook, dict) else None
            if not isinstance(command, str):
                continue
            if is_shipped(event, hook):
                found.append(show(event, command))
            elif any(n in command for n in needles):
                lookalikes.append(show(event, command))

if found:
    print(f"Installer-written hooks in {path} (the pseudolife-memory plugin provides these now):")
    print("\n".join(found))
else:
    print(f"No installer-written Pseudolife hooks in {path}.")
if lookalikes:
    print("Left alone - not an exact installer-written entry; review by hand:")
    print("\n".join(lookalikes))
if not found:
    sys.exit(3)
if not plugin_active(obj):
    print("Not removed: the pseudolife-memory plugin is not installed and enabled for all "
          "projects, so these may be the only Pseudolife hooks that run.")
    sys.exit(4)
if os.environ.get("DRY_RUN"):
    sys.exit(0)

# Emptied groups go; an emptied event stays an empty list, as this script's
# episode-hook clean-up leaves it.
for event in list(hooks):
    groups = hooks[event]
    if event not in shipped or not isinstance(groups, list):
        continue
    kept_groups = []
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(entries, list):
            kept = [h for h in entries if not is_shipped(event, h)]
            if len(kept) != len(entries):
                if not kept:
                    continue
                group = {**group, "hooks": kept}
        kept_groups.append(group)
    hooks[event] = kept_groups

text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
if b"\r\n" in raw:
    text = text.replace("\n", "\r\n")
# Write through a symlink (dotfile setups link settings.json into a repo),
# as install mode does, rather than replacing the link with a file.
target = os.path.realpath(path)
tmp = None
try:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup, n = f"{path}.bak-{stamp}", 1
    while os.path.exists(backup):
        backup, n = f"{path}.bak-{stamp}-{n}", n + 1
    shutil.copy2(path, backup)
    print(f"Backed up -> {backup}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target),
                               prefix=os.path.basename(target) + ".", suffix=".tmp-pseudolife")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    shutil.copymode(target, tmp)
    os.replace(tmp, target)
except OSError as exc:
    if tmp and os.path.exists(tmp):
        try:
            os.chmod(tmp, stat.S_IREAD | stat.S_IWRITE)
            os.remove(tmp)
        except OSError:
            pass
    print(f"Could not write {path}: {exc}", file=sys.stderr)
    sys.exit(1)
print(f"Removed {len(found)} installer-written hook entries from {path}.")
PY
  exit "$rc"
fi

if [ -f "$SETTINGS_PATH" ]; then
  bak="$SETTINGS_PATH.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS_PATH" "$bak"
  echo "Backed up -> $bak"
else
  mkdir -p "$(dirname "$SETTINGS_PATH")"
fi

SETTINGS_PATH="$SETTINGS_PATH" BRIEFING_COMMAND="$COMMAND" \
  UPS_COMMAND="$UPS_COMMAND" COORDINATION_COMMAND="$COORDINATION_COMMAND" \
  LEGACY_COORDINATION_LINE="$COORDINATION_LINE" "$PYBIN" - <<'PY'
import json, os

path = os.environ["SETTINGS_PATH"]
briefing_cmd = os.environ["BRIEFING_COMMAND"]
ups_cmd = os.environ.get("UPS_COMMAND", "")
coordination_cmd = os.environ["COORDINATION_COMMAND"]
legacy_line = os.environ["LEGACY_COORDINATION_LINE"]

obj = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)

hooks = obj.setdefault("hooks", {})
hooks.setdefault("SessionStart", [])
hooks.setdefault("SessionEnd", [])


def has_command(groups, needle, *, coordination=False):
    return any(needle in (h.get("command") or "")
               and ("--coordination" in (h.get("command") or "")) == coordination
               for g in groups for h in (g.get("hooks") or []))


def add_group(groups, command):
    groups.append({"hooks": [{"type": "command", "command": command}]})


def drop_exact(groups, commands):
    """Remove hooks whose command is exactly one of ``commands``: a user's
    own hook that merely mentions the same words stays."""
    removed = False
    for g in groups:
        before = len(g.get("hooks") or [])
        g["hooks"] = [h for h in (g.get("hooks") or []) if h.get("command") not in commands]
        removed = removed or len(g["hooks"]) != before
    groups[:] = [g for g in groups if g.get("hooks")]
    return removed


if has_command(hooks["SessionStart"], "pseudolife-mcp briefing"):
    print(f"Briefing hook already present in {path} - skipping.")
else:
    add_group(hooks["SessionStart"], briefing_cmd)
    print(f"Installed SessionStart briefing hook -> {path}")
    print(f"  command: {briefing_cmd}")

if drop_exact(hooks["SessionStart"],
              {f"echo '{legacy_line}'", f"Write-Output '{legacy_line}'"}):
    print("Removed the unconditional coordination check-in hook.")
if not has_command(hooks["SessionStart"], "pseudolife-mcp briefing", coordination=True):
    add_group(hooks["SessionStart"], coordination_cmd)
    print(f"Installed SessionStart coordination hook -> {path}")
    print(f"  command: {coordination_cmd}")

if ups_cmd:
    hooks.setdefault("UserPromptSubmit", [])
    if has_command(hooks["UserPromptSubmit"], "mid-session discipline"):
        print(f"Mid-session discipline hook already present in {path} - skipping.")
    else:
        add_group(hooks["UserPromptSubmit"], ups_cmd)
        print(f"Installed UserPromptSubmit discipline hook -> {path}")

# Episode hooks are OBSOLETE since the 2026-06-30 session-scoped episodes
# rework: the daemon lazily opens/closes episodes keyed by mcp-session-id
# (see docs/guide/episodes.md). Earlier installer versions added
# them — remove any we find so old installs converge too.


def drop_command(groups, needle):
    removed = False
    for g in groups:
        before = len(g.get("hooks") or [])
        g["hooks"] = [h for h in (g.get("hooks") or [])
                      if needle not in (h.get("command") or "")]
        removed = removed or len(g["hooks"]) != before
    groups[:] = [g for g in groups if g.get("hooks")]
    return removed


if drop_command(hooks["SessionStart"], "pseudolife-mcp episode-start"):
    print("Removed obsolete episode-start hook (daemon owns episodes now).")
if drop_command(hooks["SessionEnd"], "pseudolife-mcp episode-end"):
    print("Removed obsolete episode-end hook (daemon owns episodes now).")

with open(path, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
    f.write("\n")
PY

if [ "$CLIENT" = codex ]; then
  echo ""
  echo "IMPORTANT: Codex will skip this new or changed hook until you review and trust its exact definition."
  echo "  Start Codex, open /hooks, review the definition from $SETTINGS_PATH, and approve it."
  echo "Current Codex runtimes enable hooks by default, including Windows."
  echo "  Check /hooks for trust or managed-policy blocks; older runtimes may need updating."
  echo "  If [features] hooks = false is intentional, use the standing AGENTS.md block."
fi

# The hooks wire the session lifecycle, but the memory LOOP only fires if a
# standing instruction tells the agent to use the tools (issue #12: an install
# with healthy hooks + daemon still never called memory_* because no standing
# instructions carried the block). Check-and-advise only — never edit it here.
repo="$(cd "$(dirname "$0")/.." && pwd)"
instruction_path="$(dirname "$SETTINGS_PATH")/$instruction_file"
if ! grep -q "pseudolife-memory" "$instruction_path" 2>/dev/null; then
  echo ""
  echo "REMINDER: $instruction_path has no Pseudolife memory section."
  echo "Append the bundled block for stronger recall/capture guidance:"
  echo "  cat $repo/examples/CLAUDE.memory.md >> $instruction_path"
  echo "(or add it to a per-project CLAUDE.md / AGENTS.md instead)"
fi
