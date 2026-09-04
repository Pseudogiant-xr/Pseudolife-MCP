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

**Qualifying count: 23 of the 78 knowledge-update questions**, identical
on `longmemeval_oracle.json` and `longmemeval_s_cleaned.json` (the
derivation reads only the evidence sessions, which the two files share).
The 55 skips break down as: 39 gold answers carrying no value token at
all (prose answers — "the Nikon D850", "her sister's wedding"), 7 where
the gold token is not literally in the later evidence turns (the gold is
a paraphrase), 8 ambiguous old-value candidates, 1 where the gold token
also appears in the earlier session. Nothing is guessed to rescue a
skip; a question that does not derive cleanly is not in the slice.
(Count corrected from 22 by amendment A1 — see section 9.)

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
(Implemented by amendment **A5**, section 9, which also refuses the whole
run rather than individual arms when no endpoint answers, and adds a
no-LLM `floor` rung so the bank lifecycle can be checked on CPU.)

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

## 8. Results — 2026-09-05 synthetic smoke

Two cells, both `--source synthetic --contexts-only`, seed 20260905, on a
private bench Postgres created and dropped by the run. Artifacts:
`evals/results/epistemic-bench-smoke-20260905.json` (10 entities × 5
attributes × 4 sessions — 50 questions, 72 turns, 40 cortex slots) and
`epistemic-bench-scale-20260905.json` (10 × 10 × 6 — 100 questions, 156
turns, 80 slots). Rows, including every served context, in the sibling
`.jsonl`s.

| dimension | rag | cortex | hybrid | cascade | nomem | n |
|-----------|-----|--------|--------|---------|-------|---|
| `update_following` ↑ | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 20 |
| `stale_serving` ↓ | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 20 |
| `staleness_marking` ↑ | 0.000 | 1.000 | 1.000 | 1.000 | 0.000 | 10 |
| `abstention_support` ↑ | 0.700 | 0.000 | 0.000 | 0.000 | 1.000 | 10 |
| `retraction_handling` ↑ | 0.600 | 1.000 | 1.000 | 1.000 | 0.000 | 10 |
| `answer_coverage` | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 40 |
| context chars (mean) | 373.1 | 1206.5 | 1613.7 | 1206.5 | 0.0 | |

The scale cell moves only `abstention_support` (rag 0.700 → 0.750) and
`retraction_handling` (rag 0.600 → 0.400); every other cell is identical
at double the corpus.

**Verdict against the preregistered expectations: the premise is NOT
supported by this evidence, and E6 is the reason.**

- **E1 held, uselessly.** Every memory arm scores 1.000 on
  `update_following`, rag included. A slot-shaped utterance ("the engine
  for ledger-db is ENG-2200") is a lexical key that the turn pool's BM25
  channel resolves exactly, so the synthetic corpus cannot make retrieval
  hard. D1 is saturated here and says nothing.
- **E2 FAILED, and it is the load-bearing failure.** `stale_serving` is
  0.000 on every arm at both scales. Cause, read off the rows: rag serves
  the old value **and** the current one on every changed slot in both
  cells (20 of 20 in the smoke, 40 of 40 at scale). The
  defect the spine claims to prevent cannot occur when retrieval hands
  the agent both values. That is falsification criterion 1 of section 6,
  and by E6 it means this bench has not shown the spine to be
  epistemically better — it has shown that on a corpus this size the
  question does not arise.
- **E3 held exactly as predicted.** `staleness_marking` 1.000 for
  cortex/hybrid and 0.000 for rag, structurally.
- **E4 FAILED in an unexpected direction.** The fact spine is *worse* at
  abstention support than raw turns: cortex 0.000 against rag 0.700.
  `cortex_search` at the shipped `top_k=24 / min_score=0.2` returns
  near-miss facts for every unstated slot (another entity's value on the
  same attribute), on all 10 of 10 questions. `answer_coverage` behaves
  as the pairing requires — nomem 0.000 against its 1.000 abstention.
  This is confounded with context width (amendment A3) and needs a
  width-matched rerun before it is quotable as a defect.
- **E5 held in shape and gave the finding it was written to give.**
  cortex/hybrid 1.000, rag 0.600 (0.400 at scale) — non-zero, so
  contradiction detection does fire on natural correction phrasing and
  stamps `superseded_by_text` on the entry that stated the retracted
  value. Rag can carry a retraction; it carries it about half the time.

**What this bench is now good for, and what it is not.** D3 and D5
discriminate cleanly and are the two dimensions where the spine's
representation demonstrably reaches the served context. D4 discriminates
and currently points *against* the spine. D1 and D2 are saturated on the
synthetic source and can only be answered by the LongMemEval slice, where
the value is buried in prose across a real haystack — which is exactly
the risk section 7's confound 2 named, now measured rather than
predicted. Running that slice is the next step and needs an extractor
endpoint.

**Serving observation, worth its own line.** No arm renders the stale
flag into the flattened context string. `staleness_marking` scores the
served *payload*, so a stale value reaches an agent reading the MCP
response marked, and an answerer reading the context block unmarked.
That is a real gap in the serving path, not an artefact of the metric.

## 9. Amendments

- **A1 (2026-09-05) — LongMemEval qualifying count 22 → 23.** The first
  implementation reused `ladder_sweep.value_present`, whose word-boundary
  rule excludes any adjacent `.` and therefore rejects a value a sentence
  ends on. The bench now uses a matcher that blocks a period only when it
  continues a number (`1.5` searched for `1`). One knowledge-update
  question qualified once its gold token stopped being rejected for
  ending a sentence, moving `gold-not-in-later-evidence` from 8 to 7.
  Same fix, same run: the first synthetic smoke had scored the rag arm at
  0.150 `answer_coverage` on contexts that plainly carried the value.
- **A2 (2026-09-05) — a second, larger cell was added after reading the
  first.** Post-hoc and declared as such. It was not run to find a
  different answer but to ask whether `stale_serving`'s 0.000 was a
  property of the corpus size; it is not (identical at double the
  corpus). Both cells are reported together and neither is promoted over
  the other.
- **A3 (2026-09-05) — a ninth confound, found by the run.**
  `abstention_support` is confounded with served context width: an arm
  serving 1,206 characters has more chances to sweep in a near-miss value
  than one serving 373. The artifact now records `context_chars_mean`
  per arm beside every rate, and the E4 result must not be quoted as a
  spine defect until a width-matched cortex arm has been run.
- **A4 (2026-09-05) — rows persist the full served context.** The A1 bug
  had to be diagnosed by rebuilding the bank, because the rows carried
  only character counts. Every row now carries `{arm}_context`.
- **A5 (2026-09-05) — the LongMemEval source path is implemented.**
  `--source lme` no longer refuses. Per qualifying question it builds a
  fresh bench bank through `longmemeval_bench.ingest_and_dream` — the
  same per-question bank lifecycle `run_extract` uses, imported rather
  than re-derived — and then serves the identical
  `build_contexts` / `serve_comparator_arms` arms the synthetic path
  serves. `--extractor` (default `qwen-27b`) names the rung from
  `longmemeval_bench.EXTRACTORS`; `--extractor floor` is the no-LLM regex
  extractor, which needs no endpoint and exists so the bank lifecycle can
  be smoke-tested on CPU. `--limit N` scores the first N derived
  questions, counted over the SLICE rather than over the pending set, so
  a resumed run stays on the same slice.

  Two properties this source needs and the synthetic one does not:

  - **Resumable per question.** It shares the GPU with judged work, so a
    row is appended to `epistemic-bench-lme-<tag>.jsonl` as each question
    finishes and a restart skips the ids already there. The summary is
    totalled from the ROWS, not from live serving, so questions scored by
    an earlier process still count. The refuse-overwrite guard is
    correspondingly weaker than the synthetic path's: the summary JSON
    still blocks a rerun (it is written only once the whole slice is
    scored), but an orphaned rows file is the resume point and does not.
  - **The endpoint is probed before anything costs.** A dead extractor
    must not buy a database, a dataset load, or a partial artifact.

  **Which dimensions are gradable, and which are not.** Unchanged from
  section 4 and now enforced by the code rather than asserted in prose:

  | | dimension | on the LME slice |
  |-|-----------|------------------|
  | D1 | `update_following` | graded — the derived new value in the served text |
  | D2 | `stale_serving` | graded — the derived old value served with the new one absent |
  | D3 | `staleness_marking` | **n/a**, `n: 0` — no `freshness_class`, no TTL in the dataset |
  | D4 | `abstention_support` | **n/a**, `n: 0` — no never-stated questions in the knowledge-update type |
  | D5 | `retraction_handling` | graded, but through the ENTRY channel only |
  | — | `answer_coverage` | graded |

  D5's restriction is the one worth stating plainly. The bench slot is
  synthetic (`lme:<question_id>` / `value`) because LongMemEval carries no
  entity/attribute structure, so a question never matches a served fact
  by name and the fact channel — the supersession chain — cannot fire on
  this source. What remains is the served text plus a served entry's
  `superseded_by_text`. The `cortex` arm serves no entries, so **its D5 is
  0 by construction**: a measurement artefact, not a finding. Every
  artifact says so in `caveats.d5_is_the_entry_channel_only`, alongside
  `caveats.extraction_is_the_real_path` — the mirror image of the
  synthetic source's perfect-extraction ceiling, since here a low cortex
  number is a claim about the extractor at least as much as about the
  spine.

  Artifacts: `evals/results/epistemic-bench-lme-<tag>.json[l]`, with
  `meta` recording the extractor and its URL, the derivation file and the
  git rev it was derived at, the dataset, the counts, the serving widths
  against per-question bank size, and `longmemeval_bench.bench_env_knobs()`.
