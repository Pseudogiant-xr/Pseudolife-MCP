| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/episodes/merge | 4 | deferred | served | `routes/episodes.rs`; the `episode-curation` scenario, as above: a new rollup, a merge into an existing episode, the outcome-signal and entry retarget, open and unknown sources, and the refusals; a probe compares the exact spans of seeded episodes (the bank comparison normalizes episode clocks) and `memory_episode_summary` reads back the resident entries. Declared: a non-string `hint` on a new rollup (`daemon/divergences.md`) |
