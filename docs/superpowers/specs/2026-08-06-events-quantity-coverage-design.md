# Events quantity + coverage: v2 extraction prompt, cue widening, anti-suppression header

Date: 2026-08-06. Status: design before code; **ALL GPU RUNS HELD** until
released by the user. Preregistered gates below; evidence first.

## Motivation (all from `evals/results/events-coverage-audit-0806.json`)

The aggserve-0806 audit read the haystack sessions for all 24 multi-session
residual rows (rag right, `hybrid_ev_syn` wrong) plus the 6 syn-win rows,
enumerated gold evidence with verified verbatim quotes, and joined the labels
with cue/serving ground truth. Mechanisms, n=24:

| mechanism | n | example |
|---|---|---|
| not-event-shaped (static facts) | 7 | "how many siblings", "average GPA" premise rows |
| cue-miss (regex gap) | 5 | "What is the **total amount** I spent…" fires nothing |
| extraction-or-retrieval gap (cue fired, empty block) | 4 | "how many online courses" — instances existed, none served |
| quantity-not-representable (event served, number stripped) | 4 | "completed first full marathon" — session said "in **4h 22min**" |
| partial block (some instances missed) | 2 | 1 of 4 cuisines served |
| answerer failure | 2 | undated entry discounted; one hallucination |

19 of 24 residuals are amount-arithmetic and 19/24 need the event to carry a
number. The win rows prove the whole mechanism end-to-end: `3fdac837`'s
events carried "4-day trip" and "from April 15th to 22nd" in their
descriptions and the answerer computed the total correctly. **Quantity in
description = win; quantity dropped = loss**, on otherwise-matching shapes.
The `chronicle_events` schema needs no change — `description` is free text;
the v1 prompt simply never asks for quantities.

The BEAM autopsy (same artifact) adds two findings:

1. **Answer-time ordering synthesis is evidence-dead.** 0 of 23 served
   event_ordering rows failed by misordering correctly-served events; where
   the block held the queried items the model ordered them by date unaided
   (chat 20/1: 0.0 → 0.8). Not built; recorded so it is not re-derived.
2. **Abstention-suppression is real**: 6 of 8 regressions were hybrid
   answering from ordinary retrieval (0.5–0.6) while hybrid_ev collapsed to
   "I don't know" (0.0) — a served-but-partial events block reads as
   authoritative-and-exhaustive. Same family as the TiMem window-gate
   claim-suppression lesson.

## Design (three shipped-code changes, all small; no schema bump)

1. **`events_pass_v2.txt`** — v1 body plus one rule with one inline example
   (the v3→v5 prompt lesson: no second Output block):

   > KEEP QUANTITIES AND BE EXHAUSTIVE: extract EVERY distinct occurrence,
   > and when an occurrence carries a number — a price, a distance, a
   > duration, a count — copy it into the description EXACTLY as the note
   > writes it. For example, the note [4] i sold 15 jars of jam at the
   > market on May 29th, earning $225 — yields
   > {"description":"sold 15 jars of jam at the market, earning $225",
   > "actor":"user","date":null,"date_phrase":"on May 29th","source":4}
   > (date null only if the year cannot be pinned; quantities kept verbatim).

   The date example above deliberately reuses v1's null-date rule. The
   shipped extractor constant does NOT flip to v2 in this change — v2 ships
   only through the measured gate below, exactly like the claims-prompt
   v5→v6 procedure. Addresses: quantity-not-representable (4), partial
   block (2), and makes the cue-miss/gap rows answerable once served.

2. **`has_aggregation_cue` widening** — add `total <noun>` ("total amount /
   total distance / total cost…", generalising the existing "total number"),
   `average`, and `the most`. Addresses cue-miss (5). Stays deliberately
   separate from `_TEMPORAL_CUE_RE` (which also fires the gate-failed
   timeline channel). Risk of over-firing is bounded by the measured
   aggserve gate 5: the strong four sat at exactly zero flips under wider
   serving — serving where it does not help is harmless.

3. **Partial-record header** — the bench events block header gains an
   explicit incompleteness marker as a NEW variant arm (`hybrid_ev_hdr` =
   syn + hedged header): "Events (dated, oldest first; partial record —
   other context may hold more):". Aimed at the 6 BEAM
   abstention-suppression regressions. Production serving returns a
   structured list, not this text block; if the arm wins, the production
   follow-up is a `events_partial: true` field + docs guidance, its own
   change.

Not-event-shaped (7) and answerer (2) rows are explicitly out of scope; they
bound any events-side fix at ~15/24 residuals.

## Preregistered gates (GPU runs HELD; order fixed now)

Run A (cheap, minutes): deterministic extraction smoke on synthetic
quantity-bearing notes — v2 must retain every seeded quantity verbatim and
v1-parity on the non-quantity events; plus the existing events smoke shape.
Run B (LME 500q, ~6h): arms rag / cortex / hybrid / hybrid_ev(recon) /
hybrid_ev_agg / hybrid_ev_syn / hybrid_ev_hdr (all seven `--ev-variants`
arms — agg re-earns its cost as the serving-vs-tally decomposition under
the v2 bank), extraction events prompt = v2, claims prompt untouched,
tag `evq-<date>`.

1. **rag control** (vs `aggserve-0806`): delta exactly 0 — rag never
   touches extraction, so exact-zero remains achievable and required.
2. **claims-inertness** (vs `aggserve-0806` hybrid): **noise-floor bound,
   not exact zero** (the exact-zero cross-run bar failed twice on
   2026-08-06 for a measured server-stream reason — memory signal 426):
   |delta| ≤ 0.01 AND p > 0.05. The claims prompt is byte-untouched.
3. **primary**: `hybrid_ev_syn`(v2 bank) vs the committed `aggserve-0806`
   `hybrid_ev_syn` on multi-session (n=133, paired cross-run): improvement
   with p < 0.05. The audit's mechanism ceiling says up to ~15 rows are in
   reach; ≥8 net conversions clears the sign test comfortably.
4. **header arm** (reported + gating only against harm):
   `hybrid_ev_hdr` vs same-run `hybrid_ev_syn` pooled over all types:
   no regression beyond 0.02. Its BEAM-facing win case is only measurable
   on a BEAM re-run, which this preregistration defers.
5. **non-inferiority**: syn(v2) vs same-run hybrid_ev(recon) on
   temporal-reasoning and the strong four, margin 0.02 each (unchanged
   from the aggserve preregistration).

Ship rule: gates 1–2 valid and gate 3 passes ⇒ v2 prompt + cue widening are
ship candidates (chronicle still default-off; soak review owns defaults).
Gate 3 fails with validity intact ⇒ the extraction-side hypothesis is
falsified at the prompt level and the next probe is retrieval
(`chronicle_search` matching) on the gap rows — measured, not assumed.
Verdict either way: `evals/results/evq-verdict.json`.

## Cost

Run A minutes; Run B one ~5.5 h run (6 arms). Nothing else. HELD.
