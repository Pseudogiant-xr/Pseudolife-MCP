### Changed (2026-10-30 — the maintainer's board role is called "conductor", not "delegate")
- Every word a user or agent reads now calls the maintainer's per-project
  board role the conductor: the session-start brief ("You are the
  maintainer's conductor for ..."), the per-prompt reminder, the Stop hook's
  park prompt (served and the hooks' fallback copies), the `memory_message`
  description, a done park's `reopen_by`, the grant warning, `lease delegate
  --help`, the CLI help, the Console's Roles band, role buttons, confirm
  dialogs and toasts, and the docs. Agents had confused the role with T3
  Code's `delegate_task` subagents.
- `pseudolife-mcp conductor-watch` is the decision watcher's new name;
  `delegate-watch` still runs it. The watcher registers on the board as
  `conductor-watch` and prints that name.
- Unchanged, so nothing needs migrating: the lease `delegate:<project>`, the
  `lease delegate` command, role purposes (`grant-delegate`,
  `revoke-delegate`), JSON fields and values (`"delegate"` role slots,
  `reason: delegate`, `already_delegate`, the `lease_delegate` audit event)
  and every stored value. A later change renames those ids with a migration.
