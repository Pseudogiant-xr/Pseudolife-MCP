# Retrieval — search, recall, and the knowledge graph

How `memory_search` ranks, the optional reranker and BM25 channels,
abstention, ranking-trace debugging, multi-hop `memory_recall`, and the
knowledge graph it walks. Part of the [user guide](../../README.md#documentation).

## Cross-encoder reranking

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
added to a top-20 search). A `reranker.skip_margin` skips the pass when
the top-2 bi-encoder gap is decisive. If the model fails to load, the
reranker disables itself silently and retrieval falls back to bi-encoder
ranking — search never breaks because of an optional component.

`memory_search(..., rerank=True, explain=True)` surfaces the per-candidate
`original_score`, `ce_score`, and `fused_score` under `trace.reranker`
so you can see exactly how the cross-encoder reshuffled the
bi-encoder ordering.

## BM25 hybrid retrieval

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

`memory_search(..., bm25=True, explain=True)` records per-hit `raw_bm25`,
`normalized`, and any BM25-only injections under `trace.bm25`.

## Abstention & confidence floors

Off by default (`memory.search_confidence_floor = 0.0`). Set it above zero
and `memory_search` returns `low_confidence: true` whenever the top match
scores below the floor, so the agent can abstain instead of answering from
a weak hit. A cortex fact in the result always overrides it — but *which*
cortex facts count is tunable via `memory.cortex.guard_min_score` (default
`0.2`; a LongMemEval retrieval replay showed the old `0.3` floor served
*zero* facts for 60% of questions, because terse fact embeddings rarely
score 0.3 against a natural-language query even when they are the answer —
while going below 0.2 measurably hurt by diluting the context with weak
facts): only facts scoring at/above it are treated as a confident answer,
so weak topically-adjacent facts stop suppressing abstention.

The two are calibrated as a **pair**; the [`evals/`](../../evals/README.md)
sweep recommends `guard_min_score = 0.65` + `search_confidence_floor = 0.70`
for an abstention-on deployment (doubles abstention recall at zero
false-abstain).

## Debugging a retrieval miss

```
memory_search("why didn't X come back?", sources=["pseudolife"], explain=True)
```

Returns the normal search result plus a `trace` dict: every tier's
candidates with raw_score, recency boost, source/supersession multipliers,
and the `drop_reason` (or `kept=True`) for each. The `final_topk` block
shows exactly which entries reached the result set and what score they
carried.

Also useful for state-probe queries where recency bias is unwelcome:

```
memory_search("current Python version", disable_recency_boost=True)
```

## Knowledge graph (ontology-lite)

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

The graph store is Postgres `entities` hub as source of truth, with a
NetworkX derived read-model built on demand — behind a swappable
`GraphStore` interface. There is no AGE/Cypher dependency; `memory_graph`
serves multi-hop queries (neighborhood + derived/inverse edges + shortest
path).

## memory_recall (multi-hop retrieval)

`memory_recall(query, hops=3, top_k=5)` answers **relational questions**
by iteratively following the knowledge graph — things `memory_search`
can't do with a single flat similarity pass.

**When to use it vs `memory_search`:**

- Use `memory_recall` for chain-of-links questions: "what does X ultimately
  run on?", "where does Y's data end up?", "how does A reach C?".
- Use `memory_search` for direct lookups: "what is X's port?", "what did I
  decide about Y?" — those are flat similarity queries and `memory_search`
  is faster and simpler.

**How it works.** `memory_recall` searches for a seed entity in the query,
then walks its graph neighbourhood one hop per iteration (up to `hops`,
capped at 5), accumulating bridging entities, facts, edges, and paths. It
is **read-only** — it never writes to the bank or the graph.

**Return shape:** `seeds`, `entities` (each with current canonical facts),
`edges` (with a `derived` flag for inferred transitive/inverse links),
`paths`, supporting `texts`, and `iterations`.

**`low_confidence: true`** means no seed entity matched the query — the
graph had no starting point. In that case fall back to `memory_search`.

**Driver config.** By default `memory_recall` uses the **mechanical** seed
driver (token-intersection heuristic — no LLM call, deterministic, fast).
Set `PSEUDOLIFE_RECALL_DRIVER=llm` to use the dream endpoint for seed
resolution (better recall on ambiguous entity names; requires the dream
extractor to be configured).
