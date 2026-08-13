# Epistemic stance field + provenance-span gate — preregistration (2026-08-12)

## Amendments (2026-08-13, at implementation — before any gate ran)

Three deviations from the text below, each discovered against the real
code and recorded here rather than silently rewritten:

1. **Table name**: the project-facts table is `facts`, not
   `memory_facts` (that name appears only in this spec). The stance
   column landed on `facts` (schema v29).
2. **`span_gate` ships `"off"`, not `"log"`**: the live extraction
   prompt is v5, which emits no `quote` field — under it, `log` mode
   would flag 100% of claims and the counters would be pure noise. `log`
   is the mode the gate-4 audit runs beside the v9 prompt on the bench;
   the shipped default follows the `quarantine_low_trust` pattern (off
   until a measured verdict proposes otherwise).
3. **The verified quote is not journaled into `dream_run_slots`** — the
   journal has fixed columns and adding one would be a second schema
   touch for a pure-audit nicety. The audit trail is the gate's log line
   plus the `span_flagged` / `span_parked` counters persisted in the
   dream-run row's tallies JSONB, which the gate-4 replay reads.

## Gate outcomes (2026-08-13) — prompts DID NOT ship

Gates 1, 3, 5 passed; gate 2's ladder clause passed saturated; gate 2's
KU-oracle clause **failed for both prompt arms**, so the live prompt
stays v5 and the stance/span plumbing ships dormant:

- Gate 1 (stance probe): PASS — v8 capture 0.92, false-stance 0.00,
  values clean; side-finding: v5 silently drops 70% of hedged facts
  (recovery 0.30 vs 0.925 plain), repaired by the stance rule
  (`evals/results/stance-probe-20260813-gate1.json`).
- Gates 2+6 ladder: PASS but SATURATED (all arms 1.0/0.0, both
  replicates — the ladder cannot gate prompt changes at this rung;
  `evals/results/qwen-27b-sg-*.json`).
- Gate 2 KU-oracle: **FAIL** — v8 cortex 0.731→0.615 (McNemar
  p=0.0117, net −9/78, churn 154→181), v9 hybrid 0.897→0.833 (0W/5L,
  p=0.0625), rag control 0/78 cross-arm disagreements (zero noise
  floor). Loss modes: stale-current flips, new abstentions, and one
  refusal despite the answer being in the served current fact
  (stance-in-context makes the answerer timid — a serving-side
  interaction). Artifacts:
  `longmemeval-ku-oracle-qwen-27b-sgku-{v5,v8,v9}.{jsonl,summary.json}`,
  `stance-ku-paired-verdict.json`.
- Gate 4: extraction-level evidence only (82/82 quotes verified,
  single-note easy mode); `contend` not proposed
  (`evals/results/span-gate-verdict.json`).
- Gate 5 (composition): PASS
  (`tests/test_span_quarantine_composition.py`).

Next iteration owns: why the stance rule degrades KU extraction
(update-statement interaction is the leading suspect), and whether
stance should be stripped from the ANSWER context (serving-side
timidity) independent of extraction.

### Post-gate forensics (2026-08-13, from the committed per-question banks)

Bank diffs for three representative v8 losses
(`evals/results/banks/oracle-qwen-27b-sgku-{v5,v8}/`) localize the
mechanism — it is NOT stance-field semantics (the lost banks barely use
the field) but **prompt-example interference with slot naming and
update consolidation**:

- `9ea5eabc`: v5 → 4 clean slots, `past-travel-experience` superseded
  Hawaii→Paris correctly. v8 → 19 slots: one activity list exploded
  into ~15 near-duplicate `planned-solo-travel-activities` scalars, key
  format drifted (`past travel experience`, space-separated), and the
  Paris update was NEVER extracted — the slot froze at Hawaii.
- `6aeb4375`: v5 superseded the running count 3→4; v8 froze it at 3
  (the count-exclusion behavior v5's example anchors, weakened).
- `852ce960`: both banks carry the correct current value — that loss is
  purely answer-side (the timidity component), a separate, smaller
  effect.

Iteration hints this buys: (a) the stance rule's worked example should
itself demonstrate a hedged UPDATE being consolidated onto an existing
slot (reinforcing, not diluting, the one-slot/current-value anchor);
(b) measure slot-count and key-format drift as cheap leading indicators
in any future prompt arm (both were visible here without a judge);
(c) the answer-side timidity is real but second-order — fix extraction
first.

## v10 iteration outcome (2026-08-14) — SHIPPED, with a soak watch

The v10 arm implemented hint (a) exactly (the "a hedged update is STILL
an update" sentence + a worked example reusing the v0 example's own
deploy-target slot) and hint (b) became `evals/analyze_bank_drift.py`,
validated against the v8/v9 failure banks before being trusted.

Gates (same-window v5 control, zero rag noise floor, artifacts
committed): probe capture 0.919 / false-stance 0.00 / hedged recovery
0.30→0.925; ladder 1.0/0.0 ×2; KU-oracle cortex EXACTLY unchanged
(0.731 vs 0.731, 5W/5L, net 0, p=1.0 — the v8 −0.115 regression is
repaired); hybrid 0.859 vs 0.897 (2W/5L, net −3, p=0.45, not
significant, two-sided — unlike v9's one-sided 0W/5L). Bank drift
remains elevated (slot ratio 1.20, key jaccard 0.49, frozen-update
proxy 12, churn 154→179) against a clean cross-window v5 floor
(1.00/1.00/0) — v10 restructures banks about as much as v8 did but no
longer pays for it in cortex accuracy, confirming the drift instrument
as tripwire-not-predictor.

MAINTAINER DECISION (2026-08-14): ship v10 as the live prompt, with a
production soak review due ~2026-08-21 (stance fill rate and
correctness on live dreams, supersession churn, hybrid-arm health) and
a v11 iteration targeting the residual drift (slot-naming stability;
the hybrid −0.038 and jaccard 0.49 are its baseline to beat). Artifacts:
`stance-probe-20260813-v10.json`, `qwen-27b-sg2-v10-r{1,2}.json`,
`longmemeval-ku-oracle-qwen-27b-sg2-{v5,v10}.{jsonl,summary.json}`,
`stance-v10-ku-paired-verdict.json`, `bank-drift-sg2-v5-vs-v10.json`,
`bank-drift-crosswindow-v5-floor.json`.

Two extraction-quality changes that share one surface (the dream extractor's
claim schema) and are therefore specced and measured together, in separate
arms. Both are grounded in externally verified 2026-08 results and in this
pipeline's own measured history.

## Problem, precisely

**1. Stance destruction.** The dream pass is a compression step, and
compression strips qualifiers. A note saying "we'll *probably* move the
deploy to prod-eu" consolidates into `deploy target.environment = prod-eu`
— the hedge is gone, and every downstream reader (recall, fact_get, the
Console) renders the value as flat truth. Nothing in the claim schema asks
for the hedge, nothing stores it, and the numeric `confidence` field does
not preserve it: `confidence=0.6` does not survive as "the source was
hedging" once the record is read back. arXiv:2608.06953 (single author,
pre-registered replication; abstract verified 2026-08-12) measured exactly
this failure and its fix: writing epistemic stance as a **labelled field**
rather than inline prose raises its survival through compression by ~15
points (p=0.00005; +15.6 replicated), while writing longer helps not at
all. The fix is format-level and lands on our write path.

**2. Unbacked claims outside the literal gate's reach.** The shipped
literal-faithfulness gate (design 2026-08-02, `enforce` since then) checks
only digit-bearing tokens, firing on 1.3–1.7% of gateable claims. A
hallucinated claim with no digits — a wrong value, an invented attribute,
an imported piece of world knowledge — passes untouched. SodaMem
(arXiv:2608.08055, verified) demonstrates the general form of the defense
this project already applies to world facts (source URL + quote required):
extraction must emit a **verbatim provenance span** or the claim is not
admitted as canonical. This generalizes the literal gate from "digits must
appear in the source" to "the claim must carry a span that appears in the
source".

Boundary statement, restated from the 2026-08-09 consolidation-quarantine
spec: both of these check **fidelity-to-source, not
trustworthiness-of-source**. A poisoned note quotes itself perfectly; the
MAFIA-class threat remains the two-man rule's domain. The two mechanisms
are orthogonal and compose (gate 5 below proves it).

## Design A — stance as a labelled claim field

- **Extractor schema.** Claims may carry `"stance"`: the note's own hedge
  words, near-verbatim, ≤48 chars ("probably", "unconfirmed", "per the
  runbook", "planning to"). Emitted ONLY when the note hedges; a plainly
  asserted fact carries no stance field. Hedge words move OUT of `value`
  and INTO `stance` — the value string stays clean.
- **Parse boundary.** Whitelisted in `OpenAICompatExtractor.extract`'s
  claim loop and added to the `Claim` TypedDict (`total=False`), exactly
  like `op`. The 2026-07-31 lesson applies verbatim: a field missing from
  the parse whitelist silently disables the feature while the model emits
  it correctly — the parse-boundary test is part of the watched RED.
- **Storage.** New nullable `stance TEXT` column on `memory_facts`, new
  `CortexRecord` field, **schema v29** (five-place bump per the shipping
  checklist). File-mode snapshots load absent-as-None; old banks read
  unchanged.
- **Semantics.** Stance reflects the **latest asserting write**: a
  confirm or supersede with no stance clears the stored one (a confident
  restatement removes the hedge); one with a stance replaces it. Stance is
  never blended into `confidence` or into ranking — it is a display/
  decision qualifier for the reader, not a trust input (it is model-
  emitted and steerable by note text, the same class as the claim `origin`
  field per the 2026-08-09 review).
- **Serving.** `_cortex_record_to_dict` adds `"stance"` when set; recall
  cortex entries, `memory_fact_get`, history, and the Console fact views
  render it.
- **Deliberately not in scope:** a `stance` parameter on the
  `memory_fact_set` MCP tool (tool surface and manifest budgets
  unchanged); member records (v1 is the scalar dream path only); world
  facts (their citation discipline already carries the source's words);
  any stance→confidence coupling.

## Design B — provenance-span gate

- **Extractor schema.** Claims may carry `"quote"`: a verbatim span, ≤200
  chars, copied from the cited note, sufficient to support the claim.
- **Gate.** In the claim loop beside the literal gate: normalized
  containment (casefold, collapse whitespace/punctuation runs; no fuzzy
  matching in v1) of `quote` within the **cited note's text**. Source
  scope is correct here by construction — a quote is from one note — so
  this does not inherit the literal gate's measured batch-scope
  false-drop classes (derived sums, cross-note values), which were about
  checking the *value* against a corpus, not a quote against its note.
- **Modes.** `memory.dream.span_gate: "off" | "log" | "contend"`, shipped
  at **"log"** (exposed in `config_io.py` beside `literal_gate`). Under
  `"contend"`, a claim whose quote is missing or fails containment is not
  dropped (unlike the literal gate — span failures include benign
  paraphrase and are expected at a higher base rate): it parks as a
  contender via the existing `force_contend` machinery with a
  `span:unbacked` provenance marker — visible in `memory_fact_get`,
  resolvable by `memory_fact_resolve`, promotable like any contender.
  Nothing is silently discarded. The mode flips to `"contend"` only via a
  measured verdict (gate 4), mirroring how the literal gate earned
  `enforce`.
- **Uncited claims** (no `source` index) currently skip the literal gate;
  under `"contend"` they park — admission discipline is the point — but
  the log-mode soak measures how often that fires before anyone decides.
- **Storage:** none. The engram trace already records claim→source-entry
  attribution; the quote's job is admission-time verification, and it is
  journaled in the dream-run row for audit, not stored on the fact.
- **Deliberately deferred:** NLI entailment of the value against the
  verified quote (the HallDetect arXiv:2608.05823 pattern; `nli.py`
  exists) — only worth designing if log-mode data shows verbatim
  containment is insufficient. Noted, not specced.

## Prompt and artifact protocol

The live prompt is pinned byte-identical to
`evals/prompts/ku_op_prompt_v5.txt` (`test_op_prompt_artifact.py`); it
changes only through a new measured artifact + gate.

- Two **incremental** variants in `evals/op_probe.py` `VARIANTS`, so a
  gate failure attributes to exactly one addition:
  - `v8-stance` = shipped v5 + the stance rule + one worked example
    (hedged note → claim with `stance`, clean `value`);
  - `v9-stance-quote` = v8 + the quote rule + one worked example.
- Committed artifacts `ku_op_prompt_v8_stance.txt` and
  `ku_op_prompt_v9_stance_quote.txt`, each with a construction pin test in
  `test_op_prompt_artifact.py`. `_SYSTEM_PROMPT` moves only after gates
  pass, updating the shipped-prompt pin test in the same commit.
- **The v7 precedent is the governing risk**: the combined events prompt
  measured −0.053 (p 0.011) on claims and never shipped; a same-call field
  addition has taxed claim quality before. If v9 shows a claims tax, the
  fallback is to drop `quote` from the claims call and evaluate span
  verification as a separate pass (per the events design) — or defer B
  entirely. A is not held hostage to B: v8 can ship alone.

## Preregistered gates

Sidecar rung throughout; bench server via `Start-Qwen` (reproducible
q8_0 config — judged runs never on `-Fast`); every gate writes a result
artifact under `evals/results/` in the same change as any claim it backs.

**A (stance):**
1. **Watched-RED stance probe (deterministic core + GPU probe).** A
   matched-note set (~40 hedged/asserted pairs, the 2608.06953 protocol)
   through the extraction path. RED first: assert the current v5 pipeline
   loses the hedge (stance nowhere, value confident). Then v8 must reach
   stance non-null on ≥60% of hedged claims AND false-stance on ≤10% of
   plainly asserted claims. Parse/storage/clear-on-confirm semantics get
   plain CPU tests with their own watched RED.
2. **Ladder non-inferiority (v8 vs v5):** `gold_recoverable` and
   `stale_leak` unchanged, replicated via `ladder_replicate.py`; KU-oracle
   unchanged.
3. **Value cleanliness:** hedge-token frequency in stored `value` strings
   does not increase v8 vs v5 (the field must relocate hedges, not
   duplicate them).

**B (span gate):**
4. **Log-mode firing audit:** replay the retained dream-run journals plus
   a live soak in `"log"`; report the would-park rate and a fired-on-what
   sample audit (the literal gate's verdict-artifact pattern:
   `evals/results/span-gate-verdict.json`). This number — not preference —
   decides whether `"contend"` is ever proposed as default.
5. **Composition smoke (deterministic, CPU):** span gate `"contend"` +
   quarantine ON: a hostile agent-origin note asserting a false value WITH
   a perfect self-quote still parks under the two-man rule (fidelity
   passing must not launder trust). Watched-RED in the quarantine test
   module's style.
6. **Ladder non-inferiority (v9 vs v8):** isolates the quote tax;
   additionally report sidecar output-token growth and any `max_tokens`
   truncation (quotes roughly double per-claim output).

**Schema bump (with A's ship):** v29 through all five places pinned by
`tests/test_release_ux.py` / `tests/test_atlas_currency.py` — schema.py,
README + configuration.md tables, `test_schema_v29.py` (relax v28's pin to
`>=`), CHANGELOG `v29` mention, atlas `meta.schema` + affected storage
cards. The extraction ladder replaces `regression_gate.ps1` for these
changes (the gate deliberately does not cover the dream path).

## Cost

A: schema bump + one column + serialization + prompt arm + probe set —
moderate; one GPU ladder cycle. B: config knob + containment check + prompt
arm — small code; a second GPU ladder cycle plus the replay/soak audit.
The probe set (gate 1) is new eval data but small, and reusable as the
stance-retention regression fixture afterward.

## Risks / honesty

- **The claims tax is the real risk** (v7 precedent). The incremental-arm
  design exists to catch and attribute it; the prereg commits to not
  shipping a taxed prompt.
- **Stance is steerable** by whoever writes the note, like every
  model-emitted field. It is metadata for the reader, never a trust or
  ranking input; SECURITY.md language should say so when A ships.
- **Span-gate false-park rate is unknown** until gate 4 runs. Shipping at
  `"log"` is the commitment that no claim routing changes on preference.
- **External numbers are architecture validation, not expected local
  deltas**: SodaMem's 92.8% LongMemEval-S is best-of-3 on a hosted model;
  the 2608.06953 +15pts is stance *retention*, not end-task accuracy. No
  benchmark claim lands in docs from this spec — only our own gate
  artifacts do, with `test_eval_evidence.py` rows in the same change.
