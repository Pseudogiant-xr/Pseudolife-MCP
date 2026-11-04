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
  Automatic port selection follows the daemon's connection rather than client
  defaults in the PostgreSQL container.
- Explicitly empty database connection fields override inherited environment
  values, so ambient settings cannot redirect automatic bank selection.
- Native backup advice includes the state archive, and standalone dump failures
  name `--db-port` when the selected PostgreSQL socket is unavailable.
- Automatic backup selection now follows the same runtime connection policy in
  standalone backup/restore and both update implementations, including daemon
  environment defaults, non-default ports and literal database names. Automatic
  inference requires a nonempty daemon database URL, since an absent URL selects
  file mode even when libpq environment defaults exist. A host address must be
  the selected PostgreSQL container's IP on a shared network.
  Opaque service profiles require a direct daemon connection; standalone
  backup/restore can instead deliberately override it with all four selectors:
  `--container`, `--db`, `--user` and `--db-port`.
