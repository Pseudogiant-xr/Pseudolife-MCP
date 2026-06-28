# Relation-Confidence Repair (Phase 2 / "A") — Design

**Status:** design · **Date:** 2026-06-28 · **Author:** Claude (Opus 4.8) + user

## 1. Problem & motivation

The relation-extraction eval (`evals/relation_extraction_bench.py`, 2026-06-28) measured
the dream graph-from-text path and surfaced two concrete, model-independent defects that
match what actually polluted the live graph (the 2026-06-27/28 Atlas cleanup):

1. **The relation confidence channel is dead.** `_link_dream_relations` (`service.py:~1424`)
   writes *every* dream edge with a hard-coded `confidence = config.memory.dream.relation_confidence`
   (`0.6`), ignoring everything. `graph_review`'s dubious threshold is also `0.6`, so the
   "dubious edge" detector flags **100% of agent edges** — it carries no signal and cannot
   tell a good edge from nonsense.
2. **Type-violating structural edges.** The extractor applies `runs-on`/`hosts`/`stores-data-in`
   to type-incompatible endpoints (`User runs-on Windows 11`, `daemon runs-on schema 11`,
   `… stores-data-in <command-string>`). The bench's `type_violation_rate` was `0.111` for the
   shipped Gemma-E2B (`0.0` for E4B/Qwen-27B), and these are exactly the edges pruned by hand
   in Phase 1.

The bench also showed relationship-finding is *decent even at E2B* (edge-F1 `0.815`), so the
fix is **keep-and-repair**, not retrench or a bespoke model. This spec is the foundational
repair ("A"). A later, separately-designed **deep dream** ("C" — periodic self-clean +
cross-session link discovery) depends on A: it needs a real confidence signal to gate on.

## 2. Goal & success criteria

Replace the constant edge confidence with a **real, deterministic, on-box per-edge
confidence** that pulls down on the observed red flags (type-incompatibility, the vague
`related-to` catch-all), so that:

1. `graph_review`'s dubious detector discriminates (good edges clear it; violations/vague
   edges surface for review) instead of flagging everything.
2. Type-violations are penalized (and optionally auto-dropped) without losing recall on
   correctly-extracted edges whose entities we simply can't type.
3. The mechanism is a single, explainable function — no extra model calls, no prompt change,
   no dependence on small-model self-rating.

### Non-goals
- No change to the extractor prompt or `extract_relations` (confidence is computed at *link*
  time, not requested from the model).
- No model swap (E2B→E4B) — that's a separate operational choice.
- No cross-session linking or periodic full-corpus pass — that's "C".
- No hard-coded entity-type taxonomy beyond the lexicon needed to catch the observed defects
  (YAGNI; `unknown` is always neutral).

## 3. Approach (chosen: unified soft confidence-gating)

The two repairs are **one mechanism**: a structural confidence score that folds in
type-compatibility, gated by the existing `graph_review` threshold + review queue (with an
optional hard floor). Chosen over a *separate hard type-filter + model-emitted confidence*
because the unified/soft approach (a) is non-destructive and matches the system's
park/supersede philosophy, (b) tolerates a *noisy* type signal — a wrong/unknown type only
nudges a score rather than silently dropping a real edge, preserving recall, (c) unifies both
defects under one knob, and (d) hands "C" the confidence signal it needs.

## 4. Components & file structure

A new pure, DB-free, unit-testable module **`pseudolife_memory/memory/relation_quality.py`** —
the single source of truth for relation type rules:

- `TYPE_CONSTRAINTS: dict[str, tuple[set[str], set[str]]]` — allowed `(src_types, dst_types)`
  per structural relation. Initial set (matching the bench, now homed here):
  - `runs-on`: `{service, process, component, tool, file} → {runtime, host}`
  - `hosts`: `{runtime, host} → {service, process, component}`
  - `stores-data-in`: `{service, process, tool} → {datastore, file}`
  - `part-of`: `{component, service, file, datastore} → {component, service}`
  - (`depends-on`/`uses`/`configures`/`related-to` are intentionally absent — any→any, no
    type penalty.)
- `infer_type(name: str) -> str | None` — deterministic lexicon (§5).
- `edge_confidence(src: str, relation: str, dst: str) -> float` — structural confidence (§5).

**DRY unification with the bench:** `evals/relation_extraction_bench.py` replaces its local
`RELATION_CONSTRAINTS` with `from pseudolife_memory.memory.relation_quality import TYPE_CONSTRAINTS`.
The thing the bench *measures* (`type_violation_rate`) becomes literally the thing production
*penalizes*. (The bench keeps its hand-labeled *gold* entity types for measuring the ideal;
production uses `infer_type` as the deployable on-box approximation — same relation-constraint
table, honestly different type sources.)

Three small edits:
- **`service._link_dream_relations`** (`service.py:~1424`): replace
  `conf = float(self.config.memory.dream.relation_confidence)` with
  `conf = edge_confidence(raw_src, relation, raw_dst)` (computed per edge, inside the existing
  loop, before `upsert_edge`); skip the write when `conf < self.config.memory.dream.min_relation_confidence`.
- **`config.py DreamConfig`**: add `min_relation_confidence: float = 0.0` (default = write
  everything; non-destructive). `relation_confidence` (0.6) is retained only as the documented
  legacy default and is no longer the written value.
- **`graph_review.py`**: no logic change to the detector; refresh the finding label from
  "low-confidence inferred edges" to read honestly now that confidence varies (e.g.
  "low-confidence / type-suspect edges"). `_DUBIOUS_CONF` stays `0.6` (good edges at 0.70 clear
  it; `related-to` 0.45 and violations 0.175 fall below).

## 5. The lexicon + confidence formula

**`infer_type(name)`** — operate on `name.lower().strip()`; return the first bucket matched,
else `None` (unknown). Buckets (regex/substring/suffix rules):
- `person` — `{user, the user, i, me, admin, operator}`
- `runtime` / `host` — `docker*`, `windows*`, `linux`, `wsl`, `host`, `vm`, `kubernetes`/`k8s`,
  `container`, `4090`, `gpu`, `cpu`
- `datastore` — `postgres`/`postgresql`/`pg`, `chromadb`, `redis`, `valkey`, `sqlite`, `kafka`,
  `rabbitmq`, `*-db`, `*database*`, `*bucket*`
- `file` — endswith `.py/.yaml/.yml/.json/.md/.txt/.sql/.ps1/.sh/.gguf/.toml/.ini/.cfg`
- `service` — `daemon`, `*-service`, `*-server`, `*-worker`, `*sidecar*`, `gateway`
- `tool` — snake_case identifier (`^[a-z][a-z0-9_]*$` with `_`), `memory_*`, `*()`
- `concept` (non-entity) — version strings (`\bv?\d+(\.\d+)*\b` dominates the token),
  bare numbers, `schema`, command-strings (startswith `docker `/`git `/`pip `/`docker compose`),
  `branch`/`master`/`main`
- else → `None`

The buckets are ordered so the most specific/structural wins. Notably, **command-strings and
`concept` are tested *before* the `runtime` glob**, so `docker compose -f ops/...` resolves to
`concept` while bare `docker`/`docker-desktop` resolves to `runtime`; and `file` (suffix) is
tested before `tool` (identifier). Only the buckets needed to catch the observed defects are
included; everything else is `None` (neutral).

**`edge_confidence(src, relation, dst)`** — deterministic:

```python
def edge_confidence(src, relation, dst):
    base = 0.45 if relation == "related-to" else 0.70
    constraint = TYPE_CONSTRAINTS.get(relation)
    if constraint:
        st, dt = infer_type(src), infer_type(dst)
        if st and dt:                      # only when BOTH endpoints are confidently typed
            src_ok, dst_ok = constraint
            if st not in src_ok or dt not in dst_ok:
                base *= 0.25               # known type-violation
    return round(base, 3)
```

Resulting values: clean specific edge **0.70**; `related-to` **0.45**; known type-violation
**0.175**. `unknown` on either endpoint ⇒ no penalty (the recall-preserving safety valve).

## 6. Backfill (retroactive — the one live-data step)

A dry-run-first, idempotent `ops/backfill_edge_confidence.py` recomputes confidence for the
existing agent-origin edges so the review queue is meaningful for the *current* graph, not just
future dreams.

- **Mechanism:** `SELECT e.id, s.display AS src, e.relation, d.display AS dst FROM edges e JOIN
  entities s ON e.src_id=s.id JOIN entities d ON e.dst_id=d.id WHERE e.origin='agent' AND e.superseded_at IS NULL`,
  compute `edge_confidence`, then `UPDATE edges SET confidence=%s WHERE id=%s`.
- **Safety (per the live-bank lesson):** `ops/backup.ps1` first; plain `psycopg.connect()` with
  `SET lock_timeout`/`statement_timeout`; idempotent `UPDATE` only — no DDL, no
  `PostgresStorage()`/`ensure_schema()` constructor. `--dry-run` (default) prints the
  new-confidence distribution + a sample of would-change edges; `--apply` writes.
- Idempotent + re-runnable (recompute is pure). Deterministic, low-risk. If the user prefers
  forward-only, this component is skippable without affecting the rest of A.

## 7. Data flow

```
dream batch text ─► extract_relations (unchanged) ─► (src, relation, dst) triples
        ─► _link_dream_relations: for each triple:
               conf = edge_confidence(src, relation, dst)         ◄── relation_quality
               if conf >= min_relation_confidence: upsert_edge(..., confidence=conf, origin='agent')
        ─► graph_review.dubious_edges: confidence <= 0.6 ─► review queue (now discriminating)

ops/backfill_edge_confidence.py (one-off): existing agent edges ─► recompute conf ─► UPDATE
```

## 8. Testing

- **`infer_type`** — one assertion per bucket plus the real nonsense endpoints: `user`→person,
  `schema 11`→concept, `docker compose -f ops/...`→concept/None, `postgres`/`pg`→datastore,
  `config.yaml`→file, `memory_recall`→tool, `docker-desktop`→runtime, an arbitrary junk
  string→None.
- **`edge_confidence`** — clean specific (`daemon`/`service` runs-on `docker`/`runtime`)=0.70;
  `related-to`=0.45; type-violation (`user`/person runs-on `windows 11`/runtime)=0.175;
  unknown-typed endpoint stays 0.70.
- **`_link_dream_relations` integration** — drive a small batch through the existing graph
  fixtures; assert a type-violating triple is stored at low confidence and a clean one at 0.70;
  assert `min_relation_confidence` above a value drops the violation.
- **`graph_review` discrimination** — given a graph with one 0.175 edge and one 0.70 edge, the
  dubious detector flags only the former.
- **Bench still green** — after the bench imports `TYPE_CONSTRAINTS`, `tests/test_relation_bench.py`
  passes unchanged (the constraint table is identical to what the bench had inline).
- **Backfill** — a unit test of the pure recompute (rows in → `(id, new_conf)` out) against a
  fixture row set; the DB `UPDATE` path is exercised only by the PG-backed suite (skips without
  Postgres), like the other live-bank ops.

## 9. Risks & caveats

- **Lexicon coverage is partial by design.** `infer_type` only knows the buckets needed to
  catch the observed defects; most real entities resolve to `None` (neutral). That's
  intentional — A targets the *known* nonsense, not a complete ontology. As new violation
  shapes appear, extend the lexicon (cheap, tested).
- **Production penalizes on *inferred* types; the bench measures on *gold* types.** They share
  the relation-constraint table but not the entity-typing source, so production's
  type-violation suppression is an approximation of the bench's ideal. This is honest and
  documented; closing the gap (e.g. inferred-type scoring mode in the bench) is out of scope.
- **Backfill touches live data.** Mitigated by backup-first + dry-run-first + idempotent
  UPDATE-only; skippable.
- **`related-to` base (0.45) is a tunable guess.** It sits below the 0.6 dubious cutoff so the
  catch-all surfaces for review; adjust if it proves too aggressive.

## 10. Relationship to "C" (deep dream)

A is the foundation C builds on: the deep dream's cross-session link proposals must be
confidence-gated (to avoid *adding* the pollution we're removing), and its self-clean step can
reuse `edge_confidence` + `TYPE_CONSTRAINTS` to re-score and prune the existing graph. C is
designed separately once A lands.
