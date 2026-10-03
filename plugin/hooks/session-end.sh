#!/usr/bin/env bash
# Pseudolife-MCP SessionEnd hook — closes this session's episode promptly
# (the idle reaper remains the backstop). Must never block session end.
private_regular() {
    local path="$1" maximum="$2" current parent meta
    case "$OSTYPE" in msys*|mingw*|cygwin*)
        path=$(cygpath -u "$path") || return 1 ;;
    esac
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
    # Git Bash modes do not describe an NTFS DACL. Keep the regular-file,
    # single-link and size checks, then apply lifecycle.ps1's native ACL rules.
    case "$OSTYPE" in msys*|mingw*|cygwin*)
        [ "${3:-0}" = 1 ] || return 1
        [ "$(wc -c < "$path" 2>/dev/null || echo $((maximum + 1)))" -le "$maximum" ] || return 1
        # Three Windows runs (2026-09-28), including Git Bash launch:
        # 0.344-0.375 s each; native ACL parity justifies this cost.
        PSEUDOLIFE_PRIVATE_FILE="$(cygpath -w "$path")" powershell.exe -NoProfile -NonInteractive -Command '
            $ErrorActionPreference = "Stop"
            try {
                # Git Bash can hide this module by converting PSModulePath.
                Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1"
                $item = Get-Item -LiteralPath $env:PSEUDOLIFE_PRIVATE_FILE -Force
                if ($item.PSIsContainer -or $item.Length -lt 1 -or
                    ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { exit 1 }
                $parent = $item.Directory
                while ($parent) {
                    if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 1 }
                    $parent = $parent.Parent
                }
                $acl = Get-Acl -LiteralPath $item.FullName
                $owner = ([Security.Principal.NTAccount]$acl.Owner).Translate(
                    [Security.Principal.SecurityIdentifier]).Value
                $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
                $allowed = @($acl.Access | Where-Object AccessControlType -eq Allow)
                if (-not $acl.AreAccessRulesProtected -or $owner -ne $current -or -not $allowed) { exit 1 }
                foreach ($rule in $allowed) {
                    $sid = $rule.IdentityReference.Translate(
                        [Security.Principal.SecurityIdentifier]).Value
                    if ($sid -notin $owner, "S-1-3-4") { exit 1 }
                }
                exit 0
            } catch { exit 1 }
        ' </dev/null >/dev/null 2>&1
        return $?
        ;;
    esac
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
# 1.5s, whatever hooks.json says (the SessionEnd budget is shared, and only
# a settings.json timeout raises it; on 2.1.280 a plugin hook asking for
# 10 s was cancelled at 1.5 s, 2026-09-27), and the connection checks and
# curl below can use all of it. /clear or /resume inside a running process
# keeps its shim, so the session ending here hands the process's record (see
# coordination-start.sh) to the next one: time, this process's creation identity
# and the ending session's key. The identity is read from the record's
# third line, which SessionStart measured with seconds to spare, while its
# fourth line says it still holds (identity_fresh); it is measured here only
# otherwise (past that line's window, a record written before 2026-09-28,
# or one not confirmed for the ending session), because the Windows probe,
# `ps -W`, took seconds a call on a loaded host. A record not confirmed
# for the ending session (none yet under older hooks, or another process's)
# is first replaced by that session's own key, which is right unless an
# earlier /clear under older hooks already moved it. Without a creation
# identity no handoff is written, and the next session starts from its own
# key. Same helpers as coordination-start.sh.
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
# True while the record's line 4 ("<until> <UTC offset>", written by
# coordination-start.sh's identity_expiry) says line 3 still reads as it
# would if measured now: before <until> (0: no end) and under the same
# offset ("-": none applies). Anything malformed is false.
identity_fresh() {  # $1 = the record's line 4
    local until offset now
    case "$1" in *' '*' '*|'') return 1 ;; *' '*) ;; *) return 1 ;; esac
    until=${1%% *}
    offset=${1#* }
    case "$until" in *[!0123456789]*) return 1 ;; esac
    [ "${#until}" -le 12 ] || return 1
    case "$offset" in -|[+-][0123456789][0123456789][0123456789][0123456789]) ;; *) return 1 ;; esac
    [ "$until" = 0 ] && [ "$offset" = - ] && return 0
    now=$(date '+%s %z' 2>/dev/null) || return 1
    [ "$offset" = - ] || [ "${now#* }" = "$offset" ] || return 1
    [ "$until" = 0 ] || [ "${now%% *}" -lt "$until" ]
}
case "$REASON" in
    clear|resume)
        DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
        # Character sets are spelled out, never ranges: macOS's bash 3.2 matches
        # a range by locale collation, where a-f takes upper case and 0-9 takes
        # digits such as the superscript two (tests/test_hook_glob_ranges.py).
        case "${CLAUDE_PID:-}" in
            ''|*[!0123456789]*) ;;
            *)
                if [ -n "$SID" ] && [ -d "$DIGEST_DIR" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
                    KEY=$(sha256_of "$SID")
                    RECORD="$DIGEST_DIR/claude-$CLAUDE_PID.host"
                    SWITCH="$DIGEST_DIR/claude-$CLAUDE_PID.switch"
                    LINE1="" LINE2="" LINE3="" LINE4=""
                    if [ -f "$RECORD" ] && [ ! -L "$RECORD" ]; then
                        { IFS= read -r LINE1; IFS= read -r LINE2; IFS= read -r LINE3; IFS= read -r LINE4; } 2>/dev/null < "$RECORD"
                    fi
                    case "$LINE1" in ''|*[!0123456789abcdef]*) LINE1="" ;; esac
                    case "$LINE3" in ''|*[!0123456789abcdef]*) LINE3="" ;; esac
                    [ "${#LINE3}" -eq 64 ] || LINE3=""
                    if [ -n "$KEY" ]; then
                        WHO=""
                        if [ "${#LINE1}" -eq 64 ] && [ "$LINE2" = "$KEY" ]; then
                            [ -n "$LINE3" ] && identity_fresh "$LINE4" && WHO="$LINE3"
                        else
                            write_lines "$RECORD" "$KEY" "$KEY"
                        fi
                        if [ -z "$WHO" ]; then
                            # No identity on record that still holds: measure.
                            IDENTITY=$(process_identity "$CLAUDE_PID")
                            [ -n "$IDENTITY" ] && WHO=$(sha256_of "$IDENTITY")
                        fi
                        [ -n "$WHO" ] &&
                            write_lines "$SWITCH" "$(date +%s 2>/dev/null)" "$WHO" "$KEY"
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
    # Codex caps SessionEnd at 3s (openai/codex b741e480,
    # normalize_command_hook, confirmed 2026-10-03). Both hosts share this
    # definition: one 1s request, including connection setup, leaves 2s for
    # local work (native ACL checks measured 0.344-0.375s each, 2026-09-28).
    # No retry or delay; the idle reaper backstops a busy or offline daemon.
    CURL_BUDGET=(--max-time 1 --connect-timeout 1)
    curl -L --max-redirs 0 -sf "${CURL_BUDGET[@]}" \
        "${AUTH[@]}" -X POST \
        -H "content-type: application/json" \
        -d "{\"session_id\":\"${SID}\"}" \
        "${URL}/api/hook/session-end" >/dev/null 2>&1 || true
fi
exit 0
