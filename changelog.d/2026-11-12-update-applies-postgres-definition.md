### Fixed (2026-11-12 — `pseudolife-mcp update` now applies the release's PostgreSQL settings)
- Release-mode `pseudolife-mcp update`, attended and unattended, brings the
  PostgreSQL container in line with the release's Compose definition when it
  differs, so an existing host gets a PostgreSQL change (such as the non-root
  container) without rerunning the installer. Before, update recreated only
  the daemon and the old PostgreSQL container ran until the installer was
  rerun.
- It decides on Compose's own signal (the container's configuration hash
  against `docker compose config --hash` for the service under the release's
  files, with the stack's env file and override files), so a host already in
  line changes nothing and says so in one line. Before anything changes it
  checks that the new definition mounts the same bank volume at the same
  place and that the data directory is owned by the account it runs
  PostgreSQL as, and refuses with the fix if not.
- After the bank backup it pulls the release's PostgreSQL image, tags the
  running one for rollback, recreates only that service and waits until it
  is healthy on the same cluster (the control file's system identifier)
  before the daemon moves. If the new container does not come up, update
  recreates the previous one from its own files and image, waits for it to be
  healthy and fails with the cause; the daemon is not touched. It never runs
  `down`, removes a volume or changes ownership.
- A PostgreSQL container update does not own (another Compose project, a
  stack manager's stack, one not created by Compose, a database outside
  Docker) is left as it is, with one line saying why. Rerunning update when
  the daemon is already current still applies a PostgreSQL change, backing the
  bank up first. Checkout mode (`ops/update.ps1`, `ops/update.sh`) and the
  opt-in native updater are unchanged.
- The definition applied is the one in the running updater's own package. The
  update that installs this release runs the previous release's updater, so a
  host coming from 0.23.0 or earlier gets the PostgreSQL change on its next
  attended `pseudolife-mcp update`, or unattended with the next release.
- `--no-backup` leaves an out-of-line PostgreSQL container alone and says how
  to apply it. When only PostgreSQL moved, update checks the daemon still
  reports healthy, restarting it once if not.
