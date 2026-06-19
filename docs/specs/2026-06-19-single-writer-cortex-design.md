# Single-writer cortex — eliminate auto-promote fragmentation — design spec

Status: **proposed (design)**, pending review + plan.
Target: **PseudoLife-MCP** `origin/master`. Make the LLM dream pass the *sole*
auto-writer of canonical cortex facts; retire the deterministic regex
auto-promote as a cortex writer; ship the local extractor LLM as a default,
required stack component; and add a one-time, reviewed cleanup pass for the
sibling slots past auto-promotes already left in the bank. Touches
`pseudolife_memory/utils/config.py`, `pseudolife_memory/memory/dream.py`,
`pseudolife_memory/service.py`, `ops/docker-compose.yml`, `evals/ladder_sweep.py`,
docs, tests; adds `ops/dedup_cortex.py`.

Related: [supersession + abstention tuning design](2026-06-18-supersession-and-abstention-tuning-design.md)
(the branch whose calibration exposed this root cause) and
[pluggable-LLM-extraction design](2026-06-18-pluggable-llm-extraction-design.md)
(the extractor sidecar this promotes to default).

## 1. Problem

The 2026-06-18 supersession work added a dream-path slot resolver (Feature A) to
fix small-model paraphrase forking. Calibration on `gemma-e2b` (distractor-clean
corpus) showed it gives **no** stale-leak reduction at any threshold and risks a
false-merge:

| threshold | superseded | stale_leak | false_merge |
|-----------|-----------|-----------|-------------|
| 0.0 (off) | 0 | 0.1 | 0 |
| 0.80 | 1 | 0.1 | **1** |
| 0.85–0.95 | 0 | 0.1 | 0 |

Tracing *why* the leak survives surfaced the real cause, and it is **not** the
model. Two extractors write to the cortex with different conventions:

1. **Store-path auto-promote** (`service.py:_promote_slots`) runs the
   deterministic regex `extract_slots` on **every** `store()` and writes straight
   to cortex (no slot embedding), gated by `cortex.auto_promote` (default `True`).
2. **Dream-path** runs the LLM extractor and writes via `cortex_write`.

The regex cannot get the entity/attribute boundary right for compound names,
because `_attr_suffix` (`slots.py:131`) greedily grabs up to two trailing
`_ATTR_LEXICON` words — and the lexicon contains `database`, `host`, `server`,
`engine`, `db`, `repo`, … The two real failures pull in **opposite** directions,
proving no deterministic tweak fixes both:

- `"payments database host is db-prod-2"` → entity `payments`, attribute
  `database host` (*over*-grabs; wanted `payments database` / `host`).
- `"api-gateway request timeout is 30s"` → entity `api-gateway request`,
  attribute `timeout` (*under*-grabs; wanted `api-gateway` / `request timeout`).

So for one logical fact the bank accumulates up to three sibling slots
(`payments-db/host`, `payments/database host`, `payments database/host`) with
different values. No resolver can safely reconcile that — which is exactly why
Feature A measured flat. The fragmentation is a **deterministic-regex** problem
and a **two-writers** problem; the small model is the victim, not the cause
(thinking-disabled Gemma names slots fairly consistently).

## 2. Goals / non-goals

**Goals**
- **One writer for canonical facts.** The LLM dream pass becomes the sole
  *automatic* writer to the cortex; regex output never defines canonical slot
  identity again.
- **Ship the extractor by default.** The local CPU Gemma sidecar moves from
  opt-in to a default, expected stack member, so the single quality writer is
  actually present.
- **Stop new fragmentation at the source** rather than papering over it
  downstream.
- **Clean the existing bank** with a conservative, reviewed, reversible one-time
  pass.

**Non-goals**
- No removal of `extract_slots` — it stays as the **recall-time slot-view**
  context hint (`merge_slots_view` / `format_slots_for_context`), which is
  ephemeral prompt material, never persisted to cortex, so never a pollution
  source.
- No removal of Feature A. The resolver + schema v8 `slot_embedding` stay,
  shipped **off** (`dream_slot_match_threshold = 0.0`), with its drawbacks
  documented (§5). Single-writer removes the cross-writer fragmentation it
  targeted; the only residual case is within-LLM paraphrase across dream passes,
  already mitigated by extractor `vocab`-reuse and not worth enabling on current
  data.
- No hard daemon crash when no extractor is configured — degrade with a clear
  warning (§3.2).
- No eager-dream freshness trigger (tabled — Approach B, §7) and no
  prompt-engineering the models.

## 3. Design

### 3.1 Config defaults (code)

- `CortexConfig.auto_promote: True → False` (`config.py:353`). The knob and
  `_promote_slots` stay intact and re-enableable; they simply don't fire by
  default. Documented drawback: re-enabling reintroduces regex fragmentation.
- `DreamConfig.extractor_base_url` / `extractor_model` code defaults stay `None`
  — the deployment layer (§3.3) supplies them. A non-container run with no
  extractor and no env vars gets no automatic cortex, by design.

### 3.2 Dream is the only auto-writer (code)

- Add a `NoOpExtractor` to `dream.py` whose `extract()` returns `[]`.
  `build_extractor()` returns it (instead of the cortex-writing `RegexExtractor`)
  when no LLM base-url/model resolves from config or env.
- **Remove the hardcoded regex fallback inside `dream_run`.** `dream_run`
  currently has, independent of `build_extractor`, a load-bearing fallback
  (`service.py:1311-1312`):
  ```python
  if not claims:
      claims = RegexExtractor().extract(texts, vocab)
  ```
  This is the real source of regex→cortex writes when the LLM yields nothing,
  and it would defeat the whole design: a `NoOpExtractor` returns `[]`, which
  trips this fallback and writes regex facts anyway. The plan must delete this
  block (and the `from ...dream import RegexExtractor` import at `service.py:1297`)
  and update the docstring (`service.py:1293-1296`, which currently promises a
  "regex floor fallback if it yields nothing"). After removal, an extractor that
  yields nothing — whether the no-op, or an LLM that returned junk/empty —
  writes nothing; the dream just advances the cursor. This is the single change
  that makes the LLM the *sole* automatic writer; the `build_extractor` change
  alone is insufficient.
- `RegexExtractor` **stays in the tree** as an *explicit* opt-in (parallel to the
  `auto_promote` knob) for anyone who deliberately wants the regex floor — but it
  is never reached automatically: not as the `build_extractor` default, and not
  via the (removed) `dream_run` fallback.
- The per-claim `_resolve_dream_slot` call in `dream_run` (`service.py:1315`) is
  unchanged and harmless: at the shipped `dream_slot_match_threshold = 0.0` (§5)
  it is exact-key only, so it needs no change here.
- **Soft requirement.** On daemon startup, if `dream.enabled` is true but
  `build_extractor()` resolves to the no-op, log a single clear `WARNING`:
  cortex auto-population is disabled (no extractor LLM; regex floor removed);
  only `memory_fact_set` / user-canonical writes populate the cortex. No crash —
  resilient to a slow sidecar.

### 3.3 Packaging: extractor default-on (ops)

In `ops/docker-compose.yml`:
- Drop `profiles: ["extractor"]` from `pseudolife-extractor` so it starts with
  the stack.
- Set (uncomment) `PSEUDOLIFE_DREAM_BASE_URL:
  http://pseudolife-extractor:8081/v1` and `PSEUDOLIFE_DREAM_MODEL: extractor`
  in the daemon block.
- Add `pseudolife-extractor` to the daemon's **existing** `depends_on` list (it
  already depends on `pseudolife-pg`; this is an added entry, not a new key). (The
  extractor has no external deps and is internal-only via `expose`, unchanged.)

The local Gemma sidecar is now a standard member of the stack — the "default and
required" posture, enforced at the deployment layer and surfaced by the §3.2
startup warning for any non-standard run.

### 3.4 Why not fix the regex instead

Considered and rejected: the over-grab / under-grab cases (§1) need semantic
knowledge of where the entity ends and the attribute begins. Any lexicon or
greediness tweak that fixes `payments database host` breaks `api-gateway request
timeout`, and vice versa. The regex is good enough for best-effort *context
hints* but structurally unfit to define canonical identity. Eliminating it as a
cortex writer is the correct fix, not tuning it.

## 4. One-time sibling cleanup

A conservative, **dry-run-first**, reversible maintenance pass that collapses the
fragments past auto-promotes left behind. It deliberately reuses Feature A's
value-free slot-embedding machinery — the right tool for a *reviewed, one-time*
pass even though it was wrong for silent runtime use.

### 4.1 Where it lives

- `MemoryService.cortex_dedup(threshold: float = 0.90, dry_run: bool = True) ->
  report` — needs the embedder (to backfill slot embeddings), so it sits at the
  service layer beside `_resolve_dream_slot`.
- `ops/dedup_cortex.py` — thin CLI wrapper. Ops-only on purpose; **not** an MCP
  tool, so it can never fire at runtime.

### 4.2 Algorithm

1. **Backfill** `slot_embedding` for every `current` record that lacks one (reuse
   the Feature A backfill: embed `f"{entity} {attribute}"`).
2. **Cluster** current records by slot-embedding cosine `≥ threshold` (default
   `0.90`, tunable) via union-find. Value-free, so it groups `payments-db/host`
   with `payments/database host` regardless of their differing values.
3. **Pick canonical** per cluster using the store's existing precedence:
   provenance tier (`user > action > agent`), then most-recent
   `asserted_at`/`last_confirmed`. This keeps the freshest value under the
   strongest-named slot.
4. **Retire** the cluster's other members: `status → superseded`,
   `superseded_by_value → canonical.value`, `superseded_at = now`; repoint the
   `_current` index to the canonical. **Nothing is deleted** — the audit trail
   makes every merge reversible.

### 4.3 Safety

- **Dry-run by default**: prints each proposed cluster (canonical + the siblings
  it would retire) and writes nothing. `--apply` commits.
- `--apply` loudly reminds (and is documented to require) an `ops/backup.ps1`
  snapshot first — same discipline as every other state-touching op.
- **Idempotent** — re-runnable; a clean bank yields an empty report.
- **Documented caveat**: clustering is fuzzy. `ledger-db/engine` vs
  `ledger-cache/engine` *could* exceed threshold. The dry-run report is the human
  gate; review before `--apply`. Raise `threshold` if a run looks too eager.

## 5. Feature A (resolver): keep, shipped OFF

No code change — `dream_slot_match_threshold` stays `0.0` (off, exact-key only).
Documentation gains an honest **drawbacks** note: on the `gemma-e2b` benchmark it
delivered zero stale-leak reduction and produced a false-merge at threshold
`0.80`; single-writer removes the cross-writer fragmentation it was built for;
the residual within-LLM-paraphrase case is already covered by `vocab`-reuse.
Revisit only with new data showing within-LLM forking on some corpus. Anyone
considering enabling it should read this note first.

## 6. Testing

- **Default behaviour:** `store()` with shipped defaults writes **nothing** to
  cortex (`cortex_promoted == 0`); a subsequent dream with a stub/LLM extractor
  does. The `auto_promote=True` path stays covered by setting the knob explicitly
  in the existing promotion tests (`test_cortex_promotion.py`,
  `test_mcp_server.py`, `test_write_through.py`).
- **`build_extractor`:** returns `NoOpExtractor` when unconfigured (update
  `test_build_extractor_selects_by_config`); returns the LLM extractor when
  base-url/model are set (unchanged).
- **No-op dream:** a `dream_run` with the no-op extractor pulls, promotes
  nothing, and still advances the cursor. **Regression-critical** — this test
  fails today because of the `dream_run` regex fallback (§3.2), so it directly
  guards the removal of that fallback.
- **Empty-LLM dream:** a `dream_run` whose extractor returns `[]` (simulating an
  LLM that emitted no parseable claims) also writes nothing — proving the regex
  fallback is gone, not merely bypassed for the no-op type.
- **`cortex_dedup`:** siblings (high slot-cosine, differing values) merge under
  one canonical with the others retired; `dry_run=True` mutates nothing and
  returns the same report; distinct slots (different attributes, low cosine) are
  left alone; canonical selection honours tier-then-recency.
- **Startup warning:** dream enabled + no extractor → warning emitted (no raise).

## 7. Deferred / out of scope

- **Approach B — eager-dream freshness trigger.** With auto-promote off, prose
  facts reach cortex only on the next dream sweep (`≥ min_batch` backlog or
  `idle_seconds` idle); explicit `memory_fact_set` stays immediate. Accepted as
  eventual consistency for now; a more eager trigger is tabled for empirical
  evaluation once there is real usage data.
- **Regex boundary improvement** for the slot-view (not cortex) — separable,
  low value.
- **Cross-session alias-aware dedup** in cleanup (resolve via `entity_aliases`
  before embedding) — additive, separable.

## 8. Touches

- `pseudolife_memory/utils/config.py` — `CortexConfig.auto_promote` default
  `True → False`.
- `pseudolife_memory/memory/dream.py` — `NoOpExtractor`; `build_extractor`
  returns it when unconfigured.
- `pseudolife_memory/service.py` — **remove the `dream_run` regex fallback**
  (`service.py:1311-1312`), its `RegexExtractor` import (`:1297`), and update the
  docstring (`:1293-1296`); startup/dream warning when no extractor;
  `cortex_dedup` method.
- `ops/docker-compose.yml` — extractor default-on; daemon `PSEUDOLIFE_DREAM_*` +
  `depends_on`.
- `ops/dedup_cortex.py` — new dry-run/apply CLI.
- `evals/ladder_sweep.py` — handle `auto_promote` explicitly where `ingest()`
  relied on the old default.
- `README.md`, `CHANGELOG.md`, `evals/README.md` — single-writer rationale;
  `auto_promote` default-off + drawback; extractor default/required; Feature A
  drawbacks; cleanup tool usage. Flag (do **not** auto-edit) that the user's
  global `~/.claude/CLAUDE.md` line "memory_store() automatically promotes
  slot-shaped facts to cortex" is now stale.
- **In-tree docstrings made stale by the default flip** (update for
  self-consistency): `config.py:340-351` (`CortexConfig` — describes auto-promote
  as the populate-on-every-store no-LLM floor); `dream.py:5-7` (module docstring's
  "regex floor") and `dream.py:69-70` (`OpenAICompatExtractor` "falls back to the
  regex floor"); `service.py:1293-1296` (`dream_run`, already listed above).

## 9. Decisions (locked at design)

1. The **LLM dream is the sole automatic cortex writer**; regex auto-promote
   ships **off** by default (knob retained).
2. The regex floor is removed as a *cortex* writer even when no LLM is present —
   via **both** `build_extractor → NoOpExtractor` **and** deleting the hardcoded
   `dream_run` regex fallback (`service.py:1311-1312`), which is the actually
   load-bearing one. No-LLM deployments populate cortex only via
   `memory_fact_set`. `extract_slots` is retained for the recall-time slot-view.
3. The local extractor sidecar is **default-on and required** at the deployment
   layer; missing extractor degrades with a warning, not a crash.
4. **Existing-sibling cleanup is in scope** as a reviewed, dry-run-first,
   reversible ops command (`cortex_dedup` / `ops/dedup_cortex.py`).
5. **Feature A stays, shipped off**, with documented drawbacks; revisit deferred.
6. **Approach B (eager-dream freshness)** is tabled for later empirical testing.
