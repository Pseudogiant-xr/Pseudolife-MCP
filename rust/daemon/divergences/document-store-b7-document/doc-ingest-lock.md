| Item | Python | Rust | Why / owner |
|---|---|---|---|
| **DOC-INGEST-LOCK:** other calls during an ingest | wait on the service lock while the document embeds | ingests wait for one another in arrival order (a fair lock around the whole ingest), as Python orders them; every other call is served while a document embeds, and the write lock is held for the upsert alone | no stored row differs: two ingests still apply in arrival order. An ingest takes minutes on a large document |
