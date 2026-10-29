| Item | Python | Rust | Owner |
|---|---|---|---|
| Canonical-store compaction | removes selected resident records, marks slots dirty and rewrites their rows | the same on the sweep tick, over the cortex, world and lesson stores: the selection policy (Python goldens), the removal and reindex (the cortex's self-healing demotions marked dirty too), and the per-slot sync that deletes the purged rows, under the write lock | closed (B4): `w2e_sweep.py` `sweep-compaction`, mutant `compaction-off` |
