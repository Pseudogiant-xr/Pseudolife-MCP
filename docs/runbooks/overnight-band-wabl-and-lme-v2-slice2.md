# Overnight run — band write-side ablation + LME-V2 slice2

Prepared 2026-07-24. Two independent threads sharing one night: the GPU
runs the LongMemEval-V2 procedure-slice expansion while the CPU runs the
band write-side replays. Everything is resumable; a crash costs minutes.

## What each thread answers

**Thread A — band write-side ablation** (`overnight_band_wabl.ps1`).
The published read-side ablation held ingest fixed and only re-ranked;
its verdict ("the continuum earns nothing on ranking") left one defence
standing: the write side. This run tests it. Flat-INGEST (one band at the
continuum's total capacity, 5,250) vs the stock 8-band ingest on the
full-haystack `s` dataset — the only corpus with real eviction pressure
(probe, 2026-07-24: ~468 turns stored → ~339 survive the continuum,
~28% evicted; the `oracle` corpus stores ~23/question and never evicts,
which is why the write side was untestable there). Comparisons:

- `wabl-flat-M` vs `abl-flat-M` — write-side isolation (identical flat
  ranking, different survivor sets).
- `wabl-flat-M` vs `abl-continuum-M` — whole-system, as-designed vs flat.
- `…-wabl-survival.json` — how much each arm kept, per band.

If flat-ingest matches or beats banded ingest here too, the continuum has
no measured defence left on retrieval and its case must rest on
architecture-external grounds (or be simplified away).

**Thread B — LME-V2 slice2** (`overnight_lme_v2.ps1`). The pilot was the
first 10 `procedure` questions in file order; slice2 is the full
74-question category (the three implemented eval heads cover exactly
these 74; `assert_specs_implemented` hard-gates any drift). Same knobs as
the pilot (`--max-trajectories 100 --bm25 --rerank --lexical-cortex`),
plus the compose-prompt reanswer. 74 questions of per-question paired
data replaces the pilot's 10 — more power than replicating the pilot.

## Launch (in this order)

```powershell
# 0 — preflight: every line must PASS
evals\preflight_overnight.ps1

# 1 — GPU thread (its window: llama-server crashes ~hourly under
#     sustained ingest; the supervisor restarts it, cursor resumes)
evals\overnight_lme_v2.ps1

# 2 — CPU thread, separate PowerShell window, runs alongside
evals\overnight_band_wabl.ps1 -Phase cpu
```

Within 2 minutes of each launch, verify FIRST OUTPUT exists (a launch is
not "running" until it has produced something):

- Thread B: `evals\results\lme-v2-smoke-slice2.jsonl` grows a row only
  after ~10.6 min — instead check `qwen-server.log` is growing and the
  console shows the first trajectory ingesting.
- Thread A: a new `.json.gz` under
  `evals\results\banks\s-qwen-27b-ablbands\` within ~2 min.

## Expected-by table (heartbeat cadence: 15 min)

| workload | expected progress | stalled if |
|---|---|---|
| B ingest (74 q) | ~1 row / 10.6 min → ~5–6 rows/hour | no new row in 25 min AND ledger shows no retry activity |
| A continuum replay (78 q) | ~1 dump / 71 s | no new dump in 10 min |
| A flat replay (78 q) | ~1 dump / 69 s | no new dump in 10 min |
| A rebuilds | minutes, after both replays | ledger stuck > 30 min after replays done |

Check with:

```powershell
evals\overnight_status.ps1
```

Ledgers: `%LOCALAPPDATA%\pseudolife-overnight\*.ledger.log` (one line per
phase event/retry — auditable next morning at a glance).

## Timing budget

- Thread A cpu phase: 2 × ~92 min replays + minutes of rebuild ≈ **~3.5 h**.
- Thread B: 74 × ~10.6 min ≈ **~13 h** — deliberately more than one
  night. The JSONL cursor makes the overrun harmless: rerun
  `overnight_lme_v2.ps1` in the next window and it continues where it
  stopped (worth ~50–56 questions per 9–10 h window). The compose
  reanswer + reports only run once the full slice is done.

## Morning-after checklist

1. `evals\overnight_status.ps1` — row/dump counts vs targets.
2. Read both ledgers end-to-end; every retry should be followed by
   progress. "INCOMPLETE" lines → rerun that script (it resumes).
3. Thread A: check the sanity-gate numbers printed by the rebuilds
   (mirror-vs-served ≈ read-side campaign levels for continuum; flat
   divergence is expected and is the effect under test), and skim
   `longmemeval-ku-s-qwen-27b-wabl-survival.json`.
4. Thread B (if slice complete): `lme-v2-smoke-slice2.summary.json` and
   `-compose` variant exist.

## GPU window 2 (after the night — thread A's answer phase)

```powershell
evals\overnight_band_wabl.ps1 -Phase answer
```

Answer+judges all 6 ablation tags (4 spawned replicates each), aggs, then
writes 8 paired-comparison artifacts
(`…-wabl-{iso,sys}-{wall,hist}-{rag,hybrid}.compare.json`) — every
p-value lands in a committed artifact per the house benchmark rule.

## Publication gate (before any number reaches the docs)

Per CLAUDE.md "Publishing a benchmark number": commit the artifacts with
the claim, add rows to `tests/test_eval_evidence.py` in the same change,
and mind the read-honestly framing — slice2 single-pass numbers are
n=74 single-run; the ablation compares are 5-replicate paired.
