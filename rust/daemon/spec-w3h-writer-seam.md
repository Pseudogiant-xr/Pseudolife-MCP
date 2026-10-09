# Shared writer statement contract

Graph and dream must serialize all statements on the daemon's one leased
writer connection. Two independent locks on that client can pipeline one
caller's statements into another caller's transaction.

The interface adopts `rust-cf/w2e-store` at `544faad1`, `src/txn.rs`:
`run(&Client, body)` serializes BEGIN/body/COMMIT and rolls back a failed body;
`with_client(&Client, body)` takes the same lock for a single statement or
read group. Both recover an abandoned transaction before executing the next
body. Bodies already inside the lock call their borrowed client directly,
without nesting the adapters. All callers use the same leased writer client;
dedicated probe/coordination connections are outside this interface.

Deviation from `544faad1`: set OPEN before sending/awaiting BEGIN. A withheld
BEGIN response proved the original code's gap: cancellation left OPEN false,
so the next autocommit INSERT inherited that transaction and returned success
without committing. The rejecting protocol peer observes BEGIN/INSERT and an
uncommitted write; the fix observes BEGIN/ROLLBACK/INSERT and a committed write.
The rest of `run` remains unchanged. Its error parameter is generalized to
`E: From<tokio_postgres::Error>` so a typed refusal from a body rolls back a
prior write rather than committing an `Ok(Err(refusal))`. Existing PostgreSQL
error callers retain their inferred type. A rejecting test first failed to
compile against the PostgreSQL-only interface, then proves BEGIN/write/ROLLBACK
and a successful next statement while retaining the typed refusal.
A recovery ROLLBACK when
BEGIN never reached the server may emit PostgreSQL's warning, but must succeed.

Tool bodies use spawned completion tasks so a disconnected caller does not
drop durable work. Statement/transaction isolation and pending-query recovery
are tested using owned loopback PostgreSQL protocol peers; no bank or model is
used. This increment provides the shared interface and its rejecting controls;
graph and dream adopt it in their own increments. No consumer is migrated here.
