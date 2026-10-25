### Added (2026-10-25 — the Rust daemon serves memory_recall's llm driver)
- With `memory.recall.driver: llm` (or `PSEUDOLIFE_RECALL_DRIVER=llm`), the
  Rust daemon refused every non-blank `memory_recall` as `not_implemented`.
  It now asks the dream's extraction model which known entities the question
  names, as the Python daemon does: the same prompt and request bytes to
  `<extractor base>/chat/completions` (the base URL, model and key from the
  `PSEUDOLIFE_DREAM_*` variables or `memory.dream`), the bearer key only when
  one is set, a redirect refused rather than followed, and any failure taken
  as no seeds (`low_confidence`). The returned names seed the walk when they
  are in the entity vocabulary; the expansion and re-queries are unchanged.
  The differential harness's `recall-llm` scenario compares both daemons'
  answers and outbound requests against a stub model.
