### Added (2026-10-10 — Rust offline document and query embedding)
- The Rust daemon loads existing validated ONNX artifacts on CPU, preserves
  query prefixes, model token limits, pooling, batching and the embedding LRU,
  and refuses mismatched dimensions. No model download or export is performed.
- A differential embedding row records float32 differences and exact ranking
  agreement; a generated offline fixture checks cache behavior and six mutants.
  Verified Qwen artifact provisioning remains a cutover prerequisite.
