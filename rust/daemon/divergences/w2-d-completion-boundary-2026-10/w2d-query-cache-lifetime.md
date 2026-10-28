| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `w2d-query-cache-lifetime` | Multiple configured embedders in one process | The inherited outer query cache is process-global and keys raw query text; it is valid only for the daemon's single immutable service/model configuration. Per-embedder caching also remains. Hot model replacement or multiple service instances require cache ownership changes before support. |
