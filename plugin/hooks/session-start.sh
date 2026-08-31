#!/usr/bin/env bash
# Pseudolife-MCP SessionStart hook — stdout becomes session context.
# Serves the memory-loop instructions + briefing from the running daemon;
# must never break a session start (always exits 0).
#
# Runs under Git Bash on Windows and bash/sh everywhere else. curl only —
# no pip package, no node, no python on the host.

URL="${PSEUDOLIFE_MCP_DAEMON_URL:-http://127.0.0.1:8765}"

AUTH=()
if [ -n "${PSEUDOLIFE_MCP_TOKEN:-}" ]; then
    AUTH=(-H "Authorization: Bearer ${PSEUDOLIFE_MCP_TOKEN}")
fi

# Claude Code delivers hook input as JSON on stdin (session_id is a
# documented common field). curl+sed only — no jq/python on the host.
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
SRC=$(printf '%s' "$INPUT" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
QS=""
[ -n "$SID" ] && QS="?session_id=${SID}&source=${SRC}"
# One retry bridges the daemon's short maintenance stalls (CMS autosave
# ~1.5s, dream-sweep tick; measured 2026-09-01 against a 1,123-entry bank)
# that can hold the service lock past a single attempt's timeout — a
# healthy daemon must not read as down. Plain --retry already treats a
# timeout as transient (--retry-all-errors would break curl < 7.71 at
# option parsing, killing the hook outright on older LTS hosts). Worst
# case 5+1+5=11s, inside the hook's 15s budget in hooks.json (guard-tested
# in tests/test_plugin_packaging.py). Registration is idempotent per
# session_id, so a retry after a half-completed first attempt is safe.
curl -sf --max-time 5 --retry 1 --retry-delay 1 \
    "${AUTH[@]}" "${URL}/api/hook/session-start${QS}" || \
    echo "Pseudolife-MCP: the memory daemon at ${URL} did not answer the session-start hook — it may be down, or briefly busy with a maintenance pass. The mcp__pseudolife-memory__* tools may still work: make one call (e.g. memory_stats) before treating memory as offline. If the daemon really is down, tell the user to start the stack (docker compose -f <clone>/ops/docker-compose.yml up -d) or install it first: https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart"

exit 0
