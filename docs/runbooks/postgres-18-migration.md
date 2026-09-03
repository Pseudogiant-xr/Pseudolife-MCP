# Runbook — Docker-tier PostgreSQL 16 → 18 migration

Applies to Docker-tier installs created before 2026-08-14 (image
`pseudolife-pg:16`). New installs and the lite tier already run
PostgreSQL 18 and need nothing from this page.

## Why this is a manual step

A PostgreSQL *major* upgrade cannot reuse the old data volume — the
on-disk format changed, so the cutover is a dump/restore, not a container
swap. Two additional traps make an ad-hoc migration risky:

- The PG 18 image moved its data layout: `PGDATA` is now
  `/var/lib/postgresql/18/docker` and the image declares
  `VOLUME /var/lib/postgresql`. Mounting the old
  `/var/lib/postgresql/data` path brings a container up *looking* healthy
  while the real cluster lands on an anonymous volume, silently orphaned on
  the next recreate.
- A restore that fails halfway must not leave the daemon writing into a
  partial bank.

`ops/migrate-pg18.ps1` (pwsh 7+, runs on Windows / macOS / Linux) performs
the full cutover with verification at every step. Do not hand-roll it.

## What the script does

1. **Preflight** — the compose tree must already say `pseudolife-pg:18`
   (run from the merged tree), the live server must be 16, and the new
   volume must not exist yet.
2. **Backup** — `ops/backup.ps1` as always: full dump + state archive.
3. **Quiesce** — stops the daemon, takes the final cutover dump, records
   exact table counts (entries, facts, world_facts, lessons, entities,
   edges, episodes, outcome_signals).
4. **Swap** — stops PG 16 (its volume is frozen and **retained** as the
   rollback), creates the new volume (default
   `pseudolife-mcp-bank-pg18`), points `ops/.env`'s
   `PSEUDOLIFE_BANK_VOLUME` at it, builds and starts `pseudolife-pg:18`.
5. **Restore** — replays the cutover dump under `ON_ERROR_STOP`.
6. **Verify** — table counts must match the quiesced counts **exactly**;
   `schema_version` and pgvector are checked; the daemon is restarted and
   health-polled.

## Run it

```powershell
# from the repo root, after pulling the PG18 tree (2026-08-14 or later)
pwsh ops/migrate-pg18.ps1
# custom volume name:
pwsh ops/migrate-pg18.ps1 -NewVolume my-bank-pg18
```

Confirm afterwards:

```bash
curl -s http://127.0.0.1:8765/health   # status ok, db ok
docker exec pseudolife-mcp-postgres psql -U pseudolife -d pseudolife_memory -t -c "SHOW server_version"
```

## Rollback

At any point after the swap step: stop the stack, restore the previous
`PSEUDOLIFE_BANK_VOLUME` in `ops/.env`, check out the pre-PG18
compose/Dockerfile, `docker compose up -d`. The PG 16 volume is never
modified or deleted by the migration.

## Known launch gotchas

Three bugs were found and fixed after this runbook was written (two during
the live cutover on 2026-08-14, `00cea780`; a third on 2026-08-25,
`a58c3c42`, where the cutover dump was guarded by gzip's exit status rather
than pg_dump's — the same `pipefail` defect as `ops/backup.ps1`'s). Run the
script only from a tree at or past `a58c3c42`.
