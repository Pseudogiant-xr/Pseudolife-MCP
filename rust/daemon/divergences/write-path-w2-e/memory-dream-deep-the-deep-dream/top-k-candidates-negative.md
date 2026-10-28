| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `top_k_candidates` negative | the early-exit scan stops after the first survivor's rounded-similarity tie band, then `[:top_k]` drops the last `-top_k` of those | the last `-top_k` of every surviving pair dropped | no shipped config sets one (`curation_top_k` negative slices alike in both) |
