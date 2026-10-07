# Historical ONNX CPU evidence: corrected summary

This summary supersedes the ranking and tolerance proposals in the
[historical published receipt](rust-onnx-cpu-prerequisite-20d0e75d.json).
That receipt is preserved unchanged for provenance. Its ranking section is
void: query embeddings were ranked against 2,000 random unit vectors from
`evals/rust_baseline/scaling.py`, rather than document embeddings.
Its proposed numerical tolerance is withdrawn; no tolerance is adopted.

The historical direct-ORT CPU comparison reported these fp32 observations:

| Observation | Historical value |
|---|---|
| Embedding shape | 1,025 by 1,024 |
| Maximum absolute embedding difference | 5.401670932769775e-07 |
| Minimum embedding cosine | 0.9999999999933409 |

Both arms shared tokenizer, pooling and normalisation objects. Encoding was
single-text, rather than the production batch of 64 with padding. The query
corpus was proposed; it was not the historical acceptance corpus. Direct ORT
was used because the pinned SentenceTransformers ONNX wrapper omitted required
`position_ids`. The exporter also emitted a hidden-state discrepancy warning.
These observations establish neither daemon search parity nor backend readiness.

The executed source was `20d0e75d907b4025b3901f9def7fdf63af44e4eb`; its instrument SHA256 was
`e65e89dc042b46bde6e168ec0d2164d7165955815f44ccadf48a5bb5351e3771`. The historical published receipt SHA256 is
`8cbf5676089fa186ef31b2ff06af39a65545a712d7fec9b36425ae8c6bcaca71`; it records the separate raw receipt SHA256
`2a7f92871860c78515f7468f53300c7b76763afe01cad41cf513bc2cadef197c`. This summary does not claim to reproduce or inspect
that separate raw payload.

The accompanying instrument is preserved from `c9705e88`, which subsequently
added live embedder stamps. This historical receipt does not bind that later
instrument or a new split candidate. No new run was performed for the split.
Future embedding acceptance requires a real-bank retrieval gate in the Phase 3
brief; this summary introduces no gate, threshold or ranking claim.
