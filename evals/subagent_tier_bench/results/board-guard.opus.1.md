Paths below are under `<repo>\plugin\hooks\`.

**Telling a subagent from the parent:** Claude Code puts a top-level `"agent_id"` key in the hook's stdin only for a subagent's calls (comment at `subagent-board-guard.sh:8-11`). The script reads all of stdin into a variable (`:39`). If `agent_id` is missing or empty, it exits 0 and the call goes through (`:60`). It only counts a key preceded by `{` or `,`, so the same text inside an escaped message string doesn't match (`:50-58`). The parent's calls therefore always pass.

**What it denies and allows:** It only acts on `mcp__<any>__memory_agents` and `mcp__<any>__memory_message`. Any other tool exits 0 (`:79-85`). It reads `action` from `tool_input` (`:91-94`).
- **Allowed:** `memory_agents` with no action or `list`, and `memory_message` with `receive` or `history` (`:95-97`).
- **Denied:** everything else from a subagent, meaning `update`, `claim`, `release`, `send` and `ack` (comment `:18-21`). It prints `permissionDecision:"deny"` with a reason telling the subagent to ask its parent (`:103-110`).

**When it can't read the payload, it lets the call through.** Missing `agent_id`, `tool_name` or `tool_input` each exit 0 (`:60, :79, :91`). If `PSEUDOLIFE_AGENT_COORDINATION` is set to something other than blank/1/true/yes/on, it also exits 0 (`:43-48`). The reason is that a broken guard must never block the parent (`:24-27`).

**Parse check in hooks.json:** The command first runs `bash -n` on the script and exits 0 if it fails to parse or is missing. Only then does it `exec` the script (`hooks.json:126`). Without this, bash would exit 2 on a syntax error, and exit 2 blocks the call, including the parent's (`:27-30`). The matcher is `^mcp__.+__memory_(agents|message)$` (`hooks.json:122`).

**Codex context:** Either marker sets `CODEX=1` (`:70-73`):
1. `PSEUDOLIFE_CODEX_HOOK=1`
2. `PLUGIN_ROOT` is non-empty and equals `CLAUDE_PLUGIN_ROOT`

The flag is cleared if `CLAUDECODE=1` and `CLAUDE_CODE_SESSION_ID` matches the payload's `session_id`. That means Claude Code started the hook for this same session, even if it inherited a Codex marker (`:62-76`). If `CODEX` is still set, or `CLAUDECODE` isn't 1, the script exits 0 and allows everything (`:77`). This is because a Codex child has its own board address (`:30-33`).

**PowerShell path:** On Windows, `commandWindows` runs `lifecycle.ps1 -Event SubagentBoardGuard` (`hooks.json:127`). Per its header comment, that file is the native Windows override for Codex, and Claude keeps the bash commands (`lifecycle.ps1:2`). In that branch it reads all of stdin, ignores read errors, prints nothing and exits 0, so every call is allowed (`lifecycle.ps1:7-14`).
