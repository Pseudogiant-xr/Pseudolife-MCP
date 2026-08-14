# Configuration

Every knob the daemon reads — environment variables, the tuned built-in
defaults, toolset tiers, the stdio shim, LAN sharing, data layout, and
backups. Part of the [user guide](../../README.md#documentation).

## Connection / deployment env vars

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATABASE_URL` | _(unset → lite/file mode)_ | Postgres DSN; when set, PG is the source of truth (schema v29). Unset: with the `[lite]` extra installed the daemon auto-starts an embedded PostgreSQL and fills this in itself; otherwise v0.1 file-only mode (announced loudly at startup). |
| `PSEUDOLIFE_MCP_STORAGE` | `auto` | `files` opts the daemon out of the `[lite]` embedded Postgres (file mode even when pg0-embedded is installed). Only consulted when no DSN is set. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `http://127.0.0.1:8765` | Daemon the shim connects to (and auto-starts). |
| `PSEUDOLIFE_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | Daemon bind address. |
| `PSEUDOLIFE_MCP_TOKEN` | _(unset)_ | Bearer token; **required** to bind a non-loopback host (a `PSEUDOLIFE_MCP_TOKENS` map also satisfies this). Maps to the reserved principal `default`, which keeps the `X-PL-Writer`/`PSEUDOLIFE_WRITER_ID` writer path. |
| `PSEUDOLIFE_MCP_TOKENS` | _(unset)_ | Per-principal bearer tokens: `token:principal,token:principal`. A matched token's principal **is** the writer id and keys the toolset tier (the identity axis that survives the MCP 2026-07-28 stateless core). Malformed entries are logged and skipped — a skipped token does not authenticate, and a map that parses to zero entries with no singular token refuses startup rather than running open. May be set alongside `PSEUDOLIFE_MCP_TOKEN`; the map wins for its tokens. Note the singular-token holder is fully trusted and may still assert any writer via `X-PL-Writer` — mint per-principal tokens when that distinction matters. |
| `PSEUDOLIFE_MCP_TRUST_BIND` | _(unset)_ | Set `1` to allow a non-loopback bind without a token when the boundary is external (containerized, loopback-published). The compose daemon sets this; never set it for a host daemon. |
| `PSEUDOLIFE_MCP_DATA_DIR` | `./data` (cwd-relative) | Weights cache + legacy-migration source + ChromaDB. When the `[lite]` embedded Postgres engages, the default moves to a stable per-user dir instead (`%LOCALAPPDATA%\pseudolife-mcp`, `~/.local/share/pseudolife-mcp`, or `~/Library/Application Support/pseudolife-mcp`) — a per-launch-directory Postgres bank would be a data-scattering footgun. Windows lite note: must be ASCII-only (the daemon refuses otherwise, with the remedy in the message). |
| `PSEUDOLIFE_MCP_CONFIG` | `<data_dir>/config.yaml` if present, else built-ins | Override MIRAS / embedding / memory config. |
| `PSEUDOLIFE_WRITER_ID` | `unknown` | Identifies this writer on every canonical write (schema v11). The shim forwards it as the `X-PL-Writer` header; the compose daemon defaults to `mcp-client`, and the installer pins `claude-code` / `codex` / `mcp-client` in `ops/.env` per the selected `--client`. Existing installs that predate the client selector should set `PSEUDOLIFE_WRITER_ID=claude-code` in `ops/.env` to keep their writer identity (and any `PSEUDOLIFE_MCP_TIER_MAP` keyed on it) stable. |
| `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` | `30` | Interval of the file-mode autosave loop (weights/state cadence; Postgres-mode entries are transactional regardless). |
| `PSEUDOLIFE_SESSION_REAP_SECONDS` | `300` | How often the idle-session reaper sweeps. The idle *threshold* it enforces is `PSEUDOLIFE_SESSION_IDLE_SECONDS` — see [Episodes](episodes.md). |

For the Docker stack, set these in `ops/.env`
(`cp ops/.env.example ops/.env` — the install/update scripts scaffold it too;
every value is commented, a missing file runs entirely on defaults). The
dream-extractor variables (`PSEUDOLIFE_DREAM_*`) are covered in
[Dreaming](dreaming.md).

## Built-in defaults (tuned for Claude's use case)

- **Embedding backbone `Qwen/Qwen3-Embedding-0.6B`** (`EmbeddingConfig.model_name`,
  default since schema v25) — fp32 torch, no GPU sidecar. It's
  instruction-asymmetric: query-side text (search/recall probes) is encoded
  with `EmbeddingConfig.query_prefix`'s instruction prefix via
  `encode_query()`; everything stored (entries, fact/world/lesson claim
  text, slot and entity-name embeddings) is encoded bare via `encode()` /
  `encode_single()`. `query_prefix` defaults to the Qwen3-Embedding card's
  exact instruction string — set it to `""` to restore symmetric behavior
  for a model (like the previous default, `all-MiniLM-L6-v2`) that doesn't
  distinguish query/document sides. `max_seq_length` caps the tokenizer at
  512 tokens (a min-with-model-default cap, never a raise) regardless of
  the model's native context window. See
  [asymmetric query/document encoding](retrieval.md#asymmetric-query-and-document-encoding)
  for what this changes about retrieval, and the
  [schema version history](#schema-version-history) below for the v25
  cutover itself.
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
  Note the depth ramp itself is **off by default since 2026-07-25** (see
  below), so this setting only bites once you opt back in.
- **MIRAS preset `continuum`** — the 8-tier `working / micro / instant /
  fast / medium / slow / archival / forever` continuum. Bands are plain
  cosine vector stores (v0.5); a band spec is capacity + consolidation
  cadence + promotion thresholds + an eviction policy. Since 2026-07-25 a
  band at capacity **demotes** its lowest-scoring entry to the next band
  rather than deleting it; only overflow past `forever` is a real drop,
  which makes the summed capacity (5,250) the actual bound. Previously a
  full `working` band destroyed entries — and their storage rows — while
  the deeper bands sat nearly empty, because promotion was the only other
  way out and it requires `access_count >= N or surprise > threshold`.
  **Changing the preset is safe**: restoring from Postgres or from
  `cms_state.pt` reseats entries across the new band layout in one pass,
  including rows whose old band name is gone. If the bank holds more rows
  than the new preset seats, the deepest band is left over capacity and
  the count logged rather than truncated at startup — normal eviction
  drains it from there.
- **No NLI scorer.** The `cross-encoder/nli-deberta-v3-xsmall`
  contradiction model (~278 MB) is an unwired seam, not a switch: the
  `[nli]` extra and `memory.nli.*` exist for library callers who inject a
  scorer themselves, and no daemon path constructs one. The four-path
  detector — slot identity, negation asymmetry, affirmative replacement,
  state transition — is what actually runs.
- **Cross-encoder reranker off** — wired into the pipeline but disabled by
  default; enable globally (`memory.reranker.enabled = true`) or per-call
  (`memory_search(..., rerank=True)`). Details: [Retrieval](retrieval.md#cross-encoder-reranking).
- **BM25 hybrid lexical pool ON** (since 2026-07-25) — a pure-stdlib
  sparse-retrieval channel that rescues exact-keyword queries. It shipped
  disabled, which meant every eval measured dense-only retrieval; turn it
  off with `memory.bm25.enabled = false` or per-call `bm25=False`. The
  cortex-fact analogue exists but ships **opt-in**
  (`memory.bm25.cortex_enabled = false` by default — a pre-registered A/B
  measured no end-to-end benefit on facts).
  Details: [Retrieval](retrieval.md#bm25-hybrid-retrieval).
- **Depth-ramped recency boost off** (`memory.recency_boost_enabled =
  false`, since 2026-07-25) — retrieval used to scale scores by a
  `0.4 → 0.0` ramp over band depth, treating depth as a proxy for age.
  Depth is set by promotion history, which without retrieval to accrue
  access counts tracks *surprise*, not age — so the ramp could rank a
  weaker match in `working` above a stronger match in a deeper band
  (measured: up to 18 points on the LongMemEval naive-RAG arm). Set it
  to `true` to restore the previous ranking.
- **Superseded entries stay visible** (`memory.hide_superseded = false`,
  since v0.7.3) — an entry the contradiction pipeline marked superseded
  is still retrievable, downranked ×0.55 so current facts outrank their
  own history. That is what lets the agent say "you used to have X, then
  you said Y". Set it to `true` to restore the pre-v0.7.3 hard filter;
  that filter is why a category query once missed the only entry naming
  the category, and it costs knowledge-update recall, so treat it as a
  debug/audit switch. Before 2026-07-30 this knob was mis-registered as
  `memory.show_superseded` and did nothing.
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
- **Literal-faithfulness gate on, enforcing** (`memory.dream.literal_gate
  = "enforce"`, `memory.dream.literal_gate_scope = "batch"`) — digit-bearing
  tokens in a dream claim's value (date-like spans and `~`-marked
  approximations exempt) must appear in the pull's source notes, allowing
  the legitimate re-formattings extractors produce (spelled numbers,
  hyphenated ranges/compounds, `N+` minimums); unbacked literals are
  dropped and counted (`literal_dropped`/`literal_flagged` in dream
  results). Enforcement became the default on 2026-08-02, when the
  extended matcher left the at-scale probes firing almost exclusively on
  genuinely unbacked literals — derived aggregates and imported world
  knowledge — at 1.3–1.7% of gateable claims
  (`evals/results/gate-firing-normfix-verdict.json`). `"log"` counts
  without dropping; `"off"` disables. The batch-union corpus default
  exists because derived sums and cross-note values are measured
  false-drop classes under per-note (`"source"`) gating.
- **Quarantine retype on** (`memory.dream.retype_quarantined_max = 3`) —
  per-dream cap on quarantined pairs re-offered to the extractor for
  typing, shown only the notes where both entities co-occur; a typed
  answer becomes a review proposal, never a live edge. Without it the
  quarantine only accumulates. Set `0` to disable.
- **Dream-run journal retention** (`memory.dream.runs_keep = 50`) — the
  newest N dream-run rows and their pre-image journals (schema v27)
  survive; older ones are pruned on the sweep tick beside superseded-row
  compaction. The journal is what `memory_dream(action="rollback")`
  replays, so this bounds how far back a pass stays revertible — see
  [Dream runs — audit and rollback](dreaming.md#dream-runs--audit-and-rollback-schema-v27).
- **Chronicle extraction on** (`memory.dream.chronicle = true`) — the
  dream pass runs a second, dedicated events-extraction call per
  batch and stores dated occurrences into `chronicle_events` (schema
  v28); temporally-cued searches serve them as an `events` block
  (aggregation cues widen the block and add `events_total`). Default-on
  since 2026-08-12: the pipeline passed its preregistered gates and a
  2026-08-05..08-12 production soak reviewed clean. Needs Postgres; an
  events-pass failure never stalls claims. Set `false` to opt out — see
  [Chronicle events](dreaming.md#chronicle-events-schema-v28--dated-occurrences-beside-facts).
- **Consolidation quarantine off** (`memory.dream.quarantine_low_trust =
  false`) — when on, a scalar dream claim whose backing entry is
  agent-tier (its `source` maps to origin `agent`) and outside
  `memory.dream.trusted_sources` never takes `current` directly: it
  parks via the existing contender machinery (visible in
  `memory_fact_get` as contested), promotable only by an explicit
  `memory_fact_resolve(accept=true)` or by an independent second
  witness — a later matching claim from a different witness token
  (episode, else source) or a non-agent origin. The same witness
  restating confirms but never promotes. Parks and promotions are
  journaled (schema v27) and covered by `memory_dream(rollback)`.
  Honest scope: this does not stop a poisoned entry from being stored
  or retrieved — episodic search still surfaces it; the claim is that
  poison does not silently gain *canonical* authority. Scalar claims
  only in v1; member ops keep their existing guards. See
  [dreaming](dreaming.md) and the threat model in `SECURITY.md`.
- **Aggregation-recall retrieval knobs off**
  (`memory.search.contiguity_neighbors = 0`,
  `memory.search.timeline_channel = false`) — Phase 1 retrieval-side
  experiments (neighbor expansion, a timeline channel) that measurably
  failed their gates and ship dormant; they remain settable for
  replication but there is no measured reason to enable them.
- **Staleness served as annotation** (`memory.search.stale_policy =
  "annotate"`) — stale records (past 2×TTL for their freshness class)
  carry `effective_confidence`/`stale` flags and nothing more, today's
  behavior. `"demote"` additionally sorts stale records after non-stale
  ones on list surfaces and adds a top-level `warning`; `"quarantine"`
  replaces a stale record's `value` with a wrapper string and moves the
  original to `last_known_value` (data moved, never hidden). Applied at
  the shared record serialisers, so every scalar-fact read surface —
  including the compact `memory_search` / `memory_world_search`
  projections — behaves identically. Deliberate exemptions: version
  history (the audit surface and the recovery path), `chain` summaries
  and graph fact projections (machine-consumed), and set-valued slots
  (set members are structurally always evergreen — the set API carries
  no freshness class — so no set payload can be stale). Non-stale
  records are byte-identical under every policy; an unrecognised policy
  value degrades safely to `annotate`. Console note: the web console
  renders the record `value` field, so under `quarantine` a stale fact
  shows the wrapper there — a known P2 cost to weigh before ever
  flipping the default.

## Toolset tiers

Three visibility tiers — `minimal` (9 tools: the recall/capture loop, the
set-slot pair, the gate), `core` (22: + graph/recall, world facts, lessons,
documents, episodes, stats, `memory_get`, `memory_fact_resolve`),
`full` (35) — filtered per principal at `tools/list` (the named principal
from a `PSEUDOLIFE_MCP_TOKENS` bearer, else the writer id; sessions sharing
a credential share a tier view). The filter is
visibility, not auth (the bearer token is the security boundary) — but
Claude clients gate calls against their own tool list, so in practice a
session expands its tier before calling a hidden tool. Defaults:
`PSEUDOLIFE_MCP_TOOLSET` (shipped: `core`) sets the baseline;
`PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal,claude-code:core"` sets
per-client defaults by principal (writer id). Any caller can step its tier
up or down at runtime with `memory_toolset(action="expand"|"collapse"|"status")`
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
Python 3.10+, Docker Desktop, and roughly 2 GB of disk — the
Qwen3-Embedding-0.6B weights (~1.2 GB) download on first run, on top of
CPU torch and the Python environment.

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

The pip tiers (lite / host-process) use `pseudolife-mcp backup` instead:
same shape — a `pg_dump | gzip` of the bank (`--no-owner --no-acl`, so
the artifact restores under any role — rehearsed in the test suite
against a role-named PostgreSQL 18; since the Docker tier's 16→18 bump
(2026-08-14) both tiers run PostgreSQL 18, so a lite dump restores
straight into the Docker tier; the lite tier uses the embedded
runtime's own bundled `pg_dump`, attaching to the running instance or
starting it for the duration) plus a
`pseudolife_lite_state-*.tar.gz` of the data dir (ChromaDB, weights,
config; `embedded_pg/` is excluded — the dump covers it), with the same
7-day rotation (`--keep-days`). The artifact names
(`pseudolife_lite_memory-*` / `pseudolife_lite_state-*`) are deliberately
disjoint from `ops/backup.*`'s, so the two tools can share a directory
without either's rotation or restore-picker ever touching the other's
files. A backup never initializes a bank that doesn't exist yet, a run
that produced no dump never rotates dumps, and rotation only ever
deletes files the tool itself wrote.

## Schema version history

The current Postgres meta version is **v29**; migrations are additive
`ADD COLUMN IF NOT EXISTS` on daemon start, and legacy file-mode `.pt`
banks auto-migrate into Postgres. The one exception is v25 itself: a
vector *dimension* change on an existing column is not additive, so
`ensure_schema` refuses to start against a bank still dimensioned at
v24 or earlier instead of attempting an in-place ALTER — run the
human-gated `ops/migrate_embeddings.py` first. Full step-by-step operator
procedure (backup, stop, dry-run, apply, deploy, verify, rollback):
[the v25 migration runbook](../runbooks/embedding-v25-migration.md). The
milestones:

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
| v23 | `facts.freshness_class` — read-time currency on personal cortex facts (evergreen default, so existing facts are unchanged; mark transient ones `volatile` and they decay and flag `stale`) |
| v24 | `entity_kinds` (one `artifact`/`system`/`concept` kind per entity) — `freshness_class` now defaults to inferring from the entity's kind instead of a fixed default; only `system` entities can resolve `volatile`, and an empty table resolves everything to `evergreen`, so behaviour is unchanged until it is populated |
| v25 | `entries`/`facts`/`world_facts`/`lessons.embedding` move from `vector(384)` to `vector(1024)` — default embedding backbone swaps to Qwen/Qwen3-Embedding-0.6B (measured R@10 0.809 vs shipped MiniLM's 0.572). Qwen3-Embedding is instruction-asymmetric — see [asymmetric query/document encoding](retrieval.md#asymmetric-query-and-document-encoding) — so similarity-threshold semantics shift too. `ensure_schema` refuses to start against an existing v24-dimensioned bank rather than attempting an in-place ALTER; migrate first with `ops/migrate_embeddings.py` (dry-run by default; `--apply --backup-verified` to commit) |
| v26 | `facts.kind` (`scalar` \| `member`) and `facts.value_norm` — set-valued cortex slots (many concurrently-current members per `(entity, attribute)`, not one NOW value). The per-slot current-uniqueness constraint splits by kind (`facts_slot_current_scalar_uq` keeps one live scalar row per slot; `facts_member_current_uq` allows several current members on the same slot); the daemon-start duplicate-healing pass is scoped to `kind = 'scalar'` so it never demotes member rows. Additive/idempotent; every existing fact defaults to `kind='scalar'` and dedupes exactly as before. See [Set-valued slots](memory-model.md#set-valued-slots-schema-v26) |
| v27 | `dream_runs` + `dream_run_slots` — every dream pass that pulls entries records a run row (cursor movement, tallies, lifecycle status) and a per-claim pre-image journal (what each slot held before the write, `NULL` = slot absent). The journal is what `memory_dream(action="rollback")` replays, and it survives superseded-row compaction by construction (own tables, own newest-N retention via `memory.dream.runs_keep`). `dream_run_slots.src_entry_id` deliberately carries no FK — entries are evictable. Additive/idempotent |
| v28 | `chronicle_events` — dated occurrences as first-class records beside facts (`occurred_at` = event time, nullable and never fabricated; `occurred_phrase` = the source's verbatim wording; `recorded_at` = transaction time). Additive-only: contradiction handling sets `invalidated_at`, never deletes; event writes journal into `dream_run_slots` (new nullable `chronicle_event_id` column) so rollback can delete them by exact id. No FKs — `src_entry_id` references evictable entries. Extraction into the table (`memory.dream.chronicle`) shipped off by default and flipped on 2026-08-12 after its preregistered gates and a production soak both passed. Additive/idempotent |
| v29 | `facts.stance` — epistemic stance as a labelled field: the source's own hedge words ("probably", "per the runbook"), kept verbatim and separate from `value` so consolidation cannot silently turn a hedged claim into a confident canonical fact (the labelled-field-vs-inline retention result is arXiv:2608.06953). `NULL` = asserted plainly, exactly the pre-v29 behaviour, so the migration is a no-op on existing banks. Stance follows the latest asserting write (a plain restatement clears the hedge), surfaces in `memory_fact_get`/recall/history only when set, and is never an input to confidence, ranking, or supersession. Written by the dream path since the v10 update-anchored stance prompt shipped its gates (2026-08-14); not exposed on the `memory_fact_set` tool surface. Additive/idempotent |

After running the entity-kind backfill (`evals/apply_entity_kinds.py --apply`), the daemon must be restarted for inference to take effect — it caches the entity-kind map for the life of its process.

A kind you set by hand is locked against later classifier runs — `evals/apply_entity_kinds.py --apply` overlays the model's labels onto the existing table rather than replacing it, so a deliberate marking is never reverted by a re-apply. The R@10 figures behind the v25 swap, and the rest of the shootout, are in [Benchmarks — embedding backbone](benchmarks.md#embedding-backbone--chosen-on-our-own-corpus).
