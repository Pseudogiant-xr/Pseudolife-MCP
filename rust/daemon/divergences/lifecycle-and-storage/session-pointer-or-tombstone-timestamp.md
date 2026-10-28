| Item | Python | Rust | Owner |
|---|---|---|---|
| Session-pointer or tombstone timestamp parse failures | fail before the embedder and resident-store abandon path; each initialization call retries parsing | fail inside required initialization and arm its retry backoff | W3 PG-HYDRATE; non-canonical metadata, declared divergence |
