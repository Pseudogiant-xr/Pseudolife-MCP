| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The cosine `_cosine` (`consolidation.py:81-92`) | torch float32: `torch.dot` (BLAS order) over float32 norms | each sum accumulated in f64 and rounded to f32, then the same float32 division | measured-tolerance numeric substitution: results agree to about 1e-7 before the 4-place rounding; a cosine within that of `min_cohesion` could join one cluster and not the other |
