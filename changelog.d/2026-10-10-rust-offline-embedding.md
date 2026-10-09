### Added (2026-10-10 — Rust offline document and query embedding)
- The Rust daemon loads existing validated ONNX artifacts on CPU, preserves
  query prefixes, model token limits, pooling, batching and the embedding LRU,
  and refuses mismatched dimensions. No model download or export is performed.
- A differential embedding row records float32 differences and exact ranking
  agreement; generated offline fixtures check padding, pooling, cache behavior
  and eight mutants. Explicit bf16 refuses; automatic precision uses fp32.
  Verified Qwen artifact provisioning remains a cutover prerequisite.
