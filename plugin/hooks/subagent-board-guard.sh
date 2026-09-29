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
# claim and release and memory_message send and ack; memory_agents list (the
# default action) and memory_message receive pass, and so does every other
# tool. The parent's calls always pass.
#
# It fails open: a payload it cannot read, or a coordination opt-out
# (PSEUDOLIFE_AGENT_COORDINATION set to anything but a yes), allows the call,
# because before this hook the rule was an instruction only and a broken
# guard must never block the parent. Codex loads the same hooks.json and
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

# Only a top-level key counts: preceded by { or , (plus whitespace), never by
# a backslash, which is how the same characters look inside an escaped
# string such as a message text (see coordination-prompt.sh). Each pattern
# captures the key's string value.
AGENT_RE='[{,][[:space:]]*"agent_id"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
SID_RE='[{,][[:space:]]*"session_id"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
TOOL_RE='[{,][[:space:]]*"tool_name"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
ACTION_RE='[{,][[:space:]]*"action"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
INPUT_RE='[{,][[:space:]]*"tool_input"[[:space:]]*:[[:space:]]*\{(.*)$'

[[ $INPUT =~ $AGENT_RE ]] && [ -n "${BASH_REMATCH[1]}" ] || exit 0

# Claude Code sets CLAUDECODE=1 and CLAUDE_CODE_SESSION_ID (the payload's
# session_id) in a hook's environment. A Codex run nested inside a Claude
# Bash tool inherits both, with the outer session's id; a hook Claude Code
# started for this very session stays Claude's whatever Codex marker it
# inherited.
SID=""
[[ $INPUT =~ $SID_RE ]] && SID=${BASH_REMATCH[1]}
CODEX=""
if [ "${PSEUDOLIFE_CODEX_HOOK:-}" = 1 ] ||
        { [ -n "${PLUGIN_ROOT:-}" ] && [ "${PLUGIN_ROOT}" = "${CLAUDE_PLUGIN_ROOT:-}" ]; }; then
    CODEX=1
fi
if [ "${CLAUDECODE:-}" = 1 ] && [ -n "$SID" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ]; then
    CODEX=""
fi
[ -z "$CODEX" ] && [ "${CLAUDECODE:-}" = 1 ] || exit 0

[[ $INPUT =~ $TOOL_RE ]] || exit 0
TOOL=${BASH_REMATCH[1]}
case "$TOOL" in
    mcp__?*__memory_agents) NAME=memory_agents ;;
    mcp__?*__memory_message) NAME=memory_message ;;
    *) exit 0 ;;
esac

# The arguments: an object after the top-level tool_input key. The action is
# the first top-level-shaped "action" key from there on: tool_input comes
# before the payload's remaining keys, and neither tool takes a nested
# object that could carry one.
[[ $INPUT =~ $INPUT_RE ]] || exit 0
ARGS="{${BASH_REMATCH[1]}"
ACTION=""
[[ $ARGS =~ $ACTION_RE ]] && ACTION=${BASH_REMATCH[1]}
case "$NAME:$ACTION" in
    memory_agents:|memory_agents:list|memory_message:receive) exit 0 ;;
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
    "Pseudolife board: refused $CALL from a subagent. A subagent shares its parent session's board address, so it may only read the board (memory_agents list, memory_message receive without ack, memory_search). Ask your parent session to update status, ack or send."
exit 0
