//! One transaction at a time on the shared writer session.
//!
//! Python runs every write under the service lock on one connection
//! (`storage/postgres.py` `_txn`), so two transactions never interleave.
//! The Rust daemon's handlers run concurrently on one `tokio_postgres`
//! client, whose pipelining would interleave two `BEGIN`..`COMMIT` blocks;
//! every multi-statement write goes through [`run`] instead.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use tokio_postgres::Client;

static TX: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
/// Set between `BEGIN` and its `COMMIT`/`ROLLBACK`. Still set when the next
/// transaction starts means the last one's future was dropped mid-way; that
/// transaction is rolled back first so its writes never ride on another
/// caller's `COMMIT`. Tool bodies run in spawned tasks (`store.rs`), so a
/// disconnecting client does not drop them; this guard covers the rest.
static OPEN: AtomicBool = AtomicBool::new(false);

/// `BEGIN`, the body, then `COMMIT`; `ROLLBACK` when the body fails.
pub async fn run<'c, T, F, Fut>(client: &'c Client, body: F) -> Result<T, tokio_postgres::Error>
where
    F: FnOnce(&'c Client) -> Fut,
    Fut: Future<Output = Result<T, tokio_postgres::Error>>,
{
    let _guard = TX.lock().await;
    if OPEN.swap(false, Ordering::SeqCst) {
        client.batch_execute("ROLLBACK").await?;
    }
    client.batch_execute("BEGIN").await?;
    OPEN.store(true, Ordering::SeqCst);
    let out = match body(client).await {
        Ok(v) => client.batch_execute("COMMIT").await.map(|_| v),
        Err(e) => {
            let _ = client.batch_execute("ROLLBACK").await;
            Err(e)
        }
    };
    OPEN.store(false, Ordering::SeqCst);
    out
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
