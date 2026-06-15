# PseudoLife-MCP

**Persistent neural memory for Claude Code via the Model Context Protocol.**

An MCP server that gives Claude (or any MCP-capable client) a long-term
memory that persists across sessions — surviving context compactions and
`/clear` resets. Claude is the LLM; this server is its memory on disk.

## What this is

A memory engine exposed over MCP. There's no chat UI and no bundled
model — just tools Claude calls to store and recall what matters. It
layers several complementary stores:

- **Associative continuum** — an 8-tier neural memory (working → forever)
  whose tiers are small MLPs updated by gradient descent at store time,
  with surprise gating, contradiction detection, supersession, and
  contrastive learning. This is the fuzzy "what do I know that's related
  to X" recall.
- **Cortex** — a slot-keyed store of canonical facts (one *current* value
  per `entity.attribute`), with deterministic reads, provenance tiers
  (`user > action > agent`), and contender parking instead of silent
  overwrites.
- **Knowledge graph** — typed entities and edges over those facts, with a
  closed relation vocabulary, on-read transitive/inverse inference, and
  optional openCypher via Apache AGE.
- **World cortex** — durable, *cited* facts about external reality (a
  current version, a price, who holds a role) with age-decayed trust, kept
  separate from your own facts.
- **Reference bank** — a ChromaDB document store for RAG over files you
  ingest.

State lives in Postgres (the durable source of truth) behind a single
long-lived daemon; every session attaches through a thin stdio shim. The
result: Claude can pick up where it left off, correct itself when facts
change, and reason over relationships — without you re-explaining context
each session.

## Tools exposed

| Tool | Purpose |
|------|---------|
| `memory_store(text, source?, tags?, origin?)` | Remember a fact, decision, observation (slot-shaped facts auto-promote to the cortex) |
| `memory_search(query, top_k?, sources?, bands?, episodes?, tags?, min_score?, disable_recency_boost?, rerank?, bm25?)` | Retrieve by associative similarity |
| `memory_trace(query, top_k?, sources?, bands?, episodes?, tags?, rerank?, bm25?)` | Search + full ranking trace — debug why an entry didn't surface |
| `memory_recent(n?, sources?, episodes?, tags?)` | List newest stores (debug + session start) |
| `memory_list_sources()` | Enumerate every source tag in the bank with entry counts |
| `memory_list_tags()` | Enumerate every multi-valued tag in the bank with occurrence counts |
| `memory_supersede(old_text, new_text)` | Explicit correction — mark old fact obsolete |
| `memory_delete(text?, substring?, source?, episode?, tag?)` | Remove memories matching any filter (hygiene) |
| `memory_episode_start(title, hint?)` | Open a bracketed working session — entries stored while open carry the episode id |
| `memory_episode_end()` | Close the currently-open episode |
| `memory_episode_list(limit?, include_open?)` | List episodes newest-first with per-episode entry counts |
| `memory_episode_summary(id)` | Stats + tag/source distribution + recent entries within an episode |
| `memory_consolidation_candidates(query?, episode?, top_k?, min_cohesion?, ...)` | Cluster mutually-similar memories ripe for consolidation |
| `memory_consolidate(replaces, new_text, source?, tags?)` | Atomic supersede + store — replace a cluster with one canonical note |
| `memory_fact_get(entity, attribute)` | The one CURRENT canonical value at a slot (+ any parked contenders) |
| `memory_fact_set(entity, attribute, value, origin?, confidence?)` | Assert a canonical fact deliberately (insert / confirm / supersede / contest) |
| `memory_fact_resolve(entity, attribute, accept)` | Settle a contested fact after checking in — adopt (`true`) or discard (`false`) the contender |
| `memory_fact_forget(entity, attribute?)` | Hard-delete canonical fact(s) at a slot/entity (no audit trail) |
| `memory_facts(limit?)` | List all current canonical facts (cortex introspection) |
| `memory_world_set(entity, attribute, value, source_url?, source_quote?, freshness_class?, confidence?, ...)` | Assert a canonical WORLD fact — sourced *external* knowledge (current model/version/price/role), kept in its own table; age-decayed trust by freshness |
| `memory_world_search(query, top_k?)` | Search world facts by similarity — each carries `effective_confidence`, a `stale` flag, and its citation |
| `memory_world_facts(limit?)` | List all current world facts (world-cortex introspection) |
| `memory_world_forget(entity, attribute?)` | Hard-delete world fact(s) at a slot/entity (cleanup; never touches user/project facts) |
| `memory_dream_status()` | Read-only: backlog of unconsolidated memories + whether a dream would fire (safe for a SessionStart nudge) |
| `memory_dream_pull(limit?)` | Recent memories not yet consolidated — the agent extracts canonical facts from these (Tier 1) |
| `memory_dream_commit(cursor)` | Advance the dream cursor after consolidating up to `cursor` |
| `memory_dream_run()` | One server-side dream with the regex floor — headless, no LLM (Tier 0) |
| `memory_graph_relate(src, relation, dst, origin?, confidence?, src_type?, dst_type?)` | Assert a typed edge between entities (closed relation vocabulary; re-assertion bumps confidence) |
| `memory_graph_unrelate(src, relation, dst)` | Retract an edge (superseded, kept for audit) |
| `memory_alias(entity, alias)` | Bind an alternative name — all fact/graph lookups resolve aliases first |
| `memory_graph(entity, depth?, include_facts?, to?)` | Entity neighborhood (≤3 hops): nodes + facts + edges, with transitive/inverse edges derived on read |
| `memory_relation_define(name, description, transitive?, inverse_of?, src_type?, dst_type?)` | Grow the closed relation vocabulary (deliberate, strong-model act) |
| `memory_graph_query(cypher, limit?)` | Read-only openCypher via Apache AGE (strong-model power tool) |
| `memory_stats()` | Per-band sizes, hit rates, totals |
| `memory_save()` | Flush CMS tensors to disk |
| `document_ingest(path, source?)` | Index a file (txt/md/pdf) in the reference bank |
| `document_search(query, top_k?)` | RAG search over reference bank only |

Each tool returns plain JSON. See `pseudolife_memory/mcp_server.py` for
docstrings — those are what Claude reads to decide when to call which tool.

## Architecture (v0.2)

One **memory daemon** owns the bank and serves MCP over HTTP; every
Claude Code session (and any LAN agent, e.g. a redacted box) attaches to
it through a thin stdio **shim**. **Postgres 16 + pgvector** (in Docker)
is the durable source of truth — the in-memory MIRAS bands are a
write-through cache hydrated at startup; band MLP weights live in an
atomically-saved, disposable `weights.pt`.

```
Claude session A ─┐
Claude session B ─┤ stdio shim ─HTTP─► pseudolife-mcp daemon ─► Postgres (Docker)
redacted (LAN) ─────┘   (per session)       (single writer)         pgvector + AGE
```

This kills two v0.1 hazards by construction: a single writer means
concurrent sessions can't clobber each other, and entries are
transactional so a crash can't wipe the bank (only the retrainable
weights cache rides the periodic save).

### Knowledge graph (ontology-lite)

The cortex's canonical facts are joined to a typed entity graph
(Postgres mode only). Edges use a **closed relation vocabulary** —
builtins `depends-on`*, `part-of`*, `runs-on`↔`hosts`, `uses`,
`configures`, `stores-data-in`, `related-to` (* = transitive) — so a
weak model can't fragment the graph with `depends_on`/`dependsOn`
variants: common forms normalize automatically, true unknowns are
rejected *with suggestions*. Soft type hints warn but never reject.
Transitive closure and inverse mirroring are computed **on read** by
NetworkX inside `memory_graph`; derived edges arrive marked
`derived: true` with rule provenance, so multi-hop conclusions read as
plain facts — the server reasons, the model reads.

When the Apache AGE extension is present (it is, in the bundled compose
image), entities/edges are mirrored into an AGE graph and
`memory_graph_query` accepts read-only openCypher. Heal a drifted
mirror with `pseudolife-mcp age-sync`.

**Weak-model deployments (redacted):** expose only `memory_search`,
`memory_store`, `memory_fact_get`/`memory_fact_set`, `memory_graph`,
and `memory_graph_relate`. Do NOT expose `memory_graph_query` (Cypher
composition is a degrees-of-freedom hazard), `memory_relation_define`,
`memory_delete`, or `memory_fact_forget`.

## Install (Windows)

Requires Python 3.10+, Docker Desktop, and ~600 MB of disk (torch +
ChromaDB + the all-MiniLM-L6-v2 embedding model, fetched on first run).

```powershell
git clone https://github.com/Pseudogiant-xr/PseudoLife-MCP.git
cd PseudoLife-MCP
python -m venv .venv
.venv\Scripts\activate
pip install -e .

# 1. Start Postgres 16 + pgvector + AGE (one-time build, then persistent).
docker compose -f ops/docker-compose.yml up -d --build

# 2. Register the daemon to auto-start at logon (binds 127.0.0.1:8765).
ops\install-autostart.ps1
Start-ScheduledTask -TaskName "PseudoLife-MCP Daemon"
```

The `pseudolife-mcp` console-script is now on your PATH with three modes:
`pseudolife-mcp serve` (the daemon), `pseudolife-mcp` (the stdio shim —
auto-starts the daemon if absent), and `pseudolife-mcp embedded` (the
v0.1 in-process stdio server; no daemon, no Postgres — an escape hatch).

## Wire into Claude Code

Point `.mcp.json` (project) or `~/.claude/mcp_servers.json` (user) at the
**shim**. It connects every session to the shared daemon:

```json
{
  "mcpServers": {
    "pseudolife-memory": {
      "command": "C:\\path\\to\\PseudoLife-MCP\\.venv\\Scripts\\pseudolife-mcp.exe",
      "env": {
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
        "PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
        "PSEUDOLIFE_MCP_DATA_DIR": "%USERPROFILE%\\.pseudolife-mcp"
      }
    }
  }
}
```

Replace `C:\path\to\PseudoLife-MCP` with wherever you cloned the repo. The
`PSEUDOLIFE_MCP_DATABASE_URL` matches the bundled `ops/docker-compose.yml`
defaults (user/password `pseudolife`, host port `5433`) — change it only if you
edit the compose file.

The shim is torch-free, so sessions attach near-instantly; the daemon
pays the one-time embedder warmup once for everyone. On first run with a
v≤0.1 `cms_state.pt` present in `PSEUDOLIFE_MCP_DATA_DIR`, the daemon
auto-migrates it into Postgres and renames the originals `*.pre-v8.bak`
(never deletes them).

**Sharing memory on the LAN (e.g. a redacted box):** run the daemon with
`PSEUDOLIFE_MCP_HOST=0.0.0.0` and a `PSEUDOLIFE_MCP_TOKEN`; remote
clients set the same `PSEUDOLIFE_MCP_DAEMON_URL` + `PSEUDOLIFE_MCP_TOKEN`.
The daemon **refuses to bind a non-loopback host without a token**, and
Postgres itself stays loopback-only — the LAN only ever sees the daemon.

**Backups:** `ops\backup.ps1` runs `pg_dump` inside the container into
`data\backups\` with 7-day rotation.

## Configuration

Connection / deployment env vars:

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATABASE_URL` | _(unset → file mode)_ | Postgres DSN; when set, PG is the source of truth (schema v8). Unset → v0.1 file-only mode. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `http://127.0.0.1:8765` | Daemon the shim connects to (and auto-starts). |
| `PSEUDOLIFE_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | Daemon bind address. |
| `PSEUDOLIFE_MCP_TOKEN` | _(unset)_ | Bearer token; **required** to bind a non-loopback host. |
| `PSEUDOLIFE_MCP_DATA_DIR` | `./data` (cwd-relative) | Weights cache + legacy-migration source + ChromaDB. |
| `PSEUDOLIFE_MCP_CONFIG` | `<data_dir>/config.yaml` if present, else built-ins | Override MIRAS / embedding / memory config. |

The built-in defaults are tuned for Claude's use case:

- **Surprise threshold `0.2`** — Claude stores deliberately, so the gate
  doesn't need to be aggressive.
- **Meta-filter off** (`memory.meta_filter.enabled = false` in the MCP
  build) — the filter exists to drop auto-captured chat noise ("I don't
  have anything saved about that"); every MCP store is a deliberate tool
  call, and the filter's patterns collided with legitimate dev facts
  about memory systems themselves.
- **Recency base half-life 24h** (`memory.recency_base_half_life_s =
  86400`, vs the 1h chat default) — Claude Code sessions are hours-to-
  days apart; with a 1h half-life the recency boost was effectively
  always zero. Halves per band depth as before (1d → 2d → 4d → …).
- **Neural warmup ramp** — each band's retrieval blend
  (`w·neural + (1−w)·exact`) ramps `w` from 0 to
  `memory.neural_blend_weight` (0.6) over the band's first
  `memory.neural_warmup_updates` (50) optimisation steps, so a fresh or
  sparsely-trained band scores by near-pure cosine instead of riding an
  untrained MLP's output. `neural_warmup_updates: 0` restores the fixed
  v0.1 blend exactly.
- **MIRAS preset `continuum`** — the 8-tier `working / micro / instant /
  fast / medium / slow / archival / forever` continuum.
- **No NLI scorer** — the `cross-encoder/nli-deberta-v3-xsmall`
  contradiction model is ~278 MB and optional. The four-path detector
  works without it. Install with `pip install .[nli]` if you want it.
- **Cross-encoder reranker off** — the `ms-marco-MiniLM-L-6-v2` reranker
  (~80 MB) is wired into the pipeline but disabled by default. Flip it
  on either globally (`memory.reranker.enabled = true` in config) or
  per-call (`memory_search(..., rerank=True)`). First call lazy-loads
  the model from the HuggingFace hub; subsequent calls cost ~10ms per
  reranked candidate. Details below under **Cross-encoder reranking**.
- **BM25 hybrid lexical pool off** — a pure-stdlib BM25 sparse-retrieval
  channel runs in parallel with the dense embedder when enabled, fusing
  scores so exact-keyword queries (`process_chunk_v2`, `v0.7.6`,
  error codes) still surface even if the embedder underweights them.
  Off by default; flip via `memory.bm25.enabled = true` or
  `memory_search(..., bm25=True)`. Details below under
  **BM25 hybrid retrieval**.
- **No HyDE / no reflection** — both rely on an LLM callback. Claude *is*
  the LLM, so the natural way to reflect is for Claude to call
  `memory_store` with a self-composed summary.

## Usage patterns

**At session start:**
```
memory_search("project context for X")
```
Loads what you've worked on before, persistent across compactions.

**During work:**
```
memory_store("Decided to use stdio transport for the MCP because no port conflicts", source="pseudolife")
```
Stores a real decision. Skip fleeting chatter — the surprise gate will
drop near-duplicates anyway.

**When corrected:**
```
memory_supersede(
  "Provider interface uses synchronous calls",
  "Provider interface uses async calls — sync version was the v0.7 prototype only"
)
```
Marks the old fact superseded *and* stores the correction. Both will
surface in future retrieval, with the new one ranked higher.

**End of session:**
```
memory_save()
```
Optional — tensors persist eventually anyway, but `save()` snapshots
immediately so a crash doesn't lose recent stores.

**Discovering what's in the bank:**
```
memory_list_sources()
```
Returns every source tag and its entry count. Run this before scoped
searches so you know what tags actually exist instead of guessing.

**Debugging a retrieval miss:**
```
memory_trace("why didn't X come back?", sources=["pseudolife"])
```
Returns the same envelope as `memory_search` plus a `trace` dict: every
tier's candidates with raw_score, recency boost, source/supersession
multipliers, and the `drop_reason` (or `kept=True`) for each. The
`final_topk` block shows exactly which entries reached the result set
and what score they carried.

Also useful for state-probe queries where recency bias is unwelcome:
```
memory_search("current Python version", disable_recency_boost=True)
```

**Hygiene:**
```
memory_delete(source="test-noise")
memory_delete(substring="Junk entry")
memory_delete(text="Exact fact to remove")
```
At least one filter is required — bare `memory_delete()` returns an
error to prevent accidental wholesale deletion. For "keep the history
but mark it wrong" use `memory_supersede` instead.

**Cross-encoder reranking (Tier B):**
```
memory_search("which python testing framework do we use", rerank=True)
```
After the bi-encoder retrieval builds the top-N candidate set, run
`cross-encoder/ms-marco-MiniLM-L-6-v2` over each `(query, candidate)`
pair and fuse the resulting relevance score with the bi-encoder score:
```
final = fusion_weight * sigmoid(ce_score) + (1 - fusion_weight) * original
```
The default `fusion_weight = 0.7` leans on the cross-encoder but
preserves enough of the bi-encoder signal that recency / source /
supersession multipliers still nudge order on near-ties. Off by
default — enable per call with `rerank=True`, or globally via:
```yaml
memory:
  reranker:
    enabled: true
    model_name: cross-encoder/ms-marco-MiniLM-L-6-v2
    top_n: 20            # rerank the top-N candidates only
    fusion_weight: 0.7   # 1.0 = pure CE, 0.0 = pure bi-encoder
```
First call lazy-loads the ~80 MB model from the HuggingFace Hub; later
calls cost ~10 ms per reranked candidate on CPU (≈ 200 ms wall-clock
added to a top-20 search). If the model fails to load, the reranker
disables itself silently and retrieval falls back to bi-encoder ranking
— search never breaks because of an optional component.

`memory_trace(..., rerank=True)` surfaces the per-candidate
`original_score`, `ce_score`, and `fused_score` under `trace.reranker`
so you can see exactly how the cross-encoder reshuffled the
bi-encoder ordering.

**BM25 hybrid retrieval (Tier B2):**
```
memory_search("process_chunk_v2", bm25=True)
memory_search("ship blocker for v9.42.0", bm25=True)
```
Dense MiniLM-L6 embeddings are great for *semantic* similarity but
can underweight tokens with no real semantic neighbours — function
names, version strings, error codes, hex hashes. BM25 is the classic
sparse-lexical scorer (Okapi BM25 with Lucene-style IDF) that weights
tokens by inverse document frequency, so rare-but-exact tokens count
for a lot. The BM25 pool runs in parallel with dense retrieval and
fuses with weighted score-sum:
```
final = dense_score + weight * normalized_bm25_score
```
Entries already in the dense pool get *boosted*; entries only BM25
found enter at `weight * normalized_bm25` (intentionally below a
typical dense hit so semantic recall still drives ordering). The
tokenizer keeps underscored identifiers and dotted version strings
whole, lowercases everything, and filters a tiny stop list.
Configure globally with:
```yaml
memory:
  bm25:
    enabled: true
    k1: 1.5       # term-frequency saturation
    b: 0.75       # length-normalisation
    weight: 0.3   # contribution to the fused score
    top_n: 20     # how many BM25 hits to consider
    min_score: 0.1  # floor on normalised BM25 (drops noise)
```
No new dependencies — pure stdlib. Cost is one O(N tokens) index
rebuild per query, ≈ 20-50ms on a 40K-entry bank.

`memory_trace(..., bm25=True)` records per-hit `raw_bm25`,
`normalized`, and any BM25-only injections under `trace.bm25`.

**Episodes + tags (Tier C, schema v6):**

An *episode* is a bracketed working session. While an episode is open,
every memory stored carries the episode's id + title automatically, so
later queries can scope by session:

```
memory_episode_start("Tier C implementation work")
memory_store("Decided to keep tags orthogonal to source instead of merging them")
memory_search("design choices", episodes=[episode_id])
memory_episode_summary(episode_id)   # stats + tag distribution + recent entries
memory_episode_end()
```

Starting a new episode while one is already open auto-closes the prior
one (with a `closed_by_new_start=True` flag for telemetry) — Claude
isn't required to reliably call `memory_episode_end`. Episodes
persist alongside CMS state in `cms_state.pt` under the `episodes`
key. Pre-v6 saves load cleanly with an empty episode log.

Tags are a parallel multi-valued axis to `source`: pass
`tags=["decision", "blocker"]` on store, filter with
`memory_search(..., tags=[...])`. Normalised at store time (lowercased,
stripped, deduped). Set intersection non-empty for the filter to pass
(OR within the filter list, AND with the other filters).

**Consolidation workflow (Tier C):**

Long-running banks accumulate near-duplicate memories — the same fact
phrased five different ways across five sessions. The literature on
agent memory ([HiMem 2026](https://arxiv.org/abs/2601.06377);
[MIRIX 2024](https://arxiv.org/abs/2507.07957); the
[ICML 2025 position paper](https://arxiv.org/abs/2502.06975)) calls
consolidation — turning episodes into reusable semantic notes — *the*
most-important under-implemented capability of long-term LLM memory.

PseudoLife-MCP can't run an LLM inside the server (Claude Code doesn't
yet expose MCP sampling — see [feature request #1785](https://github.com/anthropics/claude-code/issues/1785)).
But it can surface clusters for Claude to consolidate manually:

```
memory_consolidation_candidates(query="MCP transport choice", top_k=20)
# → {clusters: [{cohesion: 0.84, size: 3, members: [<entry>, ...]}, ...]}

memory_consolidate(
  replaces=["MCP uses stdio transport", "stdio was chosen for MCP", "decided on stdio for MCP"],
  new_text="MCP transport is stdio — chosen over TCP to avoid port conflicts.",
  tags=["consolidated"],
)
# → {superseded_count: 3, new_memory_stored: true, ...}
```

The clustering is deterministic greedy: highest-relevance entry seeds
the cluster, any unclustered candidate whose cosine with the seed
clears `min_cohesion` (default 0.6) joins, cohesion is the mean
intra-cluster cosine, clusters are sorted by `cohesion × size`. Cost
is O(N²) within the candidate pool, bounded to `top_k` candidates.

`memory_consolidate` reuses the supersession machinery so the
predecessors stay in the bank but rank below the canonical note —
the audit trail survives but retrieval defaults to the current
phrasing. Useful idiom: tag the consolidation with `["consolidated"]`
so you can later scan with `memory_search(..., tags=["consolidated"])`
to see what's been distilled.

### Canonical facts — the cortex (schema v7)

Alongside the associative continuum (the 8 MIRAS bands) sits the **cortex**: a
slot-keyed canonical-fact store. Where the continuum is similarity-ranked and
decaying, the cortex is **identity-not-similarity, supersession-not-decay,
currency-not-frequency** — one *current* value per `(entity, attribute)` slot,
retrievable out of the context window.

- **Auto-capture.** Slot-shaped facts in any `memory_store(...)` are promoted
  into the cortex automatically at a 0.5 confidence floor — weak models get a
  canonical layer with zero extra calls. The extractor is deterministic and
  precision-first (v0.2): `<entity> <attr> is <value>` with the attribute
  drawn from a closed dev lexicon (port / version / host / branch / default
  timeout / …), `my <attr> is <value>` (→ entity `user`), `<Entity>'s <attr>
  is <value>`, `the <attr> of <entity> is <value>`, and single-line
  `<entity> <attr>: <value>` / `= <value>`. Questions, negations, code
  fences, and entity-less subjects ("the default branch is master") are
  deliberately skipped — name the entity, or use `memory_fact_set`.
- **Deterministic read.** `memory_fact_get("project", "language")` returns the
  one current value — no ranking, no stale duplicates. `memory_search` also
  surfaces matching facts ahead of associative hits (a `"cortex"` block).
- **Deliberate write / correction.** `memory_fact_set(entity, attribute, value,
  origin="user")` asserts a fact at higher confidence; setting a new value at an
  existing slot supersedes the old (kept as audit history).

### Provenance contenders — never silently overwrite a user fact

Every cortex fact carries a provenance tier: **`user` > `action` > `agent`**
(set via `origin=`, or defaulted from `source`). A write may only *supersede* a
slot whose current value is backed by an equal-or-weaker tier. A **weaker-tier**
write (e.g. an `agent` value conflicting with a `user`-stated fact), or one below
the confidence margin, is **not applied** — it's parked as a *contender*:

```python
memory_fact_set("db", "host", "10.0.0.5", origin="user")   # current
memory_fact_set("db", "host", "10.0.0.9", origin="agent")  # -> action="contested"
# current stays 10.0.0.5; "10.0.0.9" is parked. memory_fact_get shows both;
# memory_search flags the fact "contested": true.
memory_fact_resolve("db", "host", accept=True)   # human said yes -> adopt (user-confirmed)
# or accept=False -> discard the contender, current unchanged.
```

This catches the case where the agent *decides* to update something and the human
only said "yes/proceed": the discrepancy surfaces (at the write, in search, and in
`memory_fact_get`) so the agent can check in rather than overwrite. Set
`memory.cortex.protect_provenance: false` in `config.yaml` to disable and restore
pure newer-wins.

### World knowledge — the world cortex (schema v9)

A third layer sits beside the personal cortex: the **world cortex**, for durable
facts about *external* reality that a frozen training cut-off may have wrong or
stale — a current model version, a price, who holds a role, a research finding.
It's a separate slot-keyed store (its own `world_facts` table, `origin=source`),
so external claims never mingle with the user/project facts.

```
memory_world_set("anthropic", "latest-model", "opus-4.8",
                 source_url="https://...", source_quote="Opus 4.8 is the latest...",
                 freshness_class="volatile")   # weeks | "slow" months | "evergreen" never
memory_world_search("which Claude model is current")
# → entries with effective_confidence (age-decayed), a `stale` flag, and the citation
```

Each fact carries a **citation** (`source_url` + the 1–2 sentence `source_quote`,
not the whole page) and a `freshness_class` that drives **age-decayed trust** at
read time: past 2×TTL a fact is flagged `stale` (a lead to re-verify, not truth).
The trust contract: prefer a fresh, *cited* world fact over frozen training
intuition when they conflict — but cite it ("as of <date>, per <source>") rather
than presenting it as your own knowledge; your own cortex/episodic facts stay the
highest-trust ground truth. `memory_search` surfaces matching world facts in a
separate block, and `memory_world_facts` lists them all for audit.

> The world cortex here is populated **manually** via `memory_world_set`. The
> live-web `research_ingest` action (fetch + distil cited world facts
> automatically) is a redacted-agent-side capability that depends on that agent's
> web tool — it is not part of the standalone MCP server.

## Dreaming — consolidating memories into facts

A **dream** distils the recent associative stream (MIRAS) into canonical cortex
facts: pull unconsolidated memories → extract `(entity, attribute, value)` →
`memory_fact_set` → advance a monotonic cursor so each memory is processed once.
Because it keys on the **cursor**, not on "sessions", returning to an old session
later just appends more tail — nothing is reprocessed, and there is no
"session finished" event to detect.

Extraction is pluggable; pick the tier that fits — **no self-hosted model is
required**:

| Tier | How it runs | Needs | Quality |
|------|-------------|-------|---------|
| **0 — baseline** | `memory_dream_run` (regex floor) — headless, on-box, free | nothing | weak (`X is Y`, `key: value`, port/version) |
| **1 — default** | the **agent itself** is the gateway: the `/dream` command | the agent you already run | highest |
| **2 — opt-in** *(planned)* | daemon calls a configured OpenAI-compatible endpoint | one base-URL + key + model | high; free if local |

**Tier 1 — `/dream` (recommended).** Copy `examples/commands/dream.md` to
`.claude/commands/dream.md` in any project, then run `/dream`. The agent reads
`memory_dream_pull`, extracts durable current-state facts, writes them with
`memory_fact_set`, and commits the cursor. To run it on a cadence instead of by
hand, point a scheduled agent/cron job at the same prompt.

**Tier 0 — zero-config.** Call `memory_dream_run` (or schedule it) for a fully
headless pass with the deterministic regex floor — no LLM, nothing leaves the
machine.

What gets consolidated and when is configurable under `memory.dream`
(`eligible_sources` / `exclude_sources`, and the `min_batch` / `idle_seconds`
backlog+quiescence thresholds that `memory_dream_status` reports).

**Privacy & cost.** Tier 0 is on-box and free. Tier 1 spends the agent tokens
you already pay for (a scheduled daily dream is small but non-zero). Tier 2 with a
*cloud* endpoint sends memory text off-box — a local model (e.g. Ollama) keeps it
on-machine.

## Data layout

Everything lives under `PSEUDOLIFE_MCP_DATA_DIR`:

```
data/
├── memory_state/
│   └── cms_state.pt        # 8-tier MIRAS tensors + metadata
├── cortex_state.pt         # Slot-keyed canonical facts (cortex, schema v7)
├── chromadb/               # Reference bank (RAG documents)
└── config.yaml             # Optional overrides
```

To wipe Claude's memory: delete the `data/` directory and restart the
MCP server. To wipe just documents: delete `data/chromadb/`. To wipe just
neural memory: delete `data/memory_state/`.

## Testing

```powershell
.venv\Scripts\activate
pip install -e .[dev]
pytest tests/ -v
```

300 tests cover the MemoryService methods (store / search / recent /
supersede / stats / save / trace / list_sources / list_tags / delete),
the `memory_search` scoring overrides, the cross-encoder reranker
(15 unit + 4 integration), the BM25 hybrid lexical pool
(23 unit + 5 integration), schema v6 + episode lifecycle (12 + 14),
tag plumbing through store/retrieval (10), greedy clustering for
consolidation (10), the episode + tag service surface (16), the
atomic consolidation operation (6), the cortex canonical-fact store
(slot dedup / supersession / no-decay / key-normalisation + the
provenance tier-rank guard, contenders, and `resolve`), auto-promotion
on `store`, the cortex service + MCP surface, the world cortex (sourced
facts with citations + age-decayed freshness + `stale` flagging), the
alias-resolved cortex lookup regression (a fact under a canonical name is
reachable via any bound alias), and MCP-level dispatch
(tool registration + docstring sanity + end-to-end invocation for every
exposed tool through the FastMCP machinery). The v0.2 Phase 0 suites
add the config knobs, the meta-filter gate + pruned patterns, the
recency base half-life, the neural warmup ramp (incl. state
round-trip), and the dev-fact extractor (positives + precision
guards). The v0.2 Phase 1 suites add the Postgres storage layer
(schema idempotency, vector round-trips, write-through consistency,
legacy `.pt` migration, atomic weights + corrupt-file recovery), the
HTTP daemon (health, token auth, two-concurrent-clients no-lost-writes)
and the stdio shim spawn path — these skip cleanly when no test
Postgres is reachable. The Phase 2 graph suite covers normalization,
unknown-relation suggestions, soft type warnings, transitive/inverse
on-read inference (marked `derived`), depth caps, paths, fact↔entity
auto-linking, and the AGE Cypher layer (skip-if-unavailable). The
delete suite includes a persistence round-trip test (store → delete →
save → reload → verify gone). Reranker tests monkeypatch
`sentence_transformers.CrossEncoder` with a deterministic stub so the
suite stays fast and offline. Test suite uses a fresh embedder per
module via the `warm_service` fixture so the ~1.5s
sentence-transformers load doesn't dominate runtime.

The PG-backed suites target a throwaway `pseudolife_memory_test` database on
the bundled dev container (`ops/docker-compose.yml`, port 5433) — created on
first run and reset per-test, so the whole suite is green on repeat runs and
never touches your real bank. Point them elsewhere with
`PSEUDOLIFE_TEST_DATABASE_URL`. With the container up, `pytest tests/` runs all
300; without any Postgres, the PG suites skip and the pure-logic suites still
pass.

## Capabilities at a glance

| Capability | Status |
|---|---|
| Transport | MCP stdio shim → HTTP daemon |
| Storage | Postgres 16 + pgvector (source of truth); ChromaDB for the reference bank |
| Associative continuum | 8-tier MIRAS bands, surprise gating, supersession, contrastive learning |
| Canonical-fact cortex | Deterministic auto-promote on `store` + `memory_fact_*` |
| Provenance contenders | Tier-rank guard `user > action > agent`; `memory_fact_resolve` |
| Knowledge graph | Typed entities/edges, closed relation vocab, on-read closure, AGE Cypher |
| World cortex | `memory_world_*` — cited external facts + age-decayed freshness (manual ingest) |
| Episodes + tags | `memory_episode_*`; multi-valued `tags=[...]` on store/search/delete |
| Consolidation | `memory_consolidation_candidates` + `memory_consolidate` |
| Cross-encoder reranker | Optional (`rerank=True` per call, ~80 MB) |
| BM25 hybrid pool | Optional (`bm25=True` per call, stdlib only) |
| NLI contradiction scorer | Optional (`pip install .[nli]`, ~278 MB) |
| Schema version | v9 (additive; legacy file-mode `.pt` banks auto-migrate into Postgres) |

## What's not built yet

- **Reflection via MCP sampling** — the MCP protocol has a `sampling`
  capability that lets servers ask the client (Claude) to generate text.
  Wiring that up would bring the periodic-reflection feature back without
  needing a bundled LLM. [Claude Code doesn't yet support
  sampling](https://github.com/anthropics/claude-code/issues/1785) — until
  it does, `memory_consolidation_candidates` + `memory_consolidate`
  give Claude the same outcome through manual tool calls.
- **Cross-machine sync** — memory lives on one PC's disk. Syncing
  `data/` via rclone / syncthing / git-lfs is left as an exercise.
- **Hierarchical summarisation** — periodic auto-summaries at multiple
  time scales (daily, weekly). Mostly subsumed by Tier C's episode +
  consolidation flow; what's left is the *cadence* automation.
- **Automated world-knowledge ingestion** — the world cortex stores and serves
  cited external facts, but populating it from the live web (`research_ingest`)
  needs a web-fetch tool the standalone server doesn't ship. Today, assert world
  facts manually with `memory_world_set`; a redacted agent with web access can
  automate it.

## License

MIT — see [LICENSE](LICENSE).
