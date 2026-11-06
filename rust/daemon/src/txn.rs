//! One transaction at a time on the shared writer session.
//!
//! Python runs every write under the service lock on one connection
//! (`storage/postgres.py` `_txn`), so two transactions never interleave.
//! The Rust daemon's handlers run concurrently on one `tokio_postgres`
//! client, whose pipelining would interleave two `BEGIN`..`COMMIT` blocks;
//! every multi-statement write goes through [`run`] instead.

#![allow(dead_code)] // Shared API; dream adopts it in a following increment.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use tokio_postgres::Client;

static TX: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
tokio::task_local! { static INSIDE_WRITER: (); }

fn reject_reentrant_call() {
    assert!(
        INSIDE_WRITER.try_with(|_| ()).is_err(),
        "writer adapters must not be nested; use the borrowed client inside a body"
    );
}
/// Set before sending `BEGIN`, until its `COMMIT`/`ROLLBACK`. Still set when the next
/// transaction starts means the last one's future was dropped mid-way; that
/// transaction is rolled back first so its writes never ride on another
/// caller's `COMMIT`. Tool bodies run in spawned tasks (`store.rs`), so a
/// disconnecting client does not drop them; this guard covers the rest.
static OPEN: AtomicBool = AtomicBool::new(false);

/// Set while a writer transaction's `COMMIT` is on the wire, until its answer
/// arrives. Still set when the next transaction starts means that future
/// was dropped mid-`COMMIT`: whether it committed is unknown.
static COMMIT_SENT: AtomicBool = AtomicBool::new(false);

/// A writer transaction's `COMMIT` failed (any error, a lost reply or a
/// server refusal alike) or was abandoned unanswered, so the bank may hold
/// what the caller never published. `Service::init` re-reads the bank before
/// serving again and clears this only once the re-read is published
/// (maintainer decision 2026-11-07: rehydrate after a commit-unknown error
/// instead of Python's per-call recovery barriers).
static COMMIT_OUTCOME_UNKNOWN: AtomicBool = AtomicBool::new(false);

/// Whether a `COMMIT` outcome is unknown since the last re-read.
pub fn commit_outcome_unknown() -> bool {
    COMMIT_OUTCOME_UNKNOWN.load(Ordering::SeqCst)
}

/// Taken before the resident copy is re-read; [`restore_commit_outcome_unknown`]
/// puts it back when the re-read fails. True when it was set.
pub fn take_commit_outcome_unknown() -> bool {
    COMMIT_OUTCOME_UNKNOWN.swap(false, Ordering::SeqCst)
}

/// The re-read failed: the next call tries again.
pub fn restore_commit_outcome_unknown() {
    COMMIT_OUTCOME_UNKNOWN.store(true, Ordering::SeqCst);
}

#[cfg(test)]
pub(crate) fn set_commit_outcome_unknown_for_test(unknown: bool) {
    COMMIT_OUTCOME_UNKNOWN.store(unknown, Ordering::SeqCst);
}

fn note_commit_outcome_unknown() {
    COMMIT_OUTCOME_UNKNOWN.store(true, Ordering::SeqCst);
}

/// Held across a `COMMIT` and forgotten once its answer arrives: dropped
/// only when the transaction's future is (a cancelled or panicking
/// caller), when the outcome is unknown.
struct CommitInFlight;

impl Drop for CommitInFlight {
    fn drop(&mut self) {
        note_commit_outcome_unknown();
    }
}

/// Caller holds TX. An error or dropped future keeps the recovery marker;
/// only a confirmed transaction-exit command makes the writer reusable.
async fn finish(client: &Client, command: &str) -> Result<(), tokio_postgres::Error> {
    client.batch_execute(command).await?;
    OPEN.store(false, Ordering::SeqCst);
    Ok(())
}

/// `BEGIN`, the body, then `COMMIT`; `ROLLBACK` when the body or commit probe fails.
///
/// # Panics
/// Panics when either writer adapter is nested in the same task. Use the
/// borrowed client directly inside an adapter body.
pub async fn run<'c, T, E, F, Fut>(client: &'c Client, body: F) -> Result<T, E>
where
    E: From<tokio_postgres::Error>,
    F: FnOnce(&'c Client) -> Fut,
    Fut: Future<Output = Result<T, E>>,
{
    reject_reentrant_call();
    INSIDE_WRITER
        .scope((), async move {
            let _guard = TX.lock().await;
            if COMMIT_SENT.swap(false, Ordering::SeqCst) {
                note_commit_outcome_unknown();
            }
            if OPEN.load(Ordering::SeqCst) {
                finish(client, "ROLLBACK").await?;
            }
            OPEN.store(true, Ordering::SeqCst);
            client.batch_execute("BEGIN").await?;
            match body(client).await {
                Ok(v) => {
                    // A swallowed statement error leaves PostgreSQL aborted; COMMIT
                    // then succeeds with a ROLLBACK tag the driver does not expose.
                    // Reject that state before reporting a committed result.
                    if let Err(error) = client.batch_execute("SELECT 1").await {
                        let _ = finish(client, "ROLLBACK").await;
                        return Err(E::from(error));
                    }
                    COMMIT_SENT.store(true, Ordering::SeqCst);
                    // A future dropped while its COMMIT is on the wire sets
                    // the flag as it drops, so no read is served from the
                    // resident copy before the next transaction notices.
                    let abandoned = CommitInFlight;
                    let committed = finish(client, "COMMIT").await;
                    std::mem::forget(abandoned);
                    COMMIT_SENT.store(false, Ordering::SeqCst);
                    match committed {
                        Ok(()) => Ok(v),
                        Err(error) => {
                            note_commit_outcome_unknown();
                            let _ = finish(client, "ROLLBACK").await;
                            Err(E::from(error))
                        }
                    }
                }
                Err(e) => {
                    let _ = finish(client, "ROLLBACK").await;
                    Err(e)
                }
            }
        })
        .await
}

/// `PostgresStorage._pinned()` for the writer-session probe: `None` while a
/// transaction or statement group holds the writer, else a guard that keeps
/// one from starting until the probe (or its reconnect) has finished, so the
/// probe never lands inside another caller's transaction on the shared,
/// pipelined client.
pub fn try_hold() -> Option<tokio::sync::MutexGuard<'static, ()>> {
    TX.try_lock().ok()
}

/// A dropped transaction is still open on the writer session: the next
/// [`run`] rolls it back first. A probe then would run inside it.
pub fn recovery_pending() -> bool {
    OPEN.load(Ordering::SeqCst)
}

/// `set_meta` (`storage/postgres.py:2722`): one upsert of a JSON value.
pub async fn set_meta(
    client: &Client,
    key: &str,
    value: &serde_json::Value,
) -> Result<(), tokio_postgres::Error> {
    run(client, |c| async move {
        c.execute(
            "INSERT INTO meta (key, value) VALUES ($1, $2) \
             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            &[&key, value],
        )
        .await
        .map(|_| ())
    })
    .await
}
/// Serialize a statement or read group on the same leased writer client.
/// Recover a dropped transaction before allowing even an autocommit statement.
/// Code already inside `run` uses its borrowed client directly, without nesting.
///
/// # Panics
/// Panics when either writer adapter is nested in the same task.
pub async fn with_client<'c, T, E, F, Fut>(client: &'c Client, body: F) -> Result<T, E>
where
    E: From<tokio_postgres::Error>,
    F: FnOnce(&'c Client) -> Fut,
    Fut: Future<Output = Result<T, E>>,
{
    reject_reentrant_call();
    INSIDE_WRITER
        .scope((), async move {
            let _guard = TX.lock().await;
            if OPEN.load(Ordering::SeqCst) {
                finish(client, "ROLLBACK").await?;
            }
            body(client).await
        })
        .await
}

#[cfg(test)]
#[path = "txn_tests.rs"]
pub(crate) mod tests;
