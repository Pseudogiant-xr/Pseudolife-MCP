//! `POST /api/daemon-notice` (`web/routes.py:289-325`,
//! `coordination.daemon_notice`): a board notice from the daemon's own
//! reserved sender to every attached, non-idle session. Each recipient's
//! decision is `hinted`, so nothing rings.

use super::Board;
use super::error::BoardError;
use super::mail::SendArgs;
use super::store::{DAEMON_PRINCIPAL, Store, now, uuid_hex};
use serde_json::{Map, Value, json};

/// The route's refusal texts (`ValueError(str)` answered as a 400 body).
pub const NOT_LISTED: &str = "principal_not_allowed: a daemon notice needs a bearer whose principal is listed in \
coordination.daemon_notice_principals (empty by default). Give the scheduled run \
its own principal: a PSEUDOLIFE_MCP_TOKENS entry token:<name> on the daemon, \
<name> in coordination.daemon_notice_principals (and in allowed_principals, so the \
run can read the board), and that token as the scheduled run's bearer";
pub const TEXT_REQUIRED: &str = "text is required";
pub const TEXT_PRINTABLE: &str = "text must be at most 4000 bytes of printable text";

impl Board {
    /// Warmup and dispatch drain the committed audit outbox. Never wait for
    /// cold full-tier initialization on a heartbeat or lease operation.
    pub async fn drain_lease_grants(&self) {
        if !self.config.enabled || self.service.config.read_only {
            return;
        }
        let Some(ready) = self.service.snapshot().ready else {
            return;
        };
        if ready.startup.clock_state().hlc_reseed_pending
            || ready.storage.resident_invalidated().is_some()
        {
            return;
        }
        let mut conn = self.conn.lock().await;
        let Ok(client) = conn.client().await else {
            return;
        };
        self.drain_lease_grants_locked(client).await;
    }

    pub(crate) async fn drain_lease_grants_locked(&self, client: &mut tokio_postgres::Client) {
        let Some(ready) = self.service.snapshot().ready else {
            return;
        };
        if !self.config.enabled
            || self.service.config.read_only
            || ready.startup.clock_state().hlc_reseed_pending
            || ready.storage.resident_invalidated().is_some()
        {
            return;
        }
        let result: super::error::Result<()> = async {
            let mut after_name = String::new();
            let mut pending_fences = std::collections::HashSet::new();
            loop {
                let pending = client
                    .query(
                        "SELECT l.name,l.fence FROM coordination_leases l WHERE l.expires_at>$1 AND l.acquired_at>$3 \
                     AND l.name>$4 AND NOT EXISTS (SELECT 1 FROM coordination_events a \
                     WHERE a.event='lease_update' AND a.agent_id=l.holder_agent_id \
                     AND a.created_at>=l.acquired_at AND a.created_at<=l.acquired_at+$5 \
                     AND a.payload::jsonb->>'name'=l.name AND a.payload::jsonb->>'fence'=l.fence::text \
                     AND a.payload::jsonb->>'accepted'='true') \
                     AND EXISTS (SELECT 1 FROM coordination_events e WHERE e.event='lease_grant' \
                     AND e.actor='daemon' AND e.agent_id=l.holder_agent_id AND e.created_at>=l.acquired_at \
                     AND e.payload::jsonb->>'name'=l.name \
                     AND e.payload::jsonb->>'fence'=l.fence::text) \
                     AND NOT EXISTS (SELECT 1 FROM coordination_messages m WHERE \
                     m.sender_principal=$2 AND m.request_id='lease-grant:'||l.fence::text) \
                     AND NOT EXISTS (SELECT 1 FROM coordination_events e WHERE e.event='send' \
                     AND e.principal='daemon' AND e.created_at>=l.acquired_at \
                     AND e.payload::jsonb->>'request_id'='lease-grant:'||l.fence::text) \
                     ORDER BY l.name LIMIT 50",
                        &[&now(), &DAEMON_PRINCIPAL, &(now() - super::lease::LEASE_GRANT_WINDOW as f64),
                          &after_name, &(super::lease::LEASE_GRANT_WINDOW as f64)],
                    )
                    .await?;
                let Some(last) = pending.last() else {
                    break;
                };
                // Refused entries must not pin the first page on every drain.
                after_name = last.get("name");
                for grant in pending {
                    let fence: i64 = grant.get("fence");
                    pending_fences.insert(fence);
                    for attempt in 0..2 {
                        let cached = self.daemon_identity.lock().expect("identity lock").clone();
                        let identity = match cached {
                            Some(identity) => identity,
                            None => {
                                let params = json!({"label": "daemon", "status": "daemon notices"});
                                let row = self
                                    .store
                                    .register(
                                        client,
                                        DAEMON_PRINCIPAL,
                                        params.as_object().expect("object"),
                                    )
                                    .await?;
                                let identity = (
                                    row["agent_id"].as_str().expect("id").to_string(),
                                    row["credential"].as_str().expect("credential").to_string(),
                                );
                                *self.daemon_identity.lock().expect("identity lock") =
                                    Some(identity.clone());
                                identity
                            }
                        };
                        let sent = self
                            .store
                            .send_lease_grant(
                                client,
                                &identity,
                                grant.get("name"),
                                grant.get("fence"),
                                super::dispatch::stamp(&ready),
                            )
                            .await;
                        match sent {
                            Ok(Some(recipient)) => self.hub.notify(&recipient),
                            Ok(None) => {}
                            Err(e)
                                if attempt == 0
                                    && matches!(
                                        e_code(&e),
                                        Some("instance_not_found" | "invalid_credential")
                                    ) =>
                            {
                                *self.daemon_identity.lock().expect("identity lock") = None;
                                continue;
                            }
                            Err(BoardError::Refused(_)) => {
                                if self.lease_grant_refusals.lock().expect("refusal lock").insert(fence) {
                                    eprintln!("lease grant notice refused; retained for retry");
                                }
                                break;
                            }
                            Err(e) => return Err(e),
                        }
                        break;
                    }
                }
            }
            self.lease_grant_refusals
                .lock()
                .expect("refusal lock")
                .retain(|fence| pending_fences.contains(fence));
            Ok(())
        }
        .await;
        if result.is_err() {
            eprintln!("lease grant notice not sent; retained for retry");
        }
    }

    /// The route body: `Ok(result)` or `Err(message)` for a 400.
    pub async fn daemon_notice_route(
        &self,
        principal: &str,
        body: &Map<String, Value>,
    ) -> Result<Value, String> {
        if principal == DAEMON_PRINCIPAL
            || !self
                .config
                .daemon_notice_principals
                .iter()
                .any(|p| p == principal)
        {
            return Err(NOT_LISTED.into());
        }
        let Some(text) = body
            .get("text")
            .and_then(Value::as_str)
            .filter(|t| !super::text::py_strip(t).is_empty())
        else {
            return Err(TEXT_REQUIRED.into());
        };
        let text = super::text::py_strip(text);
        if text.len() > 4000
            || text.chars().any(|c| {
                let n = c as u32;
                (n < 32 && c != '\n' && c != '\t') || (127..160).contains(&n)
            })
        {
            return Err(TEXT_PRINTABLE.into());
        }
        let posted = format!("{text}\n(posted by the unattended updater, principal {principal})");
        Ok(match self.daemon_notice(&posted).await {
            None => json!({"recipients": null, "reason": "board_unavailable"}),
            Some(n) => json!({"recipients": n}),
        })
    }

    /// `coordination.daemon_notice`: the number reached, or None when the
    /// board cannot carry it. Never fails its caller.
    pub async fn daemon_notice(&self, text: &str) -> Option<i64> {
        if !self.config.enabled {
            return None;
        }
        // `_ensure_tier(full=True)` before the coordination lock.
        let ready = self.full_tier().await.ok()?;
        let mut conn = self.conn.lock().await;
        let client = conn.client().await.ok()?;
        for attempt in 0..2 {
            let identity = self.daemon_identity.lock().expect("identity lock").clone();
            let identity = match identity {
                Some(i) => i,
                None => {
                    let mut params = Map::new();
                    params.insert("label".into(), json!("daemon"));
                    params.insert("status".into(), json!("daemon notices"));
                    let row = self
                        .store
                        .register(client, DAEMON_PRINCIPAL, &params)
                        .await
                        .ok()?;
                    let i = (
                        row["agent_id"].as_str()?.to_string(),
                        row["credential"].as_str()?.to_string(),
                    );
                    *self.daemon_identity.lock().expect("identity lock") = Some(i.clone());
                    i
                }
            };
            let (hlc, epoch) = super::dispatch::stamp(&ready);
            let to = json!("all");
            let body = json!(text);
            let request_id = json!(uuid_hex());
            let sent = self
                .store
                .send(
                    client,
                    DAEMON_PRINCIPAL,
                    &identity.0,
                    &identity.1,
                    SendArgs {
                        to: Some(&to),
                        text: &body,
                        request_id: &request_id,
                        reply_to: None,
                        clears: None,
                        urgent: None,
                        hlc,
                        expected_writer_epoch: Some(epoch),
                        enforce_writer_epoch: true,
                        notice: true,
                        lease_grant: false,
                        maintainer_proof: None,
                    },
                )
                .await;
            match sent {
                Ok(result) => return result["recipients"].as_i64(),
                Err(e) if e_code(&e) == Some("no_recipients") => return Some(0),
                Err(e)
                    if attempt == 0
                        && matches!(
                            e_code(&e),
                            Some("instance_not_found" | "invalid_credential")
                        ) =>
                {
                    *self.daemon_identity.lock().expect("identity lock") = None;
                    continue;
                }
                Err(_) => return None,
            }
        }
        None
    }
}

impl Store {
    async fn send_lease_grant(
        &self,
        client: &mut tokio_postgres::Client,
        identity: &(String, String),
        name: &str,
        fence: i64,
        stamp: (String, i64),
    ) -> super::error::Result<Option<String>> {
        let tx = client.transaction().await?;
        let row = tx
            .query_opt(
                "SELECT * FROM coordination_leases \
                                WHERE name=$1 FOR UPDATE",
                &[&name],
            )
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let row = super::db::LeaseRow::from_row(&row);
        let holder = row.holder_agent_id.clone();
        let Some(holder) = holder else {
            return Ok(None);
        };
        if row.fence != fence || row.expires_at.unwrap_or(0.0) <= now() {
            return Ok(None);
        }
        let acquired = row.acquired_at.unwrap_or(0.0);
        let request_id = format!("lease-grant:{fence}");
        if tx
            .query_opt(
                "SELECT 1 FROM coordination_messages WHERE sender_principal=$1 AND request_id=$2",
                &[&DAEMON_PRINCIPAL, &request_id],
            )
            .await?
            .is_some()
        {
            return Ok(None);
        }
        if tx
            .query_opt(
                "SELECT 1 FROM coordination_events WHERE event='send' AND principal=$1 \
             AND created_at>=$3 AND payload::jsonb->>'request_id'=$2 LIMIT 1",
                &[&DAEMON_PRINCIPAL, &request_id, &acquired],
            )
            .await?
            .is_some()
        {
            return Ok(None);
        }
        if super::lease::unaccepted_grant(&tx, &row, name)
            .await?
            .is_none()
        {
            return Ok(None);
        }
        let recipient = json!(holder);
        let request = json!(request_id);
        let text = json!(format!(
            "Lease {name} was granted to you. Renew it before starting work; release it if no longer needed. This notice grants no user approval."
        ));
        self.send_in(
            &tx,
            DAEMON_PRINCIPAL,
            &identity.0,
            &identity.1,
            SendArgs {
                to: Some(&recipient),
                text: &text,
                request_id: &request,
                reply_to: None,
                clears: None,
                urgent: None,
                hlc: stamp.0,
                expected_writer_epoch: Some(stamp.1),
                enforce_writer_epoch: true,
                notice: false,
                lease_grant: true,
                maintainer_proof: None,
            },
        )
        .await?;
        super::db::commit(tx).await?;
        Ok(Some(holder))
    }
}

fn e_code(e: &BoardError) -> Option<&str> {
    match e {
        BoardError::Refused(r) => Some(r.code.as_str()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[tokio::test]
    async fn cold_grant_drain_never_waits_for_initialization() {
        let dir = std::env::temp_dir().join(format!("pl-cold-grant-{}", uuid::Uuid::new_v4()));
        let mut config = crate::config::Config::default();
        config.coordination.enabled = true;
        let service = Arc::new(crate::service::Service::new(
            config,
            crate::config::DaemonEnv::from_env(&|_| None).unwrap(),
            dir,
            "unused-cold-dsn".into(),
            true,
        ));
        let board = Board::new(
            service.config.coordination.clone(),
            "unused-cold-dsn".into(),
            service.clone(),
            Arc::new(crate::auth::PrincipalStore::new()),
        );
        let _initializing = service.hold_service_lock().await;
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            board.drain_lease_grants(),
        )
        .await
        .expect("cold drain waited on initialization");
        assert!(service.snapshot().storage.is_none());
    }
}
