| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/episodes/rename | 4 | deferred | served | `routes/episodes.rs`; the `episode-curation` scenario, as above, including the missing-`id` 405, the empty-title answer and Python's exception texts for non-string fields; `memory_episode_summary` reads back the restamped resident entries |
