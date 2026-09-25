#!/usr/bin/env bash
# Pseudolife-MCP Stop hook: wake an idle Claude Code session when addressed
# board mail arrives. Opt-in: hooks.json runs it only when
# PSEUDOLIFE_AGENT_WAKE_HOOK=1 (checked again here), and it is a no-op
# anywhere but Claude Code (Codex loads the same hooks.json).
#
# hooks.json registers it with "async": true and "asyncRewake": true, so it
# waits in the background after each turn, and exit code 2 starts a new turn
# even when the session is idle (Desktop Code tab, Claude Code 2.1.280,
# 2026-09-23: under a second from exit to turn). Claude Code shows stderr to
# the model as a system reminder labelled "Stop hook blocking error", so
# stderr carries one line saying what this is, then the shim's digest, which
# already reads as agent-origin, not user authority.
#
# It fires when the digest's watermark (line 1) is past the <key>.seen marker
# and the body is non-empty. Mail that landed during the turn therefore fires
# at once, and a digest the session already saw (through the prompt hook, the
# tool-result hint or an earlier wake) does not fire again at the next turn
# end; SessionStart clears .seen on resume and compact, so pending mail can
# wake the session once more after those. Firing prints first, then advances
# .seen, so those paths stay quiet about it, then appends a "wait" line to
# the ledger. A marker that cannot advance means no wake at all: it would
# otherwise fire again at every turn end.
#
# One watcher per session: every firing writes a fresh token to <key>.wake,
# and an older watcher that finds another token there exits quietly at its
# next poll, so the newest turn end always owns the wait.
#
# Budget: hooks.json sets "timeout": 3600, which Claude Code enforces on
# asyncRewake hooks, and the wait ends at MAX_WAIT, before the kill. An hour
# covers twice the p90 acknowledgement latency (1,740 s) that sessions
# without a watcher showed in the 2026-09-23 10-session messageboard trial;
# a session idle for longer has usually finished, and its mail still
# surfaces on the next prompt. PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT shortens the
# wait, never lengthens it.
MAX_WAIT=3540
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

if [ "${PSEUDOLIFE_AGENT_WAKE_HOOK:-}" != "1" ] || [ "${CLAUDECODE:-}" != "1" ]; then
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
case "$SID" in ''|*[!A-Za-z0-9._-]*) exit 0 ;; esac
[ "${#SID}" -le 128 ] || exit 0
# A Codex run nested inside a Claude Bash tool inherits CLAUDECODE; Claude
# Code sets CLAUDE_CODE_SESSION_ID in a hook's environment to the payload's
# session_id, updated on /clear.
[ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ] || exit 0

DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
KEY=$(printf '%s' "$SID" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64)
# After /clear, hooks get a new session id while the shim keeps writing the
# digest under its spawn-time one; the /clear digest-keying change has
# SessionStart record that key per Claude Code process (line 1) with the
# sha256 of the session id it is confirmed for (line 2), and without such a
# record the new id's digest never appears. The key is followed only when
# line 2 names this session, so a reused PID's record cannot wake it for a
# dead process's mail. A symlinked, CRLF or upper-case record is refused.
case "${CLAUDE_PID:-}" in
    ''|*[!0-9]*) ;;
    *)  HOST="$DIGEST_DIR/claude-$CLAUDE_PID.host"
        if [ -f "$HOST" ] && [ ! -L "$HOST" ]; then
            RECORDED=""
            CONFIRMED=""
            { IFS= read -r RECORDED; IFS= read -r CONFIRMED; } < "$HOST"
            case "$RECORDED$CONFIRMED" in
                *[!0-9a-f]*) ;;
                *) [ "${#RECORDED}" -eq 64 ] && [ "$CONFIRMED" = "$KEY" ] && KEY=$RECORDED ;;
            esac
        fi ;;
esac
[ "${#KEY}" -eq 64 ] || exit 0
FILE="$DIGEST_DIR/$KEY.txt"
SEEN="$DIGEST_DIR/$KEY.seen"
LEASE="$DIGEST_DIR/$KEY.wake"
WAKES="$DIGEST_DIR/$KEY.wakes"
[ -L "$FILE" ] && exit 0

WAIT=${PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT:-$MAX_WAIT}
case "$WAIT" in ''|*[!0-9]*) WAIT=$MAX_WAIT ;; esac
[ "${#WAIT}" -le 5 ] || WAIT=$MAX_WAIT
WAIT=$((10#$WAIT))
[ "$WAIT" -le "$MAX_WAIT" ] || WAIT=$MAX_WAIT

TOKEN="$$-$RANDOM$RANDOM"
# No digest directory (coordination has never run here) fails this write.
printf '%s\n' "$TOKEN" > "$LEASE.$$" && mv -f "$LEASE.$$" "$LEASE" || exit 0
still_owner() {
    local current=""
    IFS= read -r current < "$LEASE"
    [ "$current" = "$TOKEN" ]
}

# Claude Code going away ends the wait, where kill -0 can see it. Under Git
# Bash CLAUDE_PID is a Windows PID that only tasklist or ps -W can probe, at
# 1.6-3.5 s a call on the 2026-09-23 test machine, so there the budget bounds
# an orphaned watcher.
PARENT=""
case "${CLAUDE_PID:-}" in
    ''|*[!0-9]*) ;;
    *) kill -0 "$CLAUDE_PID" && PARENT=$CLAUDE_PID ;;
esac

read_seen() {
    SEEN_AT=0
    [ -f "$SEEN" ] && IFS= read -r SEEN_AT < "$SEEN"
    SEEN_AT=${SEEN_AT//[$'\r\n ']/}
    case "$SEEN_AT" in ''|*[!0-9]*) SEEN_AT=0 ;; esac
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
        case "$t" in ''|*[!0-9]*) continue ;; esac
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
# its first heartbeat); one that vanishes mid-watch means the shim exited,
# the only sign of that under Git Bash, so the watch ends rather than fire
# into a later session. The wait-mail command being built beside this hook
# has the same fire-and-mark contract; it can replace the loop once it
# ships, keeping the lease, parent and cap checks beside it.
wait_for_mail() {
    # SECONDS counts from the start of the script, spawns included.
    local deadline=$WAIT snapshot present=0
    while :; do
        still_owner || return 3
        if [ -n "$PARENT" ] && ! kill -0 "$PARENT"; then return 3; fi
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
                ''|*[!0-9]*) ;;
                *) if [ -n "$BODY" ] && [ "$WATERMARK" -gt "$SEEN_AT" ] && wake_budget_ok; then
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
    printf '%s\twait\t%s\t%s\t%s\n' "$NOW" "${KEY:0:8}" "$WATERMARK" "$(( ${#text} + 1 ))" \
        >> "$DIGEST_DIR/ledger.log"
    exit 2
}

# Only a clean return from the wait can fire: bash abandons the rest of a
# line on an expansion error and runs the next, which here is exit 0.
wait_for_mail && fire
exit 0
