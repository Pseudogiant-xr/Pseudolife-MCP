#!/usr/bin/env bash
# Pseudolife-MCP PreToolUse hook: a Claude Code subagent only reads the board.
#
# A subagent (Agent tool) runs inside its parent's MCP shim, so every board
# call it makes carries the parent's identity (#425, schema v47): a status
# update overwrites the parent's, an ack marks the parent's mail read before
# the parent sees it, a send goes out under the parent's name. The shim
# cannot tell the two apart; this hook can. Claude Code gives a plugin's
# PreToolUse hook "agent_id" (and "agent_type") in stdin for a subagent's
# tool calls and neither for the parent's, and a permissionDecision "deny"
# blocks only the call it answers (Claude Code 2.1.283, 2026-09-30). The
# maintainer decided that day: subagents only read the board and never send;
# the parent owns status, ack and send.
#
# hooks.json runs it only for tools named mcp__<server>__memory_agents or
# mcp__<server>__memory_message (any server name: installs register
# pseudolife-memory, the Desktop Code tab also pseudolife-desktop), and the
# name is checked again here. For a subagent it denies memory_agents update,
# claim and release and memory_message send and ack, and receive with ack
# true (it acknowledges what it returns); memory_agents list (the default
# action) and memory_message receive without ack and history (read-only: no
# ack, delivery or wake) pass, and so does every other tool. The parent's
# calls always pass.
#
# It fails open: a payload it cannot read, or a coordination opt-out
# (PSEUDOLIFE_AGENT_COORDINATION set to anything but a yes), allows the call,
# because before this hook the rule was an instruction only and a broken
# guard must never block the parent. For the same reason hooks.json runs
# `bash -n` on this file first and allows when it does not parse or is
# missing: bash exits 2 on a syntax error, and exit 2 blocks the call, the
# parent's included (the Stop entry has the same check). Codex loads the same hooks.json and
# lists this entry, but a Codex child has a board address of its own, so in
# Codex context (the markers stop-wake.sh reads) it allows everything, as
# lifecycle.ps1 -Event SubagentBoardGuard does on Windows.
#
# No daemon request and no external process unless a setting is set: it runs
# before every board call.

# The whole payload, with a builtin (read returns 1 at end of input).
IFS= read -r -d '' INPUT

# Read the setting the way the shim and doctor do: trimmed and lower-cased,
# blank meaning unset. Only a non-empty value costs a spawn.
if [ -n "${PSEUDOLIFE_AGENT_COORDINATION:-}" ]; then
    case "$(printf %s "$PSEUDOLIFE_AGENT_COORDINATION" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in
        ''|1|true|yes|on) ;;
        *) exit 0 ;;
    esac
fi

# The keys it needs, read by walking the JSON in bash (no process per call).
# A key counts only where Claude Code puts it: agent_id, session_id and
# tool_name in the payload itself, action and ack in its tool_input object,
# in any order. The same name anywhere else never counts: inside a string
# (a message text), in an object nested in an argument, or as an argument
# the model guessed (2026-10-15: a main session passed agent_id to
# memory_agents and was refused as a subagent, which sent it chasing the
# wrong cause; the daemon refuses that argument itself, by name). It checks
# nesting and strings, not every rule of JSON (Claude Code sends valid
# JSON): a payload that is not an object, or whose brackets or last string
# do not close, allows the call.
#
# Each step copies what is left of the input, so the time grows with the
# square of the number of values: 4,000 took 5.9 s on Git Bash (review,
# 2026-10-15), past hooks.json's 5 s. The widest valid board call took 102
# steps that day (children holds at most 8; every other argument is a
# scalar), so past MAX_STEPS the call cannot be a valid one and goes
# through to the daemon's unknown-argument refusal. At the cap, an 8 KB
# text followed by 4,000 values ends in 0.8 s on Git Bash.
[[ $INPUT == *'"agent_id"'* ]] || exit 0
[[ $INPUT =~ ^[[:space:]]*\{ ]] || exit 0
STR_RE='^"(([^"\\]|\\.)*)"'
COLON_RE='^[[:space:]]*:[[:space:]]*'
SKIP_RE='^[^]"{}[]+'
MAX_STEPS=512
AGENT="" SID="" TOOL="" ARGS="" ACTION="" ACK=""
rest=$INPUT depth=0 args=0 key="" key_depth=-1 value="" whole="" steps=0
while [ -n "$rest" ]; do
    steps=$((steps + 1))
    [ "$steps" -le "$MAX_STEPS" ] || exit 0
    case ${rest:0:1} in
        '"')
            [[ $rest =~ $STR_RE ]] || exit 0
            value=${BASH_REMATCH[1]}
            rest=${rest:${#BASH_REMATCH[0]}}
            if [[ $rest =~ $COLON_RE ]]; then
                rest=${rest:${#BASH_REMATCH[0]}}
                key=$value key_depth=$depth
                continue
            fi ;;
        '{'|'[')
            if [ "${rest:0:1}" = '{' ] && [ "$depth:$key_depth:$key" = "1:1:tool_input" ]; then
                ARGS=1 args=2
            fi
            depth=$((depth + 1)) key_depth=-1 rest=${rest:1}
            continue ;;
        '}'|']')
            depth=$((depth - 1)) key_depth=-1 rest=${rest:1}
            [ "$depth" -gt 0 ] || { [ "$depth" = 0 ] && whole=1; break; }
            [ "$depth" -ge 2 ] || args=0
            continue ;;
        *)
            [[ $rest =~ $SKIP_RE ]] || exit 0
            rest=${rest:${#BASH_REMATCH[0]}}
            value=${BASH_REMATCH[0]%%[,[:space:]]*} ;;
    esac
    # A value: a string, or the literal a skipped run starts with.
    if [ "$key_depth" = "$depth" ]; then
        case "$depth:$args:$key" in
            1:0:agent_id) AGENT=$value ;;
            1:0:session_id) SID=$value ;;
            1:0:tool_name) TOOL=$value ;;
            2:2:action) ACTION=$value ;;
            2:2:ack) ACK=$value ;;
        esac
    fi
    key_depth=-1
done
[ -n "$whole" ] && [ -n "$AGENT" ] || exit 0

# Claude Code sets CLAUDECODE=1 and CLAUDE_CODE_SESSION_ID (the payload's
# session_id) in a hook's environment. A Codex run nested inside a Claude
# Bash tool inherits both, with the outer session's id; a hook Claude Code
# started for this very session stays Claude's whatever Codex marker it
# inherited.
CODEX=""
if [ "${PSEUDOLIFE_CODEX_HOOK:-}" = 1 ] ||
        { [ -n "${PLUGIN_ROOT:-}" ] && [ "${PLUGIN_ROOT}" = "${CLAUDE_PLUGIN_ROOT:-}" ]; }; then
    CODEX=1
fi
if [ "${CLAUDECODE:-}" = 1 ] && [ -n "$SID" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
    CODEX=""
fi
[ -z "$CODEX" ] && [ "${CLAUDECODE:-}" = 1 ] || exit 0

case "$TOOL" in
    mcp__?*__memory_agents) NAME=memory_agents ;;
    mcp__?*__memory_message) NAME=memory_message ;;
    *) exit 0 ;;
esac

# A receive's ack counts as the boolean or as the string a host may
# stringify it to.
[ -n "$ARGS" ] || exit 0
case "$NAME:$ACTION" in
    memory_message:receive) [ "$ACK" = true ] || exit 0 ;;
    memory_agents:|memory_agents:list|memory_message:history) exit 0 ;;
esac

# The reason is what the subagent reads ("PreToolUse:<tool> hook error:
# <reason>"). A subagent ignored hook text it could not attribute in an
# earlier probe, so it names its source and what to do instead. The action
# is quoted only when it is a plain word.
CALL="$NAME"
case "$ACTION" in
    ''|*[!abcdefghijklmnopqrstuvwxyz_]*) ;;
    *) CALL="$NAME(action=$ACTION)" ;;
esac
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' \
    "Pseudolife board: refused $CALL from a subagent. A subagent shares its parent session's board address, so it may only read the board (memory_agents list, memory_message receive without ack or history, memory_search). Ask your parent session to update status, ack or send."
exit 0
