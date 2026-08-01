# Dream literal fidelity — design

**Date:** 2026-08-02
**Status:** approved approach (two layers: prompt mandate + deterministic faithfulness gate)
**Follow-up to:** `evals/results/c2op-count-verdict.json` (v5 ship), external findings on
consolidation detail loss (arXiv:2607.21503 "validated compaction", arXiv:2607.19359
"compression residuals", arXiv:2605.12978 consolidation-degradation result)

## Problem

Consolidation is lossy in two directions the current dream pass does not control:

1. **Detail loss** — the extraction prompt asks for "durable, current-state facts" but
   never mandates that exact dates, quantities, versions, and identifiers survive
   verbatim. A note stating "the audit is due 2026-09-30" can consolidate to
   "due end of September" — recoverable by the gold-recoverability metric only until a
   query needs the exact value.
2. **Fabricated precision** — nothing checks that a literal appearing in a claim's value
   was ever stated in the source notes. A hallucinated number is written to the cortex
   with the same confidence as a real one, and supersede-in-place makes it the current
   truth.

## Design

Two independent layers, shipped together:

### Layer 1 — prompt mandate (v6)

Append one rule + one inline single-claim example to the shipped extraction prompt,
after the count-exclusion block:

> KEEP LITERALS VERBATIM: when a fact's value contains a date, a number, a version, or
> an identifier, copy it EXACTLY as the note writes it — never round it, re-format it,
> or leave it out. For example, the note [6] the security audit is due 2026-09-30 —
> yields the single claim {"entity":"security audit","attribute":"due date",
> "value":"2026-09-30","confidence":0.9,"source":6} inside the one claims array, not
> "end of September".

Construction discipline: the block is composed in `evals/op_probe.py` as
`v6-literal-fidelity` = `_BASE` + v0 op block + v5 count block + literal block, with the
v5 count block factored into a shared constant so v5/v6 cannot drift. The rule is a
single claim object with no second `Output:` block (the v3→v5 lesson: a standalone
Output block induces multi-object JSON), never mentions `op`, and avoids the word
"quantity" (owned by the count rule). Pinned artifact: `evals/prompts/ku_op_prompt_v6.txt`
(`tests/test_op_prompt_artifact.py`).

### Layer 2 — deterministic literal-faithfulness gate

Pure helpers in `pseudolife_memory/memory/dream.py`:

```python
def hard_literals(value: str) -> list[str]          # gateable tokens in a value
def literal_violations(value: str, corpus: str) -> list[str]
```

Token spec, conservative by construction:

- **Date-like spans are exempt.** Format variance makes digit matching unsafe
  ("2026-08-01" vs "August 1, 2026" shares no digit token). Dates are Layer 1's job;
  the gate owns fabricated numbers/identifiers.
- Gateable tokens: whitespace/punctuation-delimited tokens containing ≥1 ASCII digit,
  after masking date-like spans.
- Normalization per token: casefold; strip currency symbols and `%`; strip `,`/`_`
  thousands separators; strip surrounding punctuation; strip a leading `v`; strip
  ordinal suffixes.
- A token passes when any of: exact normalized match against a corpus token; numeric
  equality after `float()` (`08`↔`8`, `3.20`↔`3.2`, `1,500`↔`1500`); bidirectional
  substring against a corpus token (`v0.12.0`↔`0.12.0`, `PR-81`↔`#81`).
- No gateable tokens, or empty corpus → no violations. Spelled-out numbers
  ("thirty-two") carry no digits and are never gated.

**Corpus scope: the union of the batch's note texts, default — not just the cited
note.** Two measured false-drop classes force this:

1. Derived sums: `c2op-count-verdict.json` qid `01493427` — the gold total "25" is
   itemized across notes and never stated as one number; the extractor recovering it is
   correct behavior a per-note gate would drop.
2. Cross-note values: the batched extraction call exists precisely so a fact's initial
   and update turns are seen together (service.py:3029-3033); a claim's correct value
   can come from note A while `source` cites note B.

Configuration (`DreamConfig`):

```python
literal_gate: str = "log"            # "off" | "log" | "enforce"
literal_gate_scope: str = "batch"    # "batch" | "source"
```

Wiring: in `dream_run`'s claim loop, before the `has_trace` guard. Claims with no
resolvable `src_id` skip the gate. `log` counts, `enforce` drops. Counters surface as
top-level result keys `literal_flagged` / `literal_dropped` (NOT inside `tally`, which
is summed into the reported `claims` count). Member ops are gated identically to
scalars.

## Pre-registered ship rules (before any GPU run)

1. **Op-probe sanity** (`evals/op_probe.py`, v5 vs v6): op adoption 7/7 and count-decoy
   7/7 must both hold for v6, matching v5's sidecar numbers. This is the primary
   regression risk of touching a measured prompt; failure here stops the line.
2. **KU gate** (`longmemeval_bench.py --dataset oracle --extractor qwen-27b`, v6 arm vs
   the committed `c2op-count` v5 arm, paired permutation): v6 ships iff the `cascade`
   arm is not significantly below v5 and the digit-gold question class does not regress.
   The `rag` arm is the control; a nonzero rag delta invalidates the run.
3. **Gate default** (ladder rung `e4b-ft`, modes off/log/enforce under v6): `enforce`
   ships as the default iff its `gold_recoverable` is non-inferior to `log`; otherwise
   the default stays `log` and `enforce` is opt-in. The known casualty class is the
   derived-sum golds — that is exactly what this comparison measures.

Verdict artifact: `evals/results/literal-fidelity-verdict.json`
(`c2op-count-verdict.json` shape: preregistration, code sha, commands, arms, paired
result, class breakdown, decision, caveats).

## Non-goals

- No LLM-round-trip verification of claims (cost doubles the extraction call; the
  deterministic gate covers the fabrication class that is cheaply checkable).
- No date-format normalization matching (exempted, see above).
- No gating of claims produced outside the dream loop (`memory_fact_set` is a
  deliberate user/agent assertion, not a compaction).
