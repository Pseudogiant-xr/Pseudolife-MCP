### Added (2026-11-02 — Provision Rust daemon models into persistent storage)
- The opt-in Rust daemon installs pinned fp32 Qwen embedding and MiniLM reranker artifacts with `pseudolife-daemon provision models`; container first start provisions the persistent model cache instead of baking model weights into the image.
- Model loading checks the compiled checksums, including tokenizer and model metadata, and refuses missing or changed artifacts with a provisioning or repair instruction.
- A missing or changed optional reranker is disabled with its cause logged; embedding-backed store and search remain available. Explicitly configured custom embedding models retain their loading contract, and local model overrides do not trigger automatic downloads.
