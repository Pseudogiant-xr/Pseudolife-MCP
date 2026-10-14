### Added (2026-10-14 — Brief the maintainer's delegate at session start and remind it each turn)

- The session holding an operator-granted `delegate:<project>` lease now gets
a short brief after the board check-in at session start (start the decision
watcher, park on its events, hand raw output to a subagent, answer a quiet
wake in one line) and one reminder line after the per-prompt memory note.
Other sessions see no change, and the check-in text itself is unchanged.

- The plugin's hook scripts (`coordination-start.sh`, `session-start.sh` and
`lifecycle.ps1`) pass the session's board address from its `.agent` file as
`agent=` on the requests they already make; there is no new request per
prompt. The daemon answers only for an address the bearer's principal owns,
with the same operator-or-maintainer grant proof as delegate mail, and caches
each answer for 60 seconds, so a grant or a revoke shows within a minute.
Sessions pick the scripts up after the client update (`pseudolife-mcp update
--all`, or `python ops/update_clients.py`) and a restart. `hooks.json` did
not change, so Codex users do not approve the hooks again.
