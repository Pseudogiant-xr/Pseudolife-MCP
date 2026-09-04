# Retrieval — search, recall, and the knowledge graph

How `memory_search` ranks, the optional reranker and BM25 channels,
abstention, ranking-trace debugging, multi-hop `memory_recall`, and the
knowledge graph it walks. Part of the [user guide](../../README.md#documentation).

## Asymmetric query and document encoding

Since schema v25, the default embedding backbone (`Qwen/Qwen3-Embedding-0.6B`)
is instruction-asymmetric: `memory_search` and every other retrieval probe
(cortex/world/lesson search, recall seed queries) encode the query text with
an instruction prefix (`EmbeddingConfig.query_prefix`) that stored documents
never carry — entries, fact/world/lesson claim text, and slot/entity-name
embeddings are all encoded bare, and every stored-to-stored comparison
(dedup, curation, alias candidates, the surprise gate) stays bare on BOTH
ends. Two distinct threshold effects follow, and they should not be
conflated. The `min_score` 0.2/0.25 recall floors (and `supersede()`'s
embedding-fallback paraphrase probe) now gate a prefixed-query-to-document
cosine rather than a doc-to-doc one — their *semantics* shifted, not just
their scale, and they read somewhat more conservative at the shipped
defaults. By contrast, `alias_candidate_min_cosine`,
`curation_min_similarity` and the surprise gate remain doc-to-doc
comparisons whose cosine *distributions* merely shift with the new model.
All are left unrecalibrated pending live data. See
[Configuration](configuration.md#built-in-defaults-tuned-for-claudes-use-case)
for the config fields and the [schema version history](configuration.md#schema-version-history)
for the v25 cutover itself.

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

**On by default since 2026-07-25** — pass `bm25=False` to opt out of a
single call. It shipped disabled for a year, which meant every published
retrieval number measured the dense pool alone.

```
memory_search("process_chunk_v2")
memory_search("ship blocker for v9.42.0")
```

Dense embeddings (Qwen3-Embedding-0.6B by default) are great for *semantic*
similarity but can underweight tokens with no real semantic neighbours — function
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

Since 2026-07-30 the same channel is also *available* for **cortex fact
retrieval** (`cortex_search`), but ships **opt-in**
(`memory.bm25.cortex_enabled = false`, or per-call `bm25=True`) — unlike
the turn pool. The pre-registered A/B that decided this
(`evals/results/bm25-ab-confirmation.json`, `_s` haystacks, reproducible
server, rag arm as byte-identical control): the fusion changed 56/78
served fact contexts yet moved **nothing end to end** — cortex accuracy
0.179 and cascade 0.423 in both arms — and cost ~1 question on the
oracle regression-gate slice. Lexical gaps in fact retrieval are real
but the abstentions trace to fact *coverage*, not ranking, so the
default stays honest to the measurement. When enabled, the fusion is
identical to the turn pool's, run over each fact record's composed
`entity — attribute: value` text — per member record for set-valued
slots, so an exact member name can rank on its own; grouping into one
set entry happens after fusion — with one deliberate difference:
lexical fact hits gate on the normalised `bm25.min_score`, *not* the
caller's dense `min_score` floor, so an exact-name query can rescue a
fact the embedder under-scores (useful on identifier-heavy corpora —
agent-trajectory content is where this channel earned its keep in the
LME-V2 smoke).
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

## Superseded entries

An entry the contradiction pipeline marked superseded is **still
retrieved**, with its score multiplied by `0.55`. Current values therefore
outrank their own history without the history disappearing, which is what
lets an answer read "you used to have X, then you changed it to Y".

```yaml
memory:
  hide_superseded: true   # restore the pre-v0.7.3 hard filter
```

The filter is opt-in for a reason: hard-dropping superseded entries once
made a category query miss the only entry that named the category (the
entry had been superseded on an unrelated detail), and superseded rows
carry knowledge-update recall on LongMemEval. Use it for debugging and
audit, not as a deployment default. When on, it applies to both pools —
including BM25-only injections, which otherwise bypass the dense pool's
filters. `explain=True` reports dropped entries with
`drop_reason: "superseded"`.

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

Beyond one call, every `memory_search` also appends a row to the retrieval
event log (query, the ranked served list, the ranking components and the
knobs in force) and bumps a per-slot read counter, which `memory_stats`'
`read_audit` section summarises — see
[Configuration](configuration.md#built-in-defaults-tuned-for-claudes-use-case)
for both kill switches.

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

A merge folds the absorbed node's canonical into the survivor's aliases
without rewriting the cortex records written under it. Since 2026-09-02
both directions resolve: `memory_graph`, `memory_recall` and the dossier
attach an alias-keyed fact to the surviving node, and `memory_fact_get` /
`memory_history`'s chain view reach a record under the node's canonical or
any of its aliases. An alias that is also another entity's canonical is
skipped — that record belongs to the other entity. Slot-mode
`memory_history(entity, attribute)` is the one surface that still does no
alias resolution.

## memory_recall (multi-hop retrieval)

`memory_recall(query, hops=3, top_k=5)` answers **relational questions**
by iteratively following the knowledge graph — things `memory_search`
can't do with a single flat similarity pass.

**`top_k` bounds the seed search only, not the result.** It caps how many
initial hits name the entities the graph walk starts from; graph expansion
then fans out from those seeds with no bound of its own. The response is
kept bounded separately — see Return shape below.

**When to use it vs `memory_search`:**

- Use `memory_recall` for chain-of-links questions: "what does X ultimately
  run on?", "where does Y's data end up?", "how does A reach C?".
- Use `memory_search` for direct lookups: "what is X's port?", "what did I
  decide about Y?" — those are flat similarity queries and `memory_search`
  is faster and simpler.

**How it works.** `memory_recall` searches for a seed entity in the query,
then walks its graph neighbourhood one hop per iteration (up to `hops`,
capped at 5), accumulating bridging entities, facts, edges, and paths. It
never creates or modifies a memory, fact or edge — the only writes on the
path are telemetry: its seed searches append to the retrieval event log
(`memory.retrieval_log.enabled`) like any other `memory_search`. The facts
it attaches to a neighbourhood are deliberately *not* counted as slot
reads: they are context, not a direct answer.

### Constraint pinning (schema v35)

TypeRetrieve (arXiv 2608.22752):
a fact whose `distortion_tolerance` is `constraint` is served *ahead of*
the cosine ranking, marked `pinned: true`, when it is **in scope** — and
scope is defined cheaply and precisely, with no second embedding pass:
in `memory_search`'s cortex block, the query *names the fact's entity*
(both sides go through the cortex's slot normalisation, so `payments db`
matches `payments-db`, and the entity must occur as a separator-bounded
run, so `db` does not match `payments-database`; a raw-string test — it
does not resolve graph aliases, so a constraint written under an alias
later folded into another name is pinned by `memory_recall` but not by
the cortex block, a known open follow-up now that
`graph.alias_canonical_map` exists); in `memory_recall`, the
fact's entity is a **seed** of the walk (hop 0 — the entities the query
itself resolved to; hop-discovered entities are context, not scope, and
keep record order). Pinning is exemption from ranking, not from
relevance: a pin must clear the caller's `min_score` floor (the cortex
block's `guard_min_score`), pins take at most half of `top_k` so the
ranked answer always keeps the rest, and among pins the best cosine wins
the slots. A pin displaces the weakest ranked fact rather than growing
the payload, and
a pinned fact that never made the ranked list is served in the identical
shape with its true cosine as `score`, so a reader can see it was pinned,
not ranked. In recall the pin also guarantees the rule survives the
per-entity fact cap below. An unlabelled bank is served byte-identically;
`memory.cortex.pin_constraints = false` restores plain ranking. Under
`stale_policy = demote` a stale (`slow` / `volatile`) constraint still
sinks below the fresh ranked facts — staleness is a trust decision and
outranks the pin. The labels themselves are described in
[memory-model](memory-model.md#who-said-it-and-how-exactly-must-it-survive-schema-v35).

**Return shape:** `seeds`, `entities` (each with current canonical facts),
`edges` (with a `derived` flag for inferred transitive/inverse links),
`paths`, supporting `texts`, and `iterations`. A served fact that stands on
a memory corrected since the fact was last confirmed carries `re_verify:
true` plus `re_verify_reason` — see below.

**Re-verify: a flag, not a cascade.** The `re_verify` marker above appears
on `memory_search`'s cortex block, `memory_fact_get`, and `memory_recall`
(the default `verbose=False` projection carries it too). It is
best-effort, computed at read time from evidence that still exists:
`memory_traces.entry_id` is `ON DELETE CASCADE`, so a capacity eviction of
the source entry loses the trail before it ever flags anything. Full
contract: [memory model](memory-model.md#how-current-is-this-fact).

**Output caps (issue #186).** A plain 3-hop query on a hub entity can
return dozens of entities/edges and every matched entry's full text —
issue #186's live audit (2026-08-21, real daemon) measured one such query
at 93.7 KB, enough that the calling client refused it. `entities` /
`edges` / `texts` are each capped (10 / 15 / 6 by default), and each
entity's `facts` list is separately capped (5). These are NOT flat prefix
slices — `entities`/`edges` reserve a minimum quota per graph hop so a
hub seed's own crowded 1-hop ring can't silently push the deeper,
harder-to-reach hops (the actual reason to call `memory_recall` over
`memory_search`) out of the response; `edges` also prefers connections
between two entities that both survived the entity cap; `texts` reserves
budget for hop-discovered support so it isn't purely a copy of the flat
seed search. With `verbose=False` (the default), `texts` are also
truncated to a preview length; `verbose=True` returns full text (though
the same entity/edge/text counts and per-entity fact cap still apply).
A reproducible in-tree probe (`evals/recall_cap_probe.py`, no DB/daemon
needed) exercises this same capping path on a synthetic 41-entity/40-edge
fixture graph and recorded a 24.5 KB → 3.8 KB (84.4%) reduction with
deep-hop entities still present in the result
(`evals/results/recall-cap-186-payload-probe.json`) — a different, smaller
graph than the live audit's, so the two byte counts aren't comparable
to each other, only each to its own before/after.

**Search fan-out caps (2026-09-04).** The output caps above bound what
comes *back*; these bound what the walk *spends*. Each hop re-queried every
newly discovered entity by name, so on a star-shaped graph one hub's ring
set the price of the whole call — measured on a restored copy of the live
bank at a mean of 89.15 searches and 25.25 s per call (max 205 and
57.67 s), enough that two live calls timed out at the MCP layer that
morning. Three knobs bound it, all in the Console's Recall group:
`memory.recall.max_searches_per_hop` (default 6) re-queries only the top N
newly discovered entities per hop — ranked by mentions in the seed hits,
then by lowest degree — while still returning the rest as entities with
their facts; `max_total_searches` (default 31) and `time_budget_seconds`
(default 20.0) are hard ceilings over the whole call including the seed
search. 31 is deliberately a backstop, not a working limit: `hops` is
clamped to 1..5, so the most the per-hop cap can spend is
1 + 6 x 5 = 31 and no request the tool accepts is cut by the ceiling —
only a raised `max_searches_per_hop` reaches it. Hitting either ceiling
stops the walk and adds `truncated: true` and `searches_issued: N` to the
response instead of raising, and those two fields are absent when neither
bound, so a walk that stayed under every cap has an unchanged response.
Read their absence narrowly: it means no ceiling tripped, NOT that nothing
was dropped. `max_searches_per_hop` is the cap that binds in ordinary use
and it deliberately sets no flag, because it changes only which re-queries
run, never the entities and edges the walk returns. `truncated` claims only
what it knows: some re-queries, and possibly deeper hops, were skipped, so
supporting texts and deeper entities may be missing — a ceiling that trips
inside the last permitted hop's re-queries leaves that hop's entities and
edges complete and cuts only `texts`. Graph expansion is deliberately
untouched (the hub gate and
`max_entities` already bound it): on the paired 20-question run
(`evals/recall_fanout_bench.py`,
`evals/results/recall-fanout-cap-20260904.json`) the caps took the mean
call to 12.40 searches and 4.166 s with the entity, edge and iteration
counts identical on every question and no expected target lost. A fourth
knob, `skip_part_of_expansion`, is eval-only and default-off: it drops the
re-query for entities reached only through `part-of` edges.

**`low_confidence: true`** means no seed entity matched the query — the
graph had no starting point. In that case fall back to `memory_search`.

**Driver config.** By default `memory_recall` uses the **mechanical** seed
driver (token-intersection heuristic — no LLM call, deterministic, fast).
Set `PSEUDOLIFE_RECALL_DRIVER=llm` to use the dream endpoint for seed
resolution (better recall on ambiguous entity names; requires the dream
extractor to be configured).
