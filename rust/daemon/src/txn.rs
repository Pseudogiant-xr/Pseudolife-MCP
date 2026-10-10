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
/// Set before sending `BEGIN`, until its `COMMIT`/`ROLLBACK`. Still set when the next
/// transaction starts means the last one's future was dropped mid-way; that
/// transaction is rolled back first so its writes never ride on another
/// caller's `COMMIT`. Tool bodies run in spawned tasks (`store.rs`), so a
/// disconnecting client does not drop them; this guard covers the rest.
static OPEN: AtomicBool = AtomicBool::new(false);

/// Caller holds TX. An error or dropped future keeps the recovery marker;
/// only a confirmed transaction-exit command makes the writer reusable.
async fn finish(client: &Client, command: &str) -> Result<(), tokio_postgres::Error> {
    client.batch_execute(command).await?;
    OPEN.store(false, Ordering::SeqCst);
    Ok(())
}

/// `BEGIN`, the body, then `COMMIT`; `ROLLBACK` when the body fails.
pub async fn run<'c, T, E, F, Fut>(client: &'c Client, body: F) -> Result<T, E>
where
    E: From<tokio_postgres::Error>,
    F: FnOnce(&'c Client) -> Fut,
    Fut: Future<Output = Result<T, E>>,
{
    let _guard = TX.lock().await;
    if OPEN.load(Ordering::SeqCst) {
        finish(client, "ROLLBACK").await?;
    }
    OPEN.store(true, Ordering::SeqCst);
    client.batch_execute("BEGIN").await?;
    match body(client).await {
        Ok(v) => match finish(client, "COMMIT").await {
            Ok(()) => Ok(v),
            Err(error) => {
                let _ = finish(client, "ROLLBACK").await;
                Err(E::from(error))
            }
        },
        Err(e) => {
            let _ = finish(client, "ROLLBACK").await;
            Err(e)
        }
    }
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
pub async fn with_client<'c, T, E, F, Fut>(client: &'c Client, body: F) -> Result<T, E>
where
    E: From<tokio_postgres::Error>,
    F: FnOnce(&'c Client) -> Fut,
    Fut: Future<Output = Result<T, E>>,
{
    let _guard = TX.lock().await;
    if OPEN.load(Ordering::SeqCst) {
        finish(client, "ROLLBACK").await?;
    }
    body(client).await
}

#[cfg(test)]
#[path = "txn_tests.rs"]
mod tests;
