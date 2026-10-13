//! `POST /api/daemon-notice` (`web/routes.py:289-325`,
//! `coordination.daemon_notice`): a board notice from the daemon's own
//! reserved sender to every attached, non-idle session. Each recipient's
//! decision is `hinted`, so nothing rings.

use super::Board;
use super::error::BoardError;
use super::mail::SendArgs;
use super::store::{DAEMON_PRINCIPAL, uuid_hex};
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
        self.service.storage_tier().await.ok()?;
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
            let (hlc, epoch) = self.tick().await.ok()?;
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

fn e_code(e: &BoardError) -> Option<&str> {
    match e {
        BoardError::Refused(r) => Some(r.code.as_str()),
        _ => None,
    }
}
