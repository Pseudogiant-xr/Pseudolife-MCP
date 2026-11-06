# Rust mirror: a private copy of the bank

The mirror is a Rust writer on its own copy of the bank, on the daemon host.
Nothing in the live stack changes, and no session talks to it. The design is
`docs/specs/2026-11-06-full-rust-shadow.md` (§4.2); the Compose project is
`ops/docker-compose.rust-mirror.yml`.

Run everything from a clone that is **not** the live stack's checkout, as
root on the Docker host. Keep these files private (mode 600), outside any
tracked tree:

- `mirror.env` holds `PSEUDOLIFE_MIRROR_PG_PASSWORD` (a new random value),
  `PSEUDOLIFE_MIRROR_BACKUPS` (the live `data/backups` directory, mounted
  read-only) and `PSEUDOLIFE_MIRROR_DATA` (a directory owned by uid 10001).
  It also carries the live writer's `TZ` and `PSEUDOLIFE_WRITER_ID` values.
- `secret` holds at least 32 random characters. Every replay-only
  credential derives from it.

Never copy the live `ops/.env`, and never pass a live token to the mirror.

The mirror builds nothing. It runs the images that are already present:
the live stack's `pseudolife-pg:18`, and the read-only companion's Rust
image and provisioned model volume. Start the companion once first (see
`docs/dev/run-rust-beside-python.md`).

```sh
C="docker compose -p pseudolife-mirror --env-file mirror.env -f ops/docker-compose.rust-mirror.yml"
$C up -d mirror-pg
python3 rust/daemon/harness/mirror/seed.py seed --backups <live>/data/backups --secret-file secret --record seed.json
python3 rust/daemon/harness/mirror/seed.py tokens --secret-file secret --out tokens.env
python3 rust/daemon/harness/mirror/seed.py config --backups <live>/data/backups \
    --backup <the seeded backup> --out <PSEUDOLIFE_MIRROR_DATA>/config.yaml   # needs PyYAML
$C --env-file tokens.env up -d mirror-daemon
python3 rust/daemon/harness/mirror/seed.py health --schema <schema_version from seed.json>
```

- **Seeding:** `seed` restores into `pl_cf_mir_seed` and rewrites its
  credentials. It then clones the result as `pl_cf_mir_bank`, the daemon's
  database. Harnesses may clone `pl_cf_mir_seed` while it is idle.
- **Reseeding:** stop `mirror-daemon` first; `seed` refuses while it runs.
- **Refusals:** every refusal exits 3 and says what to fix. Nothing has
  changed when it does.
- **Stopping:** `$C stop` stops the mirror. Never run `down -v` here or on
  the live project.
