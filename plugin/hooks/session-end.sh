#!/usr/bin/env bash
# Pseudolife-MCP SessionEnd hook — closes this session's episode promptly
# (the idle reaper remains the backstop). Must never block session end.
URL="${PSEUDOLIFE_MCP_DAEMON_URL:-http://127.0.0.1:8765}"
AUTH=()
if [ -n "${PSEUDOLIFE_MCP_TOKEN:-}" ]; then
    AUTH=(-H "Authorization: Bearer ${PSEUDOLIFE_MCP_TOKEN}")
fi
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
if [ -n "$SID" ]; then
    # One retry bridges short daemon maintenance stalls (autosave/sweep
    # lock holds; measured 2026-09-01). Plain --retry treats a timeout as
    # transient; --retry-all-errors would break curl < 7.71 outright.
    # Worst case 3+1+3=7s, inside the hook's 10s budget (guard-tested in
    # tests/test_plugin_packaging.py). The idle reaper backstops a miss.
    curl -sf --max-time 3 --retry 1 --retry-delay 1 \
        "${AUTH[@]}" -X POST \
        -H "content-type: application/json" \
        -d "{\"session_id\":\"${SID}\"}" \
        "${URL}/api/hook/session-end" >/dev/null 2>&1 || true
fi
exit 0
