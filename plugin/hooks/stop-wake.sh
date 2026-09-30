#!/usr/bin/env bash
# Pseudolife-MCP Stop hook: wake an idle Claude Code session when addressed
# board mail arrives. On by default since 2026-09-28 (before that, opt-in
# with PSEUDOLIFE_AGENT_WAKE_HOOK=1): hooks.json skips it when
# PSEUDOLIFE_AGENT_WAKE_HOOK is 0/false/no/off or PSEUDOLIFE_AGENT_COORDINATION
# is set to anything but a yes (both checked again here), refuses a copy that
# does not parse. Codex loads the same hooks.json: on Windows it runs the
# entry's native command (lifecycle.ps1 -Event Stop), on macOS and Linux this
# script, which in Codex context runs only the park gate below and never the
# wake (the doorbell is Codex's wake path). Anywhere else it is a no-op.
#
# hooks.json registers it with "async": true and "asyncRewake": true, so it
# waits in the background after each turn, and exit code 2 starts a new turn
# even when the session is idle (Desktop Code tab, Claude Code 2.1.280,
# 2026-09-23: under a second from exit to turn). Claude Code shows stderr to
# the model as a system reminder labelled "Stop hook blocking error", so
# stderr carries one line saying what this is, then the shim's digest, which
# already reads as agent-origin, not user authority.
#
# The daemon decides, this hook rings (schema v49, maintainer decision
# 2026-09-28). Every send gets a wake decision in the daemon; for a ring
# (rung: mail that clears what the parked recipient declared it needs, or
# nudged: an idle session that never parked) the shim writes <key>.ring
# beside the digest: line 1 the digest watermark the ring is for, line 2 the
# decision and its reason. It fires when that ring is past the <key>.seen
# marker, the digest's watermark (line 1) is past it too and the body is
# non-empty. Chatter, mail the daemon withheld, an acknowledgement changing
# the digest: none of these ring. Mail the session already saw (through the
# prompt hook, the tool-result hint or an earlier wake) does not fire again
# at the next turn end; SessionStart clears .seen on resume and compact, so a
# pending ring can wake the session once more after those. Firing prints
# first (a nudge adds one sentence asking for a park record), then advances
# .seen, so those paths stay quiet about it, then appends a "wait" line to
# the ledger with the ring's reason. A marker that cannot advance means no
# wake at all: it would otherwise fire again at every turn end.
#
# Before arming the wait, the park gate: when the turn that just ended is
# not itself a stop-hook continuation (stop_hook_active), and the shim has
# named this session's board address in <key>.agent, one bounded request
# asks the daemon (GET /api/hook/park-gate?agent=<id>&since=<turn start>,
# the start from the <key>.turn stamp the prompt hook leaves) whether the
# session parked. "block" ends the turn at once with the daemon's message
# as the wake text, once: the continuation's Stop carries
# stop_hook_active=true and is not asked. The daemon answers allow for a
# parked or done session; no answer (a daemon that is down, a bearer it
# refuses) is allow too, so the gate never holds a turn on an error. It
# appends a "gate" line to the ledger when it blocks.
#
# In Codex context (PSEUDOLIFE_CODEX_HOOK=1, or PLUGIN_ROOT equal to
# CLAUDE_PLUGIN_ROOT, as the sibling bash hooks read it) the same gate runs
# for the thread whose id the payload carries, with the daemon and bearer
# resolved as coordination-start.sh resolves them (the managed connection
# file under CODEX_HOME, or the explicit settings), and a block is answered
# the way Codex documents for Stop: {"decision":"block","reason":...} on
# stdout with exit 0, which Codex turns into a continuation prompt. Allow,
# no address, a continuation's Stop or no answer prints nothing, and the
# script exits without arming the wait: the same behaviour lifecycle.ps1
# gives Codex on Windows.
#
# With the argument subagent-stop (the plugin's SubagentStop entry) the
# script runs that park gate for a Codex child thread, and only in Codex
# context: a native child (collaboration.spawn_agent) or fork has its own
# board address, which the shim records under the child's MCP threadId,
# and Codex names the child in the payload's agent_id (equal to that
# threadId in the 2026-09-29 probe on Codex 0.158.0) while session_id
# stays the root thread's. The lookup is keyed by agent_id, which must be
# the canonical lower-case UUID the shim accepts; there is no fallback to
# session_id. A child gets no prompt hook, so no turn stamp: the daemon
# judges its standing record. Claude Code fires SubagentStop too, and there
# the entry does nothing.
#
# After a wake fires, the hook posts one woke marker (POST
# /api/hook/woke?agent=<id>, in the background, 2 s): the daemon logs that
# this session's turn is starting, so wake precision can be measured from
# the turn rather than from the ring being served. The answer is ignored.
#
# One watcher per session: every firing writes a fresh token to <key>.wake,
# and an older watcher that finds another token there exits quietly at its
# next poll, so the newest turn end always owns the wait.
#
# Budget: hooks.json sets "timeout": 3600, which Claude Code enforces on
# asyncRewake hooks, and the wait ends at MAX_WAIT, before the kill. An hour
# covers twice the p90 acknowledgement latency (1,740 s) that sessions
# without a watcher showed in the 2026-09-23 10-session messageboard trial;
# The arm expires after 59 minutes; longer waits need a background wait-mail
# arm (four hours by default), or their mail surfaces on the next prompt.
# PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT shortens the
# wait, never lengthens it.
MAX_WAIT=3540
# Matches the board's 60 s listener lease ceiling; renew every poll so a
# killed watcher is no longer advertised as armed after one minute.
LISTENER_LEASE=60
# The shim rewrites the digest on its 20 s heartbeat, so a 5 s poll adds
# little latency; each poll spawns one sleep.
POLL=5
# At most MAX_WAKES wakes in any WAKE_WINDOW seconds. Two opted-in sessions
# can keep waking each other, and every wake is an unattended model turn;
# mail over the cap waits for the window, delayed but never dropped. The
# busiest session of the same trial received 32 messages in one evening, and
# a wake happens only while the session is idle.
MAX_WAKES=20
WAKE_WINDOW=3600
# Under Git Bash the parent is a Windows PID that only `ps -W` lists; it is
# checked at arm time and then this often (seconds), not at every poll.
PARENT_CHECK=60

# Read the two settings the way the shim and doctor do: trimmed and
# lower-cased, blank meaning unset. Only a non-empty value costs a spawn.
setting() {
    [ -n "$1" ] || return 0
    printf %s "$1" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'
}
OFF=""
case "$(setting "${PSEUDOLIFE_AGENT_WAKE_HOOK:-}")" in 0|false|no|off) OFF=1 ;; esac
case "$(setting "${PSEUDOLIFE_AGENT_COORDINATION:-}")" in ''|1|true|yes|on) ;; *) OFF=1 ;; esac
CODEX_HOOK_CONTEXT=""
if [ "${PSEUDOLIFE_CODEX_HOOK:-}" = 1 ] ||
        { [ -n "${PLUGIN_ROOT:-}" ] &&
          [ "${PLUGIN_ROOT}" = "${CLAUDE_PLUGIN_ROOT:-}" ]; }; then
    CODEX_HOOK_CONTEXT=1
fi
MODE="${1:-}"
case "$MODE" in ''|subagent-stop) ;; *) OFF=1 ;; esac
[ "$MODE" = subagent-stop ] && [ -z "$CODEX_HOOK_CONTEXT" ] && OFF=1
if [ -n "$OFF" ] || { [ -z "$CODEX_HOOK_CONTEXT" ] && [ "${CLAUDECODE:-}" != "1" ]; }; then
    # Drain the payload with a builtin: a cheap exit.
    while IFS= read -r _; do :; done
    exit 0
fi
# stderr becomes the reminder text: keep everything else off it.
exec 3>&2 2>/dev/null

INPUT=$(cat)
# Only a top-level "session_id" counts (see coordination-prompt.sh).
SID=$(printf '%s' "$INPUT" | grep -o '[{,][[:space:]]*"session_id"[[:space:]]*:[[:space:]]*"[^"\\]*"' |
      head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)"$/\1/')
# Character sets are spelled out, never ranges: macOS's bash 3.2 matches
# a range by locale collation, where a-f takes upper case and 0-9 takes
# digits such as the superscript two (tests/test_hook_glob_ranges.py).
case "$SID" in ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]*) exit 0 ;; esac
[ "${#SID}" -le 128 ] || exit 0
# A Codex run nested inside a Claude Bash tool inherits CLAUDECODE; Claude
# Code sets CLAUDE_CODE_SESSION_ID in a hook's environment to the payload's
# session_id, updated on /clear. Codex names its thread in the payload only.
# A hook Claude Code started for this very session stays Claude's whatever
# Codex marker its environment carries: taken for Codex it would lose the
# wake, and its block would go to a stdout an async hook cannot use.
if [ "${CLAUDECODE:-}" = "1" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
    CODEX_HOOK_CONTEXT=""
fi
[ -n "$CODEX_HOOK_CONTEXT" ] || [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ] || exit 0
# The thread whose records the hook reads: the payload's session, or for a
# Codex child's stop the child named by the one top-level agent_id.
SUBJECT=$SID
if [ "$MODE" = subagent-stop ]; then
    [ -n "$CODEX_HOOK_CONTEXT" ] || exit 0
    SUBJECT=$(printf '%s' "$INPUT" | grep -o '[{,][[:space:]]*"agent_id"[[:space:]]*:[[:space:]]*"[^"\\]*"' |
              sed 's/.*:[[:space:]]*"\([^"]*\)"$/\1/')
    case "$SUBJECT" in *$'\n'*) exit 0 ;; esac
    # The canonical thread id: 36 characters, lower-case hex in 8-4-4-4-12.
    case "$SUBJECT" in ''|*[!0123456789abcdef-]*) exit 0 ;; esac
    [ "${#SUBJECT}" -eq 36 ] || exit 0
    [ "${SUBJECT:8:1}${SUBJECT:13:1}${SUBJECT:18:1}${SUBJECT:23:1}" = "----" ] || exit 0
    HEX=${SUBJECT//-/}
    [ "${#HEX}" -eq 32 ] || exit 0
fi

DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
KEY=$(printf '%s' "$SUBJECT" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64)
# After /clear, hooks get a new session id while the shim keeps writing the
# digest under its spawn-time one; the /clear digest-keying change has
# SessionStart record that key per Claude Code process (line 1) with the
# sha256 of the session id it is confirmed for (line 2), and without such a
# record the new id's digest never appears. The key is followed only when
# line 2 names this session, so a reused PID's record cannot wake it for a
# dead process's mail. A symlinked, CRLF or upper-case record is refused.
# Codex has no such record: its shim keys the digest by the thread id.
[ -n "$CODEX_HOOK_CONTEXT" ] && CLAUDE_PID=""
case "${CLAUDE_PID:-}" in
    ''|*[!0123456789]*) ;;
    *)  HOST="$DIGEST_DIR/claude-$CLAUDE_PID.host"
        if [ -f "$HOST" ] && [ ! -L "$HOST" ]; then
            RECORDED=""
            CONFIRMED=""
            { IFS= read -r RECORDED; IFS= read -r CONFIRMED; } < "$HOST"
            case "$RECORDED$CONFIRMED" in
                *[!0123456789abcdef]*) ;;
                *) [ "${#RECORDED}" -eq 64 ] && [ "$CONFIRMED" = "$KEY" ] && KEY=$RECORDED ;;
            esac
        fi ;;
esac
[ "${#KEY}" -eq 64 ] || exit 0
FILE="$DIGEST_DIR/$KEY.txt"
SEEN="$DIGEST_DIR/$KEY.seen"
LEASE="$DIGEST_DIR/$KEY.wake"
WAKES="$DIGEST_DIR/$KEY.wakes"
RING="$DIGEST_DIR/$KEY.ring"
AGENT="$DIGEST_DIR/$KEY.agent"
TURN="$DIGEST_DIR/$KEY.turn"
[ -L "$FILE" ] && exit 0

# A bearer file the hook may read: a regular, owner-only, single-link file
# of bounded size under no symlinked directory. The same check as
# coordination-start.sh and session-start.sh.
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

# Sets GATE_URL to $1 without its trailing slash when it is a bare http(s)
# origin (no path, query, fragment or userinfo).
gate_url() {
    local rest
    GATE_URL=${1%/}
    case "$GATE_URL" in http://*|https://*) ;; *) return 1 ;; esac
    case "$GATE_URL" in *\?*|*\#*|*@*) return 1 ;; esac
    rest=${GATE_URL#*://}
    case "$rest" in ''|*/*) return 1 ;; esac
}

# Sets GATE_AUTH from $1 (a bearer, or empty) or, when $2 names a file, its
# one line, which must be a private file holding a token with no spaces.
gate_auth() {
    local token="$1" file="$2"
    if [ -n "$file" ]; then
        if ! private_regular "$file" 4096; then
            printf '%s\ttoken\t%s\t0\t0\trejected\n' "$(date +%s)" "${KEY:0:8}" \
                2>/dev/null >> "$DIGEST_DIR/ledger.log"
            return 1
        fi
        token=$(cat "$file")
        token=${token//[$'\r\n']/}
        case "$token" in ''|*[[:space:]]*) return 1 ;; esac
    fi
    GATE_AUTH=()
    [ -n "$token" ] && GATE_AUTH=(-H "Authorization: Bearer $token")
    return 0
}

# Claude Code's daemon: the hook's environment the way the other hooks read
# it (PSEUDOLIFE_MCP_DAEMON_URL, PSEUDOLIFE_MCP_TOKEN or a private
# PSEUDOLIFE_MCP_TOKEN_FILE).
claude_connection() {
    gate_url "${PSEUDOLIFE_MCP_DAEMON_URL:-http://127.0.0.1:8765}" || return 1
    gate_auth "${PSEUDOLIFE_MCP_TOKEN:-}" "${PSEUDOLIFE_MCP_TOKEN_FILE:-}"
}

decode_connection_value() {
    local value
    if value=$(printf '%s' "$1" | base64 --decode 2>/dev/null); then
        printf '%s' "$value"
    else
        printf '%s' "$1" | base64 -D 2>/dev/null
    fi
}

# Codex's daemon: the managed connection file setup writes under the Codex
# home (a canonical five-line record naming the daemon URL and a rotatable
# bearer file, base64), else the explicit settings, with the same checks
# coordination-start.sh and session-end.sh make: an explicit URL may not
# disagree with the managed one, an explicit credential file needs the
# matching managed URL, and a tokenless managed connection ignores both.
codex_connection() {
    local home="${CODEX_HOME:-${HOME}/.codex}" connection managed="" managed_url=""
    local managed_token_file="" url_b64 token_b64 tokenless="" explicit_url="" token_file=""
    connection="$home/pseudolife/connection.json"
    if [ -e "$connection" ]; then
        private_regular "$connection" 16384 || return 1
        [ "$(wc -l < "$connection")" -eq 5 ] &&
            [ "$(sed -n '1p' "$connection")" = '{' ] &&
            [ "$(sed -n '2p' "$connection")" = '  "version": 1,' ] &&
            [ "$(sed -n '5p' "$connection")" = '}' ] || return 1
        url_b64=$(sed -n '3s/^  "daemon_url": "\([A-Za-z0-9+\/=]*\)",$/\1/p' "$connection")
        token_b64=$(sed -n '4s/^  "token_file": "\([A-Za-z0-9+\/=]*\)"$/\1/p' "$connection")
        managed_url=$(decode_connection_value "$url_b64")
        managed_token_file=$(decode_connection_value "$token_b64")
        [ -n "$managed_url" ] || return 1
        managed=1
    fi
    if [ -n "$managed" ] && [ -z "$managed_token_file" ]; then
        tokenless=1
    else
        explicit_url="${PSEUDOLIFE_MCP_DAEMON_URL:-}"
        token_file="${PSEUDOLIFE_MCP_TOKEN_FILE:-$managed_token_file}"
    fi
    if [ -n "$explicit_url" ] && [ -n "$managed_url" ] &&
            [ "${explicit_url%/}" != "${managed_url%/}" ]; then
        return 1
    fi
    if [ -z "$tokenless" ] && [ -n "$managed_url" ] &&
            [ -n "${PSEUDOLIFE_MCP_TOKEN_FILE:-}" ] &&
            { [ -z "$explicit_url" ] ||
              [ "${explicit_url%/}" != "${managed_url%/}" ]; }; then
        return 1
    fi
    gate_url "${explicit_url:-${managed_url:-http://127.0.0.1:8765}}" || return 1
    if [ -n "$tokenless" ]; then
        gate_auth "" ""
    else
        gate_auth "${PSEUDOLIFE_MCP_TOKEN:-}" "$token_file"
    fi
}

# The park gate's one request, to the client's daemon. Prints the body; any
# failure is allow, and the caller drops what a failed request printed (a
# body cut off at the time limit). -L with no redirects allowed makes a 3xx
# a failure: without it curl -f passes a redirect's body through.
gate_answer() {
    if [ -n "$CODEX_HOOK_CONTEXT" ]; then
        codex_connection || return 1
    else
        claude_connection || return 1
    fi
    curl -L -sf --max-redirs 0 --connect-timeout 1 --max-time 2 \
        "${GATE_AUTH[@]}" "$GATE_URL/api/hook/park-gate?$1"
}

# Prints $1 as a JSON string literal (backslash, quote, newline and tab
# escaped); false for text carrying any other control character, which the
# caller replaces with the fixed message rather than emit invalid JSON.
json_string() {
    local text="$1"
    text=${text//\\/\\\\}
    text=${text//\"/\\\"}
    text=${text//$'\n'/\\n}
    text=${text//$'\t'/\\t}
    case "$text" in *[[:cntrl:]]*) return 1 ;; esac
    printf '"%s"' "$text"
}

# The woke marker, after a wake fired: one POST to /api/hook/woke naming this
# session's board address (<key>.agent), on Claude Code's connection, in the
# background with every output closed, so a daemon that hangs cannot hold
# the wake back. The answer is ignored; no address, no marker.
woke_marker() {
    local agent=""
    [ -f "$AGENT" ] && [ ! -L "$AGENT" ] || return 0
    IFS= read -r agent < "$AGENT"
    agent=${agent%$'\r'}
    case "$agent" in *[!0123456789abcdef]*) return 0 ;; esac
    [ "${#agent}" -eq 32 ] || return 0
    claude_connection || return 0
    curl -L -sf -X POST --max-redirs 0 --connect-timeout 1 --max-time 2 \
        "${GATE_AUTH[@]}" "$GATE_URL/api/hook/woke?agent=$agent" \
        >/dev/null 2>&1 3>&- </dev/null &
}

ACTIVE=$(printf '%s' "$INPUT" |
         grep -o '[{,][[:space:]]*"stop_hook_active"[[:space:]]*:[[:space:]]*true' | head -1)
if [ -z "$ACTIVE" ] && [ -f "$AGENT" ] && [ ! -L "$AGENT" ]; then
    AGENT_ID=""
    IFS= read -r AGENT_ID < "$AGENT"
    AGENT_ID=${AGENT_ID%$'\r'}
    case "$AGENT_ID" in *[!0123456789abcdef]*) AGENT_ID="" ;; esac
    [ "${#AGENT_ID}" -eq 32 ] || AGENT_ID=""
    if [ -n "$AGENT_ID" ]; then
        SINCE=""
        if [ -f "$TURN" ] && [ ! -L "$TURN" ]; then
            IFS= read -r SINCE < "$TURN"
            SINCE=${SINCE%$'\r'}
        fi
        case "$SINCE" in ''|*[!0123456789]*) SINCE="" ;; esac
        [ "${#SINCE}" -le 12 ] || SINCE=""
        QUERY="agent=$AGENT_ID"
        [ -n "$SINCE" ] && QUERY="$QUERY&since=$SINCE"
        ANSWER=$(gate_answer "$QUERY") || ANSWER=""
        ANSWER=${ANSWER//$'\r'/}
        case "$ANSWER" in
            block|block$'\n'*)
                MESSAGE=""
                case "$ANSWER" in *$'\n'*) MESSAGE=${ANSWER#*$'\n'} ;; esac
                while [ "${MESSAGE%$'\n'}" != "$MESSAGE" ]; do MESSAGE=${MESSAGE%$'\n'}; done
                DEFAULT_MESSAGE="Before ending: update your board status with why you stopped and what you need (memory_agents update park_reason=... park_needs=... park_clear_by=... park_resume=...). Use done only when no follow-up is expected: nothing will ring you. Waiting on a merge click or a review that may still bring fixes? Park needs_approval with park_clear_by set to the reviewer's agent id or maintainer, or waiting_peer. A park records intent; automatic wake requires a live listener. Check the sender's wake receipt; no_path means mail is queued for receive on a later turn. For waits over 59 minutes, especially needs_approval waiting on maintainer, arm wait-mail in the background or keep the Codex doorbell active; otherwise record that you are reachable on your next turn."
                [ -n "$MESSAGE" ] || MESSAGE=$DEFAULT_MESSAGE
                if [ -n "$CODEX_HOOK_CONTEXT" ]; then
                    # Codex reads the decision from stdout (exit 0).
                    if ! REASON=$(json_string "$MESSAGE"); then
                        MESSAGE=$DEFAULT_MESSAGE
                        REASON=$(json_string "$MESSAGE")
                    fi
                    printf '{"decision":"block","reason":%s}\n' "$REASON"
                else
                    printf '%s\n' "$MESSAGE" >&3
                fi
                # UTF-8 bytes plus the newline, on every client: ${#MESSAGE}
                # counts characters under a UTF-8 locale.
                printf '%s\tgate\t%s\t0\t%s\tblock\n' "$(date +%s)" "${KEY:0:8}" \
                    "$(( $(printf '%s' "$MESSAGE" | wc -c) + 1 ))" >> "$DIGEST_DIR/ledger.log"
                [ -n "$CODEX_HOOK_CONTEXT" ] && exit 0
                exit 2
                ;;
        esac
    fi
fi
# The wake is Claude Code's; Codex's is the doorbell.
[ -z "$CODEX_HOOK_CONTEXT" ] || exit 0

WAIT=${PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT:-$MAX_WAIT}
case "$WAIT" in ''|*[!0123456789]*) WAIT=$MAX_WAIT ;; esac
[ "${#WAIT}" -le 5 ] || WAIT=$MAX_WAIT
WAIT=$((10#$WAIT))
[ "$WAIT" -le "$MAX_WAIT" ] || WAIT=$MAX_WAIT

TOKEN="$$-$RANDOM$RANDOM"
ARMED="$DIGEST_DIR/$KEY.$TOKEN.wake-armed"
# No digest directory (coordination has never run here) fails this write.
printf '%s\n' "$TOKEN" > "$LEASE.$$" && mv -f "$LEASE.$$" "$LEASE" || exit 0
still_owner() {
    local current=""
    IFS= read -r current < "$LEASE"
    [ "$current" = "$TOKEN" ]
}
renew_listener() {
    local now expiry remaining
    still_owner || return 1
    now=$(date +%s)
    remaining=$((WAIT - SECONDS))
    [ "$remaining" -gt 0 ] || return 1
    [ "$remaining" -le "$LISTENER_LEASE" ] || remaining=$LISTENER_LEASE
    expiry=$((now + remaining))
    printf '%s\n%s\n' "$TOKEN" "$expiry" > "$ARMED.$$" && mv -f "$ARMED.$$" "$ARMED"
}
disarm_listener() {
    # Only token-specific paths: a superseding watcher owns the shared lease.
    rm -f "$ARMED" "$LEASE.$$" "$ARMED.$$"
}
trap disarm_listener EXIT
trap 'exit 0' HUP INT TERM

# Claude Code going away ends the wait. kill -0 sees a POSIX parent at every
# poll. Under Git Bash CLAUDE_PID is a Windows PID that only `ps -W` (Git's
# own) lists, at 65 ms a call on the idle maintainer host (2026-09-27) and
# 1.6-3.5 s on the loaded 2026-09-23 test machine, so there it runs at arm
# time and then every PARENT_CHECK seconds. Returns 0 while the PID is
# listed, 1 once a listing lacks it, 2 when there is no listing to judge by
# (the watch then goes on as if unchecked).
windows_pid_listed() {  # $1 = a Windows PID
    local listing
    listing=$(ps -W 2>/dev/null) || return 2
    [ -n "$listing" ] || return 2
    printf '%s\n' "$listing" |
        awk -v p="$1" '$4 == p || ($1 ~ /^[A-Za-z]$/ && $5 == p) { found = 1; exit } END { exit found ? 0 : 1 }'
}
PARENT="" WINDOWS_PARENT="" PARENT_CHECKED=0
case "${CLAUDE_PID:-}" in
    ''|*[!0123456789]*) ;;
    *)  case "${OSTYPE:-}" in
            msys*|cygwin*)
                windows_pid_listed "$CLAUDE_PID"
                [ $? -eq 0 ] && PARENT=$CLAUDE_PID && WINDOWS_PARENT=1
                ;;
            *) kill -0 "$CLAUDE_PID" && PARENT=$CLAUDE_PID ;;
        esac ;;
esac
# True while the parent is (as far as this host can tell) still running.
parent_alive() {
    if [ -z "$WINDOWS_PARENT" ]; then
        kill -0 "$PARENT"
        return
    fi
    [ $((SECONDS - PARENT_CHECKED)) -ge "$PARENT_CHECK" ] || return 0
    PARENT_CHECKED=$SECONDS
    windows_pid_listed "$PARENT"
    [ $? -ne 1 ]
}

read_seen() {
    SEEN_AT=0
    [ -f "$SEEN" ] && IFS= read -r SEEN_AT < "$SEEN"
    SEEN_AT=${SEEN_AT//[$'\r\n ']/}
    case "$SEEN_AT" in ''|*[!0123456789]*) SEEN_AT=0 ;; esac
}

# Sets RING_AT and RING_REASON from the shim's ring marker; true when the
# daemon decided a ring this session has not seen. Anything but a regular
# file with a watermark and a reason is no ring: the hook rings on the
# daemon's word, never on a guess.
ring_past_seen() {
    RING_AT=0
    RING_REASON=""
    [ -f "$RING" ] && [ ! -L "$RING" ] || return 1
    { IFS= read -r RING_AT; IFS= read -r RING_REASON; } < "$RING"
    RING_AT=${RING_AT//[$'\r\n ']/}
    RING_REASON=${RING_REASON%$'\r'}
    case "$RING_AT" in ''|*[!0123456789]*) return 1 ;; esac
    [ "${#RING_AT}" -le 12 ] || return 1
    RING_AT=$((10#$RING_AT))
    case "$RING_REASON" in ''|*[!-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_\ ]*) return 1 ;; esac
    [ "$RING_AT" -gt "$SEEN_AT" ]
}

# Sets NOW, and RECENT to the wake times still inside the window; true while
# another wake fits under the cap.
wake_budget_ok() {
    local t count=0
    NOW=$(date +%s)
    RECENT=""
    # Fails closed, like the marker: a non-file here could never record one.
    if [ -e "$WAKES" ] && [ ! -f "$WAKES" ]; then return 1; fi
    [ -f "$WAKES" ] || return 0
    while IFS= read -r t || [ -n "$t" ]; do
        t=${t%$'\r'}
        case "$t" in ''|*[!0123456789]*) continue ;; esac
        [ "${#t}" -le 12 ] || continue
        # Decimal, whatever the padding: bash reads 089 as bad octal.
        t=$((10#$t))
        # A future entry (clock stepped back, corruption) would never age out.
        [ "$t" -le "$NOW" ] || continue
        [ $((NOW - t)) -lt "$WAKE_WINDOW" ] || continue
        RECENT="$RECENT$t
"
        count=$((count + 1))
    done < "$WAKES"
    [ "$count" -lt "$MAX_WAKES" ]
}

# The call site: returns 0 with WATERMARK and BODY set once the digest holds
# mail this session has not seen and the wake cap allows it, 3 on timeout, a
# lost lease or a gone parent, 2 when the digest turns into a symlink or
# vanishes. A digest absent at arm time is waited for (the shim writes it on
# its first heartbeat); one that vanishes mid-watch means the shim exited
# (a crash that takes the shim down with Claude Code leaves it, which the
# parent check above catches), so the watch ends rather than fire
# into a later session. The wait-mail command being built beside this hook
# has the same fire-and-mark contract; it can replace the loop once it
# ships, keeping the lease, parent and cap checks beside it.
wait_for_mail() {
    # SECONDS counts from the start of the script, spawns included.
    local deadline=$WAIT snapshot present=0
    while :; do
        still_owner || return 3
        if [ -n "$PARENT" ] && ! parent_alive; then return 3; fi
        renew_listener || return 3
        [ -L "$FILE" ] && return 2
        if [ ! -f "$FILE" ] && [ "$present" = 1 ]; then return 2; fi
        if [ -f "$FILE" ]; then
            present=1
            # One read, one snapshot: the shim replaces the file atomically.
            snapshot=""
            IFS= read -r -d '' snapshot < "$FILE"
            snapshot=${snapshot//$'\r'/}
            WATERMARK=${snapshot%%$'\n'*}
            BODY=""
            case "$snapshot" in *$'\n'*) BODY=${snapshot#*$'\n'} ;; esac
            while [ "${BODY%$'\n'}" != "$BODY" ]; do BODY=${BODY%$'\n'}; done
            read_seen
            case "$WATERMARK" in
                ''|*[!0123456789]*) ;;
                *) if [ -n "$BODY" ] && [ "$WATERMARK" -gt "$SEEN_AT" ] && ring_past_seen \
                        && wake_budget_ok; then
                       return 0
                   fi ;;
            esac
        fi
        [ "$SECONDS" -lt "$deadline" ] || return 3
        sleep "$POLL"
    done
}

# Print, mark, record; exit 2 is the wake. Returning means no wake.
fire() {
    local text
    still_owner || return
    # Something other than a file at the marker's path could never be replaced.
    if [ -e "$SEEN" ] && [ ! -f "$SEEN" ]; then return; fi
    text="Pseudolife board mail woke this session (Claude Code labels this delivery a Stop hook error; nothing failed):
$BODY"
    case "$RING_REASON" in
        nudged*) text="$text
Set your park status before you stop: memory_agents(action=update, park_reason=..., park_needs=..., park_clear_by=..., park_resume=..., park_expires=<epoch, default 12 h>), so mail wakes you only when it clears that need." ;;
    esac
    printf '%s\n' "$text" >&3
    # The prompt hook may have moved the marker while this watcher slept:
    # only ever raise it.
    read_seen
    if [ "$WATERMARK" -gt "$SEEN_AT" ]; then
        printf '%s\n' "$WATERMARK" > "$SEEN.$$" && mv -f "$SEEN.$$" "$SEEN"
    fi
    # Backstop for a write that failed some other way: no mark, no wake.
    read_seen
    [ "$SEEN_AT" -ge "$WATERMARK" ] || return
    printf '%s%s\n' "$RECENT" "$NOW" > "$WAKES.$$" && mv -f "$WAKES.$$" "$WAKES"
    printf '%s\twait\t%s\t%s\t%s\t%s\n' "$NOW" "${KEY:0:8}" "$WATERMARK" "$(( ${#text} + 1 ))" \
        "$RING_REASON" >> "$DIGEST_DIR/ledger.log"
    # The turn is starting: say so, once.
    woke_marker
    exit 2
}

# Only a clean return from the wait can fire: bash abandons the rest of a
# line on an expansion error and runs the next, which here is exit 0.
wait_for_mail && fire
exit 0
