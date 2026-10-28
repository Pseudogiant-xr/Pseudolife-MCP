| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A read concurrent with a fact write | waits for the service lock, so it sees the write or none of it | reads the snapshot it took: it may predate a write still in progress, or show the written record before that write has persisted or answered its caller | readers never hold a lock across an await; each read is internally consistent, and a read that starts after a write returns sees it |
