#!/usr/bin/env bash
# Pseudolife-MCP SessionEnd hook — closes this session's episode promptly
# (the idle reaper remains the backstop). Must never block session end.
private_regular() {
    local path="$1" maximum="$2" current parent meta
    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    current=$(dirname "$path")
    while [ "$current" != "." ] && [ "$current" != "/" ]; do
        [ ! -L "$current" ] || return 1
        parent=$(dirname "$current")
        [ "$parent" != "$current" ] || break
        current="$parent"
    done
    meta=$(stat -c '%u %a %h' "$path" 2>/dev/null ||
           stat -f '%u %Lp %l' "$path" 2>/dev/null) || return 1
    set -- $meta
    case "${2:-}" in *00) ;; *) return 1 ;; esac
    [ "${1:-x}" = "$(id -u)" ] && [ "${3:-0}" = 1 ] || return 1
    [ "$(wc -c < "$path" 2>/dev/null || echo $((maximum + 1)))" -le "$maximum" ]
}
decode_connection_value() {
    if value=$(printf '%s' "$1" | base64 --decode 2>/dev/null); then
        printf '%s' "$value"
    else
        printf '%s' "$1" | base64 -D 2>/dev/null
    fi
}

INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
REASON=$(printf '%s' "$INPUT" | sed -n 's/.*"reason"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)

# Coordination digest key, first: Claude Code gives a plugin SessionEnd hook
# 1.5s, whatever hooks.json says, and the connection checks and curl below
# can use all of it. /clear or /resume inside a running process keeps its
# shim, so the session ending here hands the process's record (see
# session-start.sh) to the next one: time, this process's creation identity
# and the ending session's key. A record not confirmed for the ending
# session (none yet under older hooks, or another process's) is first
# replaced by that session's own key, which is right unless an earlier
# /clear under older hooks already moved it. Without a creation identity no
# handoff is written, and the next session starts from its own key.
# Same helpers as session-start.sh.
sha256_of() {  # $1 = text
    printf '%s' "$1" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64
}
write_lines() {  # $1 = path, then its lines; atomic replace, best effort
    local path="$1"
    shift
    printf '%s\n' "$@" 2>/dev/null > "$path.$$" && mv -f "$path.$$" "$path" 2>/dev/null ||
        rm -f "$path.$$" 2>/dev/null
}
process_identity() {
    local stat boot="" fields line=""
    case "${OSTYPE:-}" in
        msys*|cygwin*)
            line=$(ps -W 2>/dev/null |
                awk -v p="$1" '$4 == p || ($1 ~ /^[A-Za-z]$/ && $5 == p) { print; exit }')
            ;;
        linux*)
            if IFS= read -r stat 2>/dev/null < "/proc/$1/stat"; then
                IFS= read -r boot 2>/dev/null < /proc/sys/kernel/random/boot_id
                read -r -a fields <<< "${stat##*) }"
                [ -n "${fields[19]:-}" ] && line="$boot ${fields[19]}"
            fi
            ;;
        *)
            line=$(ps -o lstart= -p "$1" 2>/dev/null)
            ;;
    esac
    [ -n "$line" ] && printf '%s' "$line"
}
case "$REASON" in
    clear|resume)
        DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
        case "${CLAUDE_PID:-}" in
            ''|*[!0-9]*) ;;
            *)
                if [ -n "$SID" ] && [ -d "$DIGEST_DIR" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
                    KEY=$(sha256_of "$SID")
                    RECORD="$DIGEST_DIR/claude-$CLAUDE_PID.host"
                    SWITCH="$DIGEST_DIR/claude-$CLAUDE_PID.switch"
                    LINE1="" LINE2=""
                    if [ -f "$RECORD" ] && [ ! -L "$RECORD" ]; then
                        { IFS= read -r LINE1; IFS= read -r LINE2; } 2>/dev/null < "$RECORD"
                    fi
                    case "$LINE1" in ''|*[!0-9a-f]*) LINE1="" ;; esac
                    if [ -n "$KEY" ]; then
                        if [ "${#LINE1}" -ne 64 ] || [ "$LINE2" != "$KEY" ]; then
                            write_lines "$RECORD" "$KEY" "$KEY"
                        fi
                        IDENTITY=$(process_identity "$CLAUDE_PID")
                        [ -n "$IDENTITY" ] &&
                            write_lines "$SWITCH" "$(date +%s 2>/dev/null)" "$(sha256_of "$IDENTITY")" "$KEY"
                    fi
                fi
                ;;
        esac
        ;;
esac

CONNECTION_HOME="${CODEX_HOME:-${HOME}/.codex}"
CONNECTION="${CONNECTION_HOME}/pseudolife/connection.json"
MANAGED_URL=""
MANAGED_TOKEN_FILE=""
MANAGED_CONNECTION=""
CONNECTION_ERROR=""
CODEX_HOOK_CONTEXT=""
if [ "${PSEUDOLIFE_CODEX_HOOK:-}" = 1 ] ||
        { [ -n "${PLUGIN_ROOT:-}" ] &&
          [ "${PLUGIN_ROOT}" = "${CLAUDE_PLUGIN_ROOT:-}" ]; }; then
    CODEX_HOOK_CONTEXT=1
fi
if [ -n "$CODEX_HOOK_CONTEXT" ] && [ -e "$CONNECTION" ]; then
    if ! private_regular "$CONNECTION" 16384; then
        CONNECTION_ERROR=1
    else
        [ "$(wc -l < "$CONNECTION")" -eq 5 ] &&
            [ "$(sed -n '1p' "$CONNECTION")" = '{' ] &&
            [ "$(sed -n '2p' "$CONNECTION")" = '  "version": 1,' ] &&
            [ "$(sed -n '5p' "$CONNECTION")" = '}' ] || CONNECTION_ERROR=1
        URL_B64=$(sed -n '3s/^  "daemon_url": "\([A-Za-z0-9+\/=]*\)",$/\1/p' "$CONNECTION")
        TOKEN_FILE_B64=$(sed -n '4s/^  "token_file": "\([A-Za-z0-9+\/=]*\)"$/\1/p' "$CONNECTION")
        MANAGED_URL=$(decode_connection_value "$URL_B64")
        MANAGED_TOKEN_FILE=$(decode_connection_value "$TOKEN_FILE_B64")
        [ -n "$MANAGED_URL" ] || CONNECTION_ERROR=1
        [ -n "$CONNECTION_ERROR" ] || MANAGED_CONNECTION=1
    fi
fi

MANAGED_TOKENLESS=""
if [ -n "$MANAGED_CONNECTION" ] && [ -z "$MANAGED_TOKEN_FILE" ]; then
    MANAGED_TOKENLESS=1
    EXPLICIT_URL=""
    TOKEN_FILE=""
else
    EXPLICIT_URL="${PSEUDOLIFE_MCP_DAEMON_URL:-}"
    TOKEN_FILE="${PSEUDOLIFE_MCP_TOKEN_FILE:-$MANAGED_TOKEN_FILE}"
fi
if [ -n "$EXPLICIT_URL" ] && [ -n "$MANAGED_URL" ] &&
        [ "${EXPLICIT_URL%/}" != "${MANAGED_URL%/}" ]; then
    CONNECTION_ERROR=1
fi
if [ -z "$MANAGED_TOKENLESS" ] && [ -n "$MANAGED_URL" ] &&
        [ -n "${PSEUDOLIFE_MCP_TOKEN_FILE:-}" ] &&
        { [ -z "$EXPLICIT_URL" ] ||
          [ "${EXPLICIT_URL%/}" != "${MANAGED_URL%/}" ]; }; then
    CONNECTION_ERROR=1
fi
URL="${EXPLICIT_URL:-${MANAGED_URL:-http://127.0.0.1:8765}}"
URL="${URL%/}"
case "$URL" in
    http://*|https://*) ;;
    *) CONNECTION_ERROR=1 ;;
esac
AUTHORITY="${URL#*://}"; AUTHORITY="${AUTHORITY%%/*}"
case "$AUTHORITY" in ""|*@*) CONNECTION_ERROR=1 ;; esac
case "$URL" in *\?*|*\#*) CONNECTION_ERROR=1 ;; esac
URL_REST="${URL#*://}"
case "$URL_REST" in */*) CONNECTION_ERROR=1 ;; esac

TOKEN=""
if [ -z "$MANAGED_TOKENLESS" ]; then TOKEN="${PSEUDOLIFE_MCP_TOKEN:-}"; fi
if [ -n "$TOKEN_FILE" ]; then
    TOKEN=""
    if ! private_regular "$TOKEN_FILE" 4096; then
        CONNECTION_ERROR=1
    else
        TOKEN=$(cat "$TOKEN_FILE")
        if [ -z "$TOKEN" ] || printf '%s' "$TOKEN" | LC_ALL=C grep -q '[[:space:]]'; then
            CONNECTION_ERROR=1
        fi
    fi
fi

AUTH=()
if [ -z "$CONNECTION_ERROR" ] && [ -n "$TOKEN" ]; then
    AUTH=(-H "Authorization: Bearer $TOKEN")
fi

if [ -n "$SID" ] && [ -z "$CONNECTION_ERROR" ]; then
    # One retry bridges short daemon maintenance stalls (autosave/sweep
    # lock holds; measured 2026-09-01). Plain --retry treats a timeout as
    # transient; --retry-all-errors would break curl < 7.71 outright.
    # Worst case 3+1+3=7s, inside the hook's 10s budget (guard-tested in
    # tests/test_plugin_packaging.py). The idle reaper backstops a miss.
    CURL_BUDGET=(--max-time 3 --retry 1 --retry-delay 1)
    if [ -n "$CODEX_HOOK_CONTEXT" ]; then
        # Codex caps SessionEnd at three seconds (ops/setup-codex-hooks.py):
        # one two-second attempt and no retry, as lifecycle.ps1 does; the
        # idle reaper backstops a miss (guard-tested in tests/test_codex_hooks.py).
        CURL_BUDGET=(--max-time 2)
    fi
    curl -L --max-redirs 0 -sf "${CURL_BUDGET[@]}" \
        "${AUTH[@]}" -X POST \
        -H "content-type: application/json" \
        -d "{\"session_id\":\"${SID}\"}" \
        "${URL}/api/hook/session-end" >/dev/null 2>&1 || true
fi
exit 0
