#!/usr/bin/env bash
# Pseudolife-MCP SessionStart hook — stdout becomes session context.
# Serves the memory-loop instructions + briefing from the running daemon;
# must never break a session start (always exits 0).
#
# Runs under Git Bash on Windows and bash/sh everywhere else. curl only —
# no pip package, no node, no python on the host.

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

# Claude Code delivers hook input as JSON on stdin (session_id is a
# documented common field). curl+sed only — no jq/python on the host.
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
SRC=$(printf '%s' "$INPUT" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
[ -n "$SRC" ] || SRC=$(printf '%s' "$INPUT" | sed -n 's/.*"session_start_reason"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
# Coordination digest key (see user-prompt-submit.sh for the file layout).
# Claude Code keeps an MCP server's CLAUDE_CODE_SESSION_ID for the life of
# the process, while /clear and /resume give hooks a new session id
# (env-vars docs, 2026-09-23). The shim writes its digest under its spawn
# id, so a launch records that key per Claude Code process, named by the
# CLAUDE_PID Claude Code exports to hooks (not to MCP servers), and later
# sessions of the same process keep it. The env id equals the stdin id only
# in a hook Claude Code started for this session: a host run from a Claude
# Bash command inherits both variables and must leave the record alone.
# --continue can still spawn the shim with a startup id no hook ever sees;
# the record cannot name that one.
read_record() {  # $1 = path; prints the key it holds when well formed
    local key=""
    [ -f "$1" ] && [ ! -L "$1" ] || return 0
    IFS= read -r key 2>/dev/null < "$1"
    case "$key" in *[!0-9a-f]*) return 0 ;; esac
    [ "${#key}" -eq 64 ] && printf '%s' "$key"
}
write_record() {  # $1 = path, $2 = line; atomic replace, best effort
    printf '%s\n' "$2" 2>/dev/null > "$1.$$" && mv -f "$1.$$" "$1" 2>/dev/null || rm -f "$1.$$" 2>/dev/null
}
DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
if [ -n "$SID" ] && [ -d "$DIGEST_DIR" ]; then
    KEY=$(printf '%s' "$SID" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64)
    case "${CLAUDE_PID:-}" in
        ''|*[!0-9]*) ;;
        *)
            if [ -n "$KEY" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
                RECORD="$DIGEST_DIR/claude-$CLAUDE_PID.host"
                SWITCH="$DIGEST_DIR/claude-$CLAUDE_PID.switch"
                HOLD=""
                # A launch (startup, fork, or a resume nothing marked)
                # replaces the record. An in-session /resume keeps it:
                # session-end.sh marks one, and the marker is honoured only
                # for a minute and only on resume, so one left by a dead
                # process whose PID came back cannot keep its record.
                case "$SRC" in
                    clear|compact) HOLD=1 ;;
                    resume)
                        STAMP=""
                        [ -f "$SWITCH" ] && [ ! -L "$SWITCH" ] && IFS= read -r STAMP 2>/dev/null < "$SWITCH"
                        NOW=$(date +%s 2>/dev/null)
                        # Validated before any arithmetic: bash aborts the
                        # whole script on a malformed number such as 08.
                        case "$STAMP" in ''|0*|*[!0-9]*) STAMP="" ;; esac
                        case "$NOW" in ''|0*|*[!0-9]*) STAMP="" ;; esac
                        if [ -n "$STAMP" ] && [ "${#STAMP}" -le 12 ] && [ "${#NOW}" -le 12 ] &&
                                [ $((NOW - STAMP)) -ge 0 ] && [ $((NOW - STAMP)) -le 60 ]; then
                            HOLD=1
                        fi
                        ;;
                esac
                HELD=$(read_record "$RECORD")
                if [ -z "$HOLD" ]; then
                    write_record "$RECORD" "$KEY"
                elif [ -n "$HELD" ]; then
                    KEY="$HELD"
                    write_record "$RECORD" "$KEY"  # refreshed, so the sweep keeps a live one
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
# The plugin release this hook runs from, read beside the script so the
# daemon can open the briefing with a notice when the two differ (a cached
# plugin moves only on /plugin update; the daemon on every deploy). A copy
# without a manifest beside it (Codex's content-addressed hooks) sends
# nothing. Only a version-shaped value goes on the wire.
PLUGIN_VERSION=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9A-Za-z.+-]*\)".*/\1/p' \
    "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/.claude-plugin/plugin.json" 2>/dev/null | head -1)
# A digest of the four hook scripts beside this one, so the daemon can tell
# a cached plugin at its own version apart from its own hooks (the version
# only moves with a release). Same function as pseudolife_memory.plugin_hooks
# and lifecycle.ps1: SHA-256 over `name NUL bytes NUL`, CRLF read as LF.
hooks_digest() {  # $1 = directory
    local name
    for name in lifecycle.ps1 session-start.sh user-prompt-submit.sh session-end.sh; do
        [ -f "$1/$name" ] || return 1
    done
    for name in lifecycle.ps1 session-start.sh user-prompt-submit.sh session-end.sh; do
        printf '%s\0' "$name"; tr -d '\r' < "$1/$name"; printf '\0'
    done | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64
}
PLUGIN_HOOKS_DIGEST=$(hooks_digest "$(dirname "$0")" 2>/dev/null) || PLUGIN_HOOKS_DIGEST=""
case "$PLUGIN_HOOKS_DIGEST" in
    *[!0-9a-f]*) PLUGIN_HOOKS_DIGEST="" ;;
esac
[ "${#PLUGIN_HOOKS_DIGEST}" -eq 64 ] || PLUGIN_HOOKS_DIGEST=""
QS=""
[ -n "$SID" ] && QS="?session_id=${SID}&source=${SRC}"
if [ -n "$PLUGIN_VERSION" ]; then
    # `+` (a local version label) would decode to a space server-side.
    QS="${QS:-?}${QS:+&}plugin_version=${PLUGIN_VERSION//+/%2B}"
fi
if [ -n "$PLUGIN_HOOKS_DIGEST" ]; then
    QS="${QS:-?}${QS:+&}plugin_hooks_digest=${PLUGIN_HOOKS_DIGEST}"
fi
# One retry bridges the daemon's short maintenance stalls (CMS autosave
# ~1.5s, dream-sweep tick; measured 2026-09-01 against a 1,123-entry bank)
# that can hold the service lock past a single attempt's timeout — a
# healthy daemon must not read as down. Plain --retry already treats a
# timeout as transient (--retry-all-errors would break curl < 7.71 at
# option parsing, killing the hook outright on older LTS hosts). Worst
# case 5+1+5=11s, inside the hook's 15s budget in hooks.json (guard-tested
# in tests/test_plugin_packaging.py). Registration is idempotent per
# session_id, so a retry after a half-completed first attempt is safe.
if [ -n "$CONNECTION_ERROR" ]; then
    echo "Pseudolife-MCP: session briefing unavailable because the managed connection or credential file is invalid."
    exit 0
fi
curl -L --max-redirs 0 -sf --max-time 5 --retry 1 --retry-delay 1 \
    "${AUTH[@]}" "${URL}/api/hook/session-start${QS}" || \
    echo "Pseudolife-MCP: the memory daemon did not answer the session-start hook. Try memory_stats before treating memory as offline; then check the configured daemon and credential files."

exit 0
