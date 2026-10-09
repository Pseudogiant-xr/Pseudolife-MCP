//! The coordination board (slice W2-F, `spec.md` "Contract spec:
//! coordination board"): the agent store, addressed mail, resource leases,
//! the audit chain, wake decisions and the async receive hub, served on
//! `POST /api/coordination/<action>` and the board hooks.

pub mod canon;
pub mod console;
pub mod db;
pub mod dispatch;
pub mod error;
pub mod hlc;
pub mod http;
pub mod hub;
pub mod lease;
pub mod mail;
pub mod notice;
pub mod store;
pub mod text;
pub mod wake;

// The shim's byte-exact credential-shape guard and its pinned CPython 3.11
// Unicode tables, compiled into this crate from the same source files.
#[path = "../../../shim/src/cli/lease/operator/secrets.rs"]
mod secrets;
#[path = "../../../shim/src/board/unicode14.rs"]
#[allow(dead_code)]
mod unicode14;

use crate::auth::PrincipalStore;
use crate::config::CoordinationConfig;
use crate::service::Service;
use error::Result;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Instant;
use store::{Store, WakePolicy};

pub struct Board {
    pub config: CoordinationConfig,
    pub(crate) store: Store,
    /// The coordination lock and the board's own connection.
    pub(crate) conn: tokio::sync::Mutex<db::Conn>,
    pub(crate) hlc: Mutex<hlc::Hlc>,
    pub(crate) pruned_at: Mutex<Option<Instant>>,
    pub(crate) hub: hub::Hub,
    pub(crate) service: Arc<Service>,
    pub(crate) principals: Arc<PrincipalStore>,
    /// The daemon's own board address, registered by its first notice.
    pub(crate) daemon_identity: Mutex<Option<(String, String)>>,
}

type StoreCall<'a> =
    Pin<Box<dyn Future<Output = Result<serde_json::Value>> + Send + 'a>>;

impl Board {
    pub fn new(
        config: CoordinationConfig,
        dsn: String,
        service: Arc<Service>,
        principals: Arc<PrincipalStore>,
    ) -> Board {
        let wake = WakePolicy {
            per_recipient_per_hour: config.wake.per_recipient_per_hour,
            urgent_per_sender_per_hour: config.wake.urgent_per_sender_per_hour,
            nightly_total: config.wake.nightly_total,
            fan_out_stagger_seconds: config.wake.fan_out_stagger_seconds,
            active_seconds: config.wake.active_seconds,
            authority_per_sender_per_hour: config.wake.authority_per_sender_per_hour,
            maintainer_per_recipient_per_hour: config.maintainer_per_recipient_per_hour,
        };
        Board {
            store: Store {
                wake,
                maintainer_principals: config.maintainer_principals.clone(),
            },
            config,
            conn: tokio::sync::Mutex::new(db::Conn::new(dsn)),
            hlc: Mutex::new(hlc::Hlc::default()),
            pruned_at: Mutex::new(None),
            hub: hub::Hub::default(),
            service,
            principals,
            daemon_identity: Mutex::new(None),
        }
    }

    /// One store call under the coordination lock, after the storage tier
    /// is up (the hooks' path: no prune, no instance credentials).
    pub async fn with_store<F>(&self, f: F) -> Result<serde_json::Value>
    where
        F: for<'a> FnOnce(&'a Store, &'a mut tokio_postgres::Client) -> StoreCall<'a>,
    {
        self.service
            .storage_tier()
            .await
            .map_err(error::BoardError::Internal)?;
        let mut conn = self.conn.lock().await;
        let client = conn.client().await?;
        f(&self.store, client).await
    }
}
