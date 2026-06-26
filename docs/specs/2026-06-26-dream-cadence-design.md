# Dream cadence — faster post-activity consolidation (quiescence kept) — design

**Date:** 2026-06-26 · **Status:** approved (design), pending plan
**Background:** full-review 2026-06-25 item **P1.4**; fresh-eyes review **F-DREAM**
(`docs/2026-06-21-fresh-eyes-review.md`).

## Problem

The cortex is populated by the daemon's background **dream sweep**, gated by
(`service.py:1813`):

```
would_fire = enabled AND (
    backlog >= min_batch (8)
    OR (backlog >= 1 AND idle >= idle_seconds (1800s = 30 min))
)
```

polled every `sweep_interval_seconds` (600s), **daemon-only** — the embedded
stdio entry point never starts the sweep (`mcp_server.py:_dream_sweep_loop`,
`daemon.py:140`). During an active session, facts stored as *prose* accumulate in
the MIRAS bands but do not become `fact_get`-able until the backlog reaches 8 or
the bank goes idle for 30 minutes. The fresh-eyes review (F-DREAM) flagged this
and noted the cadence is undocumented (the numbers live only in `DreamConfig`
defaults) and is easily misremembered as a turn-based trigger.

## Decision

**Quiescence-gating is correct and stays.** We deliberately do *not* consolidate
while the user is actively storing, for two reasons:

- **Cost.** Each sweep runs *per-entry* LLM extraction on the CPU Gemma sidecar
  (~30 tok/s, ~70s per generation); firing it mid-session competes with the
  user's foreground Claude work on the same box.
- **Correctness (the stronger reason).** Mid-session is exactly when the user is
  changing their mind; consolidating mid-thrash canonicalizes transient state
  that then needs superseding. "Let the episode settle, then distil it."

The remaining gap — prose-stored facts not auto-resolving via `fact_get`
mid-session — is already covered three ways: `memory_search` finds the prose the
whole time, `memory_fact_set` is instant-canonical for known facts, and
`memory_dream_run` forces a full sweep on demand. The only real weakness is that
a 30-minute idle wait *feels* slow after finishing a chunk of work and stepping
away.

## Change

Lower the `DreamConfig.idle_seconds` **default 1800 → 600** (`config.py`).

This is the single lever: the sweep is daemon-only and `_apply_mcp_defaults` does
**not** override the dream cadence (`service.py:305`), so the library default is
authoritative everywhere it matters. Unlike `retention_boost` — which needed a
daemon-only split because it changed *library* band eviction — `idle_seconds` is
inert outside the sweep, so a plain default change is clean and needs no split.

**Effect:** post-quiet consolidation lands ~10 minutes after the last store
instead of 30. It still **never fires while actively storing** (any store resets
`idle`), so there is **zero** added mid-session extractor CPU. `min_batch` (8) and
`sweep_interval_seconds` (600) are unchanged; the `would_fire` logic is untouched.

**Consistency mirrors** (so the Console UI and tests don't drift from the default):

- `web/config_io.py` — the `memory.dream.idle_seconds` knob `default` `1800.0 → 600.0`.
- `web/static/js/views/observatory.js` — the config-not-loaded fallback `?? 1800 → ?? 600`.
- `tests/test_dream.py:95` — the default assertion `idle_seconds == 1800.0 → 600.0`.

## Documentation

- **README "Dreaming" section.** Add the concrete cadence (backlog ≥ 8 **or**
  idle ≥ 600s, polled every 600s, daemon-only, OR-logic), state plainly that
  quiescence is *intentional* and why, correct the "fires every ~N turns"
  mental model (no turn trigger exists, by design), and document the on-demand
  escape hatches: `memory_fact_set` = instant-canonical, `memory_dream_run` =
  force a full sweep now.
- **Global CLAUDE.md** (companion, *outside* this repo — applied only with the
  user's explicit nod): one line in the memory section noting the dream is
  quiescence-gated and pointing at `fact_set` / `memory_dream_run` for "I want it
  canonical now."

## Out of scope

- **Turn-based trigger** — rejected; it would fire mid-active-session, the thing
  the decision above argues against. (Corrects the recollection, not the code.)
- **Episode-end dream trigger** (the rejected approach C) — adds a synchronous
  LLM call to `memory_episode_end`; the 30→10-min idle change already catches the
  same "work just paused" case.
- **Live-daemon redeploy** to adopt 600 — a separate, backup-first daemon rebuild
  run by the user, per the standard ops procedure (never `down -v`).
- **Per-entry extraction cost / batching** — a separate scaling watch-item,
  untouched here.

## Testing

The `would_fire` logic is unchanged, so the only test change is the updated
default assertion (`tests/test_dream.py:95`). The existing trigger tests
(`tests/test_dream.py:273-279`, which set their own explicit `min_batch` /
`idle_seconds`) stay green. Full suite green under the HF-offline env.

## Success criteria (verifiable)

1. `DreamConfig().idle_seconds == 600.0`; the three mirrors (`config_io.py`,
   `observatory.js`, `test_dream.py`) match the new default.
2. `would_fire` semantics are byte-identical — only the default value moved.
3. The README documents the real cadence + rationale + on-demand path, with no
   remaining claim of a turn-based trigger.
4. Full test suite green.
