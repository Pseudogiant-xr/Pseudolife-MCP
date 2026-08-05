# Deep-dream judge stage — design proposal

Status: PROPOSED (not implemented). Companion to the 2026-08-05 review-autonomy
changes (analyzer exemptions, junk auto-apply, echo suppression, fold-direction
ranking, relate/batch verdicts), which remove the mechanical share of the
review queue. This spec covers the remaining share: verdicts that need
judgment, currently supplied by a human or an interactive agent session.

## Problem

A deep-dream apply on the 2026-08-05 bank produced ~400 findings. An
interactive triage settled ~85% of them from the evidence the daemon already
attaches (src/dst snippets, scopes, degree, provenance), following a small,
repeatable policy. With the companion changes the queue shrinks substantially,
but merge/link verdicts that need semantic judgment still accumulate between
human visits. PseudoLife is an agent memory system; its maintenance loop
should not require a human reader by default.

## Proposal

Add an optional judge pass to `deep_dream(apply=true)`: after proposals are
filed, the daemon sends each pending finding with its evidence to the
configured extractor endpoint (the same primary/fallback selection
`dream_run_auto` uses) and applies the verdict under guardrails.

### Verdict policy (mirrors the 2026-08-05 triage, in priority order)

1. **Auto-distinct, no LLM call needed** — disjoint non-empty scopes
   (llama-server.exe vs llama-server precedent), or variant-token conflict
   (already enforced at proposal time).
2. **Auto-merge, no LLM call needed** — naming-layer variants: same
   normalized name modulo path prefix (`tests/test_x.py` ~ `test_x.py`),
   or alias-resolution to the same node. Fold thin into evidence-bearing.
3. **LLM verdict** for the rest, one finding per call, forced choice:
   `merge | distinct | relate(relation) | unsure`. The prompt carries the
   snippets, scopes, degree, and fact counts for both sides, plus the
   confirmed-distinct precedents (postgres vs postgres.py class).
4. **Apply rules**: `merge` → accept with decided_by="dream-judge" (graph
   snapshot from the same apply is the undo); `distinct` → reject +
   dismiss_pair; `relate` → typed edge + dismiss_pair; `unsure` → leave
   pending. Malformed output → leave pending, never retry into a loop.

### Human tripwire (retained deliberately)

A merge between two entities that BOTH carry evidence above a threshold
(degree + facts >= `judge_max_auto_evidence`, default 4) is never
auto-applied — it stays pending for Atlas regardless of the verdict. This is
the postgres-vs-postgres.py class where a wrong fold is expensive and the
2026-08-05 audit showed name similarity misleads.

### Config

`memory.deep_dream.judge` (default off until the eval below passes):
`enabled`, `max_verdicts_per_pass` (default 50), `judge_max_auto_evidence`
(default 4), `min_confidence` on the extractor's self-reported certainty.

### Failure containment

Judge calls are best-effort like dream relation extraction: any exception
leaves the queue exactly as filed. The pass runs after the snapshot, so every
auto-applied verdict is covered by the same undo artifact. Per-pass verdict
cap bounds cost and blast radius.

## Evaluation gate (pre-registered, required before default-on)

Replay corpus: the 2026-08-05 triage produced ~400 human/agent-settled
verdicts (accepted/rejected proposals with decided_by, dismissed pairs,
recent_merge_decisions). Run the judge policy offline against those findings
and score agreement:

- Gate A: >= 95% agreement on merges it chooses to auto-apply (false-merge
  is the expensive error).
- Gate B: >= 85% agreement on distinct verdicts.
- Gate C: 0 auto-merges in the tripwire class.
- Report abstention rate; > 50% unsure means the policy adds calls without
  clearing the queue and fails the gate.

The replay needs no GPU for rules 1-2 (pure functions); rule-3 scoring runs
on the reproducible bench server per the eval discipline in CLAUDE.md.

## Out of scope

- Judging lesson/world slot duplicates (the analyzer exemptions removed the
  bulk; the remainder is the key-mint-drift repair loop, a separate design).
- Contested-fact resolution beyond the echo rule (machine-verifiable slots
  such as deployed versions should get verifier hooks, not LLM verdicts).
- Any change to the extraction prompt (ladder-gated separately).
