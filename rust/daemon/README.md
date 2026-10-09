# pseudolife-daemon (contract-first port)

The Rust port of the Pseudolife memory daemon, built contract-first: each
slice writes a one-page spec of the exact Python behaviour it must match
(`spec.md`, with `file:line` sources), then proves it with a live
differential harness against the Python daemon on a disposable bank
(`harness/`), a mutant control, and two independent reviews.

This crate starts as the 2026-10-09 spike (`spike/rust-daemon-contract-first`):
`GET /health`, the bearer gate (env tokens and stored principals), read-only
hydration of a schema-55 bank, ONNX query embedding, and `GET /api/search`
with the slot pool and BM25. It is not a release artifact and nothing
installs or runs it yet. The spike's declared divergences still apply until
the slices that close them land (see the spike report's section 7).

## Running it beside the Python daemon

The daemon reads `PSEUDOLIFE_MCP_DATABASE_URL` (a disposable bank only),
`PSEUDOLIFE_SPIKE_MODEL_DIR` (a directory holding the verified fp32 ONNX
export: `model.onnx`, `model.onnx_data`, `tokenizer.json`), and the usual
`PSEUDOLIFE_MCP_HOST`, `PSEUDOLIFE_MCP_PORT`, `PSEUDOLIFE_MCP_TOKEN` and
`PSEUDOLIFE_MCP_TOKENS`. ONNX Runtime is loaded dynamically, so set
`ORT_DYLIB_PATH` to the runtime library.

`harness/compare.py <python-port> <rust-port> <tokens.env> <out.json>` runs
the contract cases and the ranking comparison against both daemons and
exits non-zero on any diff.
