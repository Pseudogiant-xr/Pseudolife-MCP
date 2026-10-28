| Item | Python | Rust | Why |
|---|---|---|---|
| Embedding model | `embedding.model_name` from the Hugging Face cache, torch or ONNX with fallback | existing local/cached ONNX only; complete model-root override in `PSEUDOLIFE_DAEMON_ONNX_DIR`; missing graph refuses; canonical Qwen/MiniLM pools only | verified default Qwen artifact provisioning remains a W3-J cutover blocker; other heads are deferred |
