| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `w2d-tokenizer-runtime` | Other SentenceTransformers tokenization semantics | The inherited embedder follows the ST 5.3.0 / transformers 4.57.6 contract in `spec-embedding.md`. ST 5.7.0 changes padded-query cortex scores; supporting that runtime is deferred rather than changing the oracle output or score tolerance. Evidence records the actual runtime versions. |
