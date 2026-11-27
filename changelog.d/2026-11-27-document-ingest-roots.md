### Changed (2026-11-27 — document ingest reads from configured directories)
- `document_ingest` on the Rust daemon reads only files inside its documents
  directories: `memory.reference.ingest_roots` in `config.yaml` plus
  `PSEUDOLIFE_DAEMON_DOCUMENT_ROOTS`, which the shipped Rust Compose overlays
  set to `/documents`. Any other path, a relative path, a path with `..` or a
  link leading out of a documents directory is refused
  (`document_outside_roots`, `document_path_not_absolute`,
  `document_path_dotdot`), the same way whether or not it exists; a file
  the daemon cannot read is named as `document_unreadable`; with no
  directory configured every ingest is refused (`no_document_roots`), each
  message naming the setting and the fix. `/proc`, `/sys`, `/dev` and `/`
  are refused as documents directories when the daemon starts.
- The Docker install now has a documents folder: `~/.pseudolife-mcp/documents`
  (Windows: `%USERPROFILE%\.pseudolife-mcp\documents`), mounted read-only at
  `/documents`. The installers and `pseudolife-mcp update` create it when it
  is missing (`0755` inside an owner-only `~/.pseudolife-mcp` on Linux and
  macOS) and change nothing when it exists. Copy a file there and ingest it
  as `/documents/<name>`. A folder Docker created as root (an upgrade run by
  an older client) is given back to you automatically, in that upgrade's
  client steps or the next install or update, with a one-off container of
  the daemon image already on the host; where Docker cannot, the `sudo
  chown` that fixes it is named.
- The stack-manager file `ops/docker-compose.stack.yml` mounts no documents
  folder (a stack manager resolves `~` under its own account), so a stack
  the manager deployed refuses every ingest until `pseudolife-mcp update`
  mounts the folder.
- Upgrading: an agent that ingested files from other paths is refused until
  the file is copied into the documents folder or its directory is added to
  `memory.reference.ingest_roots` (with Docker, also mounted into the
  container).
