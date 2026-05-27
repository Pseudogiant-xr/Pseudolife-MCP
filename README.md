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
| `memory_store(text, source?)` | Remember a fact, decision, observation |
| `memory_search(query, top_k?, sources?, bands?, min_score?, disable_recency_boost?, rerank?)` | Retrieve by associative similarity |
| `memory_trace(query, top_k?, sources?, bands?, rerank?)` | Search + full ranking trace — debug why an entry didn't surface |
| `memory_recent(n?, sources?)` | List newest stores (debug + session start) |
| `memory_list_sources()` | Enumerate every source tag in the bank with entry counts |
| `memory_supersede(old_text, new_text)` | Explicit correction — mark old fact obsolete |
| `memory_delete(text?, substring?, source?)` | Remove memories matching any filter (hygiene) |
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

## Data layout

Everything lives under `PSEUDOLIFE_MCP_DATA_DIR`:

```
data/
├── memory_state/
│   └── cms_state.pt        # 8-tier MIRAS tensors + metadata
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

59 tests cover the MemoryService methods (store / search / recent /
supersede / stats / save / trace / list_sources / delete), the
`memory_search` scoring overrides, the cross-encoder reranker
(15 unit tests + 4 integration), and MCP-level dispatch (tool
registration, docstring sanity, end-to-end invocation through the
FastMCP machinery). The delete suite includes a persistence round-trip
test (store → delete → save → reload → verify gone). Reranker tests
monkeypatch `sentence_transformers.CrossEncoder` with a deterministic
stub so the suite stays fast and offline. Test suite uses a fresh
embedder per module via the `warm_service` fixture so the ~1.5s
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
| Reference bank | Yes | Yes |
| Schema version | v5 | v5 (same on-disk format) |

The MCP build is **not** save-compatible with PseudoLife's data dir, even
though the on-disk schema is the same — they're separate memory
instances by design. If you want to inspect PseudoLife's memory from
Claude, copy its `memory_state/` into the MCP's data dir; the loader will
read it cleanly.

## What's not built yet

- **Multi-tag support** — `source` is currently a single free-form
  string. Multi-tag would let you filter by `tags=["pseudolife", "v0.7.6"]`.
  Likely lands as a schema-v6 additive field.
- **Reflection via MCP sampling** — the MCP protocol has a `sampling`
  capability that lets servers ask the client (Claude) to generate text.
  Wiring that up would bring the periodic-reflection feature back without
  needing a bundled LLM.
- **Cross-machine sync** — memory lives on one PC's disk. Syncing
  `data/` via rclone / syncthing / git-lfs is left as an exercise.

## License

MIT. The PseudoLife memory stack this is derived from is also MIT.
