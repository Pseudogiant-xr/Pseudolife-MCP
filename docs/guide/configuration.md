# Configuration

Every knob the daemon reads — environment variables, the tuned built-in
defaults, toolset tiers, the stdio shim, LAN sharing, data layout, and
backups. Part of the [user guide](../../README.md#documentation).

## Connection / deployment env vars

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATABASE_URL` | _(unset → file mode)_ | Postgres DSN; when set, PG is the source of truth (schema v22). Unset → v0.1 file-only mode. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `http://127.0.0.1:8765` | Daemon the shim connects to (and auto-starts). |
| `PSEUDOLIFE_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | Daemon bind address. |
| `PSEUDOLIFE_MCP_TOKEN` | _(unset)_ | Bearer token; **required** to bind a non-loopback host. |
| `PSEUDOLIFE_MCP_TRUST_BIND` | _(unset)_ | Set `1` to allow a non-loopback bind without a token when the boundary is external (containerized, loopback-published). The compose daemon sets this; never set it for a host daemon. |
| `PSEUDOLIFE_MCP_DATA_DIR` | `./data` (cwd-relative) | Weights cache + legacy-migration source + ChromaDB. |
| `PSEUDOLIFE_MCP_CONFIG` | `<data_dir>/config.yaml` if present, else built-ins | Override MIRAS / embedding / memory config. |
| `PSEUDOLIFE_WRITER_ID` | `unknown` | Identifies this writer on every canonical write (schema v11). The shim forwards it as the `X-PL-Writer` header; the compose daemon defaults to `mcp-client`, and the installer pins `claude-code` / `codex` / `mcp-client` in `ops/.env` per the selected `--client`. Existing installs that predate the client selector should set `PSEUDOLIFE_WRITER_ID=claude-code` in `ops/.env` to keep their writer identity (and any `PSEUDOLIFE_MCP_TIER_MAP` keyed on it) stable. |

For the Docker stack, set these in `ops/.env`
(`cp ops/.env.example ops/.env` — the install/update scripts scaffold it too;
every value is commented, a missing file runs entirely on defaults). The
dream-extractor variables (`PSEUDOLIFE_DREAM_*`) are covered in
[Dreaming](dreaming.md).

## Built-in defaults (tuned for Claude's use case)

- **Surprise threshold `0.0`** — the v0.5 store gate measures *novelty*
  (`1 − max cos` to existing entries). Claude stores deliberately, so the
  gate stays permissive (store everything; novelty still drives
  eviction/promotion scoring). Raise it above zero to dedup near-duplicate
  stores.
- **Meta-filter off** (`memory.meta_filter.enabled = false` in the MCP
  build) — the filter exists to drop auto-captured chat noise ("I don't
  have anything saved about that"); every MCP store is a deliberate tool
  call, and the filter's patterns collided with legitimate dev facts
  about memory systems themselves.
- **Recency base half-life 24h** (`memory.recency_base_half_life_s =
  86400`, vs the 1h chat default) — Claude Code sessions are hours-to-
  days apart; with a 1h half-life the recency boost was effectively
  always zero. Halves per band depth as before (1d → 2d → 4d → …).
- **MIRAS preset `continuum`** — the 8-tier `working / micro / instant /
  fast / medium / slow / archival / forever` continuum. Bands are plain
  cosine vector stores (v0.5); a band spec is capacity + consolidation
  cadence + promotion thresholds + an eviction policy.
- **No NLI scorer** — the `cross-encoder/nli-deberta-v3-xsmall`
  contradiction model is ~278 MB and optional. The four-path detector
  works without it. Install with `pip install .[nli]` if you want it.
- **Cross-encoder reranker off** — wired into the pipeline but disabled by
  default; enable globally (`memory.reranker.enabled = true`) or per-call
  (`memory_search(..., rerank=True)`). Details: [Retrieval](retrieval.md#cross-encoder-reranking).
- **BM25 hybrid lexical pool off** — a pure-stdlib sparse-retrieval channel
  that rescues exact-keyword queries; flip via `memory.bm25.enabled = true`
  or per-call `bm25=True`. Details: [Retrieval](retrieval.md#bm25-hybrid-retrieval).
- **Abstention off** (`memory.search_confidence_floor = 0.0`) — set it
  above zero and `memory_search` returns `low_confidence: true` whenever
  the top match scores below the floor. Calibrated as a pair with
  `memory.cortex.guard_min_score`; the recommended abstention-on values
  and the calibration story: [Retrieval](retrieval.md#abstention--confidence-floors).
- **Dream slot resolver off** (`memory.cortex.dream_slot_match_threshold =
  0.0`) — a positive cosine floor lets the dream pass map a paraphrased
  `(entity, attribute)` onto an existing slot before writing, to catch
  small-model supersession forks. ⚠️ Calibration found **no measurable
  benefit** on the benchmark (stale-leak flat; a false-merge at `0.80`):
  the residual fragmentation comes from the deterministic regex
  auto-promote, not paraphrase. Left off; enable only with the
  false-merge risk in mind. See
  [the single-writer cortex design](../specs/2026-06-19-single-writer-cortex-design.md)
  for the structural fix.
- **No HyDE / no reflection** — both rely on an LLM callback. Claude *is*
  the LLM, so the natural way to reflect is for Claude to call
  `memory_store` with a self-composed summary.
- **Auto-outcome inference on** (`memory.lessons.infer_outcomes = true`) —
  a session episode that closes with entries but zero `memory_outcome`
  calls gets up to `memory.lessons.infer_outcomes_max_signals` (default
  `3`) signals inferred from its own record on the end-of-session dream;
  see [Episodes](episodes.md#inferred-outcomes-at-session-close). Set
  either to `false` / `0` to turn it off.
- **Dream edge quarantine on** (`memory.dream.relation_quarantine_below =
  0.5`) — dream-extracted graph edges scoring below the floor are filed as
  review proposals (`source="dream-low-confidence"`) instead of entering
  the live graph. At the default this catches exactly the untyped
  `related-to` co-mention edges (confidence 0.45); typed relations (0.70)
  write live as before. Set `0.0` to disable and restore write-live
  behavior.

## Toolset tiers

Three visibility tiers — `minimal` (7 tools: the recall/capture loop + the
gate), `core` (20: + graph/recall, world facts, lessons, documents,
episodes), `full` (33) — filtered per session at `tools/list`. The filter is
visibility, not auth (the bearer token is the security boundary) — but
Claude clients gate calls against their own tool list, so in practice a
session expands its tier before calling a hidden tool. Defaults:
`PSEUDOLIFE_MCP_TOOLSET` (shipped: `core`) sets the baseline;
`PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal,claude-code:core"` sets
per-client defaults by writer id. Any session can step its own tier up or
down at runtime with `memory_toolset(action="expand"|"collapse"|"status")`
— the daemon emits `tools/list_changed` so the client refreshes its list.
Eager-loading clients (Claude Desktop) start at ~1.5k tokens of manifest on
`minimal`; clients that defer schemas client-side (Claude Code) barely
notice tiers at all.

**Weak-model deployments:** set `PSEUDOLIFE_MCP_TOOLSET=core` — it exposes
the curated core set and hides the power/hygiene tools (`memory_forget`,
`memory_relation_define`, `memory_dream`, `memory_graph_review`, …) that a
small model can misuse.

## Host-process install (Windows, for GPU / dev)

Runs Postgres in Docker but the daemon on host Python. Use this if you
want to hack on the daemon or run the embedder on a local GPU. Requires
Python 3.10+, Docker Desktop, and ~600 MB of disk.

```powershell
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
python -m venv .venv
.venv\Scripts\activate
pip install -e .

# 1. Start Postgres 16 + pgvector (one-time build, then persistent).
docker compose -f ops/docker-compose.yml up -d --build pseudolife-pg

# 2. Register the daemon to auto-start at logon (binds 127.0.0.1:8765).
ops\install-autostart.ps1
Start-ScheduledTask -TaskName "Pseudolife-MCP Daemon"
```

The `pseudolife-mcp` console-script is now on your PATH — run
`pseudolife-mcp --help` for all modes. The main ones: `pseudolife-mcp serve`
(the daemon), `pseudolife-mcp` (the stdio shim — auto-starts the daemon if
absent), `pseudolife-mcp embedded` (the v0.1 in-process stdio server; no
daemon, no Postgres — an escape hatch), and `pseudolife-mcp briefing`
(print the session-start briefing; used by the hook).

## stdio shim (per-session identity)

The installer wires this by default (`ops/install.sh` / `ops/install.ps1`;
pass `--transport http` / `-Transport http` to opt out) because it's the
mechanism that gives **concurrent** Claude Code sessions distinct identity —
a per-process `X-PL-Session` header, the strongest of the five
[session-identity](#session-identity) tiers. The shim works against
**either** daemon deployment, host-process or the containerized stack — it's
just an HTTP client to `PSEUDOLIFE_MCP_DAEMON_URL` and only spawns a new host
daemon when nothing answers there already. Point Claude Code at it directly:

```json
{
  "mcpServers": {
    "pseudolife-memory": {
      "command": "C:\\path\\to\\Pseudolife-MCP\\.venv\\Scripts\\pseudolife-mcp.exe",
      "env": {
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
        "PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
        "PSEUDOLIFE_MCP_DATA_DIR": "${USERPROFILE}\\.pseudolife-mcp"
      }
    }
  }
}
```

Replace `C:\path\to\Pseudolife-MCP` with wherever you cloned the repo. The
`PSEUDOLIFE_MCP_DATABASE_URL` matches the bundled `ops/docker-compose.yml`
defaults (user/password `pseudolife`, host port `5433`) — change it only if
you edit the compose file or override the password. The default password is
safe for the stock loopback-only stack (nothing off-box can reach Postgres);
to use your own anyway, set `POSTGRES_PASSWORD` in `ops/.env` **before the
first launch** (see the note in `ops/docker-compose.yml` for changing it
later).

The shim is torch-free, so sessions attach near-instantly; the daemon pays
the one-time embedder warmup once for everyone. On first run with a v≤0.1
`cms_state.pt` present in `PSEUDOLIFE_MCP_DATA_DIR`, the daemon
auto-migrates it into Postgres and renames the originals `*.pre-v8.bak`
(never deletes them).

## Session identity

Every request resolves "which session/episode does this write belong to"
through one chokepoint, evaluated in strict precedence order:

| tier | source | scope | notes |
|---|---|---|---|
| 1 | `X-PL-Session` header | per shim process = per session | the stdio shim sends this on every call; any integrator can |
| 2 | explicit `episode` argument | per call | pass an open episode id (or its unambiguous ≥8-char prefix) on `memory_store` / `memory_outcome` / `memory_fact_set`; the daemon mints it and advertises it in the SessionStart briefing |
| 3 | hook-registered active session | machine-scoped pointer | the SessionStart hook forwards Claude Code's own `session_id`; a SessionEnd hook closes it |
| 4 | `mcp-session-id` header | per connection | legacy fallback — the MCP 2026-07-28 revision (SEP-2567, "Sessionless") removes this header and protocol sessions entirely, so treat this tier as a dead end, not something to build on |
| 5 | none | — | writer id + idle-gap sessionization (the reaper) — the documented floor when nothing above resolved |

**Why the header outranks the handle when both are present.** A shim
header is infrastructure-asserted per OS process; an `episode` handle is
model-supplied and can be confused between two concurrent sessions'
briefings. But identity and target episode are separable — a write still
lands in the handle's named episode even when the header wins identity for
stamping. An unknown, closed, or ambiguous handle never fails the write —
it degrades to the next tier and the result carries
`"episode_warning": "unknown or closed episode handle"`.

**Tier 3's limitation.** The active-session pointer is one machine-scoped
value, last-start-wins: whichever SessionStart hook fired most recently
owns it until its own SessionEnd clears it (or a later SessionStart
overwrites it). Two concurrent sessions that are both *unheaded* (no shim)
and *handle-less* (no `episode` argument) still misattribute to the newer
one — tiers 1 and 2 are the actual concurrency answer, not tier 3. Accepted
as YAGNI until a real multi-writer/LAN deployment needs a per-writer
pointer.

This cuts across clients, not just across Claude Code sessions: because the
pointer is machine-scoped, a **second client that sets no identity of its
own** — e.g. Codex or a ChatGPT connector talking to the daemon over direct
HTTP with no shim, no hook, and no `episode` argument — resolves at tier 3
to whatever session the Claude Code hook last registered, so its writes are
attributed to Claude's session episode. The fix is the same as for
concurrent sessions: give the second client a tier-1 identity (run it
through the stdio shim) or pass explicit tier-2 `episode` handles on its
writes. The installer's shim mode wires **Codex** through the shim by
default (2026-07-19), so an installer-wired Codex doesn't hit this;
ChatGPT connectors and other direct-HTTP clients still do.

**Pointer TTL.** A client that crashes or is killed never fires SessionEnd,
so without a bound its pointer would attribute every later tier-3 write to a
dead session until the next SessionStart overwrote it. The pointer therefore
expires: one older than `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` (default
`21600` = 6 h, the resume window — past it a return starts a fresh episode
anyway; `0` disables the TTL) is treated as stale and tier 3 falls through to
the transport/idle-gap floor. The timestamp refreshes on-set only, which
Claude Code re-fires on resume/compact, so a genuinely active session stays
live; resolution never refreshes it (a wrong client's traffic can't keep a
dead session's pointer alive).

The resolved identity becomes the episode's `session_key` wherever it's
used; `session_key` is a free-text field, so none of this required a schema
change.

## Sharing memory on the LAN

Run the daemon with `PSEUDOLIFE_MCP_HOST=0.0.0.0` and a
`PSEUDOLIFE_MCP_TOKEN`; remote clients set the same
`PSEUDOLIFE_MCP_DAEMON_URL` + `PSEUDOLIFE_MCP_TOKEN`. The daemon **refuses
to bind a non-loopback host without a token**, and Postgres itself stays
loopback-only — the LAN only ever sees the daemon.

## Data layout

**Containerized / daemon mode (recommended).** The durable source of truth
is **Postgres**, which lives in an *external* Docker volume —
`pseudolife-mcp-bank` by default (entries + facts + graph). A second
external volume, `pseudolife-mcp-state`, holds the daemon's ChromaDB
reference bank, the band-counter `weights.pt`, and the cortex snapshot.
Both are declared `external` in `ops/docker-compose.yml` precisely so a
container teardown can't take them with it. The host `data/` dir then holds
only backups (`data/backups/` from `ops/backup.ps1` — a `pg_dump` of the
bank *plus* a tar of the state volume) and one-time legacy-import staging —
*not* the live bank.

To wipe the bank in this mode you must drop those volumes deliberately —
**never `docker compose down -v` or `docker volume rm` without
`ops/backup.ps1` first**; `stop` / `start` and `up -d --build` keep both
volumes.

**File mode (no daemon / no Postgres — the `embedded` CLI, or unset
`PSEUDOLIFE_MCP_DATABASE_URL`).** Everything lives under
`PSEUDOLIFE_MCP_DATA_DIR`:

```
data/
├── memory_state/
│   └── cms_state.pt        # 8-tier MIRAS entries + metadata (file mode)
├── cortex_state.pt         # Slot-keyed canonical facts (cortex, schema v8)
├── chromadb/               # Reference bank (RAG documents)
└── config.yaml             # Optional overrides
```

In **file mode only**, wipe memory by deleting `data/` and restarting; wipe
just documents via `data/chromadb/`; wipe just the episodic bands via
`data/memory_state/`. (In containerized mode these files are not the source
of truth — see the volume note above.)

## Backups

`ops\backup.ps1` (Windows) / `ops/backup.sh` (Linux/macOS) runs `pg_dump`
inside the container into `data\backups\` with 7-day rotation, and also
tars the daemon **state volume** (ingested `document_ingest` files, cortex
snapshot, graph snapshots — those live only there, not in Postgres) into a
sibling `pseudolife_state-*.tgz`. An optional off-disk mirror via
`PSEUDOLIFE_BACKUP_MIRROR` carries both artifacts;
`PSEUDOLIFE_BACKUP_MIRROR_KEEP=N` (or `-MirrorKeep` / `--mirror-keep`) caps
the mirror at the newest N files per kind — handy for cloud-synced folders.
The matching `restore` script rehearses the newest backup into a scratch
database by default (never touching the live bank) and only replaces the
live bank with an explicit `-Apply` / `--apply`; add
`-StateArchive <pseudolife_state-*.tgz>` / `--state-archive` to also
restore the state volume (opt-in, so a DB-only restore never clobbers
current state).

## Schema version history

The current Postgres meta version is **v22**; migrations are additive
`ADD COLUMN IF NOT EXISTS` on daemon start, and legacy file-mode `.pt`
banks auto-migrate into Postgres. The milestones:

| Version | What it added |
|---|---|
| v11 | Temporal/provenance stamp (tx/valid time, HLC ordering, writer/session) |
| v12 | Graph-insight communities |
| v13 | Provenance-trace engram + reinforcements |
| v14 | Episode `session_key` |
| v15 | Episode `parent_id` (nesting) |
| v16 | `entity_sources` (per-entity project attribution) |
| v17 | `edge_proposals` (deep-dream link candidates) |
| v18 | `entity_proposals` (deep-dream merge/junk candidates) |
| v19 | Partial unique indexes enforcing one current row per slot on facts/world_facts/lessons (+ startup heal of pre-existing duplicates; per-slot write-through persistence replaces the full-table snapshot rewrite) |
| v20 | `dismissed_pairs` (reviewed-distinct pairs stop resurfacing as duplicate findings) |
| v21 | `merge_decisions` audit + write-time near-duplicate merge proposals |
| v22 | `edges(dst_id)` index (dst-side graph lookups no longer sequential-scan) |
