# Retention bench realism — re-derive `retention_boost` under an honest workload — design

**Date:** 2026-06-26 · **Status:** approved (design), implementing
**Background:** full-review item **P1.6**. `retention_boost=1.0` (daemon default,
`_apply_mcp_defaults`) was tuned on `evals/retention_bench.py`, a *synthetic*
bench. The live retention machinery is dormant (bands ~0.4 % full), so it cannot
be revalidated against real accrual — but the synthetic bench can be made
**honest**, and the default re-derived from that.

## The problem with the current bench

Eviction scoring (`memory/miras/retention.py`) depends only on `access_count`,
`timestamp` (age), and `surprise_score` — **not the embedding**. The final score
is `source_weight × (access_count/age + surprise·w) + retention_boost·log1p(reinforcements)`.
Against that, the current bench (`evals/retention_bench.py`) makes three
unrealistic choices that **inflate** `retention_boost`'s apparent value:

1. **Uniform reinforcement** — exactly every 4th entry, exactly 5 each. Real
   reinforcement is heavy-tailed (a few hot entries, most never).
2. **Noiseless access** — `access_count = i` (perfect linear with recency).
3. **Reinforcement independent of access** — the decisive flaw. In the real
   system `memory_get` / `memory_reinforce` bump `access_count`, so a reinforced
   entry already carries elevated access; the base `access_count/age` term
   *already* partly protects it. Ignoring this makes reinforced-old entries look
   unprotected, so the boost looks essential.

## The honest model (deterministic, seeded)

Evolve `retention_bench.py` in place:

- **Heavy-tailed reinforcement.** A `REINFORCED_FRACTION` (≈0.25) of entries are
  reinforced; counts ~ a capped Pareto power law (most 1–3, a few large).
- **Coupled, noisy access.** `access_count = i + ACCESS_COUPLE·reinforcements +
  gauss(0, ACCESS_NOISE)`, floored at 0 — each reinforcement is ~1 access
  (`ACCESS_COUPLE = 1.0`).
- **One workload, reused across the grid.** Generate the reinforcement set,
  counts, and access values **once** under a fixed seed, then run every
  `retention_boost` in `GRID` against that identical workload — so the only
  variable is the boost (no confound). Keep neutral source (isolates the boost),
  4× overfill (forces eviction), and the existing `GRID`.

Timestamps stay `i` (clean recency gradient); bursty timestamps are a possible
later refinement, not needed for the boost question.

## Test invariant change

`tests/test_retention_bench.py` currently asserts *"at boost=0 reinforced gets no
protection"* (`reinforced_survival_rate ≤ unreinforced_survival_rate`). The
coupling overturns this by design. Replace with the invariant that survives an
honest model:

- `reinforced_survival_rate` is **non-decreasing** across the grid (more boost can
  only raise a reinforced entry's score — a sound monotonicity sanity check).

"Boost measurably helps" is **no longer a hard assertion** — the honest model may
show the boost is largely redundant once access-coupling is credited. That is the
finding, not a regression.

## Deliverable

1. The honest bench + the updated test.
2. Re-run; report the new knee and the reinforced/unreinforced survival curve.
3. **Decision:** keep `retention_boost=1.0` in `_apply_mcp_defaults`, or change it
   to the re-derived value — with the numbers. If it changes, update
   `_apply_mcp_defaults` + a CHANGELOG note. If the model shows the boost is
   redundant at this workload, say so plainly (don't force a knee).

## Out of scope

- Right-sizing band capacities (entangled with the 2026-07-06 continuum re-eval).
- Any live-daemon redeploy.
- Bursty timestamps / real-corpus embeddings (embeddings don't affect eviction).
