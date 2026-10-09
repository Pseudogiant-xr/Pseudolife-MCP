# Offline embedding contract

Python is the oracle: `memory/embedding.py:162-266` resolves a configured
artifact without downloading; `onnx_artifacts.py` validates each Transformer
module and permits only same-repository Hub blob leaf links. Local file links,
linked directories, escaping paths and unknown modules are refused.

`EmbeddingPipeline.__init__` validates backend and CPU dtype (environment
override first), caps the native model sequence length, and reports ONNX's
artifact precision as null. ONNX ignores the requested torch CPU dtype.
The Rust runtime loads an existing graph with the CPU provider only, with no
exporter, torch fallback or network client. Missing artifacts are a named
divergence: Python falls back to torch; Rust refuses.

`encode:406-482` preserves input order and duplicates, forwards only cache
misses, batches by config, returns float32 rows, and copies returned storage.
The LRU key is `(unmodified text, normalize)`; negative cache size disables it.
`encode_query:488-507` prefixes before this same cache path. Tokenization strips
text, respects model lowercasing, truncation direction and native cap, and pads
within each batch. Pooling follows the model's Pooling module; Normalize modules
apply even when the caller disables optional normalization.

This slice implements the shipped Qwen last-token/1024 and MiniLM mean/384
layouts, including optional Normalize. Other pooling modes, Dense heads,
multiple Transformers, unsupported graph inputs and non-float32 hidden states
refuse explicitly. These are named deferrals, not guessed postprocessing.
Saved default SentenceTransformer prompts and named-user `~user` model paths
also refuse explicitly; current-user `~/` model paths and the standard
`XDG_CACHE_HOME` Hub location are supported.
Model dimension is checked against metadata and hydrated vector widths before
using vectors. The storage schema guard remains unchanged (1024); a configured
MiniLM model cannot consume a populated Qwen bank. The embedding boundary writes
no database state; existing hydration reconciliation retains its own checks.

Comparison is exact by default. The harness records max absolute/ULP error,
cosine floor, token IDs/masks, graph/tokenizer/metadata SHA-256 identities,
runtime versions and full/top-eight retrieval order. A proposed tolerance is
diagnostic until the delegate accepts it; rank arrays retain exact comparison.
Corpus and results never imply bit identity outside their recorded inputs.

Delegate ruling (2026-10-10): for the identified fp32 Qwen export and MiniLM
artifact, max absolute difference is at most `1e-6` and cosine at least
`0.99999999997`; token IDs, masks, cache semantics and full/top-eight order are
exact. The bounds cover the recorded Windows/Linux stacks: ST 5.3.0,
transformers 4.57.6, ORT 1.27.0; torch 2.10.0 Windows / 2.13.0 Linux. ULP
differences near zero are reported, not given a separate tolerance.

ONNX-PREREQUISITE remains deferred: the default installation has no verified
supplied Qwen graph. W3-J owns provisioning and reconciliation with the existing
verified export; the stock Python ONNX wrapper's missing-position-IDs failure is
a Python-side finding. This implementation remains a cutover blocker until
that runtime artifact is provisioned. CPU ONNX does not implement torch's
automatic bf16 policy; it validates the dtype request and uses graph precision.
