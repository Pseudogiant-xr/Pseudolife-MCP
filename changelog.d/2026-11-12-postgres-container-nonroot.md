### Security (2026-11-12 — nothing in the PostgreSQL container runs as root)
- The PostgreSQL container now runs as the image's `postgres` account
  (UID/GID `999:999`) with every capability dropped and `no-new-privileges`,
  so its startup bootstrap, its healthcheck and every `docker exec` the
  backup, restore and update tooling makes run without root. The existing
  data directory is already owned by that account and is used unchanged; no
  ownership is rewritten.
- The healthcheck calls the server's own `pg_isready` instead of Debian's
  Perl wrapper in `/usr/bin`, which read system account files on every probe
  (host monitors logged thousands of sensitive-file reads a day). The image
  also puts the server's client binaries ahead of the wrapper on `PATH`.
- The image no longer ships `gosu`, its only Go binary and the source of the
  Go standard-library findings image scanners reported. A root start from an
  older Compose file still works: a small `setpriv` shim performs the same
  privilege drop.
- `pseudolife-mcp restore`, `ops/restore.sh` and the Rust shadow's role setup
  no longer depend on root inside the container: the restore streams the
  backup in as the container user, `ops/restore.ps1` stages it in a
  directory that user owns and checks it can read it before anything is
  dropped, and the shadow role script carries no password (psql reads it from
  the environment). Previously a private backup copied from a Linux host
  would have been unreadable to a non-root container.
- Existing hosts pick the settings up the next time the installer runs
  (`docker compose up -d` recreates the PostgreSQL container); `pseudolife-mcp
  update` still recreates only the daemon, and every tool works with either
  container.
