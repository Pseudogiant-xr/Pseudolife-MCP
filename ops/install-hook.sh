#!/usr/bin/env bash
# Idempotently add the Pseudolife-MCP session-start briefing to Claude Code or
# Codex SessionStart hooks, ALONGSIDE (never replacing) existing hooks.
# Bash port of ops/install-hook.ps1 for Linux/macOS hosts.
#
#   ops/install-hook.sh
#   ops/install-hook.sh --client codex
#   ops/install-hook.sh /path/to/settings.json
#
# Backs up settings.json first; re-running is a no-op once installed. Uses
# python3 (no jq dependency) for the JSON edit.
set -euo pipefail

CLIENT=claude
if [ "${1:-}" = "--client" ]; then
  CLIENT="${2:-}"
  shift 2
fi
case "$CLIENT" in claude|codex) ;; *)
  echo "invalid --client '$CLIENT' (claude|codex)" >&2; exit 2 ;;
esac
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
