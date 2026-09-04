# Configuration

Every knob the daemon reads — environment variables, the tuned built-in
defaults, toolset tiers, the stdio shim, LAN sharing, data layout, and
backups. Part of the [user guide](../../README.md#documentation).

## Connection / deployment env vars

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATABASE_URL` | _(unset → lite/file mode)_ | Postgres DSN; when set, PG is the source of truth (schema v37). Unset: with the `[lite]` extra installed the daemon auto-starts an embedded PostgreSQL and fills this in itself; otherwise v0.1 file-only mode (announced loudly at startup). |
| `PSEUDOLIFE_MCP_STORAGE` | `auto` | `files` opts the daemon out of the `[lite]` embedded Postgres (file mode even when pg0-embedded is installed). Only consulted when no DSN is set. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `http://127.0.0.1:8765` | Daemon the shim connects to (and auto-starts). |
| `PSEUDOLIFE_MCP_NO_SPAWN` | _(unset)_ | Set `1` on the **shim** to disable its spawn-a-daemon fallback: when nothing answers at `PSEUDOLIFE_MCP_DAEMON_URL` it waits (up to ~3 min) for an external daemon instead. The Docker-tier installers set this on every shim registration — after a reboot the shim can probe before Docker Desktop has bound the port, and a spawned host fallback then wins the bind race and shadows the real bank with whatever stale local state it finds. Leave unset on pip/lite installs, where the spawn fallback is the intended zero-config path. |
| `PSEUDOLIFE_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | Daemon bind address. |
| `PSEUDOLIFE_MCP_TOKEN` | _(unset)_ | Bearer token; **required** to bind a non-loopback host (a `PSEUDOLIFE_MCP_TOKENS` map also satisfies this). Maps to the reserved principal `default`, which keeps the `X-PL-Writer`/`PSEUDOLIFE_WRITER_ID` writer path. |
| `PSEUDOLIFE_MCP_TOKENS` | _(unset)_ | Per-principal bearer tokens: `token:principal,token:principal`. A matched token's principal **is** the writer id and keys the toolset tier (the identity axis that survives the MCP 2026-07-28 stateless core). Malformed entries are logged and skipped — a skipped token does not authenticate, and a map that parses to zero entries with no singular token refuses startup rather than running open. May be set alongside `PSEUDOLIFE_MCP_TOKEN`; the map wins for its tokens. Note the singular-token holder is fully trusted and may still assert any writer via `X-PL-Writer` — mint per-principal tokens when that distinction matters. |
| `PSEUDOLIFE_MCP_TRUST_BIND` | _(unset)_ | Set `1` to allow a non-loopback bind without a token when the boundary is external (containerized, loopback-published). The compose daemon sets this; never set it for a host daemon. |
| `PSEUDOLIFE_MCP_DATA_DIR` | `./data` (cwd-relative) | Weights cache + legacy-migration source + ChromaDB. When the `[lite]` embedded Postgres engages, the default moves to a stable per-user dir instead (`%LOCALAPPDATA%\pseudolife-mcp`, `~/.local/share/pseudolife-mcp`, or `~/Library/Application Support/pseudolife-mcp`) — a per-launch-directory Postgres bank would be a data-scattering footgun. Windows lite note: must be ASCII-only (the daemon refuses otherwise, with the remedy in the message). |
| `PSEUDOLIFE_MCP_CONFIG` | `<data_dir>/config.yaml` if present, else built-ins | Override MIRAS / embedding / memory config. |
| `PSEUDOLIFE_WRITER_ID` | `unknown` | Identifies this writer on every canonical write (schema v11). The shim forwards it as the `X-PL-Writer` header; the compose daemon defaults to `mcp-client`, and the installer pins `claude-code` / `codex` / `mcp-client` in `ops/.env` per the selected `--client`. Existing installs that predate the client selector should set `PSEUDOLIFE_WRITER_ID=claude-code` in `ops/.env` to keep their writer identity (and any `PSEUDOLIFE_MCP_TIER_MAP` keyed on it) stable. |
| `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` | `30` | Interval of the file-mode autosave loop (weights/state cadence; Postgres-mode entries are transactional regardless). |
| `PSEUDOLIFE_SESSION_REAP_SECONDS` | `300` | How often the idle-session reaper sweeps. The idle *threshold* it enforces is `PSEUDOLIFE_SESSION_IDLE_SECONDS` — see [Episodes](episodes.md). |
| `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` | _(unset)_ | Set `1` to restore the retired `mcp-session-id` transport-session fallback for one release (rollback hatch; logs a warning on first use). The header names the HTTP *connection*, not the session — concurrent sessions share it — and the MCP 2026-07-28 revision removes it from the protocol. Session identity rides the hook-registered episode handle and `X-PL-Session` instead — see [Episodes](episodes.md). |
| `PSEUDOLIFE_DAEMON_MEM_LIMIT` | `4g` | Docker tier only (read by compose, not the daemon): hard memory cap on the daemon container, with the memory+swap total pinned to the same value — no swap, so exceeding the cap is a clean container restart rather than a host-wide memory event. Steady state is ~2.8 GB with the default embedder; raise for very large banks. |

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
  eviction scoring at capacity). Raise it above zero to dedup
  near-duplicate stores.
- **Meta-filter off** (`memory.meta_filter.enabled = false` in the MCP
  build) — the filter exists to drop auto-captured chat noise ("I don't
  have anything saved about that"); every MCP store is a deliberate tool
  call, and the filter's patterns collided with legitimate dev facts
  about memory systems themselves.
- **Recency base half-life 24h** (`memory.recency_base_half_life_s =
  86400`, vs the 1h chat default) — Claude Code sessions are hours-to-
  days apart; with a 1h half-life the recency boost was effectively
  always zero. This knob is **doubly dormant under the flat default**:
  the depth ramp it feeds has been off since 2026-07-25 AND the ramp is
  structurally inert with one band; it only bites on a multi-band preset
  with `recency_boost_enabled = true`.
- **MIRAS preset `flat`** (default since 2026-08-15) — one band named
  `flat` at capacity 5,250 (the previous continuum's summed total), with
  a `balanced` retention policy. Eviction is a retention-scored **true
  drop** that only fires at genuine capacity, is counted
  (`memory_stats().true_drops`) and logged — a bank under real pressure
  is visible, never silent. This is the arm the preregistered flat-band
  verdict measured as tying the 8-band continuum on every gate (ranking,
  forced-eviction retention quality, real recorded queries — see the
  [benchmarks page](benchmarks.md#band-structure)), so the simpler
  structure ships. The **`continuum` preset is retained** as the one-line
  rollback: the 8-tier `working … forever` layout with promotion
  thresholds, per-tier retention policies, and the 2026-07-25 demotion
  cascade (a full band demotes into the next; only overflow past
  `forever` drops).
  **Changing the preset in either direction is safe**: hydration reseats
  every row across the new band layout in one pass (rows whose old band
  name is gone land in the first band) and **reconciles the stored band
  stamps** to the new layout, idempotently. If the bank holds more rows
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
  weaker shallow match above a stronger deep one (measured: up to 18
  points on the LongMemEval naive-RAG arm). Under the flat default the
  ramp is additionally structural dead weight (one band, no depths), so
  the knob has left the Console config surface; it still applies to
  multi-band presets via `config.yaml`.
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
- **Constraint pinning on** (`memory.cortex.pin_constraints = true`, schema
  v35) — a fact whose `distortion_tolerance` is `constraint` is served
  AHEAD of the cosine ranking, marked `pinned: true`, when it is in
  scope: in `memory_search`'s cortex block when the query names the
  fact's entity (separator-insensitive, word-bounded), in
  `memory_recall` when the entity is a seed of the walk. A pin must still
  clear the caller's `min_score` floor, pins take at most half of `top_k`
  (best cosine first) so the ranked answer keeps the rest, and the
  payload never grows. An unlabelled bank is served byte-identically
  either way; set `false` for plain ranking.
- **Slot read telemetry on** (`memory.cortex.read_tracking = true`, schema
  v33) — every cortex slot served as an answer (`memory_fact_get` and the
  cortex-first block of `memory_search`) bumps its `slot_reads` counter,
  one small upsert per fact-serving call. Feeds the `read_audit` section
  of `memory_stats` (never-read fractions, slot coverage). Deliberately
  uncounted: internal verification lookups, and the facts attached to
  `memory_recall`/`memory_graph` neighborhoods (context, not a direct
  answer) — treat a slot's never-read status as a lower bound. Set
  `false` to disable the write; the audit section stays available either
  way (it just stops moving). Since v34 the section also carries
  `graduation_candidates`: entries served in ≥60% of the last 30 days'
  distinct sessions (once ≥8 sessions are on record) — static-context
  ("promote to CLAUDE.md") candidates; vet against the cortex before
  promoting, since the log counts serves before the handler's fact-dedup.
- **Engram cross-index on** (`memory.traces.enabled = true`, schema v13) —
  the dream links each consolidated fact-slot to the dense episodes it came
  from. Forwards, that link is where a fact came from: `memory_get`'s
  `consolidated_into` / `source_entries`. Backwards, it powers two
  read-time cautions: `re_verify` on served facts and `derived_flagged` on
  `memory_supersede` (see [Memory model](memory-model.md#how-current-is-this-fact)).
  Set `false` to silence both — the read surfaces stop paying for the
  cross-index query but otherwise serve exactly as before.
  `memory.traces.retention_boost` (default `0.0`) is the separate Phase-2
  MTT-retention weight this same cross-index feeds; `0.0` is today's
  eviction behavior unchanged.
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
- **Provenance-span gate off** (`memory.dream.span_gate = "off"`) — the
  literal gate's sibling: where the literal gate checks digit-bearing
  values, the span gate checks that a scalar claim's *quoted source span*
  actually appears in the pull's notes — fidelity-to-source, not
  trustworthiness-of-source. `"log"` counts without acting; `"contend"`
  parks unbacked scalar claims as visible contenders with a
  `span:unbacked` marker, resolvable via `memory_fact_resolve`. Ships off
  because flipping it on requires the live extraction prompt to emit
  quotes (the v10 prompt does not).
- **Lesson-synthesis dedup on**
  (`memory.lessons.synthesis_dedup_min_similarity = 0.88`) — a synthesized
  lesson that near-matches an existing *current* lesson at a different key
  with the same polarity is silently skipped and counted (`lessons_deduped`
  beside `lesson_signals`/`lessons_written` in the dream-run row).
  Opposite-polarity matches and explicit `lesson_write` callers are never
  gated. `0` disables.
- **Slot-index shadow verification on** (`memory.slot_index_shadow_rate =
  0.01`) — ~1% of slot-pool queries recompute the index from scratch and
  compare; divergences land in `stats()` as
  `slot_index_shadow_divergences`. `0.0` disables, `1.0` checks every
  query (dev/debug).
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
- **Session digests off** (`memory.dream.digest_enabled = false`) — when
  on, the idle dream cycle writes one narrative prose digest per closed
  session episode as a retrievable `source="digest"` band entry (never
  re-mined for facts — `digest` is in `exclude_sources`), and the
  session briefing's recap renders the digest body. The zero-start
  cursor backfills history when first enabled,
  `memory.dream.digest_max_per_cycle` (default `4`) episodes per dream
  pass. `memory.dream.digest_target_chars` (default `1200`) is the prose
  length target passed to the extractor — re-targeted from `800` to the
  length the extractor naturally writes (probe, 2026-08-27) — and
  `memory.dream.digest_context_chars` (default `24000`) caps the
  per-call session context, with longer sessions split on line
  boundaries and map-reduce merged. Default-off pending human review of
  the sidecar quality probe
  (`evals/digest_sidecar_probe.py`).
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
- **Candidate pool at the served width**
  (`memory.search.candidate_pool_multiplier = 1`,
  `memory.search.fusion = "weighted_sum"`) — the
  retrieve-then-rerank shape (a dense pool `top_k x multiplier` wide,
  optionally merged by reciprocal rank fusion instead of raw-sorting
  incommensurate channel scores) exists and is settable, and it **lost**
  its judged run: on the LongMemEval knowledge-update oracle slice
  (2026-09-04, n=78) multiplier 4 cost naive RAG 0.115 accuracy under
  `rrf` and 0.077 under `weighted_sum`, while serving between a third
  and a half more context tokens. Table, caveats and artifacts in
  `evals/README.md` ("Judged verdict (2026-09-04)"). CAUTION if you
  enable `rrf` anyway: it changes the SCALE of every served score to
  ~0.016-0.05, so `memory.search_confidence_floor` must stay 0, and
  `rrf` must not be combined with the cross-encoder reranker
  (`memory.reranker.fusion_weight` collapses to cross-encoder-only
  ordering, `memory.reranker.skip_margin` can never be reached) or with
  a populated reference bank (its raw cosines are not rescaled and
  outrank every memory once the reranker fires). Neither combination has
  been measured.
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
`PSEUDOLIFE_MCP_TOOLSET` (unset → `full`; the Docker compose file ships
`core`, so lite and host-process installs start at `full`) sets the baseline;
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

# 1. Start Postgres 18 + pgvector (one-time build, then persistent).
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
daemon when nothing answers there already (a cross-process lock keeps
concurrent shims from each spawning one). On a Docker-tier install set
`PSEUDOLIFE_MCP_NO_SPAWN=1` in the shim's env — the installers do — so the
shim waits for the container instead of spawning a fallback that races its
port bind. Point Claude Code at it directly:

```json
{
  "mcpServers": {
    "pseudolife-memory": {
      "command": "C:\\path\\to\\Pseudolife-MCP\\.venv\\Scripts\\pseudolife-mcp.exe",
      "env": {
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
        "PSEUDOLIFE_MCP_NO_SPAWN": "1",
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
(never deletes them). The import records its progress in a
`legacy_migration` meta row, so one that fails part-way resumes on the next
start instead of leaving a short bank behind. While it is unfinished the
daemon keeps serving and `/health` stays `status: "ok"` (so healthchecks and
`ops/update.ps1` are not tripped by it) but carries an extra
`migration_partial` field; the matching ERROR lines in the daemon log name
the resume path. A resume merges rather than overwrites — cortex facts
written during that window are kept, and only slots nobody has written land
from the legacy bank. Leave the original `.pt` files in place until it
completes: the resume reads them, and deleting one makes the bank
unfinishable.

## Session identity

Every request resolves "which session/episode does this write belong to"
through one chokepoint, evaluated in strict precedence order:

| tier | source | scope | notes |
|---|---|---|---|
| 1 | `X-PL-Session` header | per shim process = per session | the stdio shim sends this on every call; any integrator can |
| 2 | explicit `episode` argument | per call | pass an open episode id (or its unambiguous ≥8-char prefix) on `memory_store` / `memory_outcome` / `memory_fact_set`, and on the lifecycle tools `memory_episode_start` / `memory_episode_end` / `memory_session_title` — where a resolved handle wins outright (they never consult the header tiers); the daemon mints it and advertises it in the SessionStart briefing |
| 3 | hook-registered active session | machine-scoped pointer | the SessionStart hook forwards Claude Code's own `session_id`; a SessionEnd hook closes it. A singleton — concurrent sessions race it, which is why the lifecycle tools take the per-call handle |
| 4 | `mcp-session-id` header | per connection | **retired** — the header names the connection (concurrent sessions share it) and the MCP 2026-07-28 revision (SEP-2567, "Sessionless") removes it from the protocol. `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1` restores it for one release as a rollback hatch |
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
default (2026-07-19), and **Gemini CLI** the same way (2026-08-29); each
first-class provider's registration also carries its own
`PSEUDOLIFE_WRITER_ID` (`claude-code` / `codex` / `gemini`) so a shared
bank attributes writes per agent (see
[the providers guide](providers.md)). ChatGPT connectors and other
direct-HTTP clients still hit the tier-3 leak.

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

The token is also what relaxes the MCP endpoint's DNS-rebinding guard. With
a token set, `/mcp` accepts any `Host` header — a LAN address, a
reverse-proxy hostname, a Tailscale name, a compose service name — because
`Authorization` already proves intent. Tokenless (loopback use, or a
container published to 127.0.0.1 via `PSEUDOLIFE_MCP_TRUST_BIND`), `/mcp`
serves loopback `Host` values only and answers anything else with
`421 Invalid Host header`; that is the guard against a rebinding browser
reaching an unauthenticated bank. So: fronting the daemon with a reverse
proxy under a real hostname means setting a token.

## Data layout

**Containerized / daemon mode (recommended).** The durable source of truth
is **Postgres**, which lives in an *external* Docker volume —
`pseudolife-mcp-bank` by default (entries + facts + graph). A second
external volume, `pseudolife-mcp-state`, holds the daemon's ChromaDB
reference bank, the counter file `weights.pt`, and the cortex snapshot.
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
│   └── cms_state.pt        # Associative entries + metadata (file mode)
├── cortex_state.pt         # Slot-keyed canonical facts (cortex, schema v8)
├── chromadb/               # Reference bank (RAG documents)
└── config.yaml             # Optional overrides
```

In **file mode only**, wipe memory by deleting `data/` and restarting; wipe
just documents via `data/chromadb/`; wipe just the associative store via
`data/memory_state/`. (In containerized mode these files are not the source
of truth — see the volume note above.)

## Windows / WSL2 memory (Docker tier)

Docker Desktop's WSL2 VM (`Vmmem`) claims up to **~50% of host RAM** by
default, which is far more than the stack needs. Under dream load the whole
stack wants ~6–7 GB with the default extractor sidecar, or ~2 GB in
`sonnet-only` mode — where the Qwen3 embedding backbone is the bulk of it.
Cap the VM by copying `ops/wslconfig.example` to
`%USERPROFILE%\.wslconfig`, tuning `memory=`, then `wsl --shutdown`.

The daemon container is separately hard-capped at 4 GB, with memory+swap
pinned to the same value so exceeding it is a clean container restart rather
than a host-wide memory event. `PSEUDOLIFE_DAEMON_MEM_LIMIT` in `ops/.env`
raises it for very large banks.

After `wsl --shutdown` the host port forward is gone; `docker restart
pseudolife-mcp-daemon` re-establishes it.

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

### Logical export / import

Beside the physical backups, `pseudolife-mcp export` writes the bank as a
portable ZIP — one JSONL file per table plus a manifest (schema version,
embedding dimension, per-table counts) — from a single read-only snapshot,
so it is safe to run against a live daemon (the snapshot stays open for
the duration, which delays autovacuum on busy tables — prefer a quiet
moment for a very large bank). Unlike a `pg_dump`, the
artifact is deployment-tier- and Postgres-version-independent,
human-readable, and loads additively across schema versions: an export
from an older build imports into a newer one, with new columns taking
their DDL defaults. Embeddings travel verbatim (the manifest pins their
dimension), so neither command needs the embedding model.

`pseudolife-mcp import <archive.zip>` loads an export into a **fresh,
empty bank** in one transaction. It refuses a non-empty bank, refuses
while any other connection holds the database — stop the daemon first
(Docker tier: `docker compose -f ops/docker-compose.yml stop
pseudolife-daemon`); `--force` overrides for connections you know are
inert — and refuses an export whose format version or embedding dimension
it cannot honor. Operational telemetry (retrieval/read logs, the dream-run
journal) deliberately stays behind, and the manifest lists exactly which
tables were excluded. Ingested `document_ingest` files live on the state
volume/data dir, not in Postgres — carry those with the physical backup's
state archive.

Both commands resolve the bank the way `backup` does: the explicit
`PSEUDOLIFE_MCP_DATABASE_URL` first (for the Docker tier that is
`postgresql://pseudolife:<POSTGRES_PASSWORD from
ops/.env>@127.0.0.1:5433/pseudolife_memory`), else the lite tier's
embedded instance, attached or started for the duration — never
initialized: importing is how a fresh bank gets *filled*, but creating
one is the daemon's job.

## Schema version history

The current Postgres meta version is **v37**; migrations are additive
`ADD COLUMN IF NOT EXISTS` on daemon start, and legacy file-mode `.pt`
banks auto-migrate into Postgres. The one exception is v25 itself: a
vector *dimension* change on an existing column is not additive, so
`ensure_schema` refuses to start against a bank still dimensioned at
v24 or earlier instead of attempting an in-place ALTER — run the
human-gated `ops/migrate_embeddings.py` first. Full step-by-step operator
procedure (backup, stop, dry-run, apply, deploy, verify, rollback):
[the v25 migration runbook](../runbooks/embedding-v25-migration.md).
Separately from the schema meta version, Docker-tier installs created
before 2026-08-14 also need the PostgreSQL 16 → 18 volume cutover —
[the PostgreSQL 18 migration runbook](../runbooks/postgres-18-migration.md).
The milestones:

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
| v30 | `entity_proposals.judge_verdict` / `judge_confidence` / `judge_note` / `judge_model` / `judged_at` — the autonomous Step-C judge's shadow verdict on a pending merge proposal, recorded by the sweep (`memory.deep_dream.judge_mode`: `off` \| `shadow` \| `auto-reject`) and surfaced beside the evidence in review payloads. The verdict is an opinion on the pending row; the durable decision record stays `merge_decisions`, written only when a decision path (human, agent, or the confidence-gated auto-reject) ratifies it. `NULL` = not yet judged, exactly the pre-v30 behaviour, so the migration is a no-op on existing banks. Judge-model floor measured by `evals/judge_ladder.py` (`evals/results/judge-ladder-20260816.json`). Additive/idempotent |
| v31 | `retrieval_events` + `retrieval_uses` — the retrieval event log (learned-reranker Phase 0). Every `memory_search` appends one event row (query text, the ranked served list as JSONB with entry ids/scores/ranks, writer session/episode); a later `memory_get`/`memory_reinforce` on a served entry in the same session writes an implicit relevance label (most-recent serving event wins, bounded by `memory.retrieval_log.use_window_seconds`). Together they are the (query, served, used) training tuples for a future learned fusion/reranker stage — purely observational, no retrieval behaviour changes. Served ids carry no FK (entries are evictable; training joins tolerate dangling ids); labels CASCADE from their event; events are pruned on the dream-sweep tick after `memory.retrieval_log.retention_days` (default 365). Kill-switch: `memory.retrieval_log.enabled`. Additive/idempotent |
| v32 | `retrieval_events.params` — the ranking knobs in force for the query (effective `top_k` / keep-threshold, the recency ramp, BM25 weight and scorer params, the reranker's fusion weight + margin gate and whether it actually fired, timeline/contiguity settings, and the call's filters), logged beside a widened `served` list whose per-entry `components` blob carries the fusion INPUTS: bi-encoder score, cross-encoder score (`null` when the margin gate skipped the pass — a distinction a learned head needs), BM25 boost, surprise, recency and the source/supersession multipliers. Phase 0 logged only the fused score, which is the output a Phase-1 learned head is supposed to predict; the inputs are not recoverable afterwards, because config is mutable at runtime and band recency, supersession flags and access counts all mutate on every serve. Nothing new is computed at serve time — these values were already in hand and were being discarded. `NULL` params = a v31-era row. Additive/idempotent |
| v33 | `slot_reads` + `entries.explicit_reinforcements` — read telemetry. `slot_reads` counts how many times each cortex slot was *served as an answer* (`memory_fact_get` and `memory_search`'s cortex-first block), keyed on the stable `(entity_norm, attribute_norm)` slot like `memory_traces` so counters survive cortex snapshot saves; deliberately uncounted are internal verification lookups (e.g. the dream rollback's post-revert check) and the facts attached to `memory_recall`/`memory_graph` neighborhoods (context, not a direct answer), so never-read is a lower bound. `explicit_reinforcements` moves only on `memory_reinforce`, splitting the deliberate "this was useful" signal out of the shared `reinforcements` counter, which also counts dream-trace links (and still feeds the retention formula unchanged). Both feed the new `read_audit` section of `memory_stats` (never-read fractions by age and source, read/write balance, slot coverage) — motivated by the 2026-08-26 bank audit, where entry reads were measurable but the 4.6k fact slots had no read signal at all. Kill-switch: `memory.cortex.read_tracking`. Additive/idempotent |
| v34 | `retrieval_events.served_facts` — the fact half of the reranker training tuple. The v31 event log recorded only served *entries*; the cortex-first block's facts, served above those entries in every `memory_search` response, were invisible to a future learned reranker. The search handler now attaches them (`[{entity_norm, attribute_norm, rank, score, kind, contested}]`) to the exact event row that search wrote, keyed by the event id `search(return_event_id=True)` hands back — no session-window guessing. `NULL` = a pre-v34 row or a search that served no facts. Also (no DDL): `memory_stats` `read_audit` gains `graduation_candidates` — entries served in ≥60% of the last 30 days' distinct sessions (once ≥8 sessions are on record), i.e. static-context ("promote to CLAUDE.md") candidates that retrieval keeps re-paying for per query. Additive/idempotent |
| v35 | `entries.authority` / `entries.distortion_tolerance` and `facts.authority` / `facts.distortion_tolerance` — the write-time label pair (authority collapse, arXiv 2608.01679; the compaction cliff, arXiv 2608.22752). `authority` is the SPEECH ACT of the text (`directive` \| `observation` \| `quoted`), deliberately a separate axis from the `origin` tier (who wrote — which drives supersession arithmetic and which entries never persisted anyway); `distortion_tolerance` is the fidelity class (`constraint` \| `procedural` \| `belief` \| `preference` \| `episodic`). Set at write time — explicit `memory_store` / `memory_fact_set` parameters, or a deterministic heuristic under the `auto` default that asserts only `constraint` (rule-sized deontic/imperative text) and `quoted`/`directive` — and inherited through `memory_supersede` / `memory_consolidate` / fact supersession unless the new write restates one. Consumers: the dream carries a `constraint` source's text verbatim onto a derived fact and a post-dream guard reports any constraint entry left without a verbatim carrier (`constraint_verbatim` / `constraint_misses`); a `quoted` source is low-trust for the two-man rule; `constraint` facts are pinned ahead of cosine in `memory_search`'s cortex block and `memory_recall` (`memory.cortex.pin_constraints`). `NULL` = observation / unlabelled, exactly the pre-v35 reading, so the migration is a no-op on an existing bank — no backfill, by design. Additive/idempotent |
| v36 | Review-queue autonomy (2026-09-02). `edge_proposals.judge_verdict` / `judge_confidence` / `judge_note` / `judge_model` / `judged_at` / `judge_relation` / `decided_by` / `decided_at` — the link judge's opinion on a pending link proposal (the retype verdict's corrected relation in `judge_relation`) and who settled the row; `entity_proposals.judge2_verdict` / `judge2_confidence` / `judge2_model` / `judged2_at` — the merge judge's SECOND opinion beside the v30 first one (two-vote agreement is the apply gate for rows the single-vote 0.8 reject gate leaves pending); and `curation_judgments` (`store`, sorted slot keys, verdict, keep, fold, confidence, note, model, judged_at) — the store-curation judge's memo, because the lesson/world duplicate listings are recomputed per pass and would otherwise be re-sent every sweep. `NULL` judge columns = not yet judged, exactly the pre-v36 behaviour, so the migration is a no-op on existing banks. Gates measured by `evals/queue_judge_ladder.py` against `evals/results/queue-judge-panel-20260902.json`. Additive/idempotent |
| v37 | Retire-not-delete (2026-09-03). `store_decisions` (`id`, `store`, `entity_norm`, `attribute_norm`, `action`, `decided_by`, `reason`, `record` JSONB, `decided_at`) — the FK-free audit of lesson/world forgets and restores. A `memory_forget(scope="lesson"\|"world")` now retires the slot's rows (`status='retired'`, rows kept; `memory.compaction` treats them like any non-live record) instead of deleting them, and the audit row carries the verbatim record so `lesson_restore` / `world_restore` (`memory_graph_review(action="restore_slot")`, `POST /api/lessons/restore`, `POST /api/world/restore`) still work after compaction has purged the retired row. Also (no DDL): merge and junk rejects write text-keyed tombstones to `dismissed_pairs` (canonical pair / `junk:<canonical>` self-pair) so a verdict outlives the CASCADE-deleted proposal row. No column changes; the table starts empty on an existing bank, so the migration is a no-op there. Additive/idempotent |

After running the entity-kind backfill (`evals/apply_entity_kinds.py --apply`), the daemon must be restarted for inference to take effect — it caches the entity-kind map for the life of its process.

A kind you set by hand is locked against later classifier runs — `evals/apply_entity_kinds.py --apply` overlays the model's labels onto the existing table rather than replacing it, so a deliberate marking is never reverted by a re-apply. The R@10 figures behind the v25 swap, and the rest of the shootout, are in [Benchmarks — embedding backbone](benchmarks.md#embedding-backbone--chosen-on-our-own-corpus).

### Extension schemas

A fork or downstream customization that adds its own tables should not
consume the next integer `schema_version` — that number belongs to this
repository's migration ladder, and claiming it guarantees a collision with
the next upstream release. The sanctioned pattern instead:

- **A namespaced marker key**: store the extension's lineage under its own
  `meta` key ending in `_schema_version` (for example
  `myext_schema_version = "v34-myext"`), leaving the integer
  `schema_version` untouched. Keys with this suffix are build-owned:
  the logical export/import skips them exactly like `schema_version`
  itself, so a marker never travels into a bank whose build does not
  provide the extension.
- **Additive, idempotent DDL** (`CREATE TABLE IF NOT EXISTS`, `CREATE OR
  REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`) applied
  after upstream's `ensure_schema` tail, so daemon startup converges on
  the same shape regardless of what version it last ran.
- **Explicit enumeration**: add the extension's tables to
  `BENCH_RESET_TABLES` (so bench tooling can reset them) and to the
  transfer CLI's `EXCLUDED_TABLES` (so ordinary bank transfer neither
  moves nor blocks on them); give them their own portable format if their
  data needs to travel.

Upstream migrations stay unaware of extensions by construction — an
extension that follows this pattern rebases cleanly across upstream
schema bumps, and an upstream bank that has never seen the extension
simply carries no marker.
