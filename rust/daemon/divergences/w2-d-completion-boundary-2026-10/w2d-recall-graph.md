| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `w2d-recall-graph` | `/api/graph/communities`, `/api/graph/proposal-evidence`, `/api/curation/retired` | These three routes remain 501 under D9. The Console read routes are served by `read/graph.rs`, with REST parity in `w2e_reads.py`; graph review and duplicate curation are served by the graph action routes. |
