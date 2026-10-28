| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Reranker or BM25 settings Python refuses (`reranker.fusion_weight` outside [0, 1], `reranker.top_n < 1`, `bm25.k1 < 0`, `bm25.b` outside [0, 1]) | init fails (500 on every route) or every BM25 search answers 400 | accepted as given | canonical configs use the shipped defaults; W1-A's early-refusal policy can absorb them |
