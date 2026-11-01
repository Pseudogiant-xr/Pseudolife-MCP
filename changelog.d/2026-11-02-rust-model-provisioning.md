### Added (2026-11-02 — Provision Rust daemon models into persistent storage)
- The opt-in Rust daemon installs pinned fp32 Qwen embedding and MiniLM reranker artifacts with `pseudolife-daemon provision models`; container first start provisions the persistent model cache instead of baking model weights into the image.
- Model loading checks the compiled checksums, including tokenizer and model metadata, and refuses missing or changed artifacts with a provisioning or repair instruction.
