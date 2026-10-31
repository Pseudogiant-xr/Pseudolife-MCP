| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `w2d-recall-graph` | `/api/graph/communities`, `/api/graph/proposal-evidence`, `/api/curation/retired` | Retired under D9 / Proposal A / B2r (2026-10-23): deprecated for removal in a later release; no replacement; no caller. Python handlers unchanged; Rust declared 501 unchanged. The Console read routes are served by `read/graph.rs`, with REST parity in `w2e_reads.py`; graph review and duplicate curation are served by the graph action routes. |
