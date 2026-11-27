### Changed (2026-11-27 — release downloads stay on HTTPS, and the daemon spawn lock is private to its account)
- `pseudolife-mcp update`'s Compose download and the native client's archive
  download refuse a redirect from HTTPS to plain HTTP. Only an explicit
  `http://` `PSEUDOLIFE_RELEASE_BASE_URL` or `PSEUDOLIFE_NATIVE_ARCHIVE_URL`
  (a loopback fixture or a trusted mirror) still fetches over HTTP.
- On Linux and macOS the native client takes its daemon spawn lock in
  `~/.pseudolife-mcp/spawn-locks`, a folder only its account can use (made
  only when the client starts a daemon, not when one already answers), so
  another account on the host can no longer hold the shared `/tmp` lock and
  stall the daemon start until it times out (Windows' `%TEMP%` is already
  per-user, so its lock is unchanged). The temporary-folder lock older clients
  take is still held beside it when no other account can hold it (owned by
  this account, owner-only, one link, no symlink), so two client versions
  starting at once still start one daemon. A `/tmp` file older clients left
  readable by others is skipped instead; if both versions then start a daemon,
  the second fails to bind the port and the first one serves.
- On Windows the tunnel profile reader opens a profile without following a
  link swapped in after its path check, as the credential reader already did.
