### Changed (2026-10-31 — the conductor role is stored under its own name)
- The maintainer's board role, called "conductor" since 2026-10-30,
  is now stored that way too: a grant is the lease `conductor:<project>`
  recorded as `lease_conductor`, its rings carry `reason: conductor`, a
  conductor claiming its project's coordinator lease is refused
  `already_conductor`, the Console's signed purposes are `grant-conductor`
  and `revoke-conductor`, and the maintainer status's role slots are
  `{"conductor", "coordinator"}`. Python daemon, Rust daemon and shim, and
  the Console change together.
- Nothing to migrate by hand. A grant made before the upgrade
  (`delegate:<project>`, `lease_delegate`) keeps its authority until it
  expires (at most 7 days) or the project is granted again, which moves the
  role to `conductor:<project>` and frees the old lease in the same change
  (`lease_break`, reason `renamed`); the Console's revoke frees either name.
  Both names stay reserved. Wake rows recorded as `delegate` still count
  against the authority budget and are still served, and a Console tab
  loaded before the upgrade still grants and revokes (its challenge keeps
  the old purpose it asked for). Before deploying, `lease break` any
  `conductor:` lease `pseudolife-mcp lease list` shows held, since any
  session could take one until now, and run `lease delegate` from a client
  at the daemon's version.
- `pseudolife-mcp conductor-watch` is the watcher's command and
  `pseudolife_memory.conductor_watch_cli` its module; `delegate-watch` still
  runs it after one stderr line naming `conductor-watch`. The `lease
  delegate` command keeps its name; its help names the new lease.
