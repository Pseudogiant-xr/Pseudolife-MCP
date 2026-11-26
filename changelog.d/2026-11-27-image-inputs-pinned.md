### Changed (2026-11-27 — the extractor and Rust daemon images build from pinned, hash-checked inputs)
- `ops/Dockerfile.extractor` pins both base images by digest
  (`curlimages/curl:8.23.0`, `ghcr.io/ggml-org/llama.cpp:server-b12767`) and
  the default GGUF by Hugging Face revision, and checks the download against
  `MODEL_SHA256`. Baking a different model (the documented Gemma 4 E2B QAT
  alternative included) now takes `--build-arg MODEL_SHA256=<sha256>` beside
  `MODEL_URL`; a mismatch stops the build and names that argument.
- `ops/Dockerfile.daemon-rust` no longer has a Python stage: it downloads the
  ONNX Runtime 1.27.0 wheel for the build platform (amd64 or arm64), checks
  its PyPI SHA-256 and unzips the native library. Other platforms stop with
  their name. The unused `CUDA_VISIBLE_DEVICES`, `HF_HUB_OFFLINE` and
  `TRANSFORMERS_OFFLINE` variables are gone from the image.
