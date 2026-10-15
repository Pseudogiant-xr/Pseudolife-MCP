**Subagent vs parent** (`plugin/hooks/subagent-board-guard.sh`): Claude Code puts `agent_id` in the PreToolUse stdin only for a subagent's calls (lines 8-11). The script matches a top-level `"agent_id"` with a non-empty value (`AGENT_RE`, line 54). If there is none, it exits 0 and allows the call (line 60). It also exits 0 unless `CLAUDECODE=1` (line 77).

**Denied** (subagent only):
- Tool names are matched at lines 81-85 as `mcp__?*__memory_agents` or `mcp__?*__memory_message`. Any other tool passes.
- Action is parsed from `tool_input` (lines 91-94).
- Denied: `memory_agents` with update, claim or release, and `memory_message` with send or ack. These fall through to the deny at lines 103-110.
- Allowed: `memory_agents` with no action or `list`, and `memory_message` `receive` or `history` (lines 95-97).
- The deny output is a `permissionDecision: "deny"` JSON with a reason that tells the subagent to ask its parent.
- If `tool_input` can't be matched, it allows (line 91).

**Unreadable payload:** it fails open. A missing `agent_id` (line 60), `tool_name` (line 79) or `tool_input` (line 91) means exit 0. Setting `PSEUDOLIFE_AGENT_COORDINATION` to anything but a yes also exits 0 (lines 43-48).

**Parse guard** (`plugin/hooks/hooks.json:126`): the command is `bash -n <script> 2>/dev/null || exit 0; exec bash <script>`. `bash -n` exits 2 on a syntax error, and exit 2 would block the parent's call. This guard turns a script that doesn't parse, or is missing, into an allow. The matcher at line 122 is `^mcp__.+__memory_(agents|message)$`.

**Codex context** (lines 67-77): either of two markers sets `CODEX=1`:
1. `PSEUDOLIFE_CODEX_HOOK=1`.
2. `PLUGIN_ROOT` non-empty and equal to `CLAUDE_PLUGIN_ROOT`.

The marker is cancelled if `CLAUDECODE=1` and `CLAUDE_CODE_SESSION_ID` equals the payload's `session_id`. That case is a Codex run nested in a Claude Bash tool, where a hook Claude started for this session stays Claude's. In Codex context, line 77 exits 0, so everything is allowed. The reason given in the header is that a Codex child has its own board address (lines 30-33).

**PowerShell path** (`plugin/hooks/lifecycle.ps1:7-14`): `hooks.json` gives `commandWindows` as `lifecycle.ps1 -Event SubagentBoardGuard` (line 127), and Codex runs that on Windows. For `SubagentBoardGuard` the script drains stdin in a try/catch and exits 0 with nothing printed. It allows every call, with no parsing and no deny.
