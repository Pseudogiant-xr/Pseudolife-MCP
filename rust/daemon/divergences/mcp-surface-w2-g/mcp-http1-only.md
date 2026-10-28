| Item | Python | Rust | Owner |
|---|---|---|---|
| MCP-HTTP1-ONLY | the current daemon listener serves HTTP/1 | the Rust listener enables Axum's `http1` feature only; SSE uses `Connection: keep-alive`, which must be removed before any HTTP/2 listener is enabled | HTTP/2 transport deferred |
