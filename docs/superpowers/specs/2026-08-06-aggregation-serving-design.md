# Aggregation-cued event serving: closing the multi-session gap

Date: 2026-08-06. Status: design before code. Preregistered gates below;
baseline is the committed `ev2-sep-0804` run.

## The evidence (all from committed artifacts)

Multi-session is the last type where memory loses to raw RAG:
hybrid_ev 0.406 vs rag 0.504 (n=133, `ev2-sep-0804`). The autopsy of
those rows decomposes the gap exactly:

1. **The gap IS the aggregation class.** 116/133 multi-session questions
   carry an aggregation cue (how many / how much / how often /
   percentage / total). On them: rag 0.517 vs hybrid_ev 0.405. On the
   17 non-aggregation rows the arms tie (0.412 = 0.412).
2. **The serve gate misses the class.** Events were served on only
   21/116 aggregation-cued rows: `_TEMPORAL_CUE_RE` knows "how many
   times" but not "how many <noun>", "how much", "how often". The
   occurrence records that could enumerate the countable instances are
   in the store and never reach the context.
3. **The serve cap forbids counting even when serving fires.**
   `chronicle_search(limit=6)`: a "how many" question whose true count
   exceeds 6 cannot be answered from the served list by construction.
4. **Union ceiling**: rag∪hybrid_ev = 0.594 on multi-session (25
   rag-only vs 12 ev-only wins) — routing alone caps below what fixing
   the served context could reach, and 54/133 both-wrong rows are out of
   scope for any serving change.
5. Priors: retrieval-side knobs failed flat (Phase 1, all four);
   events-in-context measured harmless-when-present on non-target types
   (ev2 gate 4) — which bounds the dilution risk of serving more.

## Design (shipped-code changes, both small)

1. **`has_aggregation_cue(text)`** in `memory/cms.py`, beside
   `has_temporal_cue` — a SEPARATE predicate, deliberately NOT a
   widening of `_TEMPORAL_CUE_RE`: that regex also fires the timeline
   channel, which failed its gates and measured harmful on spurious
   firing; the aggregation cue must not touch it. Cue set (word-bounded,
   casefolded): `how many`, `how much`, `how often`, `what percentage`,
   `in total`, `total number`, `altogether`, `each time`, `every time`.
2. **Serving gate + cap** in `service.search`: fire the events block on
   `has_temporal_cue OR has_aggregation_cue`; when the aggregation cue
   fires, `chronicle_search(limit=30)` (counting needs the full set),
   else the existing 6. Temporal-only queries are byte-identical to
   today (same gate, same limit, same ordering) — the first-6-of-30
   prefix under the same ORDER BY equals the old limit-6 result, which
   the harness uses to reconstruct the old-gate arm for pairing.
3. **Tally line**: when the aggregation cue fired, the served block
   (service `events` result and the harness rendering) appends
   `Total events listed: N` — a computed statement about the list, not
   a claimed answer; the answerer still judges relevance. Motivated by
   (3): enumeration fixes representational absence, the tally fixes
   arithmetic over a 30-line list.

No new config knobs: chronicle itself remains the gate; off ⇒ all of
this is dead code. The BEAM run in flight imported its modules at
process start and is unaffected by edits.

## Measurement: within-run variants on the LME harness

One extraction (chronicle on, separate pass — identical to ev2), four
judged arms + two variant contexts per question, all from the SAME
pinned search call:

- `hybrid_ev` — RECONSTRUCTED old gate: events shown iff
  `has_temporal_cue(q)`, truncated to the first 6. Pairs against the
  committed ev2 arm as an exact-reproduction validity gate.
- `hybrid_ev_agg` — the new gate/cap: events shown on either cue, full
  served list, no tally.
- `hybrid_ev_syn` — `hybrid_ev_agg` + the computed tally line.

Run: `--dataset oracle --extractor qwen-27b --types all --chronicle
--ev-variants --tag aggserve-0806`, reproducible q8_0 server, ~8 h.
Never overwrite any `ev2-sep-0804` artifact.

## Preregistered gates, in order (1–3 validity; failure ⇒ 4–5 void)

1. **rag control**: rag vs `ev2-sep-0804` rag, 500 shared questions —
   exactly 0 flips.
2. **claims-inertness**: vanilla hybrid vs ev2 hybrid — exactly 0
   flips.
3. **reconstruction check**: this run's `hybrid_ev` vs ev2 `hybrid_ev`
   — exactly 0 flips. A nonzero means the old-gate reconstruction (or
   the prefix property in Design 2) is wrong and every variant delta is
   confounded.
4. **primary**: `hybrid_ev_syn` vs same-run `hybrid_ev` on
   multi-session (n=133), paired sign-flip permutation 10k/seed 0 —
   improvement with p < 0.05. Decomposition reported (not gating):
   `hybrid_ev_agg` vs `hybrid_ev` (serving effect) and `hybrid_ev_syn`
   vs `hybrid_ev_agg` (tally effect).
5. **non-inferiority**: `hybrid_ev_syn` vs same-run `hybrid_ev` on
   temporal-reasoning AND on the four strong types (two compares,
   margin 0.02 each) — the tr +0.090 win and the strong set must
   survive the wider gate.

Ship rule: 5/5 pass ⇒ the serving change is a ship candidate PR
(chronicle still default-off; the live soak, task #36, is unaffected
until merged and is reviewed on its own schedule). Gate 4 fails with
validity intact ⇒ honest negative recorded — with enumerable events
served and counted, the residual gap is answerer-side or
extraction-coverage-side, measured next by inspecting served-context
loss rows. Verdict either way: `evals/results/aggserve-verdict.json`;
every published number gets a `test_eval_evidence.py` row.

## Known risks, stated up front

- **Wrong-count commitment**: serving countable events may convert
  abstentions into confident wrong counts if event extraction missed
  instances (the events pass has never been coverage-audited against a
  gold instance list). The judge scores wrong == wrong either way; the
  paired comparison nets this against the abstention losses it cures.
- **Dilution on cue false-positives**: bounded by ev2 gate 4
  (harmless-when-present) and by the cue list omitting bare
  `total`/`count`/`all the`.
- **OR-match noise in the tally**: `chronicle_search` ORs query terms;
  `Total events listed` counts listed lines, not claimed occurrences of
  the asked-about thing. Phrasing keeps it a list property.

## Cost

One ~8 h run (500 q × extraction + events pass + 6 judged arms).
Queued behind the BEAM re-run on the same GPU. CPU-side build is small:
one predicate, one gate line, one limit branch, one tally line, three
harness contexts, and their tests.
