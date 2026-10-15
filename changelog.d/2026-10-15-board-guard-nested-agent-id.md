### Fixed (2026-10-15 — The subagent board guard no longer refuses a main session that passes an agent_id)

- The Claude Code plugin's subagent board guard took any `"agent_id"` key
in its input as the mark of a subagent, including one inside the tool's
arguments. A main session that guessed an `agent_id` argument for
`memory_agents(action="update")` was refused as a subagent and told to ask
its parent, which sent it chasing the wrong cause and left its board status
stale (seen 2026-10-15). The guard now reads the hook input as JSON in
bash: `agent_id`, `session_id` and `tool_name` count only as keys of the
payload itself, and `action` and `ack` only as keys of its `tool_input`
object, in any key order. That parent call now reaches the daemon, which
refuses the unknown argument by name. An `action` or `ack` nested deeper
in an argument no longer counts as the call's, and a payload cut off
mid-object is let through like any other it cannot read. A payload with
more values than any board call can carry (the widest valid one takes 102
of the walker's 512 steps) is let through to the daemon's unknown-argument
refusal instead of running into the hook's 5 s timeout. Measured on
Windows Git Bash the same day: an 8 KB subagent send takes 57 ms against
43 ms before, a small call is unchanged at about 28 ms, and still no
process is spawned. `hooks.json` is unchanged, so Codex users approve
nothing new; the plugin reaches clients with `pseudolife-mcp update`.
