| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The member dedup cosine at exactly 0.9 | torch float32 `dot` and `norm` (BLAS / vectorized reduction order) | float32 products summed in f64, rounded once to f32 | within one f32 step of torch (measured on 2,000 unit 1024-vectors: at most 4.5e-8 apart); a cosine that close to 0.9 may decide `member_confirmed` against `member_added` differently, as may the ONNX/torch embedding gap (about 1e-6) itself; the harness members sit at least 0.08 from the line |
