| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `w2d-recall-graph` | `/api/graph/communities`, `/api/graph/review`, `/api/graph/proposal-evidence`, `/api/curation/duplicates`, `/api/curation/retired` | The Console graph, path, projects, digest, provenance, wiki, recall and dream status reads are served by `read/graph.rs`, with REST parity in `w2e_reads.py`. The remaining review/curation routes belong to the graph action slice; communities, proposal evidence and retired curation remain 501 under D9. |
