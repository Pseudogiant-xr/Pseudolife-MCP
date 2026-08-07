# e4b-v3 events LoRA + anti-suppression instruction arm

Date: 2026-08-07. Status: design before code; **ALL GPU RUNS HELD** until
released by the user. Preregistered gates below; evidence first.

## Motivation (all committed)

From `evals/results/evq-residual-decomposition-0807.json` (offline matcher
replay + Opus ceiling probe, `evals/results/evq-opus-probe-0807.json`):

- Of the 18 evq-0806 multi-session residual rows (rag right, syn wrong):
  **0 hit the 30-event serving cap** and every needed instance sentence
  passes `chronicle_search`'s any-term matcher — retrieval is exonerated,
  and the previously-preregistered retrieval probe is dead.
- **14/18 are extraction-side**: instances matchable and servable, absent
  from the bank. qwen-27b served 0–3 events across whole ~50-session
  haystacks; the Opus probe under the **same v2 prompt** captured the gold
  instances with quantities verbatim ("a 440-page book", "finished episode
  12", "finished around 15 episodes"). The gap is **extractor capacity,
  not the prompt**.
- All 3 evq primary-gate losses share one mechanism, **block-authority**:
  the answerer treats a fuller-but-still-partial events block as
  exhaustive (counts 4 model kits because 4 are listed; totals 3 of 4
  items). The descriptive hedge (`hybrid_ev_hdr`, "partial record — other
  context may hold more") rescued **none** of them and flips zero
  multi-session rows — a stronger, directive lever is required.

Assets banked 2026-08-07 (gitignored, validated: 900 unique ids each, 0
malformed): `evals/data/distill-events-opus1.jsonl` (Opus-5 teacher, v2
events prompt, 225 empty rows at the 25% cap — abstention examples — 423
events carrying digits) and `evals/data/distill-extract-opus1.jsonl`
(claims regen, Opus-5 teacher, sonnet_extractor_v2 prompt).

Deployed comparator, read from `ops/Dockerfile.extractor:19` (not from
memory): the sidecar image defaults to
`pseudolife-extractor-e4b-v2-Q4_K_M.gguf` — the **claims-only** e4b-v2
LoRA. Its events pass rides on raw Gemma-4B behaviour. The dream-pass
primary is the Claude CLI shim (`ops/install-shim-autostart.ps1`); the
sidecar is the fallback and the self-contained path for users without a
Claude subscription — the population this training run serves.

## Design (two independent levers, one gate run)

### 1. e4b-v3: single multi-task adapter (claims + events)

Train ONE QLoRA over the concatenation of both datasets, shuffled with a
fixed seed: claims rows keep the arm-1 student format (registry hint
absent from the stored student prompt), events rows carry the v2 events
prompt as their system message. Task separation is carried by the system
prompt, exactly as the production sidecar switches per pass — one GGUF,
no per-pass model swap (the CPU sidecar cannot afford one).

- `evals/distill_train_e4b.py` config per the 2026-07-06/07-10 lessons:
  fixed 5120 shape, compile ON, expandable_segments, frozen-step watchdog.
  `--data` extended to accept multiple files (merged + shuffled, seed 0).
- Pre-check before training: token-length distribution of the events rows;
  rows over the 5120 budget are dropped with the count logged.
- **Preregistered fallback**: if the claims-conformance gate (T1) fails,
  v3 does **not** ship in any form — the sidecar serves one model, so an
  events-only adapter would still displace the measured claims LoRA. The
  events capacity gap then stays open and the next candidate is a
  bigger-student bake-off, its own preregistration.

### 2. Anti-suppression instruction arm (`hybrid_ev_ins`)

New `--ev-variants` arm: syn content plus one directive line appended
after the block (not a header rewording — the descriptive hedge is
measured dead):

> This list is an extracted index, not the complete record: when counting
> or totaling, re-scan the conversation above and include occurrences not
> listed here.

Mechanism-targeted at the measured churn (the answerer counting the list
instead of the context). Production serving returns a structured list; if
the arm wins, the production follow-up is docs guidance + an
`events_partial` field, its own change.

### 3. Bench plumbing

`EXTRACTORS` gains `"e4b-v3"` (:8081, operator swaps the GGUF as with
every rung); `--ev-variants` gains the ins arm; the v3 adapter is merged
and exported Q4_K_M exactly like v2. All gate comparisons are automated
inside the launch script (aggserve lesson) so the verdict is readable at
wake.

## Preregistered gates (order fixed now; GPU HELD)

### Run T — training + conformance (~3–4 h GPU)

- **T0 launch validity**: `--smoke` pass + affirmative launch
  verification within 2 minutes of the full run (unattended rules apply
  if overnight).
- **T1 claims conformance**: ladder rung `e4b-v3` vs `e4b-v2`, 5
  replicates each via `replicate.py` (reproducible protocol; it warns on
  server drift), `gold_recoverable` and cortex **non-inferior, mean drop
  ≤ 0.02**. FAIL ⇒ the preregistered fallback above; no partial ship.
- **T2 events quantity smoke**: `evals/events_quantity_smoke.py` with
  extractor `e4b-v3` must PASS (every seeded quantity verbatim, parity
  intact) — the student must inherit the teacher's quantity behaviour.
- **T3 events capacity spot** (cheap, ~25 calls): e4b-v3 on the 7
  decomposition probe rows' instance-bearing sessions vs the committed
  Opus labels (`evq-opus-probe-0807.json`): the student must capture
  **≥ half** the gold instances Opus captured. Below that, the 500-q run
  is not worth its 6 hours — stop and record.

### Run B — LME 500 q, 8 arms, extractor = e4b-v3 both passes, tag `evlora-<date>`

Arms: rag / cortex / hybrid / hybrid_ev(recon) / hybrid_ev_agg /
hybrid_ev_syn / hybrid_ev_hdr / hybrid_ev_ins. Claims prompt: the shipped
constant. Events prompt: v2 (matching the teacher). Judge/answer calls on
the reproducible `Start-Qwen` server only.

1. **rag control** (vs `evq-0806`): delta exactly 0 — rag never touches
   extraction; exact-zero stays required and achievable.
2. **claims-shift covariate** (reported; bounds attribution, does not
   pass/fail the run): hybrid(e4b-v3) vs committed `evq-0806` hybrid,
   pooled and per-type. |Δ| ≤ 0.02 pooled ⇒ gate 3 attributes cleanly to
   the events side; larger ⇒ gate 3 is read as a joint claims+events
   effect and the verdict says so.
3. **primary**: `hybrid_ev_syn`(v3 bank) vs the committed `evq-0806`
   `hybrid_ev_syn` on multi-session (n=133, paired cross-run):
   improvement with p < 0.05. The decomposition puts 14 rows in reach;
   ≥ 8 net conversions clears the sign test comfortably. Secondary
   readout (reported): the within-run events uplift syn vs hybrid on ms
   (evq's was +0.098 under qwen extraction) — it must not shrink.
4. **ins arm**: vs same-run syn, pooled over all types: no regression
   beyond 0.02 (hard gate). Reported: the 3 churn qids (85fa3a3f,
   dd2973ad, gpt4_59c863d7) individually, and ms paired vs syn. The ins
   line becomes a production candidate only if ms improves with p < 0.05;
   flipping churn rows while losing elsewhere is churn, not a win.
5. **non-inferiority**: within-run syn vs hybrid_ev(recon) on
   temporal-reasoning and the strong four, margin 0.02 each; **plus a KU
   guard**: hybrid(v3) knowledge-update within 0.02 of the committed evq
   hybrid KU (0.897) — the claims-regen data must not cost the KU win.

**Ship rule**: T1–T3 pass, B1 valid, B3 passes ⇒ the v3 GGUF ships
(HF upload, `ops/Dockerfile.extractor` MODEL_URL bump, deploy via
`ops/update.ps1`, live-verify an extract through the sidecar), chronicle
stays default-off — the soak review owns defaults. The ins line ships
only on its own gate 4 bar. B3 fails with validity intact ⇒ 4B student
capacity is falsified for events coverage; record it, and the next
candidates are a bigger student bake-off or documented shim-primary
reliance — measured, not assumed.

Verdict either way: `evals/results/evlora-verdict.json`, artifacts
committed with the claims, evidence-guard rows in the same change.

## Cost

Run T: one training run (~2–3 h at the v2 precedent) + 10 ladder
replicates (~1 h) + two smokes (minutes). Run B: one ~6 h 500-q run
(8 arms). Nothing else. HELD.
