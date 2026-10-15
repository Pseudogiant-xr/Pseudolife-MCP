**Subagent vs. parent.** The script reads the whole payload (`subagent-board-guard.sh:39`) and treats the call as a subagent's only if a top-level `"agent_id"` is present and non-empty (`:54`, `:60`). The pattern requires `{` or `,` before the key, so an escaped string containing the text does not match. The parent's payload has no `agent_id`, so it exits 0 at `:60`.

**Denied and allowed.** The tool name must match `mcp__<server>__memory_agents` or `mcp__<server>__memory_message` (`:79-85`; the matcher is at `hooks.json:122`). For a subagent:
- Denied: `memory_agents` with `update`, `claim`, `release`, or any other non-list action; `memory_message` with `send`, `ack`, or no action.
- Allowed: `memory_agents` with `list` or no action (`:96`); `memory_message` with `receive` or `history` (`:96`).
- Allowed: every other tool.

The deny is emitted as `permissionDecision: "deny"` with a reason naming the subagent and the parent-only actions (`:108-109`).

**Unreadable payload.** Every failed match exits 0, which allows the call: no `agent_id` (`:60`), no `tool_name` (`:79`), no `tool_input` (`:91`). The opt-out `PSEUDOLIFE_AGENT_COORDINATION` set to anything but a yes also allows (`:43-48`). The script's header states that this fail-open choice is deliberate (`:24-28`).

**Parse-failure guard.** `hooks.json:126` runs `bash -n` on the script first and runs it only if that succeeds (`|| exit 0`). Bash exits 2 on a syntax error, which would block the parent's calls too, so an unparseable or missing script is skipped.

**Codex markers.** The script treats the context as Codex when `PSEUDOLIFE_CODEX_HOOK=1`, or when `PLUGIN_ROOT` equals `CLAUDE_PLUGIN_ROOT` (`:70-73`). A Claude session whose `session_id` matches `CLAUDE_CODE_SESSION_ID` clears that flag (`:74-76`). In Codex context the script exits 0 and allows everything (`:77`). The same markers are read in `stop-wake.sh:136-139`.

**lifecycle.ps1.** `hooks.json:127` gives this entry a `commandWindows` that runs `lifecycle.ps1 -Event SubagentBoardGuard`. The branch at `lifecycle.ps1:11-14` drains stdin and exits 0, so it allows every call with no subagent check and no deny. The script's comments say Codex runs this command on Windows (`lifecycle.ps1:7-10`). If Claude Code also honours `commandWindows`, the guard is a no-op on Windows for Claude as well, which contradicts `subagent-board-guard.sh:7-8`. I did not verify which command Claude Code runs on Windows.
