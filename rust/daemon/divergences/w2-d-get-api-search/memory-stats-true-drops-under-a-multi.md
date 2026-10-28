| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_stats` `true_drops` under a multi-band preset | counts evictions out of the last band only (earlier bands relocate) | counts every eviction, as every Rust eviction is a true drop (the multi-band store row below) | the opt-in presets; flat is the default |
