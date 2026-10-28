| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Cross-encoder model | `memory.reranker.model_name` from the Hugging Face cache, lazily | the ONNX export in `PSEUDOLIFE_DAEMON_RERANK_DIR` (same checkpoint; `model_name` is only logged) | model packaging is cutover work (W3-J); parity of the export is proven by `harness/w2d_rerank_check.py` |
