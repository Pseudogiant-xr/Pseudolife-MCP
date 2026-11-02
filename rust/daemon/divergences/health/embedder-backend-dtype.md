| Key | Python | Rust | Owner | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| `embedder.backend`/`dtype` | `torch` with a dtype, or `onnx` with null | `onnx`, null | backend choice, not a contract | presentation | `rust/daemon/src/service.rs::Service::init` reports ONNX/null; `pseudolife_memory/daemon.py::_build_health_payload` reports the loaded embedder's backend/dtype. This bucket covers those truthful diagnostic labels only; precision/model-result differences are separate startup entries. B14b owner: B14b presentation disposition. |
