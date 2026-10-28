## Coordination board completion (W2-F)

The board transaction boundary is independent of the shared writer adapter:
`Board::conn` serializes the dedicated client, driver transactions roll back
when dropped, and `board::db::commit` probes for an aborted block before
reporting success. A failed connection is replaced only on the next call;
no uncertain write is retried. This does not use the writer's global recovery
marker, which belongs to a different PostgreSQL session.

Once storage is published, board calls bypass the full initialization mutex,
matching Python’s storage-tier fast path. The dedicated connection reuses the
shared connector’s ten-second socket-and-startup deadline without acquiring
the leased writer’s transaction lock.
