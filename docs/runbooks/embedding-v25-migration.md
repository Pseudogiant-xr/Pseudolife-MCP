# Embedding backbone v25 migration — operator runbook

One-time, human-gated cutover of a live bank from schema v24
(`vector(384)`, `all-MiniLM-L6-v2`) to v25 (`vector(1024)`,
`Qwen/Qwen3-Embedding-0.6B`). This is a **user-gated morning step**, not
part of the overnight PR — do not run it unattended. Full design
rationale: `docs/superpowers/specs/2026-07-28-embedding-backbone-v25-design.md`.

## Step 0 — ChromaDB reference-bank pre-flight

`ops/migrate_embeddings.py` only touches Postgres (`entries` / `facts` /
`world_facts` / `lessons`). The ChromaDB **reference bank** (`document_ingest`
/ `document_search`, path `/data/chromadb` inside the daemon container) is a
separate embedding store this migration does not cover.

```
docker exec pseudolife-mcp-daemon ls -la /data/chromadb
```

- **Empty or missing** — nothing to migrate; proceed.
- **Has data** — **STOP and reassess before continuing.** A Chroma
  collection pins its embedding dimension at creation; documents ingested
  under MiniLM's 384-d vectors don't become 1024-d just because the daemon's
  default embedder changes, and `document_search` would start comparing
  384-d query vectors against a 384-d collection using whatever embedder is
  live at query time — silently wrong once the query embedder is Qwen3. This
  runbook has no answer for a populated reference bank; decide (recreate the
  collection and re-ingest, or leave the sidecar on the old model) before
  going further.

## Step 1 — merge the PR

Land the branch on `master` first. `ops/update.ps1` (step 8 below) builds
the daemon image from whatever `ops/Dockerfile.daemon` + `pyproject.toml`
say on the branch it's run from — running it against an unmerged branch
would deploy code nobody else can reproduce from `master`.

## Step 2 — back up, and verify both files landed

```powershell
pwsh ops\backup.ps1
```

Confirm the output lists **both** artifacts before continuing:

- `pseudolife_memory-<stamp>.sql.gz` — the Postgres dump (`entries` /
  `facts` / `world_facts` / `lessons`, the four tables this migration
  rewrites).
- `pseudolife_state-<stamp>.tgz` — the daemon **state volume** (ingested
  `document_ingest` files, cortex/graph snapshots).

The migration's rollback story is "restore the pre-migration pg_dump" —
that's only true if the dump in this pair actually exists and is recent.
A backup run that silently wrote only one of the two files is not a backup
you can roll back from.

## Step 3 — stop the daemon

```powershell
docker stop pseudolife-mcp-daemon
```

`ops/migrate_embeddings.py --apply` re-embeds every row and rewrites the
`embedding` column type underneath the daemon's own in-memory state; a live
writer racing the migration corrupts the bank. The daemon must be fully
stopped before `--apply`, not just quiescent.

## Step 4 — verify it's actually stopped, via `docker ps`, not the health probe

```powershell
docker ps --filter name=pseudolife-mcp-daemon
```

**Do not** use `curl http://127.0.0.1:8765/health` (or anything that hits
the same port) to confirm the daemon is down. On this host, a closed
loopback port **times out** instead of refusing the connection, and the
migration script's own reachability check (`_daemon_reachable` in
`ops/migrate_embeddings.py`) treats a timeout as "answers" — deliberately
fail-safe, because a socket that accepts the TCP connection but never
responds is a daemon that's up and hung, not one that's absent. The
consequence: after a genuine stop, the health probe can still *look*
reachable, and `--apply` without `--assume-daemon-stopped` would then
correctly (if confusingly) refuse. `docker ps` showing no running
container is the only trustworthy stopped-check on this host.

## Step 5 — dry run

```
python ops/migrate_embeddings.py
```

Dry-run is the default (no `--apply`): it prints the per-table row counts
and current-vs-target dimension and writes nothing.

## Step 6 — review the plan output

Check the printed row counts against what you expect for this bank (the
live bank is roughly ~2.4k facts + ~500 entries + world facts + lessons,
≈4k texts total). A count wildly off from that is a signal `--dsn` (or the
`PSEUDOLIFE_MCP_DATABASE_URL` it defaults from) is pointed at the wrong
database — catch that here, before anything is written.

## Step 7 — apply

```
python ops/migrate_embeddings.py --apply --backup-verified --assume-daemon-stopped
```

Expect roughly 2-4 minutes for a ~4k-text bank on CPU. Both flags are
required and both are gates, not conveniences:

- `--backup-verified` — asserts step 2 actually happened; the script
  refuses `--apply` without it.
- `--assume-daemon-stopped` — skips the health-endpoint check described in
  step 4. Only pass this **after** confirming via `docker ps` (step 4) that
  the daemon is genuinely down — the flag exists precisely because this
  host's fail-safe health probe can't tell "stopped" from "up and hung," so
  something has to positively assert it instead.

Each of the four tables (`facts`, `world_facts`, `lessons`, `entries` — in
that order, entries deliberately last) migrates in its own transaction;
`SCHEMA_META_VERSION` is stamped 25 only after all four succeed. If it
fails partway, already-migrated tables stay committed and the daemon's
dimension guard (below) will keep refusing to boot until you re-run this
step — it is designed to fail loud, not to leave a half-migrated bank
silently in service.

## Step 8 — deploy the new image

```powershell
ops\update.ps1 -Tag pre-v25-embedding
```

This builds the new daemon image (which bakes `Qwen/Qwen3-Embedding-0.6B`
alongside `all-MiniLM-L6-v2`, per the merged Dockerfile) and restarts only
the daemon container (`--no-deps` — Postgres and the extractor sidecar are
untouched). `-Tag pre-v25-embedding` names the rollback image tag `update.ps1`
stamps on the *current* (pre-deploy) image before rebuilding — that tag is
the rollback anchor referenced below.

This is also where a failed or incomplete migration surfaces, loudly:
`ensure_schema`'s dimension guard
(`_refuse_on_embedding_dim_mismatch`) refuses to let the daemon finish
constructing storage against a bank whose `entries.embedding` isn't
`vector(1024)`. If step 7 silently missed a table, the daemon does not boot
healthy and serve half-migrated data — `update.ps1`'s own health wait fails
and prints the rollback commands. A RuntimeError naming
`ops/migrate_embeddings.py` and both dimensions in `docker logs
pseudolife-mcp-daemon` means: stop here, do not retry blindly, re-check
step 7's output first.

## Step 9 — verify live

- **`memory_search` end-to-end** — issue a real query through the MCP
  client and confirm results come back (not just that `/health` reports
  `ok`; `/health` never constructs storage eagerly, so a healthy response
  is necessary but not sufficient).
- **A fact write with no `freshness_class`** — confirms the entity-kind
  freshness inference (schema v24) still resolves correctly after a fresh
  daemon restart, since the entity-kind map is cached for the life of the
  process and this restart just built a new one.
- **Grep the daemon log for the embedding backend line**:

  ```
  docker logs pseudolife-mcp-daemon | grep "Embedding backend:"
  ```

  Expect `Embedding backend: torch (model=Qwen/Qwen3-Embedding-0.6B, dim=1024, device=cpu)`
  — this is the positive confirmation that the live daemon actually loaded
  the new backbone (Qwen3-Embedding-0.6B has no ONNX export, so `torch` is
  the correct backend here, not a fallback failure).

## Rollback

There is no in-place downgrade: a `vector(1024)` column cannot be cast back
to `vector(384)` with the old values intact (they're discarded by the
`USING NULL` cast at migration time). If anything above fails or the
re-embedded bank looks wrong:

1. Restore the pre-migration `pg_dump` from step 2
   (`pseudolife_memory-<stamp>.sql.gz`) into the live database.
2. Restore the pre-v25 image tag `ops/update.ps1` captured in step 8:
   ```powershell
   docker tag pseudolife-daemon:<version>-pre-v25-embedding pseudolife-daemon:<version>
   docker compose -f ops\docker-compose.yml up -d --no-deps pseudolife-daemon
   ```

Do not attempt to "fix forward" a partially-migrated bank by hand.
