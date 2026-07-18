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
curl -sf --max-time 5 "${AUTH[@]}" "${URL}/api/hook/session-start${QS}" || \
    echo "Pseudolife-MCP: the memory daemon is not reachable at ${URL} — the mcp__pseudolife-memory__* tools are unavailable this session. Tell the user to start the stack (docker compose -f <clone>/ops/docker-compose.yml up -d) or install it first: https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart"

exit 0
