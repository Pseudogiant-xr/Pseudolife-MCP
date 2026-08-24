# Session-digest layer (Phase 2) — design (2026-08-24)

A memory bank built from raw turns and atomic facts has nothing in between:
questions that span a whole session's arc ("summarize how the project
progressed") retrieve a handful of turns and miss the rest. This design adds
a mid-density layer — one narrative digest per closed session, generated
during the dream pass and stored as a retrievable entry — so arc-shaped
questions can be answered from a few digests instead of dozens of turns.

Motivating evidence (all committed on `feat/beam-reader-sweep`):

- BEAM p1-b16 (`beam-100K-qwen-27b-p1-b16.summary.json`, n=400, budgets
  matched at 16/16): summarization is the floor — rag 0.4147 / hybrid
  0.3823 — and it is the one type the Phase-1 fixes did not move.
- The reader×volume grid (`beam-readersweep-opus-sweep.summary.json`,
  `types` block) shows no budget fixes it either: a frontier reader at
  6/16/48 turns scores 0.3833 → 0.3692 → 0.4700 on summarization while
  knowledge_update goes 0.75 → 0.90 → 1.00. The failure is coverage
  structure, not reader quality or context volume: summarization rubrics
  are 5–6 "should contain" items spanning months of sessions, and any
  affordable turn budget catches some phases and misses others.
- The competing system whose BEAM report prompted this work retrieves
  ~half of its passages from "session learning" documents — mid-density
  narrative digests (~400 chars) written back into the store at ingest —
  rather than from the transcript itself.
- Cortex extraction is not the fix for this type: atomic `(entity,
  attribute, value)` claims are the wrong granularity for narrative-arc
  rubric items ("early development focused on…", "you then tackled…").

This is a product-real layer, not benchmark tuning: the daemon's own
episodes are the natural digest scope, and `session_briefing`'s recap of
the last closed session currently reports `{title, entry_count}` with no
content — a digest fills that too.

## Decisions

1. **A digest is one prose entry per closed session root.** Generated at
   consolidation time from the episode's full entry stream, 600–900 chars,
   headed with ordering metadata the retriever gets for free:
   `Session digest: <title> (<YYYY-MM-DD>–<YYYY-MM-DD>, N entries)`.
   Content contract (prompt-enforced, matched to the observed failure
   shape): the session's goal, the phases/steps in order, key decisions
   with their reasons, problems hit and how they were resolved, and stated
   preferences/changes of direction. Strictly extractive-narrative: only
   events present in the entry stream, dates kept explicit, phrased in
   past tense anchored to the session ("in this session…") so an old
   digest cannot masquerade as a current value.

2. **Generation is a new dream stage mirroring `infer_outcomes_stage`**
   (`service.py:2673` — the structurally closest precedent: per-closed-
   episode, whole-episode context, extractor call inside the dream pass,
   meta-backed cursor so each episode is processed once).
   - `generate_digests_stage(extractor)` runs in `_dream_run_locked` in
     the same idle-cycle slot as outcome inference and lesson synthesis
     (fact consolidation keeps priority).
   - Candidate selection mirrors `_pending_inference_candidates`
     (`service.py:4745`): closed session roots with ≥1 subtree entry past
     a `session_digest_cursor` meta key, capped per cycle.
   - Context rendering reuses `_episode_inference_context`
     (`service.py:4727`) — including status-source entries; a digest
     should reflect what happened, and `dream.exclude_sources` protects
     fact extraction, not narrative (same rationale as spec 2026-07-18,
     decision 2).
   - New `OpenAICompatExtractor.summarize_session(context_text)` in
     `dream.py`, same request shape as `infer_outcomes` (json_object,
     temperature 0, thinking off), returning `{"digest": str}`; raises
     `ExtractorError` on transport failure so the cursor does not advance
     past an unprocessed episode.

3. **Context length is capped, unlike outcome inference.**
   `_episode_inference_context` joins the whole subtree uncapped; a long
   session will not fit the CPU sidecar. New config
   `memory.dream.digest_context_chars` (default 24000). Over budget, the
   stream is split into sequential segments ≤ the cap, each segment is
   digested, and the segment digests are merged by one final
   `summarize_session` call over their concatenation (map-reduce, order
   preserved). No middle-truncation: the failure this layer fixes is
   exactly "the middle of the arc went missing".

4. **Digests are band entries, not a new table.** Stored via the normal
   CMS path with `source="digest"` and `tags=["digest"]` — no new column,
   no schema bump. The alternative (a separate table with its own
   cue-gated channel, the `chronicle_events` precedent) is deliberately
   rejected: the mechanism of the win is that digests *compete in the
   main dense retrieval* against raw turns, where their density earns
   them slots on arc-shaped queries. `MemoryEntry` has no kind field;
   the source convention is the existing way eval and production code
   distinguish entry populations (`source="beam"`), and `search(sources=)`
   already filters on it.

5. **Attribution stamps the summarized episode, not the current one.** A
   digest written through `service.store()` would be stamped with
   whatever episode is open at dream time. Instead an internal write path
   sets `episode_id`/`episode_title` to the summarized root. Consequences
   that fall out correctly: `_retitle_locked` rewrites the digest's
   denormalized title alongside the episode's other entries, and
   `episode_merge` re-stamps digests with the other source entries (two
   digests on one merged episode is accepted for v1 — merge is a rare
   admin operation, and both digests remain true of their original
   scopes).

6. **Ships default-off.** `memory.dream.digest_enabled: false` until the
   BEAM measurement (below) passes — the known-facts-window precedent:
   dream-path features land inert and are enabled by a measured verdict,
   not by hope. The bench harness enables it explicitly.

7. **`session_briefing` recap gains the digest.** `_fmt_recap`
   (`briefing.py`) renders the last closed session's digest text when one
   exists, replacing the bare `{title, entry_count}` — the product-side
   payoff, independent of BEAM.

## Known risks, and how the eval catches each

- **Abstention regression.** p1-b16 measured abstention *degrading* with
  context volume (0.725 rag at 16 turns vs 0.80 at 6; opus sweep 0.625 →
  0.50): synthesized coverage invites answering where "I don't know" is
  correct. The digest prompt mandates omission over inference, and the
  BEAM gate below reads abstention per-type explicitly.
- **Stale leak.** A digest of an early session truthfully states values
  that were later superseded; dense retrieval may surface it on a
  current-value question. Mitigations: past-tense session-anchored
  phrasing with explicit dates (decision 1), and the ladder's
  `stale_leak` metric re-run as the hard gate (this is a dream-write-path
  change: per project convention the regression gate does not cover it —
  `evals/ladder_sweep.py` and the BEAM/LME benches do).
- **Retrieval crowding.** Digests competing in dense retrieval can
  displace raw turns on non-arc questions. The BEAM run reads all ten
  per-type scores, not just summarization; information_extraction and
  knowledge_update are the crowding canaries.
- **Bad digests are silent.** A hallucinated or vacuous digest produces
  no error, only wrong retrievals later. v1 keeps generation observable:
  digest writes are tallied in the dream result dict (`digests: n`) and
  the entries are inspectable via `search(sources=["digest"])`.

## Eval plan (GPU; runs only on an explicit go)

BEAM adapter changes (eval-side only): open/close one episode per BEAM
session batch so digest scope matches the benchmark's session structure;
drain dreams as today. Arms, budget-matched per the house rule:

- `rag` control: `sources=` filter excludes digests — byte-identical to
  the p1-b16 protocol, so the control chains back to every prior run.
- `hybrid` as today (digests excluded) — the no-digest comparator.
- `hybrid_digest`: hybrid retrieval with digests eligible, same total
  context budget (digest chars count against the turn budget — coverage
  must pay for itself, not ride on a bigger window).

Success criteria, all from one n=400 paired run against p1-b16 rows:
summarization ≥ 0.50 on `hybrid_digest` (from 0.3823; the frontier-reader
ceiling at 48 turns was 0.47), multi_session_reasoning not below hybrid,
abstention within noise of hybrid (the control arm's measured spread
bounds "noise"), no other type down by more than the control spread, and
overall `hybrid_digest` ≥ hybrid. Ladder re-run: `gold_recoverable` and
`stale_leak` within their existing rung tolerances. Any miss → the
feature stays default-off and the verdict artifact says why.

## Test plan (TDD, watched RED, no GPU)

- Stage generates exactly one digest per closed root and advances the
  cursor (second dream cycle writes zero) — cursor persistence via meta.
- Digest entry carries the summarized episode's id/title, `source=
  "digest"`, and the header format; `search(sources=["digest"])` finds
  it, `sources=` exclusion hides it.
- Over-budget context is segmented and merged, order preserved (fixture
  with a subtree exceeding `digest_context_chars`).
- `ExtractorError` mid-stage leaves the cursor unadvanced (retry next
  cycle) — the dream-stall lesson.
- Retitle rewrites the digest's denormalized title; merge re-stamps it.
- `digest_enabled=false` (default) generates nothing; briefing renders
  the digest when present and degrades to the bare recap when absent.
- Disable-the-hook RED check for the cursor guard: with the cursor
  advance removed, the same episode digests twice and the test goes red.

## Not changed

- Answer prompts (BEAM and LME), `RAG_TOP_K`/`HYBRID_TOP_K` defaults, and
  the regression-gate baseline — untouched so every existing comparison
  stays valid.
- Cortex extraction, quarantine, supersession, HLC — the digest stage
  writes band entries only, never slots.
- Schema: no DDL, no version bump (digests are ordinary entries).
- Extraction-coverage work (the other half of the recovered Phase-2 plan:
  ~76 claims extracted from a 150K-token chat; cortex answering "I don't
  know" 38/40 on event_ordering) is a separate design — different
  mechanism, different eval, deliberately not bundled here.

## Open questions — resolved at review (user, 2026-08-24)

1. Digest length: **ship the knob.** `memory.dream.digest_target_chars`
   (default 800), passed into the prompt as the length target.
2. Backfill: **in v1.** The cursor starts at zero, so enabling the
   feature digests every historical closed session progressively — the
   per-cycle candidate cap keeps each dream pass bounded, and the cursor
   makes progress durable. No separate backfill mechanism needed.
3. Production model: **gate production enablement on a sidecar quality
   spot-check.** `evals/digest_sidecar_probe.py` generates digests for
   sample episodes against the configured extractor endpoint and writes
   an artifact for human review; the feature stays default-off until
   that review (and the BEAM measurement) both pass.
