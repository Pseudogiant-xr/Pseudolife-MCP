//! Stored-principal refresher (spec section Q): `PrincipalRefresher` and
//! `load_rows` from `pseudolife_memory/principal_store.py` (master 35b8f5d2).
//!
//! The snapshot itself (normalization, shadowing, collapse by name, the
//! 60 s staleness rule) lives in `auth::PrincipalStore`, which the bearer
//! gate reads; this module only reads the table and feeds it.

// Wired by main in W1-A.
#![allow(dead_code)]

use std::sync::Arc;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use tokio::task::JoinHandle;
use tokio_postgres::Client;
use tokio_postgres::error::SqlState;

use crate::auth::{self, StoredPrincipal};
use crate::storage::{self, BANK_ID_META_KEY};

/// `_connect`'s `connect_timeout=5` (principal_store.py:221-223).
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// `bank_fingerprint` (principal_store.py:76-81): the first 16 hex of the
/// SHA-256 of a non-empty string bank id, else `None`.
pub fn bank_fingerprint(bank_id: Option<&serde_json::Value>) -> Option<String> {
    match bank_id {
        Some(serde_json::Value::String(id)) if !id.is_empty() => {
            Some(hex::encode(Sha256::digest(id.as_bytes()))[..16].to_string())
        }
        _ => None,
    }
}

/// The refresher's connection options (principal_store.py `_KEEPALIVES`):
/// keepalive probes after 10 s idle, every 5 s, three tries, and a 30 s cap
/// on unacknowledged writes.
fn refresher_config(dsn: &str) -> Result<tokio_postgres::Config, String> {
    let mut config = storage::parse_dsn(dsn)?;
    config
        .keepalives(true)
        .keepalives_idle(Duration::from_secs(10))
        .keepalives_interval(Duration::from_secs(5))
        .keepalives_retries(3)
        .tcp_user_timeout(Duration::from_millis(30_000));
    Ok(config)
}

/// `load_rows` (principal_store.py:226-248): every row of `principals` and
/// the bank fingerprint, on an autocommit connection. A missing table reads
/// as empty (and a missing `meta` as no bank). Never runs DDL.
pub async fn load_rows(
    client: &Client,
) -> Result<(Vec<StoredPrincipal>, Option<String>), tokio_postgres::Error> {
    let rows = match client
        .query(
            "SELECT principal, token_hash, tier, board, revoked_at IS NOT NULL \
             FROM public.principals",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) if e.code() == Some(&SqlState::UNDEFINED_TABLE) => Vec::new(),
        Err(e) => return Err(e),
    };
    let found = match client
        .query_opt(
            "SELECT value FROM public.meta WHERE key = $1",
            &[&BANK_ID_META_KEY],
        )
        .await
    {
        Ok(row) => row.map(|r| r.get::<_, serde_json::Value>(0)),
        Err(e) if e.code() == Some(&SqlState::UNDEFINED_TABLE) => None,
        Err(e) => return Err(e),
    };
    let rows = rows
        .iter()
        .map(|r| StoredPrincipal {
            principal: r.get(0),
            token_hash: r.get(1),
            tier: r.get(2),
            board: r.get(3),
            revoked: r.get(4),
        })
        .collect();
    Ok((rows, bank_fingerprint(found.as_ref())))
}

/// `PrincipalRefresher` (principal_store.py:251-325): one connection of its
/// own, reused while it lives.
pub struct Refresher {
    dsn: String,
    store: Arc<auth::PrincipalStore>,
    env_principals: Vec<String>,
    conn: Option<(Client, JoinHandle<()>)>,
    failing: bool,
}

impl Refresher {
    pub fn new(dsn: String, store: Arc<auth::PrincipalStore>, env_principals: Vec<String>) -> Self {
        Refresher {
            dsn,
            store,
            env_principals,
            conn: None,
            failing: false,
        }
    }

    /// `_read`: (re)connect when there is no live connection, with
    /// `SET statement_timeout = '5s'`, then `load_rows`.
    async fn read(&mut self) -> Result<(Vec<StoredPrincipal>, Option<String>), String> {
        if self.conn.as_ref().is_none_or(|(c, _)| c.is_closed()) {
            self.drop_conn().await;
            let config = refresher_config(&self.dsn)?;
            let (client, task) = storage::connect(&config, CONNECT_TIMEOUT).await?;
            // Stored before the SET, so a failed SET drops it like any failure.
            self.conn = Some((client, task));
            let (client, _) = self.conn.as_ref().unwrap();
            client
                .batch_execute("SET statement_timeout = '5s'")
                .await
                .map_err(|e| storage::error_text(&e))?;
        }
        let (client, _) = self.conn.as_ref().unwrap();
        load_rows(client).await.map_err(|e| storage::error_text(&e))
    }

    async fn drop_conn(&mut self) {
        if let Some((client, task)) = self.conn.take() {
            drop(client);
            let _ = task.await;
        }
    }

    /// `refresh_once`: one read; on success the snapshot is replaced and
    /// dated from the read's start (taken before any connect, as Python's
    /// `PrincipalSnapshot.refresh` does), on failure the old snapshot stays
    /// and the connection is dropped. Returns whether it succeeded.
    pub async fn refresh_once(&mut self) -> bool {
        let started = Instant::now();
        match self.read().await {
            Ok((rows, bank)) => {
                self.store
                    .refresh(rows, bank, &self.env_principals, started);
                if self.failing {
                    eprintln!("stored principals: refresh recovered");
                }
                self.failing = false;
                true
            }
            Err(e) => {
                if !self.failing {
                    eprintln!(
                        "stored principals: refresh failed ({e}); bearers that match nothing \
                         in the environment get 503 once the view is {}s old",
                        auth::STALE_AFTER.as_secs()
                    );
                }
                self.failing = true;
                self.drop_conn().await;
                false
            }
        }
    }

    /// `_run`: refresh at once, then every 10 s after each refresh ends.
    pub async fn run(mut self) {
        loop {
            self.refresh_once().await;
            tokio::time::sleep(auth::REFRESH_EVERY).await;
        }
    }
}

/// Start the refresher (spec Q1; `daemon.py` `start_stored_principals`,
/// called only when a DSN is configured). `env_principals` are the
/// principals `PSEUDOLIFE_MCP_TOKENS` maps, which shadow stored rows of the
/// same name. The task runs until aborted.
pub fn spawn_refresher(
    dsn: String,
    store: Arc<auth::PrincipalStore>,
    env_principals: Vec<String>,
) -> JoinHandle<()> {
    tokio::spawn(Refresher::new(dsn, store, env_principals).run())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_matches_python() {
        // hashlib.sha256(b"bank-1").hexdigest()[:16]
        let want = &hex::encode(Sha256::digest(b"bank-1"))[..16];
        assert_eq!(
            bank_fingerprint(Some(&serde_json::json!("bank-1"))).as_deref(),
            Some(want)
        );
        assert_eq!(bank_fingerprint(Some(&serde_json::json!(""))), None);
        assert_eq!(bank_fingerprint(Some(&serde_json::json!(7))), None);
        assert_eq!(bank_fingerprint(None), None);
    }

    fn test_dsn() -> Option<String> {
        let dsn = std::env::var("PL_W1A_TEST_DSN")
            .ok()
            .filter(|d| !d.is_empty());
        if dsn.is_none() {
            eprintln!("PL_W1A_TEST_DSN unset: skipping database test");
        }
        dsn
    }

    #[tokio::test]
    async fn db_refresh_reads_rows_and_keeps_view_on_failure() {
        let Some(dsn) = test_dsn() else { return };
        let (c, t) = storage::connect(&storage::parse_dsn(&dsn).unwrap(), Duration::from_secs(10))
            .await
            .unwrap();
        let has_table = c
            .query_opt("SELECT to_regclass('public.principals')::text", &[])
            .await
            .unwrap()
            .and_then(|r| r.get::<_, Option<String>>(0))
            .is_some();
        if !has_table {
            eprintln!("principals table absent (bank not initialized): skipping");
            return;
        }
        let hash = hex::encode(Sha256::digest(b"w1a-refresher-token"));
        c.execute(
            "INSERT INTO principals (principal, token_hash, tier, board, created_at) \
             VALUES ('w1a-refresher', $1, 'core', FALSE, 0) \
             ON CONFLICT (principal) DO UPDATE SET token_hash = EXCLUDED.token_hash",
            &[&hash],
        )
        .await
        .unwrap();
        let store = Arc::new(auth::PrincipalStore::new());
        let mut r = Refresher::new(dsn.clone(), store.clone(), vec![]);
        assert!(r.refresh_once().await);
        assert_eq!(store.tier_of("w1a-refresher").as_deref(), Some("core"));
        assert!(!store.admitted("w1a-refresher"));
        let env = auth::EnvTokens {
            map: vec![],
            single: Some("x".into()),
        };
        assert!(matches!(
            auth::resolve(Some("Bearer w1a-refresher-token"), &env, &store),
            auth::Resolved::Principal(p) if p == "w1a-refresher"
        ));
        // A failing read keeps the previous view.
        let mut bad = Refresher::new(
            "postgresql://nobody@127.0.0.1:1/pl_cf_w1a_none".into(),
            store.clone(),
            vec![],
        );
        assert!(!bad.refresh_once().await);
        assert!(store.has("w1a-refresher"));
        c.batch_execute("DELETE FROM principals WHERE principal = 'w1a-refresher'")
            .await
            .unwrap();
        assert!(r.refresh_once().await);
        assert!(!store.has("w1a-refresher"));
        drop(c);
        let _ = t.await;
    }

    /// On a database with no schema at all (PL_W1A_EMPTY_DSN), a missing
    /// table reads as no rows and no bank, and the view becomes available.
    #[tokio::test]
    async fn db_missing_tables_read_as_empty() {
        let Some(dsn) = std::env::var("PL_W1A_EMPTY_DSN")
            .ok()
            .filter(|d| !d.is_empty())
        else {
            eprintln!("PL_W1A_EMPTY_DSN unset: skipping database test");
            return;
        };
        let store = Arc::new(auth::PrincipalStore::new());
        let mut r = Refresher::new(dsn, store.clone(), vec![]);
        assert!(r.refresh_once().await);
        assert!(store.is_empty());
        assert_eq!(store.bank(), None);
        let env = auth::EnvTokens {
            map: vec![],
            single: Some("x".into()),
        };
        assert!(matches!(
            auth::resolve(Some("Bearer unknown"), &env, &store),
            auth::Resolved::None
        ));
    }
}
