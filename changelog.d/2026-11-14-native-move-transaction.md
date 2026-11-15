### Added (2026-11-14 — the native client moves a bank, and rolls back a move that failed)

- `pseudolife-mcp move --to <ssh-target> --target-url <url> --no-keep-tokens`
  in the native (Rust) client now moves a Docker bank to another host's
  installed bank, after asking (or `--yes`). In order, it:
  - claims the target and pauses both hosts' unattended updates and restart
    policies;
  - stops the source, makes its database read-only for new sessions, and
    takes its final backup;
  - fences the source database;
  - copies the backup over ssh with its SHA-256;
  - gates and restores the target, and checks its identity and row counts;
  - starts it once and waits for `--target-url` to serve the moved bank;
  - writes `/data/moved.json` into the stopped source (the commit point);
  - points this machine's clients at the target.
- Before the commit point a failure or an interrupt rolls back. The source
  serves again only after the target has been proved stopped and gated. A
  target that cannot be proved so (a lost ssh link) leaves the source stopped
  and fenced, with exit 6 and four facts: which daemon runs, whether the bank
  is served, where the data is, and the next command. Writes the target took
  while it served (rows added or removed, and rows changed in place, from its
  write counters) are named and kept in its gated bank (also exit 6), and a
  later `--resume` never replaces them.
- `pseudolife-mcp move --rollback <move id>` runs the same rollback from a
  move's record (`~/.pseudolife-mcp/moves/<move id>/`) after the move process
  died without it. Every later `move` on that host refuses, naming that
  command, until it has run. If the move died past its commit point, it lists
  what the move still had to do (exit 5).
- `--resume` retries a rolled-back move whose restore ran on the target. It
  does so only while that bank is still gated, with the row counts and write
  counters the rollback read. Otherwise it refuses before any change, naming
  `--release-target`.
- `pseudolife-mcp move --release-target <move id>` gives such a target back
  its own bank, from the safety backup the move's first restore took of it,
  and frees it for a new move. The moved bank, with any writes it took there,
  is kept in that restore's own safety backup, which the output and `--json`
  name. It refuses while this host's daemon is not serving, and each of its
  steps is safe to repeat.
  While such a target holds a copy of this host's bank, no other move starts
  from this host; the refusal names the commands that work for that move. A
  target that no longer holds anything of the move is found so and released.
  `--release-target <move id> --target-gone` gives up a target that is gone
  for good, on this host only.
- The target's half runs in its native client (move protocol 2): each
  operation is safe to repeat and is journalled on the target. Each change is
  refused unless the target's claim names the move, so a second move into the
  same target is refused, whatever `--target-work-dir` each names. A target
  serving without authentication, or a daemon whose data directory is not
  `/data` (where the move's gate files work), is refused before any change.
  Both hosts keep the move's files until a person removes them.
