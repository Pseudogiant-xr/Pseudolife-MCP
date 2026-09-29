#!/usr/bin/env bash
# Pseudolife-MCP SubagentStart / SubagentStop hook (schema v50): keep a
# Claude Code session's board row listing the subagents running under it.
# A Claude Code subagent shares its parent's shim and so its board address;
# the parent's children list is how peers see it (maintainer decision
# 2026-09-30: children are liveness information on the parent, not peers).
#
# $1 is "start" or "stop". The payload's top-level session_id is the
# parent's, agent_id and agent_type the subagent's (measured 2026-09-30,
# Claude Code 2.1.283, plugin hooks). SubagentStart carries no task
# description, so the daemon labels the entry <agent_type>#<first 8 of the
# id>. The session's board address is the one the shim names in <key>.agent
# beside its digest, found the way stop-wake.sh finds it (the key is the
# sha256 of the session id, or the shim's key a SessionStart record confirms
# for this very session). One bounded request per event,
# POST /api/hook/subagent?agent=<id>&event=<start|stop>&child=<agent_id>
# &type=<agent_type>, whose answer is ignored.
#
# Fails open: every path exits 0 and prints nothing, so a down daemon, a
# refused bearer or a session without a board address never holds a
# subagent back. Codex loads the same hooks.json; its native subagents have
# their own board addresses (linked to their parent by the shim), so in
# Codex context this is a no-op, and so is any run Claude Code did not start
# for this very session (a Codex run inside a Claude Bash tool inherits
# CLAUDECODE).
EVENT="${1:-}"
case "$EVENT" in start|stop) ;; *) exit 0 ;; esac
setting() {
    [ -n "$1" ] || return 0
    printf %s "$1" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'
}
case "$(setting "${PSEUDOLIFE_AGENT_COORDINATION:-}")" in ''|1|true|yes|on) ;; *) OFF=1 ;; esac
if [ "${PSEUDOLIFE_CODEX_HOOK:-}" = 1 ] ||
        { [ -n "${PLUGIN_ROOT:-}" ] && [ "${PLUGIN_ROOT}" = "${CLAUDE_PLUGIN_ROOT:-}" ]; }; then
    OFF=1
fi
if [ -n "${OFF:-}" ] || [ "${CLAUDECODE:-}" != "1" ]; then
    # Drain the payload with a builtin: a cheap exit.
    while IFS= read -r _; do :; done
    exit 0
fi
exec 2>/dev/null

INPUT=$(cat)
# Only top-level keys count: preceded by { or , (plus whitespace), never by
# a backslash, which is how the same characters look inside an escaped
# string such as SubagentStop's last_assistant_message.
field() {  # $1 = key
    printf '%s' "$INPUT" | grep -o '[{,][[:space:]]*"'"$1"'"[[:space:]]*:[[:space:]]*"[^"\\]*"' |
        head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)"$/\1/'
}
SID=$(field session_id)
CHILD=$(field agent_id)
KIND=$(field agent_type)
# Character sets are spelled out, never ranges: macOS's bash 3.2 matches
# a range by locale collation (tests/test_hook_glob_ranges.py).
case "$SID" in ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]*) exit 0 ;; esac
[ "${#SID}" -le 128 ] || exit 0
case "$CHILD" in ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-]*) exit 0 ;; esac
[ "${#CHILD}" -le 64 ] || exit 0
# A type the daemon would refuse is left out: the entry is still listed.
case "$KIND" in *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-]*) KIND="" ;; esac
[ "${#KIND}" -le 64 ] || KIND=""
# Claude Code sets CLAUDE_CODE_SESSION_ID in a hook's environment to the
# payload's session_id; anything else is not this session's hook.
[ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ] || exit 0

DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
[ -d "$DIGEST_DIR" ] || exit 0
KEY=$(printf '%s' "$SID" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64)
case "${CLAUDE_PID:-}" in
    ''|*[!0123456789]*) ;;
    *)  HOST="$DIGEST_DIR/claude-$CLAUDE_PID.host"
        if [ -f "$HOST" ] && [ ! -L "$HOST" ]; then
            RECORDED="" CONFIRMED=""
            { IFS= read -r RECORDED; IFS= read -r CONFIRMED; } < "$HOST"
            case "$RECORDED$CONFIRMED" in
                *[!0123456789abcdef]*) ;;
                *) [ "${#RECORDED}" -eq 64 ] && [ "$CONFIRMED" = "$KEY" ] && KEY=$RECORDED ;;
            esac
        fi ;;
esac
[ "${#KEY}" -eq 64 ] || exit 0
AGENT_FILE="$DIGEST_DIR/$KEY.agent"
[ -f "$AGENT_FILE" ] && [ ! -L "$AGENT_FILE" ] || exit 0
AGENT=""
IFS= read -r AGENT < "$AGENT_FILE"
AGENT=${AGENT%$'\r'}
case "$AGENT" in ''|*[!0123456789abcdef]*) exit 0 ;; esac
[ "${#AGENT}" -eq 32 ] || exit 0

# A bearer file the hook may read: a regular, owner-only, single-link file
# of bounded size under no symlinked directory. The same check as
# stop-wake.sh and coordination-start.sh.
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

# Claude Code's daemon, read the way the other hooks read it: a bare http(s)
# origin, and PSEUDOLIFE_MCP_TOKEN or a private PSEUDOLIFE_MCP_TOKEN_FILE.
URL=${PSEUDOLIFE_MCP_DAEMON_URL:-http://127.0.0.1:8765}
URL=${URL%/}
case "$URL" in http://*|https://*) ;; *) exit 0 ;; esac
case "$URL" in *\?*|*\#*|*@*) exit 0 ;; esac
REST=${URL#*://}
case "$REST" in ''|*/*) exit 0 ;; esac
TOKEN="${PSEUDOLIFE_MCP_TOKEN:-}"
if [ -n "${PSEUDOLIFE_MCP_TOKEN_FILE:-}" ]; then
    private_regular "$PSEUDOLIFE_MCP_TOKEN_FILE" 4096 || exit 0
    TOKEN=$(cat "$PSEUDOLIFE_MCP_TOKEN_FILE")
    TOKEN=${TOKEN//[$'\r\n']/}
    case "$TOKEN" in ''|*[[:space:]]*) exit 0 ;; esac
fi
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
# -L with no redirects allowed makes a 3xx a failure, so the bearer never
# follows one. Two seconds at most, inside the hook's five.
curl -L -sf -X POST --max-redirs 0 --connect-timeout 1 --max-time 2 "${AUTH[@]}" \
    "$URL/api/hook/subagent?agent=$AGENT&event=$EVENT&child=$CHILD&type=$KIND" \
    >/dev/null 2>&1 </dev/null
exit 0
