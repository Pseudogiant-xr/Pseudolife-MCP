### Added (2026-10-09 — Rust daemon: the rest of GET /api/search)

- The contract-first Rust daemon (`rust/daemon/`) now serves every search
  channel the Python daemon does except the Chroma reference pool: the
  cortex facts block, chronicle events (temporal, aggregation and date
  cues), the timeline channel, contiguity neighbours, the cross-encoder
  rerank (an ONNX export of `cross-encoder/ms-marco-MiniLM-L-6-v2`), RRF
  fusion, the candidate-pool multiplier, depth recency and the query
  embedding cache. Search writes the same telemetry: one `retrieval_events`
  row per search (served list, fusion components and knob snapshot), the
  served facts, slot reads and resident access counts, and the startup
  warmup probe.
- Proven by `rust/daemon/harness/w2d_search.py`, a live differential run
  against the Python daemon on disposable banks that compares responses by
  value and both banks' state after every scenario, with a mutant control;
  declared divergences are listed in `rust/daemon/divergences.md`.
