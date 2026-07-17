# Eval-hygiene sprint — replicates, paired stats, regression gate

**Status**: approved (interactive brainstorm) · **Date**: 2026-07-18
**Predecessor**: `2026-07-12-arm1-registry-datagen-design.md` (the Arm-1 gate this
sprint re-verifies), the 2026-07-17 first-principles investigation (eval-evidence
audit), memory entry "2026-07-17 eval-evidence audit finding".

## Problem

The LongMemEval-KU harness reports single runs as point estimates, and the
noise band is larger than the differences being decided on it. Three runs of
the identical sonnet-5-v1 config (same bank, byte-identical contexts,
temperature 0) scored cortex 0.808 / 0.731 / 0.782 — a 7.7 pp spread from the
answerer/judge side alone (`evals/results/longmemeval-ku-oracle-sonnet-5-v1*.summary.json`).
The +0.102 Arm-1 cortex gain that shipped as the default extractor
(2026-07-14) was a single-run-vs-single-run comparison, and its deploy gate
(+0.05) is smaller than the observed same-config variance. Nothing
regression-gates eval numbers before release.

Three deliverables: (1) a replication layer with honest statistics, (2) a
local regression gate wired into the shipping discipline, (3) re-verified
Arm-1 numbers and variance-honest docs.

## Decisions made during brainstorm

- **Scope**: build tooling *and* execute the re-verification runs on the 4090
  (overnight-style orchestrator).
- **Gate home**: local PowerShell gate script + CLAUDE.md checklist line. Not
  pytest (silent-skip trap), not GitHub CI (no local 27B judge there).
- **Stats**: mean±std per config *plus* a paired permutation test across
  questions for config comparisons. No judge-ensemble voting (would break
  comparability with all existing single-judge results).
- **Claims sequencing**: docs get the methodology/variance note now; public
  numbers are renumbered to mean±std only after the replicate data lands; the
  extractor-default decision is revisited only if the paired test fails to
  confirm the Arm-1 gain — as a follow-on, not auto-reverted here.

## Architecture — approach A (thin wrapper over existing seams)

The bench already has the right seams: `--tag` namespaces result files and
banks (`out_file`/`bank_dir`, `longmemeval_bench.py:164-171`), `--phase
extract`/`answer` decouples context-building from judging, `run_answer`
resumes rows lacking `rag_correct`, and results JSONLs (with persisted
contexts) are git-tracked. Replication therefore never re-extracts: a
replicate is a judged JSONL copied with judge fields stripped under a new
tag, then answer-phased.

Rejected alternatives: teaching the bench `--replicates N` (widens the
per-row schema all 100+ existing result files share) and a standalone
re-judge script (duplicates `_chat`/`answer_and_judge` — drift risk).

### Component 1: `evals/replicate.py`

Four subcommands sharing `--dataset --extractor --tag`:

- **`spawn -n N`** — read the source judged JSONL (e.g. tag `arm1`), strip
  `{arm}_response`, `{arm}_correct`, `{arm}_context_tokens` for all three
  arms, write replicate files under tags `<tag>-r2` … `<tag>-r<N+1>` (empty
  base tag → `r2` … `r<N+1>`). The original run is replicate r1 (matches the
  existing manual `sonnet-5-v1-r2/-r3` convention). Idempotent: existing
  replicate files are never overwritten, preserving the bench's per-row
  resumability.
- **`run`** — for each replicate tag with pending rows, call the bench's
  `run_answer` then `report` (lazy import inside the command so importing
  `replicate.py` never pulls `ladder_sweep`/torch).
- **`agg`** — discover all judged replicates for the config: the base file
  plus files whose name is the base filename with a strict `-r<digits>`
  suffix (regex `-r\d+$` on the stem — so `arm1` never matches
  `arm1-baseline`). Write `<base>.agg.json`: per-arm list of per-replicate
  accuracies, mean, sample std (ddof=1), plus `n_questions`, `n_replicates`,
  and the source filenames. Existing manual replicates (sonnet-5-v1) get
  mean±std for free.
- **`compare --b-extractor X [--b-tag Y] --arm cortex`** — paired permutation
  test between config A (from `--extractor/--tag`) and config B (from the
  `--b-*` flags; defaults mirror A, so arm1 vs arm1-baseline is
  `--extractor e4b-ft --tag arm1 --b-tag arm1-baseline`).
  Unit of pairing = question_id. Statistic = mean over questions of
  (rate_A(q) − rate_B(q)), where rate is that config's mean correctness for
  the question across its replicates. 10,000 sign-flip permutations,
  two-sided p, `random.Random(seed)` with a fixed default seed for
  reproducibility. Output: delta, p-value, both configs' mean±std. Errors if
  the two configs' question_id sets differ.

Pure functions at module top — `strip_judged(rows)`, `aggregate(rows_by_tag)`,
`paired_permutation(a_rates, b_rates, n=10000, seed=0)` — with all
endpoint/DB/torch access behind the `run` subcommand's lazy imports.

### Component 2: regression gate

`evals/regression_gate.ps1` (server-lifecycle pattern of `gate_e4b_ft.ps1`)
plus a committed baseline `evals/results/regression_gate.baseline.json`.

Pinned config: `oracle` / `e4b-ft` / tag `arm1` — the shipped default
extractor's bank and contexts.

Stages:
0. **Cleanup** — delete this gate's own previous output files (everything
   under the `arm1-gate` tag namespace). Stale judged gate files would
   otherwise resume as no-ops and silently pass.
1. **Context rebuild** via `rebuild_contexts.py --dataset oracle --extractor
   e4b-ft --src-tag arm1 --out-tag arm1-gate` from the local bank dumps with
   current knobs (catches retrieval-knob and fact-ranking changes). Banks
   are *not* git-tracked; if absent, warn loudly and fall back to copying
   the pinned `arm1` contexts into the `arm1-gate` tag as-is (then the gate
   covers only answer/judge drift). Either way the gate never touches the
   pinned `arm1` replicate files.
2. **Replicate answer runs** on the `arm1-gate` file — default 3
   (`-Replicates 1` = quick mode) → `replicate.py` spawn + run + agg.
3. **Verdict** — FAIL (exit 1) if any arm's replicate mean falls below
   `baseline_mean − margin`; margin stored in the baseline file, derived as
   `max(0.03, 2 × std_of_replicate_means)` at baseline establishment.
   The baseline file records per-arm mean, std, margin, n_replicates, date,
   and the git commit it was established at.

Scope, stated in the script header: the gate covers the
retrieval/serving/judging path. Extraction changes stay covered by the
existing ladder rule (re-run the ladder after any dream-write-path change).
CLAUDE.md's review-discipline section gains one line: eval- or
retrieval-affecting changes run `evals/regression_gate.ps1` before commit.

### Component 3: `evals/overnight_replicates.ps1`

Start the Qwen answer/judge server (`run-server-turboq.bat` pattern from
`gate_e4b_ft.ps1`), then for each config — `e4b-ft` tag `arm1`, `e4b-ft` tag
`arm1-baseline`, `qwen-27b` untagged (the README's front-door 0.705) — spawn
4 replicates, run, agg. Finish with `compare` (arm1 vs arm1-baseline, cortex
and hybrid arms) and print a verdict block.

Decision rule (pre-registered): paired p < 0.05 on the cortex arm confirms
the Arm-1 gain → shipped default stands, docs get mean±std. Otherwise the
extractor-default decision is flagged for revisit in a follow-on.

Estimated cost: ~468 short Qwen calls per replicate × 12 replicate-runs;
launchable overnight; resumable per row (kill and re-run continues).

### Component 4: docs, changelog, tests

- `evals/README.md`: new **"Variance and replication"** section — the
  observed 7.7 pp same-config spread, the replicate workflow, the mean±std
  reporting convention, a MemDelta (arXiv 2606.29914) citation on why
  single-run memory-bench comparisons mislead, and a note that findings
  tables are snapshots (`.agg.json` is authoritative where present).
- `docs/guide/benchmarks.md`: honesty paragraph — numbers are single-run
  point estimates unless marked mean±std; observed noise band. Renumber to
  mean±std for replicated configs after the overnight runs land.
- `CHANGELOG.md` `[Unreleased]`: entry for the tooling + gate.
- `tests/test_eval_replicate.py`: TDD-first, no endpoints/GPU —
  `strip_judged` (fields removed, others intact), `aggregate` (known-answer
  math), `paired_permutation` (deterministic seed; a clearly-different
  synthetic pair yields small p; a null pair yields large p), replicate-file
  discovery (tag parsing, skipping unjudged files), and compare's
  question-set-mismatch error.

## Error handling

- `spawn` refuses to overwrite existing replicate files; `run` resumes
  per row (bench semantics); `agg` skips unjudged replicates with a notice;
  `compare` fails fast on mismatched question sets or <2 total replicates
  per side.
- Gate: missing banks → loud warning + reduced-scope fallback; missing
  baseline file → instructs how to establish one (documented in the script
  header); Qwen endpoint down → retry loop, then exit 2 (distinct from the
  regression exit 1).

## Out of scope

Judge-ensemble/majority voting, the `_s` haystack starvation fix,
real-session tool-surface evals, LongMemEval-V2, any renumbering of public
claims before replicate data lands, and any change to the shipped extractor
default (data first).
