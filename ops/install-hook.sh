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

SETTINGS_PATH="$SETTINGS_PATH" BRIEFING_COMMAND="$COMMAND" "$PYBIN" - <<'PY'
import json, os

path = os.environ["SETTINGS_PATH"]
briefing_cmd = os.environ["BRIEFING_COMMAND"]

obj = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)

hooks = obj.setdefault("hooks", {})
hooks.setdefault("SessionStart", [])
hooks.setdefault("SessionEnd", [])


def has_command(groups, needle):
    return any(needle in (h.get("command") or "")
               for g in groups for h in (g.get("hooks") or []))


def add_group(groups, command):
    groups.append({"hooks": [{"type": "command", "command": command}]})


if has_command(hooks["SessionStart"], "pseudolife-mcp briefing"):
    print(f"Briefing hook already present in {path} - skipping.")
else:
    add_group(hooks["SessionStart"], briefing_cmd)
    print(f"Installed SessionStart briefing hook -> {path}")
    print(f"  command: {briefing_cmd}")

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
