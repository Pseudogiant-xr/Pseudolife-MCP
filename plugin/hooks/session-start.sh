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

curl -sf --max-time 5 "${AUTH[@]}" "${URL}/api/hook/session-start" || \
    echo "Pseudolife-MCP: the memory daemon is not reachable at ${URL} — the mcp__pseudolife-memory__* tools are unavailable this session. Tell the user to start the stack (docker compose -f <clone>/ops/docker-compose.yml up -d) or install it first: https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart"

exit 0
