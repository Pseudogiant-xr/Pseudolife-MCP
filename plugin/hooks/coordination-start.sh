#!/usr/bin/env bash
# Session-local digest handoff, then the board check-in when the daemon
# serves one for this credential (one bounded request).
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
SRC=$(printf '%s' "$INPUT" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
[ -n "$SRC" ] || SRC=$(printf '%s' "$INPUT" | sed -n 's/.*"session_start_reason"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
# Coordination digest key (see coordination-prompt.sh for the file layout).
# Claude Code keeps an MCP server's CLAUDE_CODE_SESSION_ID for the life of
# the process, while /clear and /resume give hooks a new session id
# (env-vars docs, 2026-09-23). The shim writes its digest under its launch
# id, so the hooks keep one record per Claude Code process, named by the
# CLAUDE_PID Claude Code exports to hooks (not to MCP servers): line 1 the
# shim's key, line 2 the key of the session the record is confirmed for.
# The prompt hook follows line 1 only while line 2 names its own session.
# A launch writes the session's own key. /clear and /resume carry the record
# forward only through the handoff session-end.sh has just left
# (claude-$CLAUDE_PID.switch: time, the creation identity of the process
# that wrote it, the key of the session that ended); compaction only when
# the record is already confirmed for this session. Anything unproven is
# replaced by the session's own key, so a record a dead process left behind
# is never followed, even when its PID comes back. The env id equals the
# stdin id only in a hook Claude Code started for this session: a host run
# from a Claude Bash command inherits both variables and must leave the
# record alone. --continue can still launch the shim with a startup id no
# hook ever sees; the record cannot name that one.
valid_key() {  # $1 = a line; prints it when it is a 64-hex key
    case "$1" in ''|*[!0-9a-f]*) return 0 ;; esac
    [ "${#1}" -eq 64 ] && printf '%s' "$1"
}
sha256_of() {  # $1 = text
    printf '%s' "$1" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64
}
write_lines() {  # $1 = path, then its lines; atomic replace, best effort
    local path="$1"
    shift
    printf '%s\n' "$@" 2>/dev/null > "$path.$$" && mv -f "$path.$$" "$path" 2>/dev/null ||
        rm -f "$path.$$" 2>/dev/null
}
# The creation identity of process $1, which a later process reusing the PID
# does not share; empty where the host offers none, and callers then fail
# closed. Only ever compared across one handoff, seconds apart.
process_identity() {
    local stat boot="" fields line=""
    case "${OSTYPE:-}" in
        msys*|cygwin*)
            # Git Bash's own /proc lists MSYS processes, not Windows PIDs.
            line=$(ps -W 2>/dev/null |
                awk -v p="$1" '$4 == p || ($1 ~ /^[A-Za-z]$/ && $5 == p) { print; exit }')
            ;;
        linux*)
            if IFS= read -r stat 2>/dev/null < "/proc/$1/stat"; then
                IFS= read -r boot 2>/dev/null < /proc/sys/kernel/random/boot_id
                read -r -a fields <<< "${stat##*) }"
                [ -n "${fields[19]:-}" ] && line="$boot ${fields[19]}"  # field 22, start time
            fi
            ;;
        *)
            line=$(ps -o lstart= -p "$1" 2>/dev/null)
            ;;
    esac
    [ -n "$line" ] && printf '%s' "$line"
}
DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
if [ -n "$SID" ] && [ -d "$DIGEST_DIR" ]; then
    KEY=$(sha256_of "$SID")
    case "${CLAUDE_PID:-}" in
        ''|*[!0-9]*) ;;
        *)
            if [ -n "$KEY" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
                RECORD="$DIGEST_DIR/claude-$CLAUDE_PID.host"
                SWITCH="$DIGEST_DIR/claude-$CLAUDE_PID.switch"
                LINE1="" LINE2="" SHIM="" FOR="" CARRY=""
                if [ -f "$RECORD" ] && [ ! -L "$RECORD" ]; then
                    { IFS= read -r LINE1; IFS= read -r LINE2; } 2>/dev/null < "$RECORD"
                    SHIM=$(valid_key "$LINE1")
                    FOR=$(valid_key "$LINE2")
                fi
                case "$SRC" in
                    compact)
                        [ -n "$SHIM" ] && [ "$FOR" = "$KEY" ] && CARRY=1
                        ;;
                    clear|resume)
                        STAMP="" WHO="" FROM=""
                        if [ -n "$SHIM" ] && [ -n "$FOR" ] && [ -f "$SWITCH" ] && [ ! -L "$SWITCH" ]; then
                            { IFS= read -r STAMP; IFS= read -r WHO; IFS= read -r FROM; } 2>/dev/null < "$SWITCH"
                        fi
                        NOW=$(date +%s 2>/dev/null)
                        # Validated before any arithmetic: bash aborts the
                        # whole script on a malformed number such as 08.
                        case "$STAMP" in ''|0*|*[!0-9]*) STAMP="" ;; esac
                        case "$NOW" in ''|0*|*[!0-9]*) STAMP="" ;; esac
                        if [ -n "$STAMP" ] && [ "$FROM" = "$FOR" ] &&
                                [ "${#STAMP}" -le 12 ] && [ "${#NOW}" -le 12 ] &&
                                [ $((NOW - STAMP)) -ge 0 ] && [ $((NOW - STAMP)) -le 60 ]; then
                            IDENTITY=$(process_identity "$CLAUDE_PID")
                            [ -n "$IDENTITY" ] && [ "$(sha256_of "$IDENTITY")" = "$WHO" ] && CARRY=1
                        fi
                        ;;
                esac
                if [ -n "$CARRY" ]; then
                    # Refreshed as well, so the sweep keeps a live record.
                    write_lines "$RECORD" "$SHIM" "$KEY"
                    KEY="$SHIM"
                else
                    write_lines "$RECORD" "$KEY" "$KEY"
                fi
                rm -f "$SWITCH" 2>/dev/null
                if [ "$SRC" = startup ]; then
                    # Records of long-gone processes, and temp files a killed
                    # hook left behind. A live one is rewritten on every
                    # launch, /clear, compaction and resume.
                    find "$DIGEST_DIR" -maxdepth 1 -type f \( -name 'claude-*.host*' -o -name 'claude-*.switch*' \) \
                        -mtime +30 -delete 2>/dev/null
                fi
            fi
            ;;
    esac
    # A resumed, compacted or cleared conversation lost the coordination
    # digest it saw; clearing the marker makes the next prompt hook print
    # the current one afresh.
    case "$SRC" in
        resume|compact|clear)
            [ -n "$KEY" ] && rm -f "$DIGEST_DIR/$KEY.seen" 2>/dev/null
            ;;
    esac
fi
# The board check-in, only when this credential can use the board now. The
# daemon serves the text, or an empty body when coordination is off, the
# bearer is missing or unknown, or its principal is not allowed; a client
# that set PSEUDOLIFE_AGENT_COORDINATION to anything but a yes asks for
# nothing. One request, no retry: at most about 2 s on top of the record
# work above, inside the 5 s budget in hooks.json. A daemon that does not
# answer adds nothing; the shim's initialization instructions still carry a
# compact check-in when its adapter is up. Connection and credential
# checks: the same as session-start.sh.
if [ -n "${PSEUDOLIFE_AGENT_COORDINATION:-}" ]; then
    case "$(printf '%s' "$PSEUDOLIFE_AGENT_COORDINATION" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) ;;
        *) exit 0 ;;
    esac
fi
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
[ -z "$CONNECTION_ERROR" ] || exit 0
curl -L --max-redirs 0 -sf --connect-timeout 1 --max-time 2 \
    "${AUTH[@]}" "${URL}/api/hook/coordination-start" 2>/dev/null
exit 0
