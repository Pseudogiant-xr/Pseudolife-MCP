| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `entries.access_count` persistence | resident counts written on the save cadence | resident counts bulk-synchronized on the same gated autosave and exit-save cadence | closed (B4): live `search-after-restart` and `counters-restore` |
