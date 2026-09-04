# Runbook — token-matched rag arms (2026-09-04)

Every memory-vs-RAG comparison this project has published scores a ~100-token
fact context against a ~1,200-token raw-turn context, and reports the accuracy
gap and the token gap as two separate findings. They are one trade-off. These
runs measure it directly: the same rag retrieval, ranking, formatting,
answerer and judge, served at a narrower budget, so "the fact spine loses 0.19
accuracy" can finally be read against "…and what does raw RAG score if you
give it the fact spine's tokens?"

**All three runs below have been executed** (2026-09-04) and their artifacts
are committed. What follows is both the procedure and the result. Everything
was answered and judged on the reproducible Qwen3.8 server — dot-source
`evals/qwen_server.ps1` and call `Start-Qwen` (never `-Fast`: these runs are
judged). Check the GPU is free first.

**The headline, before the detail:** turn-granular truncation *cannot* reach a
97-token budget on LongMemEval. One raw turn is already ~200 approximate
tokens, and the budget arm's floor is one turn, so `ragb100` served a mean
**219.2** tokens — 2.2x its own name — and was byte-identical to `rag1` on 74
of the 78 rows. The honest token-matched pair on this dataset is therefore
**cortex at ~97 tokens vs one-turn RAG at ~210–220**, not "cortex vs a
100-token RAG arm". At that pairing they are indistinguishable: on the
500-question run, cortex − rag1 = **−0.006 ± 0.049 (p 0.87)**.

---

## Run A — LongMemEval KU oracle, 78 questions (`raglite-v38`)

**Why it was cheap:** the `ceiling-v38` run's contexts were already committed
and its fact banks already dumped, so no extraction happened. Only the answer
and judge calls were paid.

### Which path adds the arms — and why the two obvious ones cannot

- `--phase answer` **cannot**. It answers the context keys a row already
  persisted, and only for rows not yet judged. `ceiling-v38` is fully judged
  and its rows carry three context keys. Passing `--rag-lite-top-k` to the
  answer phase is now rejected outright rather than silently doing nothing.
- `rebuild_contexts.py` **cannot**. It rebuilds cortex and hybrid from the
  dumped fact banks but copies the rag context verbatim, and the banks hold
  facts only — the rag arm's ranked turn list is not in them. Splitting the
  persisted rag block back into turns does not recover it either: turn texts
  contain blank lines, so only **6 of the 78** rows split into the 6 turns
  that were actually served.
- `evals/rag_lite_rebuild.py` (new, in this branch) **can**, on the CPU. It
  re-ingests each question's haystack turns into a fresh bench service, re-runs
  the control's pinned search, and refuses to write unless the re-derived rag
  context matches the judged one **byte for byte**.

### Step 1 — rebuild (CPU only, no server needed)

```bash
PYTHONPATH=. python evals/rag_lite_rebuild.py --dataset oracle \
    --extractor qwen-27b --src-tag ceiling-v38 --out-tag raglite-v38 \
    --rag-lite-top-k 1,2 --rag-budget-tokens 100,400
```

78 rows, all re-derived byte-exact. Writes
`evals/results/longmemeval-ku-oracle-qwen-27b-raglite-v38.jsonl`. No GPU.

### Step 2 — answer and judge (GPU)

```bash
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle \
    --extractor qwen-27b --tag raglite-v38 --phase answer
```

Arms judged: `rag`, `rag1`, `rag2`, `ragb100`, `ragb400`, `cortex`, `hybrid`
(+ the derived `cascade` in the report). The rebuild stripped **every** arm's
verdict, so this pass re-judged the carried-over arms too — deliberate: a
within-run paired comparison needs one instrument in one pass. The carried
arms re-scored exactly their `ceiling-v38` values (rag 0.859, hybrid 0.846,
cortex 0.667), which is the reproducible-server check passing.

### Result

`evals/results/longmemeval-ku-oracle-qwen-27b-raglite-v38.summary.json`:

| arm | accuracy | context tokens | note |
|-----|----------|----------------|------|
| `rag` (control, 6 turns) | 0.859 | 1184.1 | |
| `hybrid` | 0.846 | 731.3 | |
| `cascade` (derived) | 0.846 | 389.4 | |
| `rag2` | 0.551 | 429.7 | |
| `ragb400` | 0.500 | 309.0 | 17 of 78 rows over budget |
| `ragb100` | 0.333 | 219.2 | 36 of 78 over budget; = `rag1` on 74 of 78 |
| `rag1` | 0.321 | 217.1 | |
| `cortex` | 0.667 | 96.7 | |

**The comparison this run exists to make, read honestly.** The budgets were
chosen off `ceiling-v38`'s published costs: `ragb100` to match the cortex
arm's 96.7 tokens and `ragb400` the cascade's 389.4. `ragb400` landed
(309.0 served against a 400 budget). **`ragb100` did not, and could not.**
Truncation here is turn-granular and the arm always serves at least one turn
(an arm that can serve empty is a second no-memory control), so its floor is
the cost of the top-ranked turn — ~200 tokens on this dataset. It served
219.2, exceeded its budget on 36 of 78 rows, and produced the same context as
`rag1` on 74 of 78. `ragb100` **is** `rag1` under another name, and their
accuracies say so: 0.333 vs 0.321.

So the token-matched question does not get a 97-token RAG answer. What it gets
is: at ~210–220 served tokens — a bit over 2x the fact spine's cost — plain
RAG scores **0.321–0.333** against the fact spine's **0.667** at 96.7. Give
raw turns 3x the fact spine's budget (`ragb400`, 309 tokens) and RAG reaches
0.500, still below cortex. The fact spine is not merely cheaper here; per
token it is far denser than the turns it was built from.

---

## Run B — BEAM 100K, 2 chats (`raglite-smoke`; first BEAM run to carry token costs)

```bash
PYTHONPATH=. python evals/beam_adapter.py --beam-root <path-to-BEAM> \
    --tier 100K --extractor qwen-27b --out-tag raglite-smoke \
    --limit-chats 2 --rag-lite-top-k 1,2 --rag-budget-tokens 600
```

Arms answered: `rag`, `rag1`, `rag2`, `ragb600`, `cortex`, `hybrid` — the
adapter's default three plus the three knob-minted ones. Every arm records
`{arm}_context_tokens`, and `--report` prints a `context_tokens` mean per arm
and per question type. This is the **first BEAM run that carries token costs
at all**; the `chip12-b16` column was back-estimated from persisted
characters.

`--rag-budget-tokens 600` was sized off that same artifact's measured costs:
rag serves **5,539** tokens/question, hybrid 6,099, cortex **551**. So 600 is
the cortex-matched budget on BEAM — roughly one turn of the nine the rag arm
serves.

### Result — 2 chats x 20 questions = 40 rows

`evals/results/beam-100K-qwen-27b-raglite-smoke.summary.json`:

| arm | score | context tokens | note |
|-----|-------|----------------|------|
| `hybrid` | 0.5629 | 3635 | |
| `rag` (control) | 0.4462 | 3158 | |
| `rag2` | 0.3396 | 1188 | |
| `cortex` | 0.2956 | 468 | |
| `rag1` | 0.2750 | 496 | |
| `ragb600` | 0.2600 | 584 | 12 of 40 rows over budget |

BEAM's turns are shorter relative to the budget, so `ragb600` did land near
its name (584 served against 600) — the budget arm works as designed when one
turn is comfortably under budget. At that matched cost the fact spine
(`cortex`, 468 tokens, 0.2956) and the budget-matched raw turns (`ragb600`,
584 tokens, 0.2600) are close, and both are far below the full rag control at
7x the tokens. **Two chats is a smoke, not a verdict** — 40 rows on one tier.

### Afterwards

```bash
PYTHONPATH=. python evals/beam_within_run_pairs.py --tag raglite-smoke \
    --arms rag1,rag2,ragb600,hybrid,cortex
```

writes `beam-100K-qwen-27b-raglite-smoke.arms-vs-rag.json` with the paired
delta against the `rag` control and both cost columns
(`context_chars_mean`, `context_tokens_mean`).

---

## Run C — LongMemEval all-types oracle, 500 questions (`raglite-all-fresh`)

The whole benchmark, token-matched: **fresh extraction**, all six question
types, arms `rag` / `rag1` / `rag2` / `ragb400` / `cortex` / `hybrid` plus the
derived `cascade`. 07:33–11:42 on 2026-09-04 (~4 h 09 for 500 questions,
≈30 s/question end to end).

```bash
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle \
    --extractor qwen-27b --types all --tag raglite-all-fresh \
    --rag-lite-top-k 1,2 --rag-budget-tokens 400
```

### Why `ragb100` is not in this run

Run A settled it: at a 100-token budget the arm served 219.2 tokens and was
byte-identical to `rag1` on 74 of 78 rows. Answering and judging it over 500
questions would have bought a second copy of the `rag1` column at the cost of
a full extra arm-pass — 500 answer calls and 500 judge calls for a duplicate.
The `rag1` arm already IS the sub-100-token comparator that LongMemEval
admits; `ragb400` stays because it lands on its budget (312.3 served) and
measures a genuinely different width.

### Result

`evals/results/longmemeval-all-oracle-qwen-27b-raglite-all-fresh.summary.json`
(500 questions; the summary's leak check flags 25 gold-leak rows, and the
paired artifact below deliberately keeps them so every arm is paired over the
same 500):

| arm | accuracy | context tokens | note |
|-----|----------|----------------|------|
| `hybrid` | 0.730 | 1229.3 | |
| `cascade` (derived) | 0.692 | 843.7 | |
| `rag` (control, 6 turns) | 0.690 | 1124.2 | |
| `ragb400` | 0.460 | 312.3 | 98 of 500 rows over budget |
| `rag2` | 0.458 | 432.5 | |
| `rag1` | 0.316 | 206.3 | |
| `cortex` | 0.310 | 96.5 | |

Paired against the `rag` control over all 500 rows —
`evals/results/longmemeval-all-oracle-qwen-27b-raglite-all-fresh.arms-vs-rag.json`,
written by `evals/beam_within_run_pairs.py` (10,000 sign-flip permutations,
seed 0):

| arm | delta vs rag | 95% CI ± | perm p | W / L |
|-----|--------------|----------|--------|-------|
| `hybrid` | **+0.040** | 0.031 | 0.015 | 41 / 21 |
| `cascade` | +0.002 | 0.022 | 1.00 | 16 / 15 |
| `ragb400` | −0.230 | 0.041 | 0.0001 | 9 / 124 |
| `rag2` | −0.232 | 0.042 | 0.0001 | 13 / 129 |
| `rag1` | −0.374 | 0.045 | 0.0001 | 8 / 195 |
| `cortex` | −0.380 | 0.048 | 0.0001 | 16 / 206 |

And the pairing the whole exercise was for — the fact spine at 96.5 tokens
against one-turn RAG at 206.3, the cheapest comparator this dataset's turn
granularity admits:

**`cortex` − `rag1` = −0.006 ± 0.049, p 0.87 (77 W / 80 L / 343 ties).**

At roughly half the served tokens, the fact spine scores what one raw turn
scores. That is the token-matched read the project has never had, and it is
flat — which is a stronger statement than either arm's headline accuracy,
because the two arms share nothing but the question.

```bash
PYTHONPATH=. python evals/beam_within_run_pairs.py --tag raglite-all-fresh \
    --prefix longmemeval-all-oracle-qwen-27b- \
    --score-key correct --type-key question_type \
    --arms cortex,hybrid,rag1,rag2,ragb400,cascade --pairs cortex:rag1 \
    --note "paired vs the rag control over all 500 rows, ..."
```

---

## The budget arm's floor: read the measured cost, never the name

This is the one operational caveat worth carrying into any future run.

- **Overshoot is the common case on LongMemEval, not an edge case.** At a
  100-token budget, 36 of 78 rows exceeded it (mean 388.5 tokens on those
  rows); at 400, 98 of 500 did. The mechanism is the always-serve-one-turn
  floor: when the top-ranked turn alone exceeds the budget, the arm serves
  that turn rather than nothing, because an arm that can serve empty is a
  second no-memory control rather than a budget comparator.
- **Turn-granular truncation cannot reach a ~97-token budget on this
  dataset.** Nothing shorter than one turn is servable, and one turn is
  ~200 tokens. A sub-turn budget arm would have to cut inside a turn, which
  would stop being "the rag control's own context, narrower".
- **Both harnesses now say so in the artifact.** Every `ragb<N>` arm's
  summary block carries `budget_overshoot_rows` beside its
  `context_tokens` mean, and `rag_lite_contexts` emits a warning the first
  time a served block exceeds its budget in a run.
- **The budget knob has no near-duplicate guard, and cannot have one.**
  `--rag-lite-top-k` rejects a width at or above the control's, because that
  is decidable from the flag. What a *token* budget resolves to is a property
  of the data: too high and it copies the control, too low and it copies
  `rag1`. Only the run's recorded cost can say which happened.

## Adding these arms to a run that already exists

`evals/rag_lite_rebuild.py` takes `--slug ku` (default, the 78-question
knowledge-update family) or `--slug all` (the 500-question six-type family);
the slug resolves **both** the source and the destination filename. It
replaced a separate wrapper module that monkeypatched the bench's `out_file`
globally and had no test.

It was used once under the `all` slug, and it refused: the fidelity smoke
against `alltypes-0803` **stopped on row 1** because the re-derived rag
context did not match the judged one byte for byte. The retrieval stack has
moved since that run, so arms rebuilt onto it would have measured drift rather
than budget. That is why Run C is a **fresh extraction** and not a rebuild.

`--limit N` is a fidelity smoke and its output is a short file under an
otherwise normal name, so every row it writes is stamped `partial: true` and
the progress denominator is the limited slice, not the full source.
