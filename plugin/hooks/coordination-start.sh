#!/usr/bin/env bash
# Board setup and session-local digest handoff; no daemon round-trip.
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
echo "Pseudolife coordination: at the first task and on resume, use memory_agents(action=list) to check peers and memory_agents(action=update, project=<project>, task=<task>, status=<status>) to show your current scope. Then use memory_message(action=receive); read each full message and memory_message(action=ack, message_id=<id>) after reading. On a pending-message hint, receive again. Changed-message alerts are brief; receive is the source of full messages. If coordination tools are unavailable, say so and continue independently."
exit 0
