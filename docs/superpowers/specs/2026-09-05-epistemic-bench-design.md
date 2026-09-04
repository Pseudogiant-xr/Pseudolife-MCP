# Epistemic bench — design and preregistration (2026-09-05)

Three months of retrieval evals say the fact spine and naive RAG are the
same benchmark. On LongMemEval-500 the rag arm scores 0.690 and the
cascade 0.692; on BEAM-100K rag 0.6425 and hybrid 0.6226. Every one of
those numbers asks the same question — *did the served context let the
answerer produce the gold string* — and on that question a pile of raw
turns is as good as a curated set of cortex slots, because the gold
string is in the pile.

The 2026-09-04 fresh-eyes audit concluded the spine's real value is not
retrieval accuracy but **epistemics**: knowing what changed, which value
is current, how old a value is, who asserted it, and when the honest
answer is "I don't know". Nothing in the tree measures that. This bench
does, and it scores the **served context**, never a model answer — no
judge, no answerer, no GPU.

- Harness: `evals/epistemic_bench.py`
- Tests: `tests/test_epistemic_bench.py`
- Artifacts: `evals/results/epistemic-bench-<tag>.json` (+ `.jsonl` rows)

## 0. Preregistration status

Sections 1–7 were written and committed **before any harness number was
read**. The smoke artifact lands in a later commit; the commit ordering
on `eval/epistemic-bench` is the evidence. Section 8 (results) is filled
in afterwards and never edits sections 1–7 — deviations go in section 9.

## 1. What is measured

Five dimensions. Each is a deterministic predicate over one arm's served
context for one question — string containment on the served text, or a
structural read of the served payload (the entry / fact dicts the
serving call returned). No LLM is involved in scoring, and the same
predicate runs identically on every arm.

| # | Dimension | Predicate (per question, per arm) | Direction |
|---|-----------|-----------------------------------|-----------|
| D1 | `update_following` | the slot's **current** value appears in the served text | higher better |
| D2 | `stale_serving` | a **superseded** value appears in the served text and the current value does not appear anywhere in it | lower better (defect) |
| D3 | `staleness_marking` | for a slot past 2×TTL: the served payload carries the stale signal (`stale: true`, the `demote` warning, or the `quarantine` wrapper) for that slot | higher better |
| D4 | `abstention_support` | for a question whose answer was **never stated**: the served context contains no confident value for the slot | higher better |
| D5 | `retraction_handling` | for a slot the user explicitly corrected: the corrected value is served **together with** an explicit correction signal — the supersession chain in the fact line (`earlier values, oldest first:`), the fact payload's `supersedes_value`, or a served entry's `superseded_by_text` | higher better |

Companion metric, reported beside D4 and never separately:

- `answer_coverage` — over every **answerable** question (any question
  whose value was stated at least once), the current value appears in
  the served text. D4 without this is meaningless: an arm that serves
  nothing scores 1.000 on D4 and 0.000 here.

Two framing rules that make the table readable:

- **D2 is a defect count, not an accuracy.** It counts the specific
  failure "confidently serve a value that is no longer true, with
  nothing in the context saying otherwise". An arm can score well on D1
  and badly on D2 at once (it serves both values, unmarked); an arm can
  score 0 on both (it serves nothing).
- **D3 and D5 have a structural floor of 0 for the `rag` and `nomem`
  arms** on the fact channel. Raw turns carry no `freshness_class` and
  no supersession chain, so there is no marker for the predicate to
  find. That is the point of the dimension, not a bug in it — but it
  means a rag-vs-cortex delta on D3/D5 is a statement about *what the
  representation can express*, not about how well either retrieves.
  D5 is deliberately given the rag arm a channel it *can* win on:
  `superseded_by_text` is stamped on **band entries** by contradiction
  detection at `store()` time, so a raw turn can be served carrying its
  own retraction.

## 2. Arms

Reused, not re-implemented: the harness imports
`evals/longmemeval_bench.py` and calls `build_contexts` and
`serve_comparator_arms`. Retrieval widths, fact-line rendering and the
hybrid join all come from that module, so an epistemic-bench arm and a
LongMemEval arm of the same name are the same served context.

| Arm | Source | Note |
|-----|--------|------|
| `rag` | `build_contexts()["rag"]` | top-`RAG_TOP_K` raw turns |
| `cortex` | `build_contexts()["cortex"]` | fact lines with supersession chains |
| `hybrid` | `build_contexts()["hybrid"]` | facts + top-`HYBRID_TOP_K` turns |
| `nomem` | `serve_comparator_arms(..., nomem=True)` | explicit empty context |
| `cascade` | derived | **context-level proxy**, see below |

`cascade` in the judged harnesses is an *answer*-level policy: take the
cortex answer when that arm commits, fall back to rag on abstention.
There is no answer here, so this bench serves a **context-level cascade
proxy**: the cortex context when it is non-empty, the rag context
otherwise. It is a different object from the published cascade number
and is labelled as such in every artifact (`caveats.cascade_proxy`). A
cascade figure from this bench must never be compared to a judged
cascade accuracy.

`refind` is excluded: its search loop is planned by a model, and this
bench is CPU-only and judge-free by construction.

## 3. Ground truth — source (a), the synthetic generator

Seeded, deterministic, and the only source the smoke runs. N entities ×
M attributes, values changing across K sessions on an explicit timeline.
The generator emits, per question, the full epistemic state: which value
is current, which values are superseded, whether the slot is past
2×TTL, whether the slot was explicitly corrected, and whether the answer
was ever stated at all.

Question classes, all produced by one seeded generator:

- **update** — the value changed at least once. Scores D1, D2, D5.
- **stable** — the value was stated once and never changed. Scores D1;
  contributes to `answer_coverage`. Guards against an arm that "follows
  updates" by always serving the newest thing it can find.
- **stale** — a `volatile` slot last asserted more than 2×TTL (42 days)
  before the run's anchor. Scores D3.
- **correction** — the user explicitly retracts a value ("that was
  wrong, it is actually Z"). Scores D5, and D1/D2 as an update.
- **unstated** — the entity/attribute is never written and never
  mentioned in any turn. Scores D4.

Ingestion writes both channels of the bank:

1. **Turns** go in through `svc.store()`, one per stated value plus
   filler turns, so the associative store the `rag` arm reads is
   populated exactly as a real session would populate it.
2. **Facts** go in through `svc.cortex_write()` **directly**, in
   chronological order, with the session's timestamp passed as `now` and
   the intended `freshness_class`. No extractor model runs.

**Why writing facts directly is legitimate here, and where it is not.**
This bench scores the *serving* layer — what the engine hands an agent
once it holds a fact. Extraction quality is a different question with
its own instrument (the ladder, the LongMemEval extractor arms). Writing
facts directly holds extraction fixed at perfect so the serving
difference is not confounded by it. The cost is stated plainly and
carried in every artifact's `caveats`: **the synthetic source's cortex
arm is an upper bound on the deployed system**, because a real bank's
cortex is only as good as the dream pass that filled it. A synthetic
cortex win is a statement about the *ceiling* of the representation, not
about what a deployed bank serves today.

HLC ordering is the engine's, not the generator's: `cortex_write` ticks
the hybrid logical clock per call, so writing values in chronological
call order produces the real supersession chain. The generator never
touches HLC values.

## 4. Ground truth — source (b), the LongMemEval-derived slice

The knowledge-update question type carries, for each question, exactly
the structure this bench needs: two evidence sessions, dated, where the
earlier states an old value and the later states the value that is gold.
The derivation is pure parsing, with no model and no judgement:

1. The question has exactly **two** evidence sessions
   (`answer_session_ids`); order them by `haystack_dates`.
2. Take the gold answer's leading value token under one of four
   families, in priority order: `time` (`27:12`), `money` (`$40`),
   `percent` (`15%`), `number` (`220`). No token in any family → skip.
3. The gold token must appear (word-boundary match) in the **later**
   session's `has_answer` turns, and must **not** appear in the earlier
   session's. Otherwise skip.
4. In the earlier session's `has_answer` turns, collect every token of
   the **same family**, drop the gold token, drop any token that also
   appears in the later evidence. Exactly one candidate must remain —
   that is the old value. Zero or more than one → skip (ambiguous).

**Qualifying count: 22 of the 78 knowledge-update questions**, identical
on `longmemeval_oracle.json` and `longmemeval_s_cleaned.json` (the
derivation reads only the evidence sessions, which the two files share).
The 56 skips break down as: 39 gold answers carrying no value token at
all (prose answers — "the Nikon D850", "her sister's wedding"), 8 where
the gold token is not literally in the later evidence turns (the gold is
a paraphrase), 8 ambiguous old-value candidates, 1 where the gold token
also appears in the earlier session. Nothing is guessed to rescue a
skip; a question that does not derive cleanly is not in the slice.

The LME slice scores **D1, D2 and D5 only**. D3 needs a `freshness_class`
and a TTL the dataset does not carry, and D4 needs questions whose
answer was never stated, which the knowledge-update type does not
contain by construction.

**The LME slice needs an extractor to populate the cortex.** Its bank is
built by `ingest_and_dream` — the real path — so the cortex/hybrid arms
require an OpenAI-compatible extractor endpoint (`--extractor-url`). The
harness refuses those arms without one rather than silently serving an
empty cortex. `rag` and `nomem` run CPU-only on this source. The
derivation itself is CPU-only and its counts are committed as
`evals/results/epistemic-bench-lme-derivation-<date>.json`.

## 5. Pre-registered expectations

Written before any run. Each names what would count as the prediction
failing.

**E1 — D1 (update_following): `hybrid` ≥ `cortex` ≥ `rag` > `nomem` = 0.**
The cortex serves the current value *by construction* — that is what a
slot is — and the hybrid arm is a superset of it. The rag arm has to
retrieve the right turn out of a pile that also contains the old one,
which it will often do (the old and new turns are near-duplicates in
embedding space, so both tend to be retrieved). *Falsified if* rag
matches or beats cortex, which would say slot-keying buys nothing even
at perfect extraction.

**E2 — D2 (stale_serving): `rag` ≫ `cortex` ≈ `hybrid` ≈ 0.**
This is the sharpest prediction in the bench and the one the retrieval
benchmarks cannot see. When rag retrieves the old turn and misses the
new one it serves a confidently wrong value with nothing marking it. The
cortex cannot do this: the serving path reads the *current* record, and
the chain renders the current value first with older ones explicitly
labelled `earlier values`. *Falsified if* the cortex/hybrid defect rate
is within noise of rag's — that would mean the spine's supersession is
not reaching the served context.

**E3 — D3 (staleness_marking): `cortex` = `hybrid` = 1.000; `rag` = 0.000.**
Structural, not statistical: the fact payload carries `stale` for every
slot past 2×TTL, and a raw turn carries nothing. *Falsified if* cortex
is below 1.000, which would be a serving bug (a stale slot reaching an
agent unmarked), not a measurement.

**E4 — D4 (abstention_support): `nomem` = 1.000, and every memory arm
close to 1.000, with `answer_coverage` separating them.** An unstated
slot has no fact and no turn, so nothing should surface a value for it.
The interesting failure is a *near-miss*: a different entity's value for
the same attribute leaking in on embedding similarity. *Falsified if* a
memory arm scores materially below 1.000 — that is confabulation
pressure at the serving layer and is a finding in its own right. The
companion `answer_coverage` must show `nomem` at 0.000; if it does not,
the generator is leaking answers into the questions.

**E5 — D5 (retraction_handling): `cortex` ≈ `hybrid` ≫ `rag` > 0.**
The fact chain renders the correction; a raw turn can only carry one if
contradiction detection stamped `superseded_by_text` on it at store
time. Rag is expected to be **low but non-zero**, and how non-zero is
itself the finding: it measures how often contradiction detection fires
on natural correction phrasing. *Falsified if* rag is 0.000 across the
board, which would say the entry-level retraction mechanism never fires
on this data and D5 is really just re-measuring D2 through the fact
channel.

**E6 — the premise.** "The spine is epistemically better than RAG" is
supported only if **E2 holds and at least one of E3/E5 holds**. D1 alone
is not enough: D1 is retrieval accuracy wearing a different hat, and the
existing benchmarks already say that is a tie. If the spine wins only D1
here, this bench has found nothing new and should say so.

## 6. Falsification of the premise

The premise dies, and this bench should report it dead, under any of:

1. **rag's D2 defect rate is within noise of cortex's.** The spine's
   central claim is that it never serves a superseded value as current.
   If raw turns do not serve stale values either — because retrieval
   reliably prefers the newer near-duplicate turn — then supersession is
   solving a problem the embedding ranking already solved.
2. **D3 and D5 are 1.000 for cortex and 0.000 for rag, and nothing
   else moves.** A dimension only the spine can express is not evidence
   the spine is better; it is evidence the metric was chosen to fit.
   That is why E6 requires E2 (a dimension both arms can score on)
   *and* a marker dimension, never a marker dimension alone.
3. **hybrid does not beat rag on D2.** Hybrid is the shipped agent view
   and a strict superset of rag's turns. If adding the fact spine to the
   raw turns does not reduce the stale-serving defect, the spine is not
   improving what an agent actually sees.
4. **The synthetic and LME slices disagree in direction on D1/D2.**
   Synthetic is an extraction-perfect ceiling; LME is the real dream
   path. A ceiling-only effect is a finding about the representation
   that does not reach the product.

## 7. Known confounds

1. **Extraction is held at perfect on the synthetic source.** Stated in
   §3 and stamped in every artifact. The LME slice is the corrective and
   the only source that can speak about a deployed bank.
2. **The generator writes the phrasings the metrics then match on.** A
   containment metric over text a seeded generator produced is at risk
   of measuring the generator. Mitigations: values are opaque tokens
   (identifiers, times, amounts) never derivable from the question;
   utterance templates are drawn from a pool so a value's phrasing
   varies; filler turns and other entities' values are present as
   distractors so retrieval is not trivially correct. This remains the
   bench's weakest joint, which is why the LME slice exists.
3. **Word-boundary containment is not semantic equality.** A served
   context can paraphrase a value and score as a miss. The generator's
   values are chosen to be non-paraphrasable (`27:12`, `SKU-4417`), and
   the LME derivation *only* admits questions whose gold token appears
   literally in the evidence — which is exactly why 8 of the 56 skips
   are "gold not in later evidence".
4. **D3 depends on the run's wall clock.** Staleness is `now` versus the
   fact's assertion time, so the generator anchors its timeline relative
   to a `now` passed in at run start and records `anchor_epoch` in
   `meta`. Content is seed-deterministic; absolute timestamps are not.
5. **D3 reads the serving policy, not just the flag.** `stale_policy`
   ships as `annotate`, which leaves the record untouched and exposes
   `stale: true`; `demote` adds a warning and `quarantine` replaces the
   value. The predicate accepts all three shapes and the artifact
   records which policy was in force.
6. **`cascade` here is a context proxy** (§2) and not the judged
   cascade.
7. **The bench cannot see what an answerer would do with the context.**
   "The current value is in the served text" is a necessary condition
   for a correct answer, never a sufficient one. Every number here
   bounds an answerer from above and none of them is an accuracy.
8. **Set-valued slots and contenders are out of scope for v1.** A
   contested slot serves `contender_value` beside the canonical one, and
   a set slot has no single supersession chain. Both are real epistemic
   surfaces and neither is scored here; the generator does not produce
   them.

## 8. Results

Filled in after the run. See `evals/README.md` for the smoke table and
`evals/results/epistemic-bench-smoke-20260905.json` for the artifact.

## 9. Amendments

None.
