| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/episodes/prune | 4 | deferred | served | `routes/episodes.rs`; `daemon/harness/w2e.py live --only episode-curation` (`w2e_episodes.py`) compares status and body and the normalized bank after each call, plus an unkeyed store after the prune for where the current episode points |
