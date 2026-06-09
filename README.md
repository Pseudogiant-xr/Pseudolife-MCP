# PseudoLife-MCP

**Persistent neural memory for Claude Code via the Model Context Protocol.**

A stripped-down build of [PseudoLife](../PseudoLife-0.7)'s memory stack —
MIRAS 8-tier continuum + ChromaDB reference bank + supersession +
contrastive learning — wrapped as an MCP server. Lets Claude (or any
MCP-capable client) store and retrieve memories across sessions, surviving
context compactions and `/clear` resets.

## What this is

PseudoLife is a desktop chat app with a TITANS-style neural memory. The
**memory layer** is the interesting part: 8 parametric tiers updated by
gradient descent at inference time, with surprise gating, contradiction
detection, and supersession built in.

PseudoLife-MCP takes just that memory layer and exposes it as an MCP
server so Claude Code can use it directly — no Electron app, no LLM
backend, no chat engine. Claude *is* the LLM; the MCP server is its
persistent associative memory on disk.

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
| `memory_stats()` | Per-band sizes, hit rates, totals |
| `memory_save()` | Flush CMS tensors to disk |
| `document_ingest(path, source?)` | Index a file (txt/md/pdf) in the reference bank |
| `document_search(query, top_k?)` | RAG search over reference bank only |

Each tool returns plain JSON. See `pseudolife_memory/mcp_server.py` for
docstrings — those are what Claude reads to decide when to call which tool.

## Install (Windows)

Requires Python 3.10+ and ~600 MB of disk (torch + ChromaDB + the
all-MiniLM-L6-v2 embedding model, fetched on first run).

```powershell
cd C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

That's it. The `pseudolife-mcp` console-script is now on your PATH.

## Wire into Claude Code

Add this to your **`.mcp.json`** (project-level) or
**`~/.claude/mcp_servers.json`** (user-level):

```json
{
  "mcpServers": {
    "pseudolife-memory": {
      "command": "C:\\Users\\HAMO9\\ClaudeCode\\PseudoLife-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "pseudolife_memory.mcp_server"],
      "env": {
        "PSEUDOLIFE_MCP_DATA_DIR": "C:\\Users\\HAMO9\\ClaudeCode\\PseudoLife-MCP\\data"
      }
    }
  }
}
```

**Why explicit paths:** Claude Code may launch the server from any cwd.
Pointing at the venv's `python.exe` and an absolute data dir means memory
state lives in one stable location regardless of which project you start
Claude from.

After editing `.mcp.json`, restart Claude Code. The first tool call
takes 3-5 seconds (lazy embedder init); subsequent calls are sub-100ms.

## Configuration

Two env vars, both optional:

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATA_DIR` | `./data` (cwd-relative) | Where memory tensors + ChromaDB live |
| `PSEUDOLIFE_MCP_CONFIG` | `<data_dir>/config.yaml` if present, else built-ins | Override MIRAS / embedding / memory config |

The built-in defaults are tuned for Claude's use case:

- **Surprise threshold `0.2`** (vs PseudoLife's `0.3`) — Claude stores
  deliberately, so the gate doesn't need to be aggressive.
- **MIRAS preset `continuum`** — the 8-tier `working / micro / instant /
  fast / medium / slow / archival / forever` continuum, same as PseudoLife.
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

- **Auto-capture.** Slot-shaped facts in any `memory_store(...)` ("X is Y",
  "named X", "my X") are promoted into the cortex automatically at a 0.5
  confidence floor — weak models get a canonical layer with zero extra calls.
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

205 tests cover the MemoryService methods (store / search / recent /
supersede / stats / save / trace / list_sources / list_tags / delete),
the `memory_search` scoring overrides, the cross-encoder reranker
(15 unit + 4 integration), the BM25 hybrid lexical pool
(23 unit + 5 integration), schema v6 + episode lifecycle (12 + 14),
tag plumbing through store/retrieval (10), greedy clustering for
consolidation (10), the episode + tag service surface (16), the
atomic consolidation operation (6), the cortex canonical-fact store
(slot dedup / supersession / no-decay / key-normalisation + the
provenance tier-rank guard, contenders, and `resolve`), auto-promotion
on `store`, and the cortex service + MCP surface, and MCP-level dispatch
(tool registration + docstring sanity + end-to-end invocation for every
exposed tool through the FastMCP machinery). The delete suite
includes a persistence round-trip test (store → delete → save →
reload → verify gone). Reranker tests monkeypatch
`sentence_transformers.CrossEncoder` with a deterministic stub so the
suite stays fast and offline. Test suite uses a fresh embedder per
module via the `warm_service` fixture so the ~1.5s
sentence-transformers load doesn't dominate runtime.

## Differences from PseudoLife

| | PseudoLife | PseudoLife-MCP |
|---|---|---|
| Transport | Electron + FastAPI (HTTP) | MCP stdio |
| LLM | Claude / OpenAI / Gemini / LM Studio backends | None — caller is the LLM |
| Chat engine | Full streaming chat with tools | None |
| HyDE | Yes (Slice E) | No (caller can rephrase) |
| Reflection | Yes (Slice D, runs on daemon thread) | No (caller can summarize) |
| Contrastive | Yes (Slice F) | Yes — fires on `memory_supersede` and any negative-signal text |
| NLI scorer | Bundled (278 MB) | Optional (`pip install .[nli]`) |
| Cross-encoder reranker | — | Optional (`rerank=True` per call, ~80 MB) |
| BM25 hybrid pool | — | Optional (`bm25=True` per call, stdlib only) |
| Episode anchoring | — | Yes (Tier C — schema v6, `memory_episode_*`) |
| Multi-valued tags | Single `source` only | Yes (Tier C — `tags=[...]` on store/search/delete) |
| Consolidation workflow | — | Yes (Tier C — `memory_consolidation_candidates` + `memory_consolidate`) |
| Canonical-fact cortex | Yes (redacted-side dream) | Yes (deterministic auto-promote + `memory_fact_*`; no LLM dream) |
| Provenance contenders | — | Yes (tier-rank guard `user>action>agent`; `memory_fact_resolve`) |
| Reference bank | Yes | Yes |
| Schema version | v5 | v7 (additive — pre-v6 saves load cleanly; cortex co-persists in `cortex_state.pt`) |

The MCP build is **not** save-compatible with PseudoLife's data dir, even
though the on-disk schema is the same — they're separate memory
instances by design. If you want to inspect PseudoLife's memory from
Claude, copy its `memory_state/` into the MCP's data dir; the loader will
read it cleanly.

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

## License

MIT. The PseudoLife memory stack this is derived from is also MIT.
