# Runbook — token-matched rag arms (2026-09-04)

Every memory-vs-RAG comparison this project has published scores a ~100-token
fact context against a ~1,200-token raw-turn context, and reports the accuracy
gap and the token gap as two separate findings. They are one trade-off. These
two runs measure it directly: the same rag retrieval, ranking, formatting,
answerer and judge, served at a narrower budget, so "the fact spine loses 0.19
accuracy" can finally be read against "…and what does raw RAG score if you
give it the fact spine's tokens?"

Prepared by the arms work on `feat/rag-lite-arms`; **neither run has been
executed**. Both need the reproducible Qwen3.8 server — dot-source
`evals/qwen_server.ps1` and call `Start-Qwen` (never `-Fast`: these runs are
judged). Check the GPU is free first.

---

## Run A — LongMemEval KU oracle, 78 questions (cheapest; do this first)

**Why it is cheap:** the `ceiling-v38` run's contexts are already committed and
its fact banks are already dumped, so no extraction happens. Only the answer
and judge calls are paid.

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
  context matches the judged one **byte for byte**. Verified on the first 5
  ceiling-v38 rows on 2026-09-04: 5/5 byte-exact, 150 s wall.

### Step 1 — rebuild (CPU only, no server needed)

```bash
PYTHONPATH=. python evals/rag_lite_rebuild.py --dataset oracle \
    --extractor qwen-27b --src-tag ceiling-v38 --out-tag raglite-v38 \
    --rag-lite-top-k 1,2 --rag-budget-tokens 100,400
```

Expected: **~25–30 min** on the CPU embedder for 78 rows (150 s for 5 rows,
most of the first minute being model load). Writes
`evals/results/longmemeval-ku-oracle-qwen-27b-raglite-v38.jsonl`. No GPU.

### Step 2 — answer and judge (GPU)

```bash
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle \
    --extractor qwen-27b --tag raglite-v38 --phase answer
```

Arms judged: `rag`, `rag1`, `rag2`, `ragb100`, `ragb400`, `cortex`, `hybrid`
(+ the derived `cascade` in the report). The rebuild stripped **every** arm's
verdict, so this pass re-judges the carried-over arms too — deliberate: a
within-run paired comparison needs one instrument in one pass, and on the
reproducible server the carried arms re-score identically (a disagreement
means the run drifted onto the fast server).

Expected: 78 questions x 7 arms x 2 calls (answer + judge) at ~0.5 s/call =
**~12–15 min** of GPU time (`evals/README.md`: a 5-replicate 3-arm config over
these 78 questions took ~17 min).

### What it produces

| file | what |
|------|------|
| `evals/results/longmemeval-ku-oracle-qwen-27b-raglite-v38.jsonl` | judged rows, all seven arms |
| `evals/results/longmemeval-ku-oracle-qwen-27b-raglite-v38.summary.json` | accuracy + `context_tokens` per arm |

### The comparison to read

`ceiling-v38` published: rag **0.859** @ 1184 tok, hybrid **0.846** @ 731,
cascade **0.846** @ 389, cortex **0.667** @ 97. The budgets were chosen off
those numbers: `ragb100` matches the cortex arm's 96.7 tokens and `ragb400`
the cascade's 389.4. (The original plan said `ragb300`; it matches neither
published budget, which is why it is not used here.) The question the run
answers: at 97 tokens of raw turns, does RAG still beat the fact spine's
0.667 — and at 389, does it still beat the cascade's 0.846?

---

## Run B — BEAM 100K, 2 chats (first BEAM run to carry token costs)

```bash
PYTHONPATH=. python evals/beam_adapter.py --beam-root <path-to-BEAM> \
    --tier 100K --extractor qwen-27b --out-tag raglite-smoke \
    --limit-chats 2 --rag-lite-top-k 1,2 --rag-budget-tokens 600
```

Arms answered: `rag`, `rag1`, `rag2`, `ragb600`, `cortex`, `hybrid` — the
adapter's default three plus the three knob-minted ones; no `--arms` filter is
needed. Every arm now records `{arm}_context_tokens`, and `--report` prints a
`context_tokens` mean per arm and per question type. This is the **first BEAM
run that carries token costs at all**; the `chip12-b16` column was
back-estimated from persisted characters.

`--rag-budget-tokens 600` is sized off that same artifact's measured costs:
rag serves **5,539** tokens/question, hybrid 6,099, cortex **551**. So 600 is
the cortex-matched budget on BEAM — roughly one turn of the nine the rag arm
serves.

Expected: 2 chats x 20 questions = **40 rows**, 6 arms. Measured from the
committed `chip12-b16` full-tier run: ingest **401 s/chat** (~13 min for two)
and **73.7 s/row for 5 arms** (~14.7 s per arm-row), so 40 rows x 6 arms is
~59 min of answer+judge. Budget **~1 h 15 min** end to end; confirm against the
first chat's printed `ingested ... (Ns)` line before walking away. Resumable
per question row.

### What it produces

| file | what |
|------|------|
| `evals/results/beam-100K-qwen-27b-raglite-smoke.jsonl` | judged rows, six arms, with `{arm}_context_tokens` |
| `evals/results/beam-100K-qwen-27b-raglite-smoke.summary.json` | per-arm score + `context_tokens`, per-type both |
| `evals/results/banks/beam-100K-qwen-27b-raglite-smoke/` | fact banks (gitignored) |

### Afterwards

```bash
PYTHONPATH=. python evals/beam_within_run_pairs.py --tag raglite-smoke \
    --arms rag1,rag2,ragb600,hybrid,cortex
```

writes `beam-100K-qwen-27b-raglite-smoke.arms-vs-rag.json` with the paired
delta against the `rag` control and both cost columns
(`context_chars_mean`, `context_tokens_mean`).

---

## Caveats to record with any number these runs produce

- **Two chats is a smoke, not a verdict.** 40 rows on one tier; the
  comparator-arms table in `evals/README.md` is the full-tier precedent.
- **A single replicate.** Run A's answer phase is reproducible on the q8_0
  server; if the carried-over `rag`/`cortex`/`hybrid` arms do not re-score
  exactly their `ceiling-v38` values, the run is on the fast server and every
  number in it is suspect.
- **The budget arms overshoot on one row shape.** When the top-ranked turn
  alone exceeds the budget, the arm serves that turn rather than nothing (an
  arm that can serve empty is a second no-memory control). The row's recorded
  `{arm}_context_tokens` shows it; read the arm's measured mean, not its name.
