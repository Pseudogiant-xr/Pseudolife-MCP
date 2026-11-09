---
name: derived-state
description: Use when adding or changing an index, cache, memoized score or any other derived structure over mutable state (an index over band entries, a cached graph view). Covers enumerating mutation paths, stating the real read/write interleave, and preserving what the replaced code provided implicitly.
---

# Derived state, caches and indexes

A derived structure over mutable state goes stale by whichever mutation path
the author did not think of, and the daemon's real workload (store, search,
store, search) punishes the wrong maintenance policy. Before adding or
changing one:

1. **Enumerate every mutation path first**, grepping for the state, not the
   API: `hydrate_cms` / `load()` / legacy migration append to `band.entries`
   without `store()`.
2. **State the real read/write interleave and check the maintenance policy
   keeps the win under it.** The daemon alternates store and search, so
   extend in place for additions and rebuild only on removals or
   replacement; invalidate-on-write rebuilds on every read. Measure the
   policy under the actual interleave, not a synthetic one.
3. **List what the replaced code provided implicitly** (tie-break iteration
   order; `bands=` filtering on the holding band, not the stale
   `entry.bank`; live reads of supersession flags) and preserve each, or
   change it consciously and say so in the commit.
4. **Get an independent review before commit** (`/code-review` medium or a
   reviewer subagent), as `CLAUDE.md` "Review discipline" requires for perf,
   cache and index changes.
