# Extractor Stage 1.5 — JEPA ablation + Sonnet-5 recall labels

**Status**: approved design · **Date**: 2026-07-07
**Predecessor**: `docs/specs/2026-07-04-bespoke-extractor-design.md` (Stage-1 SFT — shipped)

## Context

Stage-1 SFT succeeded beyond its gate: the e4b-ft QLoRA student (1,756 cleaned
Qwen3.6-27B rows, exact production prompt) scored **KU-oracle cortex 0.564 /
hybrid 0.769**, beating its own teacher (0.397 / 0.590 same-run) and is
deployed as the production sidecar (2026-07-06). Two implications drive this
stage:

1. The student is not capacity-limited — **label quality is now the live
   ceiling**. The 2026-07-05 LongMemEval-KU `_s` autopsy found 79% of gold
   answers were never extracted at all: a recall/selectivity problem in the
   labels, not a modeling problem.
2. LLM-JEPA ([arXiv 2509.14252](https://arxiv.org/abs/2509.14252), ICLR 2026)
   reports +5–25pt gains over plain SFT on structured-output tasks at 1–2B
   scale by adding an embedding-space prediction loss between paired views of
   the same concept. Our training rows already ARE paired views (session
   notes ↔ claims JSON), so the ablation is nearly free.

Two orthogonal experiments, individually gated, run in sequence:

- **Arm A** — add the JEPA auxiliary loss, train on the *existing* data
  (isolates the loss change; zero datagen cost).
- **Arm B** — regenerate labels with Claude Sonnet 5 under a recall-boosted
  private prompt (attacks the measured recall bottleneck), trained with
  whichever loss won arm A.

A full 2×2 (labels × loss) runs only if results are ambiguous.

## Success criteria and gates

Baseline to beat: deployed e4b-ft, **KU-oracle cortex 0.564** (hybrid 0.769
tracked). Hard guards on every candidate, existing harnesses only:

| gate | harness | threshold |
|---|---|---|
| KU-oracle cortex | `evals/longmemeval_bench.py` | > 0.564 to deploy |
| ladder gold | `evals/ladder_sweep.py` | ≥ 0.9 |
| ladder stale_leak | `evals/ladder_sweep.py` | = 0.0 |
| latency/size | same E4B base + Q4_K_M | unchanged packaging |

Variance control: rerun the deployed e4b-ft baseline in the **same harness
session** as each candidate — earlier runs show meaningful run-to-run drift
(e.g. the 27B teacher scored 0.564 in one session and 0.397 in another), so
only same-session comparisons count.

Contamination: LongMemEval-KU stays fully held out. Arm B reuses the exact
forbidden-set logic from `evals/distill_datagen.py` (any session id appearing
in any knowledge-update question's haystack is excluded).

## Arm A — JEPA auxiliary loss on existing data

### Approach (decided: faithful, in-script)

Extend `evals/distill_train_e4b.py` in place, flag-gated so that
`--jepa-lambda 0` is byte-for-byte the current training path.

- **Views**: view₁ = the numbered-notes user text; view₂ = the claims-JSON
  completion. Same fact content, raw vs distilled — the analogue of the
  paper's NL ↔ code pairing.
- **Loss**: `L = L_SFT + λ · (1 − cos(pred(E(view₁)), E(view₂)))` where
  `E(·)` is the last-token hidden state of a plain-text encode of the view,
  and the predictor is the paper's mechanism: k learned predictor tokens
  appended to view₁ (the same LLM serves as encoder and predictor). Defaults
  λ = 1.0, k = 2. `--jepa-stopgrad` detaches the view₂ target if training
  destabilizes (off by default, matching the paper).
- **Trainer integration**: subclass `SFTTrainer`, override `compute_loss`.
  The SFT term keeps unsloth's fused cross-entropy untouched.

### Preserving the hard-won memory/compile discipline

The 2026-07-06 lessons are constraints, not suggestions:

- **Fixed shapes only.** Views are pre-tokenized and padded to their own
  fixed shapes — notes view 4096, claims view 1024 — so the run compiles
  exactly three graphs (5120 SFT + 4096 + 1024), each once. No dynamic
  shapes anywhere.
- **Truncation is safe here.** Front-truncating a notes view to 4096 affects
  only the auxiliary embedding; the CE target is never truncated (the
  drop-don't-truncate rule for the 5120 SFT rows is unchanged).
- **No logits on view forwards.** JEPA needs hidden states only, so the
  262k-vocab logit blowup cannot recur. Grads flow through LoRA adapters
  only; gradient checkpointing stays on.
- **Budget**: ~2 extra forwards/step at bs=1 → expect ~55–65 s/step, ~7 h for
  410 steps. One overnight on the 4090. Watch for the known spill signature
  (100% util at ~120 W = spilling, not working).

### Validation sequence

1. **Parity gate**: run the unmodified script's `--smoke` and the edited
   script's `--jepa-lambda 0 --smoke` back-to-back; step losses must match
   before any ablation runs.
2. **JEPA smoke**: 5 steps on the 40 tail rows — shape/memory/throughput.
3. **Full run** → `save_pretrained_merged` → GGUF Q4_K_M
   (`evals/distill_merge_e4b.py` path) → ladder + KU-oracle vs same-session
   baseline.

### Decision rule

- Gain ≥ +0.02 KU-oracle cortex with guards clean → JEPA becomes the loss
  for arm B (and the A candidate may deploy on its own merits).
- Neutral or negative → arm B trains plain SFT; log the negative result to
  memory (`memory_outcome`) so the JEPA line is closed with evidence.
- λ sweep {0.5, 2.0} only if the λ = 1.0 result is within noise of baseline.

## Arm B — Sonnet-5 recall-tuned labels via Max-plan subagents

### Asymmetric prompt split (decided)

- **Teacher side**: Claude Sonnet 5 labels sessions under a **private
  recall-boosted prompt** (new file `evals/prompts/sonnet_recall_system.md`):
  same JSON schema, same slot-key-reuse rules, plus explicit recall
  instructions — extract *all* durable facts, err toward inclusion, one
  atomic claim per fact, empty only when a session truly has no durable
  content.
- **Student side**: stored training rows keep the **unchanged production
  `_SYSTEM_PROMPT` + `_vocab_hint`** (imported from
  `pseudolife_memory/memory/dream.py`, as today). The student learns
  "production prompt → high-recall claims"; nothing ships prompt-wise. This
  is the OPSD privileged-teacher trick, minus the RL.

### Datagen mechanics (decided: subagents on Max)

New `evals/distill_datagen_sonnet.py`, two modes mirroring the proven
opus-4.8 `--emit-prompts → dispatch → ingest` pattern:

- **`--emit-briefs`**: one self-contained brief file per source question
  (`evals/data/sonnet_briefs/<question_id>.md`): that question's sessions in
  chronological order, vocab-maintenance rules (grow the hint from your own
  prior claims, normalization described), the JSON schema, and one worked
  example. **One subagent per question** — vocab evolution is sequential
  within a question but questions are independent, so parallel dispatch is
  per-question. A `_s` haystack (~40–50 sessions × ~1k tokens) fits one
  subagent context.
- **Dispatch**: the main session fans out N subagents per usage window;
  fully resumable — a question is done only when its rows pass ingest.
- **`--ingest`**: strict validation of returned rows. Reuse
  `validate_claims` (schema, confidence range, `source` citation in range);
  **recompute the vocab chain deterministically** via `_norm_key` from the
  subagent's own claims in order — never trust the subagent's bookkeeping;
  enforce the KU forbidden set and the empty-claims cap
  (`--max-empty-share 0.2`); rewrite the system message to the production
  prompt + recomputed vocab hint; append to
  `evals/data/distill-extract-sonnet.jsonl` (gitignored). Failed questions
  are re-dispatched, not repaired.
- **Cleaning**: existing `evals/distill_clean.py` pass →
  `distill-extract-sonnet-clean.jsonl`.

### Pilot gate before full fan-out

Label ~10 questions first. Hand-audit ~30 rows and compare against the Qwen
labels on the same sessions: claims-per-session (recall proxy), slot-key
consistency across initial/update turns, empty-claim behavior on smalltalk.
No measurable recall lift → stop arm B cheap and keep the Qwen dataset.

### Training and gate

Retrain from the same base (`unsloth/gemma-4-E4B-it`, same QLoRA config)
on the Sonnet-clean set, with arm A's winning loss. Same gates, same
same-session baseline rule. Winner deploys via the existing GGUF →
compose volume-mount path with a fresh rollback tag.

## Sequencing and budget

1. Arm A parity gate + smoke (hours) → full run (~7 h GPU, overnight).
2. Arm A eval + decision.
3. Arm B pilot (~10 questions, one usage window) → audit → full datagen
   (spread over windows, $0 marginal) → clean → retrain (3–7 h GPU) → eval.
4. Deploy the best candidate that clears gates; record outcomes to memory.

## Risks

- **unsloth vs custom `compute_loss`**: the patched model may not tolerate
  the override. Fallback (recorded as no-longer-paper-faithful): simplified
  alignment — mean-pooled views + linear projection head. The parity gate
  catches breakage before GPU-hours are spent.
- **Recall-boosted labels teach noise**: over-extraction would pollute the
  bank. Caught by stale_leak = 0.0, the empty-claims cap, and the pilot
  audit; the asymmetric split means a bad outcome ships nothing.
- **Subagent JSON indiscipline**: strict ingest validation + per-question
  retry; a question's rows are all-or-nothing.
- **Distribution shift in Sonnet labels** (JSON style, confidence
  calibration): ingest normalizes formatting; `distill_clean.py` filters
  echo-key/spam/mega-rows as before.
- **Eval variance masking small effects**: same-session baseline reruns;
  the +0.02 adoption threshold sits above observed ladder-level noise but
  below the drift seen across harness sessions — same-session comparison is
  the control.

## Deliverables

- [ ] `distill_train_e4b.py`: flag-gated JEPA (`--jepa-lambda`,
      `--jepa-pred-tokens`, `--jepa-stopgrad`), fixed view shapes, parity
      gate documented in header
- [ ] Arm A full run + same-session eval vs deployed e4b-ft
- [ ] `evals/prompts/sonnet_recall_system.md` (private teacher prompt)
- [ ] `evals/distill_datagen_sonnet.py` (`--emit-briefs` / `--ingest`)
- [ ] Pilot (~10 questions) + hand audit vs Qwen labels
- [ ] Full Sonnet datagen → clean → retrain → eval
- [ ] Deploy winner (GGUF swap + rollback tag) or record negative results
