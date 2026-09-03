# Embedding backbone v25 migration — operator runbook

One-time, human-gated cutover of a live bank from schema v24
(`vector(384)`, `all-MiniLM-L6-v2`) to v25 (`vector(1024)`,
`Qwen/Qwen3-Embedding-0.6B`). This is a **user-gated morning step**, not
part of the overnight PR — do not run it unattended. Full design
rationale: `docs/superpowers/specs/2026-07-28-embedding-backbone-v25-design.md`.

## Step 0a — you need a Python environment, and Docker-only installs do not have one

`ops/migrate_embeddings.py` runs **on the host**, not in a container: `ops/`
is not copied into the daemon image (`ops/Dockerfile.daemon` copies only
`pseudolife_memory`, `pyproject.toml` and the lockfile), so there is nothing
to `docker exec`. The script imports `psycopg`, `pgvector` and
`pseudolife_memory`, and on `--apply` it also loads the embedding model,
which the *image* bakes but the host does not.

That is fine on a development checkout with `.venv`. It is a genuine gap for
the install the README leads with — `git clone` plus `ops/install.sh`, which
provisions Docker and no Python environment at all. If that is you, you must
either build a host environment (`pip install -e .` in a venv, plus the
~1.2 GB model download on first `--apply`), or run the script through the
daemon image with the repo's `ops/` bind-mounted, which has the dependencies
and the baked model already.

> **Unverified:** the bind-mount route is sound in principle — the image
> carries every import the script needs — but it has not been executed
> end-to-end, so it is written here as a direction, not a recipe. Verify it
> on a scratch bank before trusting it with a real one.

## Step 0 — ChromaDB reference-bank pre-flight

`ops/migrate_embeddings.py` only touches Postgres (`entries` / `facts` /
`world_facts` / `lessons`). The ChromaDB **reference bank** (`document_ingest`
/ `document_search`, path `/data/chromadb` inside the daemon container) is a
separate embedding store this migration does not cover.

```
docker exec pseudolife-mcp-daemon ls -la /data/chromadb
```

- **Empty or missing** — nothing to migrate; proceed. Note that the
  `reference_bank` collection existing is not by itself "has data": a Chroma
  collection pins its embedding dimension on its **first add**, not at
  creation, so an empty collection carries `dimension = NULL` and will simply
  pin itself to 1024 on the first post-migration ingest. Check the row count,
  not the collection's existence:

  ```
  docker exec pseudolife-mcp-daemon python -c "import sqlite3; c = sqlite3.connect('/data/chromadb/chroma.sqlite3'); print(c.execute('select name, dimension from collections').fetchall()); print(c.execute('select count(*) from embeddings').fetchone())"
  ```

- **Has data** (a non-zero embedding count / a non-NULL `dimension`) —
  **STOP and reassess before continuing.** Documents ingested under MiniLM's
  384-d vectors don't become 1024-d just because the daemon's default embedder
  changes, and both `document_ingest` and `document_search` use the daemon's
  live embedder — so after the swap the reference bank is queried with **1024-d**
  vectors against a collection pinned at 384. That failure is **loud, not
  silent**: `search_documents` hands the query vector straight to Chroma with
  no `try`/`except`, so Chroma raises a dimension-mismatch error and the
  `document_search` call fails outright (the same applies to the next
  `document_ingest`). Loud is better than wrong, but it is still a broken
  tool until you act. This runbook has no answer for a populated reference
  bank; decide (recreate the collection and re-ingest, or leave the sidecar on
  the old model) before going further.

## Step 0b — STOP if you have customised `embedding:` in `config.yaml`

**The migration script does not read your config.** `_build_pipeline()` in
`ops/migrate_embeddings.py` constructs a bare `EmbeddingConfig()` — stock
dataclass defaults, nothing else. It never looks at `PSEUDOLIFE_MCP_CONFIG`
or the `embedding:` block of a `config.yaml`. The **daemon does** honour that
block (`utils/config.py`, `load_config`).

So if you have overridden anything under `embedding:` — `model_name` above
all, but `query_prefix` matters just as much for an instruct-style embedder —
this migration will re-embed the entire bank with the **stock** model/prefix
while the live daemon keeps querying with **yours**. Both sides are 1024-d, so
every guard in this runbook passes, the daemon boots healthy, `memory_search`
returns results, and the results are junk. There is no error to grep for: it
is a silent, total loss of retrieval quality across the whole bank, and the
only fix is to restore and re-run.

Check before you go further. The daemon reads `$env:PSEUDOLIFE_MCP_CONFIG` if
set, otherwise `config.yaml` in its working directory; **no file at all means
stock defaults, which is the safe case**:

```powershell
$cfg = if ($env:PSEUDOLIFE_MCP_CONFIG) { $env:PSEUDOLIFE_MCP_CONFIG } else { "config.yaml" }
if (Test-Path $cfg) { Get-Content $cfg | Select-String -Pattern 'embedding:' -Context 0,12 }
else { "no config file at $cfg - stock EmbeddingConfig defaults, nothing to reconcile" }
```

If the `embedding:` block sets `model_name` or `query_prefix`, do **not** run
the migration as written — either temporarily align the stock defaults with
your override, or patch `_build_pipeline()` to load your config, before
Step 5.

**Also note the model is only loaded on the `--apply` path**, after every
refusal gate has cleared. The dry run (Step 5) therefore proves nothing about
whether the embedder can actually load: the container bakes
`Qwen/Qwen3-Embedding-0.6B`, but the **host** does not, and this script runs
on the host. If the host's HuggingFace cache lacks the model, the first
failure you see will be during `--apply`, mid-migration. Pre-warm it (or
confirm the cache) before Step 7 if the host has never fetched that model.

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

```powershell
.venv\Scripts\python.exe ops\migrate_embeddings.py
```

Use the repo virtualenv's interpreter explicitly, not a bare `python` — the
script imports `psycopg`, `pgvector` and `pseudolife_memory`, and on a typical
dev host a bare `python` resolves to some other interpreter that has none of
them. The failure is an immediate `ModuleNotFoundError`, harmless here but
worth not debugging at 2am.

Dry-run is the default (no `--apply`): it prints the per-table row counts
and current-vs-target dimension and writes nothing.

## Step 6 — review the plan output

The point of this step is catching a wrong `--dsn` (or the
`PSEUDOLIFE_MCP_DATABASE_URL` it defaults from) **before** anything is
written. Don't compare against a memorised bank size — banks grow, and a
stale expectation raises false alarms. Read the *shape* of the output
instead:

- **A different database entirely** — the tables are missing, or the row
  counts are zero/near-zero across all four. `entries` and `facts` should both
  be substantial and `facts` is normally the largest; four empty tables on a
  bank you have been using for months is not a small discrepancy, it is the
  wrong DSN.
- **An already-migrated or fresh bank** — every table already reports
  `vector(1024)` and the script exits with "Nothing to do".
- **Counts in the right order of magnitude but off from your notes** — that is
  just drift since you last looked; `docker exec pseudolife-mcp-postgres psql
  -U pseudolife -d pseudolife_memory -c "SELECT count(*) FROM facts"` against
  the live bank settles it in one command if you want certainty.

## Step 7 — apply

```powershell
.venv\Scripts\python.exe ops\migrate_embeddings.py --apply --backup-verified
```

Expect roughly 2-4 minutes for a ~4k-text bank on CPU.

- `--backup-verified` is **required**: it asserts step 2 actually happened,
  and the script refuses `--apply` without it. There is no downgrade path, so
  this gate is not a formality.
- `--assume-daemon-stopped` is **optional, and off by default for good
  reason** — it skips the health-probe gate entirely. Add it only when the
  probe misfires:

  ```powershell
  .venv\Scripts\python.exe ops\migrate_embeddings.py --apply --backup-verified --assume-daemon-stopped
  ```

  Run without it first. If the script refuses with "the daemon answers
  `<health-url>` — or the port neither answered nor refused", and `docker ps`
  (step 4) shows no running daemon container, then you are on a host where a
  closed loopback port **times out** rather than refusing, and the fail-safe
  gate is reading that timeout as "up". That is the case on this deploy host.
  Re-run with the flag to positively assert what `docker ps` already told you.
  If the probe refuses cleanly and the script proceeds, you never needed the
  flag — and passing a gate-skipping flag you don't need is how someone
  eventually migrates a bank underneath a live daemon.

Each of the four tables (`facts`, `world_facts`, `lessons`, `entries` — in
that order, entries deliberately last) migrates in its own transaction;
`SCHEMA_META_VERSION` is stamped only after all four succeed — the script
stamps whatever the constant currently is (v37 as of this writing), not
literally 25. If it
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

Since 2026-08-25 `update.ps1` will **refuse** to move that tag when the
running daemon's image is not the one the version tag points at (a build
that ran without a completed deploy) — it warns loudly, keeps the existing
rollback tags, and prints rollback instructions that match what actually
exists. If you see that refusal, do not proceed with the migration until
you know which image is the last-good one; `-ForceRollbackTag` overrides it
only when you are sure.

**That anchor expires, silently, in two more deploys.** `update.ps1` step 2b
calls `ops/prune-rollbacks.ps1`, which matches *every* tag containing `-pre-`
— including this hand-named one — and keeps only the newest `-KeepRollbacks`
(default **2**). Nothing exempts a deliberately-named rollback tag. If you
want this image to survive past the next couple of deploys, pin it outside
that naming scheme or export it now:

```powershell
docker tag pseudolife-daemon:<version>-pre-v25-embedding keep-pre-v25:manual
# or, to survive an image-store wipe entirely:
docker save -o D:\backups\pre-v25-embedding.tar pseudolife-daemon:<version>-pre-v25-embedding
```

A failed or incomplete migration surfaces here — but **not in
`update.ps1`'s "Healthy" line, which you must not trust for this**.
`ensure_schema`'s dimension guard
(`_refuse_on_embedding_dim_mismatch`) refuses to let the daemon construct
storage against a bank whose `entries.embedding` isn't `vector(1024)` —
but that refusal fires in the WARMUP thread a few seconds after `/health`
starts answering `ok`, and `update.ps1` breaks on the first `ok` it sees.
Against a half-migrated bank, step 8 will very likely still print
`==> Healthy`. So after update.ps1 finishes, ALWAYS run both of:

```bash
sleep 20 && curl -s http://127.0.0.1:8765/health
```
— must show `"status": "ok"` with **no `init_refusal` key**; `"degraded"`
plus `init_refusal` means the guard fired. And:

```bash
docker logs pseudolife-mcp-daemon 2>&1 | grep -E "warmup init failed|Refusing to start"
```
— any hit means: stop here, do not retry blindly, re-check step 7's
output first. Step 9's end-to-end `memory_search` is the true acceptance
gate; this step only proves the container runs.

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

**Order matters — image first, then database.** `ops/restore.ps1 -Apply`
restarts the daemon and waits for it to report healthy as its last step. If
the v25 image is still the deployed one when you restore the 384-d dump, that
restart brings up a v25 daemon against a 384-d bank, `ensure_schema`'s
`_refuse_on_embedding_dim_mismatch` fires, and the restore reports failure at
the end of an otherwise-correct restore. Re-tag first and the daemon that
comes back up is the pre-v25 one, which is the daemon that matches the data.

1. Restore the pre-v25 image tag `ops/update.ps1` captured in step 8:
   ```powershell
   docker tag pseudolife-daemon:<version>-pre-v25-embedding pseudolife-daemon:<version>
   docker compose -f ops\docker-compose.yml up -d --no-deps pseudolife-daemon
   ```
2. Restore the pre-migration `pg_dump` from step 2
   (`pseudolife_memory-<stamp>.sql.gz`) into the live database — via
   `ops\restore.ps1 -Apply`, which stops the daemon, reloads the dump, and
   restarts it.

Do not attempt to "fix forward" a partially-migrated bank by hand.
