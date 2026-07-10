# Learned consolidation policy — scoping & feasibility (Auto-Dreamer direction)

**Date:** 2026-07-11
**Status:** scoping only — no code, no training. Decision-ready for the user.
**Origin:** Auto-Dreamer (arXiv 2605.20616) + MemoPilot (arXiv 2606.08656),
scoped against the Stage-1.5 closure (2026-07-10) and the known-facts-window
failure (2026-07-11).

## Why this direction survived the week

Three experiment programs have now closed with the same shape of result:

- **Stage 1.5** (SFT label variants): JEPA neutral, Sonnet labels negative.
  Imitating better *labels* doesn't transfer key discipline.
- **Known-facts window** (prompt-time intervention): cortex −0.08 on *both*
  extractors. Showing the model existing facts at consolidation time distorts
  behavior (key gravity + claim suppression) instead of improving it.
- **KU-s autopsy (2026-07-05):** 79% of gold answers are *never extracted*
  under distractor load — the bottleneck is the write policy's recall, not
  retrieval and not supersession machinery.

The common failure is optimizing a *proxy* (label imitation, prompt hints).
Auto-Dreamer and MemoPilot both argue the fix: train the consolidation
decision itself against a *downstream* objective. Auto-Dreamer's learned
offline consolidator — the exact fast-acquire / slow-consolidate split
PseudoLife already implements as episodic store vs dream pass — beat fixed,
RL, and prompted baselines with a 6–12× smaller bank and transferred across
domains without retraining.

## Two arms, sequenced cheapest-first

### Arm 1 — Registry-datagen SFT (the Stage-1.5 forward hypothesis; days)

Not RL. Regenerate the distill training set with a **per-chain key registry**:
the datagen teacher (Qwen3.6-27B) is forced, at *generation* time, to reuse
each chain's established `entity — attribute` keys and to re-state
carried-forward facts every session. The student then *learns* key reuse as a
prior instead of being shown facts at inference (which the window experiment
proved harmful). This was pre-registered as the next arm in the Stage-1.5
closure and is unaffected by tonight's failure — tonight failed at
*inference* time; this arm moves the same idea to *training* time.

- **Infra already exists:** Sonnet/Qwen datagen scripts, `--canonical-keys`,
  train cadence flags, GGUF recipe (keeper tooling on `feat/extractor-stage15`),
  `distill_train_e4b.py` working config (fixed 5120 shape, compile ON).
- **Cost:** ~1 datagen pass (27B, hours) + one QLoRA run (~overnight on the
  4090) + the standard gate (ladder + same-sitting KU-oracle re-baseline).
- **Gate (pre-registered):** cortex ≥ same-sitting e4b-ft baseline + 0.05;
  hybrid no regression; ladder clean. Same protocol as tonight.

### Arm 2 — GRPO write-policy (the Auto-Dreamer/MemoPilot bet; weeks)

Train the e4b student's dream-pass decisions (emit / re-key / supersede /
skip) with multi-turn GRPO against an end-to-end reward, per MemoPilot.

- **Reward:** the judge-free `answer_in_current_fact` metric (Stage-1.5
  keeper tooling) on held-out question chains, minus a bank-size/verbosity
  penalty (Auto-Dreamer's compactness result), minus ladder `stale_leak`.
- **Reward-hacking risks (real, must be designed against):** a substring
  reward is trivially hacked by dumping whole session text into fact values.
  Guards: hard value-length caps in reward, stale-leak penalty, echo-style
  probes, and a final human-judged eval the policy never sees.
- **Data split:** train on synthetic chains (ladder-style generator +
  seed-bench machinery), evaluate on LongMemEval-KU — never train on the
  bench. Auto-Dreamer's cross-domain transfer result is the precedent that
  this split can work.
- **Feasibility on the 4090:** QLoRA+GRPO on a 4B-class model is at the edge
  but plausible (unsloth GRPO path; the E4B training lessons — fixed shapes,
  compile ON, logit-spill symptoms — carry over). Expect multi-day wall
  clock and at least one failed run; budget accordingly.
- **Go/no-go:** only start Arm 2 if Arm 1 fails or plateaus — Arm 1's SFT
  prior may capture most of the available lift at a tenth of the cost.

## Recommendation

Run **Arm 1** next. It is the cheapest untested idea with a pre-registered
protocol, it inherits all existing tooling, and its failure mode is
informative for Arm 2 (if trained-in key discipline doesn't lift cortex,
the write-policy objective itself — not the training method — is the
problem, which is precisely what GRPO would then address). Do not start
Arm 2 first: it is 10× the cost and its reward design depends on knowing
whether SFT-level key discipline already saturates.

## Out of scope

- TiMem recall side and session-summary L2 (unchanged from the 2026-07-10
  design's exclusions).
- Any consolidation-time fact display (closed by the window gate failure).
- Post-hoc key merging (closed by Stage-1.5 arm B′).
