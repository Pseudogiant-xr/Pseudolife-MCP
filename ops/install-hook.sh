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
# Backs up settings.json first; re-running is a no-op once installed. The
# JSON edit is the native client's (no jq or Python dependency).
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

# Per-turn memory-change note (UserPromptSubmit), both clients; Codex requires
# review and trust before newly installed hooks run. `pseudolife-mcp
# prompt-hook` prints only when new lessons or other sessions' status notes
# landed since the session's last note, as the plugin's hook does. It runs
# where the briefing runs: the Docker tier's `docker exec` gets -i, since the
# hook's session id arrives on stdin.
case "$COMMAND" in
  *"pseudolife-mcp briefing --hook-json")
    PROMPT_COMMAND="${COMMAND%pseudolife-mcp briefing --hook-json}pseudolife-mcp prompt-hook"
    case "$PROMPT_COMMAND" in
      "docker exec -i "*) ;;
      "docker exec "*) PROMPT_COMMAND="docker exec -i ${PROMPT_COMMAND#docker exec }" ;;
    esac ;;
  *) PROMPT_COMMAND="pseudolife-mcp prompt-hook" ;;
esac
# Until 2026-09-26 that hook was a static echo of this line on every turn.
# Kept to replace it: install mode swaps it for the note, --remove-legacy
# removes it, and ops/setup-codex-hooks.py reads the PowerShell copy.
DISCIPLINE_LINE="Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."
UPS_COMMAND="echo '$DISCIPLINE_LINE'"
# The 2026-08-28..09-05 line ended "-> memory_outcome." Derived, so this file
# keeps one copy of the line (test_plugin_packaging.py).
LEGACY_UPS_COMMAND="echo '${DISCIPLINE_LINE% with used_ids.}.'"
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

# The JSON edits are the native client's (`installer hook-settings`): the
# binary named by PSEUDOLIFE_NATIVE_CLI (the installer passes its verified
# copy), else PSEUDOLIFE_SHIM_LAUNCHER, else the stable launcher, the order
# ops/update.sh uses. Nothing here looks for Python.
NATIVE_CLI="${PSEUDOLIFE_NATIVE_CLI:-${PSEUDOLIFE_SHIM_LAUNCHER:-}}"
if [ -z "$NATIVE_CLI" ]; then
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      NATIVE_CLI="$(cygpath -u "${LOCALAPPDATA:-$HOME/AppData/Local}" 2>/dev/null || printf '%s' "${LOCALAPPDATA:-$HOME/AppData/Local}")/pseudolife-mcp/bin/pseudolife-mcp.exe" ;;
    *) NATIVE_CLI="${XDG_DATA_HOME:-$HOME/.local/share}/pseudolife-mcp/bin/pseudolife-mcp" ;;
  esac
fi
if [ ! -f "$NATIVE_CLI" ]; then
  echo "The native pseudolife-mcp client edits $SETTINGS_PATH, and it is not installed at $NATIVE_CLI: run ops/install.sh, which installs it, or set PSEUDOLIFE_NATIVE_CLI to its binary. Nothing changed." >&2
  exit 1
fi

if [ -n "$REMOVE_LEGACY" ]; then
  rc=0
  SETTINGS_PATH="$SETTINGS_PATH" DRY_RUN="$DRY_RUN" UPS_COMMAND="$UPS_COMMAND" \
    LEGACY_UPS_COMMAND="$LEGACY_UPS_COMMAND" COORDINATION_LINE="$COORDINATION_LINE" \
    "$NATIVE_CLI" installer hook-settings remove-legacy || rc=$?
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
  PROMPT_COMMAND="$PROMPT_COMMAND" UPS_COMMAND="$UPS_COMMAND" \
  LEGACY_UPS_COMMAND="$LEGACY_UPS_COMMAND" DISCIPLINE_LINE="$DISCIPLINE_LINE" \
  COORDINATION_COMMAND="$COORDINATION_COMMAND" \
  LEGACY_COORDINATION_LINE="$COORDINATION_LINE" "$NATIVE_CLI" installer hook-settings install || exit 1

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
