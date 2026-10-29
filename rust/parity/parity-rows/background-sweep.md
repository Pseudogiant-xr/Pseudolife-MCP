| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| BACKGROUND-SWEEP | 4 | `mcp_server.start_dream_sweep` starts once when dream OR retrieval logging is enabled; sweep drives superseded compaction, dream-run journal pruning and retrieval-log pruning even with dreams off; automatic dream remains backlog/quiescence gated. `memory/dream.py:run_sweep_once` | `test_dream.py`, `test_compaction.py`, `test_dream_runs.py`, `test_retrieval_log.py` I/DB; daemon wire/restart equivalents pending | deferred |
