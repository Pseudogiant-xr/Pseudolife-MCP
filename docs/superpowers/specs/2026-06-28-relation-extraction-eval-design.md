# Relation-Extraction Eval (Phase 2 / Option B1) — Design

**Status:** design · **Date:** 2026-06-28 · **Author:** Claude (Opus 4.8) + user

## 1. Problem & motivation

The knowledge graph accumulates pollution: near-duplicate entities, type-violating
edges, and over-granular orphan nodes. A 2026-06-27/28 Atlas cleanup removed the worst
of it manually, and that triage produced a precise diagnosis: the pollution is almost
entirely from the **dream relation-extraction path**, which was shipped default-on and
**never benchmarked**.

Evidence (from `pseudolife_memory/memory/dream.py`, `service.py`, `evals/`):

- **Relation extraction was never on the eval ladder.** `evals/ladder_sweep.py` and its
  `README.md` measure only *fact* extraction (`gold_recoverable` / `stale_leak` /
  `tokens_per_query`) and *lesson* synthesis. The graph-from-text path (`extract_relations`)
  ships at `extract_relations=True` with zero quality measurement.
- **The relation confidence channel is dead.** `_link_dream_relations` (`service.py:~1413,1424`)
  writes *every* dream edge with a hard-coded `confidence = config.memory.dream.relation_confidence`
  (`0.6`), ignoring the model. The relations prompt (`dream.py` `_RELATIONS_PROMPT_HEAD`)
  does not even *ask* for confidence. `graph_review`'s `_DUBIOUS_CONF` is also `0.6`, so the
  "dubious edge" detector flags **100% of agent edges** — it is tautological and carries no
  signal.
- **No relation type constraints.** A 2B model freely applies `runs-on` / `hosts` /
  `stores-data-in` to non-host/non-store entities (e.g. `User runs-on Windows 11`,
  `live daemon runs-on schema 11`). Unknown relations silently collapse to `related-to`,
  making it a dumping ground.
- **Entity-naming inconsistency** is a *documented* Gemma-2B weakness (ladder findings
  2026-06-18: only Qwen-27B named entities consistently across turns). Entities are
  auto-created with only `norm_name` (case/punct) dedup, so each surface variant
  (`Gemma E2B` vs `Gemma 4 E2B sidecar`) becomes a separate node.

Fact extraction, by contrast, is **validated and good** (Gemma 4 E2B: gold 0.9 / stale 0.1 /
~25× fewer tokens than naive-RAG). So the strategic question — *"improve the extractor, or
is it the wrong tool?"* — reduces to **the relation half**, and the missing measurement is
what let the problem ship.

The wider goal (user's framing): measure first, then decide between **keep-and-repair**,
**Option C** (retrench the dream to facts-only, graph edges become explicit
`memory_graph_relate` only), or a **bespoke extraction model** (a separate project; the user
floated On-Policy Self-Distillation / OPSD, arXiv 2601.18734 — see §10).

## 2. Goal & success criteria

Build the **measurement** that turns "is the model strong enough at relation extraction?"
from a vibe into a number, and restore trust in the Atlas review queue with two cheap
analyzer fixes. Concretely, this is done when:

1. A new dev-only bench scores each extractor rung on a hand-labeled relation corpus and
   produces a per-rung scorecard (edge precision/recall/F1 + four defect-aligned
   diagnostics) plus a **gap-to-ceiling** report (each rung vs Qwen-27B vs Opus-4.8).
2. The Opus-4.8 ceiling rung is produced **in-session via subagents** (the user's included
   Claude Code usage), not a billed API call.
3. `graph_review.py`'s two heuristic bugs are fixed and unit-tested.

This spec is **measurement-first and deliberately thin (Option B1)**. The extractor
*repairs* (real relation confidence, type-constraint enforcement, semantic entity-dedup at
write) are **out of scope** — they become an evidence-driven follow-up spec targeting
whichever defect the numbers implicate.

### Non-goals
- No changes to the live extractor, `dream.py` prompts, or `_link_dream_relations`.
- No model swap (E2B→E4B) decision *made* here — only *measured*.
- No bespoke-model training. The bench's outputs seed it (§10), nothing more.

## 3. Approach (chosen: B1, measurement-first)

Considered three shapes during brainstorming:
- **B1 — measurement-first (chosen):** the eval rung + corpus + the two free analyzer
  fixes; defer extractor repairs until the numbers say which defect dominates.
- **B2 — measure + repair together:** rejected; repairs should be evidence-driven, not blind.
- **B3 — repair-first:** rejected; same reason, and it leaves the measurement gap open.

Follows the repo's established pattern of **one standalone bench script per concern**
(`memcot_bench.py`, `lesson_synthesis_bench.py` are separate from `ladder_sweep.py`), rather
than bloating the 636-line ladder.

## 4. Components

A new standalone **`evals/relation_extraction_bench.py`** plus two small `graph_review.py` edits.

1. **Gold relation corpus** — ~20–25 realistic, hand-authored notes as Python literals
   (like `ladder_sweep.PAIRS`), each labeled with its correct closed-vocab edges. Seeded
   from the *real* pollution patterns observed in the Atlas cleanup (the pruned nonsense
   edges are ideal adversarial cases). Self-contained + reproducible; **not** coupled to
   live-bank state.
2. **`--rung <name>` driver** — runs that rung's `extract_relations(texts, registry)`
   directly over the corpus (scoring the *raw model triples*, not the post-storage graph,
   to isolate extraction from the linking layer). Writes `evals/results/relations-<rung>.json`
   (including each rung's raw predicted triples). Reuses `ladder_sweep`'s `RUNGS`, `probe`,
   `make_extractor`.
3. **Defect-aligned scorer** — `score(predicted, gold, entities)` → the scorecard in §6.
4. **`--emit-prompts`** — writes `evals/results/relations_corpus_prompts.json`: each note
   paired with the *exact* system prompt + relation registry the headless LLM rungs receive.
   This is the single source for the in-session Opus rung (§7).
5. **`--report`** — aggregates `results/relations-*.json` into a per-rung table + the
   gap-to-ceiling verdict, mirroring `ladder_sweep.report()`.
6. **Two `graph_review.py` analyzer fixes** (§8) — pure code, unit-tested, model-independent.

The bench needs **no DB, no embedder, no ingest/consolidate** — `extract_relations` is a
pure model call. It loads only the builtin relation vocabulary (the same `(name, description)`
list the daemon seeds) and the rung registry.

## 5. Corpus schema

```python
# Entity registry: canonical name -> type + acceptable surface forms (aliases).
ENTITIES = {
    "pseudolife-daemon": {"type": "service",   "aliases": ["the daemon", "pseudolife daemon"]},
    "postgres":          {"type": "datastore", "aliases": ["the postgres db", "pg"]},
    "docker-desktop":    {"type": "runtime",   "aliases": ["docker"]},
    # ...
}

# Each note: source text + its gold closed-vocab edges (possibly empty).
CORPUS = [
    {"text": "The daemon runs in Docker and connects to Postgres on 5433.",
     "edges": [("pseudolife-daemon", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "I think the new dashboard looks much cleaner than the old one.",
     "edges": []},   # null note — opinion, no entity relationship
    # ...
]
```

Entity **types** (for type-violation scoring): `runtime`/`host`, `service`/`process`,
`tool`/`function`, `file`/`path`, `datastore`, `component`, `concept`, `person`.

Relation **type constraints** (hand-specified `(src_type → dst_type)` allowed sets) for the
structural relations only:
- `runs-on`: `{service, process, component, tool} → {runtime, host}`
- `hosts`: `{runtime, host} → {service, process, component}`
- `stores-data-in`: `{service, process, tool} → {datastore, file}`
- `part-of`: `{component, service, file} → {component, service}`
- `depends-on`: any → any (no constraint; transitive structural but not type-bound)

Four note classes (≈ even split):
1. **Clean structural** — unambiguous typed edges across the vocab.
2. **Canonicalization probes** — the same entity in different surface forms across several
   notes; gold pins one canonical + aliases (exercises naming consistency).
3. **Null notes** — opinions / chit-chat / no entity relationship; gold `[]` (exercises
   over-extraction).
4. **Type traps** — phrasing that tempts a violation (`"the migration touched schema v11"`
   must NOT yield `runs-on schema`).

## 6. Scorecard

Entity matching uses `graph.norm_name`; a predicted entity matches a gold entity if its
normalized form is in that gold entity's alias set. Relation must match exactly.

**Core (relationship correctness)** — over all predicted vs gold triples:
- `edge_precision`, `edge_recall`, `edge_f1` (lenient entity match per above; the lenience
  isolates "found the right relationship" from surface-naming, which is scored separately).

**Diagnostics (each maps to one defect → one repair lever):**
- `naming_consistency` → *duplicate nodes.* For each gold entity appearing in ≥2 predicted
  edges, count distinct predicted surface forms (post-`norm_name`). Report mean forms/entity
  (1.0 = perfect; >1 = the fragmentation that mints `Gemma E2B` vs `Gemma 4 E2B sidecar`).
- `type_violation_rate` → *nonsense edges.* Fraction of predicted **structural** edges
  (both endpoints resolvable to a gold entity) whose `(src_type → dst_type)` violates §5's
  constraint table. Edges with an unresolvable endpoint are excluded (counted under
  `over_extraction`, not here).
- `related_to_share` → *vocab laziness.* Fraction of predicted edges that are `related-to`.
- `over_extraction` → *orphan minting.* Two sub-metrics: spurious edges emitted on null
  notes (gold `[]`), and the fraction of predicted entities whose normalized form appears in
  no gold alias set **and** is absent from the note text (hallucinated).

**Operational:** `edges_per_note`, `extract_seconds`.

**Verdict = gap-to-ceiling, not a hard pass bar.** There is no naive baseline for relations,
so `--report` ranks each smaller rung against the **qwen-27b** (sovereign-local) and
**opus-4.8** (absolute) ceilings:

```
rung        edge_f1   type_viol   naming   related_to   over_extract
gemma-e2b      0.62      0.28       1.9       0.41          0.22
gemma-e4b      0.71      0.17       1.4       0.30          0.13
qwen-27b       0.84      0.04       1.1       0.12          0.04   (local ceiling)
opus-4.8       0.90      0.02       1.0       0.08          0.02   (absolute ceiling)
```

That gap is exactly what answers *improve vs retrench (C) vs train (OPSD)* — without this
spec pre-committing a threshold. The decision itself is the follow-up (§9).

## 7. Rungs & the in-session Opus ceiling

Headless rungs reuse `ladder_sweep.RUNGS` verbatim:
`floor` (regex — **n/a**, `RegexExtractor` has no `extract_relations`), `gemma-e2b`,
`gemma-e4b` (operator swaps the `:8081` GGUF), `qwen-27b` (LAN 4090). Each is run with
`--rung <name>`; unreachable LAN/sidecar rungs record `status: "unreachable"` and are skipped.

**`opus-4.8` rung — in-session, subagent-produced, frozen reference.** Procedure (documented
in `evals/README.md`):
1. `python evals/relation_extraction_bench.py --emit-prompts` → `results/relations_corpus_prompts.json`.
2. In a Claude Code session, **dispatch subagents** (Opus 4.8, the user's included usage) to
   run the extraction over those prompts — the same system prompt + registry the headless
   rungs use — returning predicted triples as JSON.
3. Collect into `results/relations-opus-4.8.json` (same shape the `--rung` driver writes).
4. `--report` scores it as the absolute ceiling.

Rationale: uses the user's subscription (no billed API key, no Anthropic-SDK dependency), and
the subagent outputs double as the **highest-quality silver labels** for the bespoke-model
path (§10). **Caveats, stated honestly:** it is a *frozen artifact* (regenerate by
re-dispatching the subagents — not headlessly re-runnable), and subagents are mildly less
deterministic than a temperature-0 endpoint — acceptable for a ceiling reference.

## 8. Analyzer fixes (`graph_review.py`)

Independent of the model; restore trust in the Atlas review queue.

1. **`_token_set` drops version/number tokens.** It currently filters tokens with `len ≤ 2`,
   discarding `v8`, `11`, `1`, etc., so `schema v8` / `schema 11` / `schema 15->16`,
   `Phase 1 plan` / `Phase 2 plan`, `locked decision 1/2/3`, `Atlas Stage 1/2/3` all collapse
   to one token and read as Jaccard `1.0` duplicates. **Fix:** keep numeric and version-like
   tokens (e.g. retain tokens matching `\d` or `v\d`), and/or require a shared *non-numeric*
   distinguishing token before flagging a duplicate. Validated against the known
   false-positive pairs from the Atlas cleanup.
2. **`_TEST_PATTERNS` over-matches legit memories.** `\bfixture\b` / `\btest-\b` flag the real
   `fixture devserver` and the `TDD pattern…fixture stubs` lesson. **Fix:** tighten to the
   genuine artifact shapes (`deploy-smoke-*`, `pl-healthcheck-*`, `payments/payments-db`,
   `noise agent`) and stop matching bare `fixture` / `test-` substrings inside descriptive
   prose. Both fixes ship with unit tests in `tests/test_graph_review.py` (new) or the
   existing graph-review test module.

## 9. Decision gate (what the numbers feed — out of scope to *act* here)

After the scorecard exists, the gap-to-ceiling drives a follow-up decision among:
- **Keep & repair** — if `gemma-e4b` (or `e2b`) is within an acceptable band of the 27B on
  edge-F1 *and* type-violation/naming are tractable: write the repair spec (real confidence,
  type constraints, entity-dedup) targeting the dominant defect.
- **Option C — retrench** — if even the local ceiling is poor or the small models are far
  from it: set `extract_relations=False`, graph edges become explicit `memory_graph_relate`
  only.
- **Bespoke model / OPSD** — if the task is learnable but no off-the-shelf small model
  clears the bar: a separate project (§10).

## 10. Future / out of scope

- **Extractor repairs** (the B2 content) — their own evidence-driven spec.
- **Bespoke extraction model** — a separate project. The user floated **OPSD** (On-Policy
  Self-Distillation, arXiv 2601.18734): one model is both teacher (privileged context) and
  student (note only), distilled on the student's own rollouts. Honest fit note: OPSD targets
  *reasoning with available ground-truth*; extraction has **no** gold labels and an obvious
  *bigger teacher* (Qwen-27B / the Opus-4.8 rung), which is the **on-policy distillation**
  setup OPSD positions itself against. For a first bespoke model, on-policy KD from the 27B is
  the more natural recipe; OPSD's privileged-context framing is interesting specifically for
  the canonicalization defect, but still needs a labeled anchor. **Every** training route
  needs labeled extraction data — and **this bench's per-rung raw triples are exactly that
  seed corpus** (gold = the hand-labeled set; silver = the Opus-4.8 / Qwen-27B outputs). So
  B1 is on the critical path regardless of the eventual decision.

## 11. Testing & isolation

- **Bench is dev-only**, not part of the package or the pytest suite (matches
  `memcot_bench.py` / `lesson_synthesis_bench.py`). No live bank, no `pseudolife_memory_bench`
  DB, no embedder — it only calls `extract_relations` against an endpoint (or reads a frozen
  JSON for the Opus rung).
- The **scorer is pure and unit-testable** (deterministic given predicted+gold). Add a small
  `tests/test_relation_bench_scorer.py` with a handful of synthetic predicted/gold pairs
  asserting each metric (a perfect match → F1 1.0 / naming 1.0 / violations 0; a known
  type-violation → counted; a null-note spurious edge → counted under over_extraction).
- The **two `graph_review.py` fixes** are unit-tested against the real false-positive pairs
  from the Atlas cleanup (regression guard).
- UTF-8 stdout (`sys.stdout.reconfigure`) like `ladder_sweep`, for the table glyphs and
  unicode entity names on Windows.

## 12. Risks

- **Corpus is hand-authored** → small and subjective. Mitigation: seed from real observed
  pollution; keep it ~25 notes (enough to separate rungs, small enough to hand-label well);
  treat absolute numbers as relative (gap-to-ceiling), not as universal truth.
- **Subagent non-determinism** on the Opus rung → mild scorecard noise at the ceiling.
  Acceptable for a reference; the headless rungs remain reproducible.
- **Lenient entity matching could over-credit** a model that gets relationships right but
  names entities badly — which is *why* `naming_consistency` is a first-class separate metric
  rather than folded into F1.
