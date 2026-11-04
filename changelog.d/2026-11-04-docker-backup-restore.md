### Added (2026-11-04 — Back up and restore Docker banks without a checkout)

- `pseudolife-mcp backup` detects Docker banks and saves a database dump and
  state archive using the existing containers, including stopped daemon mounts.
  `pseudolife-mcp restore --backup-file <dump> --state-archive <state>` asks once
  before replacing a bank (`--yes` for unattended use), first saves a complete
  safety pair outside any checkout, and supports `--leave-stopped` for moves.
  It preserves an active move gate and refuses unsafe archives or incomplete
  backups. Schema rollback guidance now names the installed restore command.
- PostgreSQL clients stay on the selected container's local socket and treat
  database names literally; inherited address/service settings cannot redirect
  a restore. `--db-port` selects a non-default server port and survives recovery.
