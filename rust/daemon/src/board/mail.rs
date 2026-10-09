//! Addressed mail (spec BS6): send, receive, ack, attempt, history, prune,
//! the per-turn mailbox preview, and the origin fields of a message.

use super::canon::{self, float_value};
use super::db::{AgentRow, MessageRow, obj, opt_f64, opt_str};
use super::error::{BoardError, Result};
use super::store::{
    ATTACHED_IDLE_WINDOW, DAEMON_PRINCIPAL, Event, MAINTAINER_PRINCIPAL, RESERVED_SENDERS,
    Store, append, chain_head, check_limit, now, random_hex, refuse_secret, uuid_hex,
};
use super::text::{self, check_string, check_value};
use super::{lease, wake};
use serde_json::{Map, Value, json};
use tokio_postgres::GenericClient;

pub const MAX_TEXT_BYTES: usize = 8192;
pub const MAX_CLEARS: usize = 120;
pub const MAX_PENDING: i64 = 256;
pub const FANOUT_MAX: i64 = 50;
pub const MESSAGE_TTL: f64 = 86400.0;
pub const DEDUPE_RETENTION: f64 = 7.0 * 86400.0;
pub const WAKE_RETENTION: f64 = 7.0 * 86400.0;
pub const MAX_ATTEMPTS: i32 = 3;
pub const SEND_RATE: i64 = 60;
pub const PREVIEW_LIMIT: i64 = 5;
pub const ID_PREFIX_MIN: usize = 8;
pub const ID_LENGTH: usize = 32;
pub const AMBIGUITY_LISTED: usize = 8;
pub const MESSAGE_ORIGIN: &str = "agent";
pub const MAINTAINER_ORIGIN: &str = "maintainer";
pub const HLC_META_KEY: &str = "coordination_hlc_highwater";
pub const WRITER_EPOCH_META_KEY: &str = "writer_lease_epoch";
pub const WITHDRAWN_TEXT: &str = "the maintainer withdrew this message; do not act on it";
pub const REPUDIATE_EVENT: &str = "maintainer_repudiate";
pub const CHILD_SEND_REFUSED: &str = "a subagent does not send board mail; ask your parent session";
const ID_CHARS: &str = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_";
const BODY_SALT_BYTES: usize = 16;

fn is_hex(s: &str) -> bool {
    s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// `is_full_agent_id`.
pub fn is_full_agent_id(v: &str) -> bool {
    v.len() == ID_LENGTH && is_hex(v)
}

/// `is_id_prefix`.
pub fn is_id_prefix(v: &str) -> bool {
    let n = v.chars().count();
    (ID_PREFIX_MIN..ID_LENGTH).contains(&n) && is_hex(v)
}

fn common_len(a: &str, b: &str) -> usize {
    a.chars().zip(b.chars()).take_while(|(x, y)| x == y).count()
}

/// `distinguishing_prefixes`.
pub fn distinguishing_prefixes(ids: &[String]) -> Vec<String> {
    let mut ordered = ids.to_vec();
    ordered.sort();
    let shared = ordered
        .windows(2)
        .map(|w| common_len(&w[0], &w[1]))
        .max()
        .unwrap_or(0);
    let cut = ID_PREFIX_MIN.max(shared + 1);
    ordered
        .iter()
        .map(|id| id.chars().take(cut).collect())
        .collect()
}

/// `_resolved`.
fn resolved(ids: Vec<String>, missing: Option<&str>, ambiguous: &str) -> Result<Option<String>> {
    if ids.is_empty() {
        return match missing {
            None => Ok(None),
            Some(code) => Err(BoardError::code(code)),
        };
    }
    if ids.len() > 1 {
        let listed: Vec<String> = ids.into_iter().take(AMBIGUITY_LISTED).collect();
        return Err(BoardError::detail(
            ambiguous,
            distinguishing_prefixes(&listed).join(", "),
        ));
    }
    Ok(ids.into_iter().next())
}

/// `message_origin`.
pub fn message_origin(row: &MessageRow) -> Map<String, Value> {
    let mut m = Map::new();
    let proof = row.maintainer_proof.as_ref().filter(|p| p.is_object());
    if row.origin != MAINTAINER_ORIGIN
        || proof.is_none()
        || row.sender_principal != MAINTAINER_PRINCIPAL
    {
        m.insert("origin".into(), json!(MESSAGE_ORIGIN));
        return m;
    }
    m.insert("origin".into(), json!(MAINTAINER_ORIGIN));
    if let Some(at) = row.repudiated_at {
        m.insert("repudiated_at".into(), float_value(at));
        m.insert("text".into(), json!(WITHDRAWN_TEXT));
        return m;
    }
    let proof = proof.expect("checked above");
    m.insert(
        "verified".into(),
        json!({"by": "passkey", "label": proof.get("label").cloned().unwrap_or(Value::Null),
               "signed_at": proof.get("signed_at").cloned().unwrap_or(Value::Null)}),
    );
    m
}

/// `_mailbox_state`: pending count, preview and maintainer count.
pub async fn mailbox_state(c: &impl GenericClient, agent_id: &str) -> Result<Value> {
    let pending: i64 = c
        .query_one(
            "SELECT count(*) AS n FROM coordination_messages WHERE recipient_agent_id=$1 \
             AND acknowledged_at IS NULL AND expires_at>$2",
            &[&agent_id, &now()],
        )
        .await?
        .get(0);
    let rows = c
        .query(
            "SELECT m.message_id,m.sender_agent_id,m.sender_principal,m.created_at,m.text,\
             a.label AS sender_label \
             FROM coordination_messages m LEFT JOIN coordination_agents a ON a.agent_id=m.sender_agent_id \
             WHERE m.recipient_agent_id=$1 AND m.acknowledged_at IS NULL AND m.expires_at>$2 \
             AND m.origin<>$3 ORDER BY m.recipient_sequence LIMIT $4",
            &[&agent_id, &now(), &MAINTAINER_ORIGIN, &PREVIEW_LIMIT],
        )
        .await?;
    let preview: Vec<Value> = rows
        .iter()
        .map(|r| {
            let sender_principal: String = r.get(2);
            let label: Option<String> = r.get(5);
            let shown = if sender_principal == DAEMON_PRINCIPAL {
                DAEMON_PRINCIPAL.to_string()
            } else {
                let l = label.unwrap_or_default();
                if text::reserved_name(&l) { String::new() } else { l }
            };
            let body: Option<String> = r.get(4);
            obj(vec![
                ("message_id", json!(r.get::<_, String>(0))),
                ("sender_agent_id", json!(r.get::<_, String>(1))),
                ("sender_principal", json!(sender_principal)),
                ("sender_label", json!(shown)),
                ("created_at", float_value(r.get(3))),
                ("excerpt", json!(text::excerpt(body.as_deref()))),
            ])
        })
        .collect();
    let maintainer: i64 = c
        .query_one(
            "SELECT count(*) AS n FROM coordination_messages WHERE \
             recipient_agent_id=$1 AND acknowledged_at IS NULL AND expires_at>$2 \
             AND origin=$3 AND sender_principal=$4",
            &[&agent_id, &now(), &MAINTAINER_ORIGIN, &MAINTAINER_PRINCIPAL],
        )
        .await?
        .get(0);
    Ok(json!({"pending_count": pending, "pending_preview": preview,
              "maintainer_pending": maintainer}))
}

/// `_receipt`.
fn receipt(row: &MessageRow) -> Map<String, Value> {
    let state = if row.acknowledged_at.is_some() {
        "acknowledged"
    } else if row.expires_at <= now() {
        "expired"
    } else if row.attempt_at.is_some() {
        "attempted"
    } else {
        "queued"
    };
    let mut m = Map::new();
    m.insert("message_id".into(), json!(row.message_id));
    m.insert("state".into(), json!(state));
    m.insert("created_at".into(), float_value(row.created_at));
    m.insert("expires_at".into(), float_value(row.expires_at));
    m.insert("acknowledged_at".into(), opt_f64(row.acknowledged_at));
    m
}

/// `_wake`: `live` when the recipient's attached adapter opted into wake.
fn path_for(agent: &AgentRow, now: f64) -> &'static str {
    let attached = agent.attachment_id.is_some() && agent.lease_until.unwrap_or(0.0) > now;
    if attached && agent.wake_enabled { "live" } else { "pull" }
}

/// `_send_result`.
fn send_result(
    to: Option<&str>,
    fanout: bool,
    rows: &[MessageRow],
    agents: &std::collections::HashMap<String, AgentRow>,
    now: f64,
) -> Value {
    let mut sorted: Vec<&MessageRow> = rows.iter().collect();
    sorted.sort_by(|a, b| a.recipient_agent_id.cmp(&b.recipient_agent_id));
    let receipts: Vec<Value> = sorted
        .iter()
        .map(|row| {
            let mut m = receipt(row);
            m.insert("recipient_agent_id".into(), json!(row.recipient_agent_id));
            m.insert("wake".into(), row.wake.clone().unwrap_or(Value::Null));
            m.insert(
                "path".into(),
                json!(agents.get(&row.recipient_agent_id).map_or("pull", |a| path_for(a, now))),
            );
            Value::Object(m)
        })
        .collect();
    if !fanout {
        return receipts.into_iter().next().unwrap_or(Value::Null);
    }
    json!({"to": to, "recipients": receipts.len(), "receipts": receipts})
}

/// `_fanout_target`.
fn fanout_target(to: &str) -> Result<Option<(&'static str, Option<String>)>> {
    if to == "all" {
        return Ok(Some(("all", None)));
    }
    if let Some(name) = to.strip_prefix("project:") {
        check_string(name, super::store::MAX_SCOPE, "recipient", false)?;
        return Ok(Some(("project", Some(name.to_string()))));
    }
    Ok(None)
}

/// `_message_ids`: one id, comma-separated ids, or a JSON array of strings.
pub fn message_ids(value: &Value) -> Result<(Vec<String>, bool)> {
    let raw = check_value(value, 8192, "message_id", false)?;
    let s = text::py_strip(raw);
    let (items, batch): (Vec<String>, bool) = if s.starts_with('[') {
        let parsed: Value = serde_json::from_str(s).map_err(|_| BoardError::code("invalid_message_id"))?;
        let Some(list) = parsed.as_array().filter(|l| !l.is_empty()) else {
            return Err(BoardError::code("invalid_message_id"));
        };
        if !list.iter().all(Value::is_string) {
            return Err(BoardError::code("invalid_message_id"));
        }
        (
            list.iter().map(|v| v.as_str().expect("str").to_string()).collect(),
            true,
        )
    } else {
        (s.split(',').map(str::to_string).collect(), s.contains(','))
    };
    let mut ids: Vec<String> = Vec::new();
    for item in items {
        let item = text::py_strip(&item).to_string();
        if item.is_empty()
            || text::py_len(&item) > 120
            || item.chars().any(|c| !ID_CHARS.contains(c))
        {
            return Err(BoardError::code("invalid_message_id"));
        }
        if !ids.contains(&item) {
            ids.push(item);
        }
    }
    if ids.len() > 50 {
        return Err(BoardError::code("invalid_message_id"));
    }
    Ok((ids, batch))
}

/// The inputs of one `send` call (already checked for types by dispatch).
pub struct SendArgs<'a> {
    pub to: Option<&'a Value>,
    pub text: &'a Value,
    pub request_id: &'a Value,
    pub reply_to: Option<&'a Value>,
    pub clears: Option<&'a Value>,
    pub urgent: Option<&'a Value>,
    pub hlc: String,
    pub expected_writer_epoch: Option<i64>,
    pub enforce_writer_epoch: bool,
    pub notice: bool,
    pub maintainer_proof: Option<Value>,
}

impl Store {
    async fn resolve_agent(
        &self,
        c: &impl GenericClient,
        value: &str,
        missing: &str,
        ambiguous: &str,
    ) -> Result<String> {
        if !is_id_prefix(value) {
            return Ok(value.to_string());
        }
        let ids: Vec<String> = c
            .query(
                "SELECT agent_id FROM coordination_agents WHERE credential_hash \
                 IS NOT NULL AND agent_id LIKE $1 ORDER BY agent_id LIMIT $2",
                &[&format!("{value}%"), &((AMBIGUITY_LISTED + 1) as i64)],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        Ok(resolved(ids, Some(missing), ambiguous)?.expect("missing raises"))
    }

    async fn resolve_message(
        &self,
        c: &impl GenericClient,
        value: &str,
        scope_sender: Option<&str>,
        recipient: &str,
        missing: &str,
        ambiguous: &str,
    ) -> Result<String> {
        if !is_id_prefix(value) {
            return Ok(value.to_string());
        }
        let limit = (AMBIGUITY_LISTED + 1) as i64;
        let like = format!("{value}%");
        let rows = match scope_sender {
            Some(sender) => {
                c.query(
                    "SELECT message_id FROM coordination_messages WHERE sender_agent_id=$1 \
                     AND recipient_agent_id=$2 AND message_id LIKE $3 ORDER BY message_id LIMIT $4",
                    &[&sender, &recipient, &like, &limit],
                )
                .await?
            }
            None => {
                c.query(
                    "SELECT message_id FROM coordination_messages WHERE recipient_agent_id=$1 \
                     AND message_id LIKE $2 ORDER BY message_id LIMIT $3",
                    &[&recipient, &like, &limit],
                )
                .await?
            }
        };
        let ids = rows.iter().map(|r| r.get(0)).collect();
        Ok(resolved(ids, Some(missing), ambiguous)?.expect("missing raises"))
    }

    fn fanout_scope(
        target: &(&'static str, Option<String>),
        only: bool,
    ) -> (String, usize) {
        let mut clauses = vec![
            "credential_hash IS NOT NULL",
            "agent_id<>$1",
            "attachment_id IS NOT NULL",
            "coalesce(lease_until,0)>$2",
            "last_activity>$3",
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
        let mut n = 3;
        if target.0 == "project" {
            n += 1;
            clauses.push(format!("project=${n}"));
        }
        if only {
            n += 1;
            clauses.push(format!("agent_id=ANY(${n})"));
        }
        (clauses.join(" AND "), n)
    }

    async fn fanout_rows(
        &self,
        c: &impl GenericClient,
        sender: &str,
        target: &(&'static str, Option<String>),
        now: f64,
        only: Option<&Vec<String>>,
    ) -> Result<Vec<AgentRow>> {
        let (where_, n) = Self::fanout_scope(target, only.is_some());
        let floor = now - ATTACHED_IDLE_WINDOW;
        let limit = FANOUT_MAX + 1;
        let sql = format!(
            "SELECT * FROM coordination_agents WHERE {where_} ORDER BY agent_id LIMIT ${}",
            n + 1
        );
        let project = target.1.clone().unwrap_or_default();
        let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = vec![&sender, &now, &floor];
        if target.0 == "project" {
            params.push(&project);
        }
        if let Some(only) = only {
            params.push(only);
        }
        params.push(&limit);
        Ok(c.query(&sql, &params)
            .await?
            .iter()
            .map(AgentRow::from_row)
            .collect())
    }

    async fn fanout_count(
        &self,
        c: &impl GenericClient,
        sender: &str,
        target: &(&'static str, Option<String>),
        now: f64,
    ) -> Result<i64> {
        let (where_, _) = Self::fanout_scope(target, false);
        let floor = now - ATTACHED_IDLE_WINDOW;
        let project = target.1.clone().unwrap_or_default();
        let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = vec![&sender, &now, &floor];
        if target.0 == "project" {
            params.push(&project);
        }
        Ok(c
            .query_one(
                &format!("SELECT count(*) AS n FROM coordination_agents WHERE {where_}"),
                &params,
            )
            .await?
            .get(0))
    }

    /// `_replayed`.
    #[allow(clippy::too_many_arguments)]
    async fn replayed(
        &self,
        c: &impl GenericClient,
        existing: &[MessageRow],
        to: Option<&str>,
        fanout: bool,
        text: &str,
        reply_to: Option<&str>,
        clears: Option<&str>,
        urgent: bool,
        now: f64,
    ) -> Result<Value> {
        let first = &existing[0];
        let fingerprint = if !fanout {
            let target = first.recipient_agent_id.as_str();
            let parent = first.reply_to.as_deref();
            if let Some(to) = to
                && to != target
                && !(is_id_prefix(to) && target.starts_with(to))
            {
                return Err(BoardError::code("request_conflict"));
            }
            if let Some(r) = reply_to
                && Some(r) != parent
                && !(parent.is_some_and(|p| !p.is_empty() && is_id_prefix(r) && p.starts_with(r)))
            {
                return Err(BoardError::code("request_conflict"));
            }
            canon::fingerprint(
                target,
                text,
                if reply_to.is_none() { None } else { parent },
                None,
                clears,
                urgent,
            )
        } else {
            canon::fingerprint(to.unwrap_or(""), text, reply_to, None, clears, urgent)
        };
        if existing.iter().any(|row| row.fingerprint != fingerprint) {
            return Err(BoardError::code("request_conflict"));
        }
        let ids: Vec<String> = existing.iter().map(|r| r.recipient_agent_id.clone()).collect();
        let agents = c
            .query("SELECT * FROM coordination_agents WHERE agent_id=ANY($1)", &[&ids])
            .await?
            .iter()
            .map(|r| {
                let a = AgentRow::from_row(r);
                (a.agent_id.clone(), a)
            })
            .collect();
        Ok(send_result(to, fanout, existing, &agents, now))
    }

    /// `send` (spec BS6).
    pub async fn send(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        args: SendArgs<'_>,
    ) -> Result<Value> {
        if args.notice && principal != DAEMON_PRINCIPAL {
            return Err(BoardError::code("invalid_request"));
        }
        let maintainer = args.maintainer_proof.is_some();
        if maintainer != (principal == MAINTAINER_PRINCIPAL)
            || (maintainer && (args.notice || !args.maintainer_proof.as_ref().is_some_and(Value::is_object)))
        {
            return Err(BoardError::code("invalid_request"));
        }
        let to = args.to.filter(|v| !v.is_null());
        let reply_to_v = args.reply_to.filter(|v| !v.is_null());
        let to: Option<String> = match to {
            None => {
                if reply_to_v.is_none() || maintainer {
                    return Err(BoardError::code("invalid_recipient"));
                }
                None
            }
            Some(v) => {
                let s = check_value(v, 120, "recipient", false)?;
                if RESERVED_SENDERS.contains(&text::casefold(text::py_strip(s)).as_str()) {
                    return Err(BoardError::code("recipient_reserved"));
                }
                Some(s.to_string())
            }
        };
        let request_id = check_value(args.request_id, 120, "request_id", false)?.to_string();
        check_string(&args.hlc, 120, "hlc", true)?;
        let clears = match args.clears.filter(|v| !v.is_null()) {
            None => None,
            Some(v) => {
                let c = check_value(v, MAX_CLEARS, "clears", false)?;
                refuse_secret(c, "clears")?;
                Some(c.to_string())
            }
        };
        let urgent = match args.urgent {
            None => false,
            Some(Value::Bool(b)) => *b,
            Some(_) => return Err(BoardError::code("invalid_urgent")),
        };
        let mut stamp: Option<(i64, i64)> = None;
        if !args.hlc.is_empty() {
            let parts: Vec<&str> = args.hlc.split(':').collect();
            let parsed: Option<Vec<i64>> = parts.iter().map(|p| p.parse::<i64>().ok()).collect();
            match parsed {
                Some(v) if v.len() == 2 && v.iter().all(|x| *x >= 0) => stamp = Some((v[0], v[1])),
                _ => return Err(BoardError::code("invalid_hlc")),
            }
        }
        let problem = match args.text {
            Value::String(t) if !text::py_strip(t).is_empty() => {
                if t.contains('\0') {
                    Some("text must not contain NUL characters".to_string())
                } else if t.len() > MAX_TEXT_BYTES {
                    Some(format!(
                        "text is {} UTF-8 bytes; the limit is {MAX_TEXT_BYTES} bytes, not characters",
                        t.len()
                    ))
                } else {
                    None
                }
            }
            _ => Some("text must be nonblank".to_string()),
        };
        if let Some(p) = problem {
            return Err(BoardError::detail("invalid_text", p).with_param("text"));
        }
        let text_body = args.text.as_str().expect("checked above").to_string();
        refuse_secret(&text_body, "text")?;
        refuse_secret(&request_id, "request_id")?;
        let mut reply_to: Option<String> = match reply_to_v {
            None => None,
            Some(v) => Some(check_value(v, 120, "reply", false)?.to_string()),
        };
        let fanout = match &to {
            None => None,
            Some(t) => fanout_target(t)?,
        };
        if fanout.is_some() && reply_to.is_some() {
            return Err(BoardError::code("invalid_reply"));
        }
        if maintainer && (fanout.is_some() || !to.as_deref().is_some_and(is_full_agent_id)) {
            return Err(BoardError::code("invalid_recipient"));
        }
        let tx = client.transaction().await?;
        let now = now();
        let sender = self.auth(&tx, principal, agent_id, credential, false).await?;
        if sender.parent_thread.is_some() {
            return Err(BoardError::detail("child_send_refused", CHILD_SEND_REFUSED));
        }
        let existing = self.existing(&tx, agent_id, &request_id).await?;
        if !existing.is_empty() {
            return self
                .replayed(
                    &tx,
                    &existing,
                    to.as_deref(),
                    fanout.is_some(),
                    &text_body,
                    reply_to.as_deref(),
                    clears.as_deref(),
                    urgent,
                    now,
                )
                .await;
        }
        let targets: Vec<String> = if to.is_none() {
            let rt = self
                .resolve_message(
                    &tx,
                    reply_to.as_deref().expect("reply required"),
                    None,
                    agent_id,
                    "invalid_reply",
                    "ambiguous_reply",
                )
                .await?;
            let routed = tx
                .query_opt(
                    "SELECT sender_agent_id FROM coordination_messages \
                     WHERE message_id=$1 AND recipient_agent_id=$2",
                    &[&rt, &agent_id],
                )
                .await?;
            reply_to = Some(rt);
            let Some(routed) = routed else {
                return Err(BoardError::code("invalid_reply"));
            };
            vec![routed.get(0)]
        } else if fanout.is_none() {
            let target = self
                .resolve_agent(
                    &tx,
                    to.as_deref().expect("direct send"),
                    "recipient_not_found",
                    "ambiguous_recipient",
                )
                .await?;
            if let Some(rt) = &reply_to {
                reply_to = Some(
                    self.resolve_message(
                        &tx,
                        rt,
                        Some(&target),
                        agent_id,
                        "invalid_reply",
                        "ambiguous_reply",
                    )
                    .await?,
                );
            }
            vec![target]
        } else {
            let target = fanout.as_ref().expect("burst");
            let rows = self.fanout_rows(&tx, agent_id, target, now, None).await?;
            if rows.len() as i64 > FANOUT_MAX {
                let n = self.fanout_count(&tx, agent_id, target, now).await?;
                return Err(BoardError::detail(
                    "fanout_too_large",
                    format!("{n} recipients, limit {FANOUT_MAX}"),
                ));
            }
            rows.into_iter().map(|r| r.agent_id).collect()
        };
        let fingerprint = canon::fingerprint(
            if fanout.is_some() {
                to.as_deref().expect("burst address")
            } else {
                &targets[0]
            },
            &text_body,
            reply_to.as_deref(),
            None,
            clears.as_deref(),
            urgent,
        );
        let mut lock_ids: Vec<String> = targets.clone();
        lock_ids.push(agent_id.to_string());
        lock_ids.sort();
        lock_ids.dedup();
        tx.query(
            "SELECT agent_id FROM coordination_agents WHERE agent_id=ANY($1) \
             ORDER BY agent_id FOR UPDATE",
            &[&lock_ids],
        )
        .await?;
        let sender = self.auth(&tx, principal, agent_id, credential, false).await?;
        let existing = self.existing(&tx, agent_id, &request_id).await?;
        if !existing.is_empty() {
            return self
                .replayed(
                    &tx,
                    &existing,
                    to.as_deref(),
                    fanout.is_some(),
                    &text_body,
                    reply_to.as_deref(),
                    clears.as_deref(),
                    urgent,
                    now,
                )
                .await;
        }
        if args.enforce_writer_epoch {
            let Some(expected) = args.expected_writer_epoch.filter(|e| *e >= 1) else {
                return Err(BoardError::ClockChanged);
            };
            let row = tx
                .query_opt(
                    "SELECT value FROM meta WHERE key=$1 FOR SHARE",
                    &[&WRITER_EPOCH_META_KEY],
                )
                .await?;
            let epoch = row
                .map(|r| r.get::<_, Value>(0))
                .and_then(|v| canon::py_int_i64(&v))
                .filter(|e| *e >= 1)
                .ok_or_else(|| BoardError::Internal("invalid writer lease epoch".into()))?;
            if epoch != expected {
                return Err(BoardError::ClockChanged);
            }
        }
        let recipients: Vec<AgentRow> = if fanout.is_none() {
            let Some(row) = tx
                .query_opt(
                    "SELECT * FROM coordination_agents WHERE agent_id=$1 \
                     AND credential_hash IS NOT NULL",
                    &[&targets[0]],
                )
                .await?
            else {
                return Err(BoardError::code("recipient_not_found"));
            };
            vec![AgentRow::from_row(&row)]
        } else {
            let rows = self
                .fanout_rows(&tx, agent_id, fanout.as_ref().expect("burst"), now, Some(&targets))
                .await?;
            if rows.is_empty() {
                return Err(BoardError::code("no_recipients"));
            }
            rows
        };
        let expiry = now + MESSAGE_TTL;
        let mut parent: Option<(String, String, String, String)> = None;
        if let Some(rt) = &reply_to {
            let row = tx
                .query_opt(
                    "SELECT sender_agent_id,recipient_agent_id,origin,\
                     sender_principal FROM coordination_messages WHERE message_id=$1",
                    &[rt],
                )
                .await?;
            let ok = row.map(|r| (r.get(0), r.get(1), r.get(2), r.get(3)));
            match ok {
                Some(p) if p.0 == recipients[0].agent_id && p.1 == agent_id => parent = Some(p),
                _ => return Err(BoardError::code("invalid_reply")),
            }
        }
        for recipient in &recipients {
            if RESERVED_SENDERS.contains(&recipient.principal.as_str())
                && !(to.is_none()
                    && recipient.principal == MAINTAINER_PRINCIPAL
                    && parent.as_ref().is_some_and(|p| {
                        p.2 == MAINTAINER_ORIGIN && p.3 == MAINTAINER_PRINCIPAL
                    }))
            {
                return Err(BoardError::code("recipient_reserved"));
            }
            if maintainer && recipient.parent_thread.is_some() {
                return Err(BoardError::code("recipient_reserved"));
            }
        }
        let sent: i64 = tx
            .query_one(
                "SELECT count(DISTINCT request_id) AS n FROM coordination_messages \
                 WHERE sender_agent_id=$1 AND created_at>$2",
                &[&agent_id, &(now - 60.0)],
            )
            .await?
            .get(0);
        if sent >= SEND_RATE {
            return Err(BoardError::code("rate_limited"));
        }
        for recipient in &recipients {
            let pending: i64 = if recipient.principal == MAINTAINER_PRINCIPAL {
                tx.query_one(
                    "SELECT count(*) AS n FROM coordination_messages WHERE \
                     recipient_agent_id=$1 AND acknowledged_at IS NULL AND expires_at>$2 \
                     AND sender_agent_id=$3",
                    &[&recipient.agent_id, &now, &agent_id],
                )
                .await?
                .get(0)
            } else {
                tx.query_one(
                    "SELECT count(*) AS n FROM coordination_messages WHERE \
                     recipient_agent_id=$1 AND acknowledged_at IS NULL AND expires_at>$2",
                    &[&recipient.agent_id, &now],
                )
                .await?
                .get(0)
            };
            if pending >= MAX_PENDING {
                let short: String = recipient.agent_id.chars().take(12).collect();
                return Err(BoardError::detail("queue_full", short));
            }
        }
        tx.execute(
            "UPDATE coordination_agents SET last_activity=$1 WHERE agent_id=$2",
            &[&now, &agent_id],
        )
        .await?;
        let mut rows = Vec::new();
        let mut events = Vec::new();
        for recipient in &recipients {
            let seq = recipient.next_sequence + 1;
            let message_id = uuid_hex();
            let mut decision = if args.notice {
                let mut m = Map::new();
                m.insert("decision".into(), json!("hinted"));
                m.insert("reason".into(), json!("daemon_notice"));
                m
            } else {
                wake::wake_decision(
                    &tx,
                    &self.wake,
                    &self.maintainer_principals,
                    &sender,
                    recipient,
                    now,
                    clears.as_deref(),
                    urgent,
                    &message_id,
                    maintainer,
                )
                .await?
            };
            let ring = decision.remove("_ring");
            let decision_word = decision["decision"].clone();
            let wake_value = Value::Object(decision);
            tx.execute(
                "UPDATE coordination_agents SET next_sequence=$1 WHERE agent_id=$2",
                &[&seq, &recipient.agent_id],
            )
            .await?;
            let origin = if maintainer { MAINTAINER_ORIGIN } else { MESSAGE_ORIGIN };
            let row = tx
                .query_one(
                    "INSERT INTO coordination_messages (message_id,sender_agent_id,recipient_agent_id,\
                     sender_principal,project,task,text,reply_to,request_id,fingerprint,\
                     recipient_sequence,hlc,created_at,expires_at,wake,origin,maintainer_proof) \
                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) RETURNING *",
                    &[
                        &message_id,
                        &agent_id,
                        &recipient.agent_id,
                        &principal,
                        &sender.project,
                        &sender.task,
                        &text_body,
                        &reply_to,
                        &request_id,
                        &fingerprint,
                        &seq,
                        &args.hlc,
                        &now,
                        &expiry,
                        &wake_value,
                        &origin,
                        &args.maintainer_proof,
                    ],
                )
                .await?;
            rows.push(MessageRow::from_row(&row));
            let salt = random_hex(BODY_SALT_BYTES);
            let mut payload = Map::new();
            payload.insert(
                "text_commitment".into(),
                json!(canon::body_commitment(&salt, &text_body)),
            );
            payload.insert("reply_to".into(), opt_str(reply_to.as_deref()));
            payload.insert("request_id".into(), json!(request_id));
            payload.insert("recipient_sequence".into(), json!(seq));
            payload.insert("expires_at".into(), float_value(expiry));
            payload.insert("wake".into(), decision_word);
            if let Some(ring) = ring {
                payload.insert("wake_reason".into(), ring);
            }
            if fanout.is_some() {
                payload.insert(
                    "fanout".into(),
                    json!({"to": to, "recipients": recipients.len()}),
                );
            }
            if maintainer {
                payload.insert("origin".into(), json!(MAINTAINER_ORIGIN));
            }
            events.push(
                Event::new("send", &Value::Object(payload))
                    .principal(principal)
                    .agent(agent_id)
                    .recipient(&recipient.agent_id)
                    .scope(&sender.project, &sender.task)
                    .message(&message_id)
                    .hlc(&args.hlc)
                    .body(&text_body, &salt),
            );
        }
        if let Some((phys, logical)) = stamp {
            tx.execute(
                "INSERT INTO meta (key,value) VALUES ($1,$2) ON CONFLICT (key) \
                 DO UPDATE SET value=EXCLUDED.value WHERE \
                 ((meta.value->>0)::bigint,(meta.value->>1)::bigint) < \
                 ((EXCLUDED.value->>0)::bigint,(EXCLUDED.value->>1)::bigint)",
                &[&HLC_META_KEY, &json!([phys, logical])],
            )
            .await?;
        }
        append(&tx, events, now, None).await?;
        tx.commit().await?;
        let agents = recipients
            .into_iter()
            .map(|r| (r.agent_id.clone(), r))
            .collect();
        Ok(send_result(to.as_deref(), fanout.is_some(), &rows, &agents, now))
    }

    async fn existing(
        &self,
        c: &impl GenericClient,
        agent_id: &str,
        request_id: &str,
    ) -> Result<Vec<MessageRow>> {
        Ok(c
            .query(
                "SELECT * FROM coordination_messages WHERE sender_agent_id=$1 AND request_id=$2",
                &[&agent_id, &request_id],
            )
            .await?
            .iter()
            .map(MessageRow::from_row)
            .collect())
    }

    /// `receive`.
    pub async fn receive(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        after: Option<&Value>,
        limit: Option<&Value>,
        for_delivery: bool,
    ) -> Result<Value> {
        let row = self.auth(&*client, principal, agent_id, credential, false).await?;
        let limit = check_limit(limit.unwrap_or(&json!(50)))?;
        let mut seq: i64 = 0;
        if let Some(after) = after.filter(|v| !v.is_null()) {
            let parsed = after.as_str().and_then(|s| {
                let (mailbox, raw) = s.split_once(':')?;
                let n = py_int_str(raw)?;
                (mailbox == agent_id && n >= 0).then_some(n)
            });
            let Some(n) = parsed else {
                return Err(BoardError::code("invalid_cursor"));
            };
            seq = n;
            if seq > row.next_sequence {
                return Err(BoardError::detail("invalid_cursor", "cursor_ahead"));
            }
        }
        let now = now();
        let rows: Vec<MessageRow> = if for_delivery {
            client
                .query(
                    "SELECT * FROM coordination_messages WHERE recipient_agent_id=$1 \
                     AND recipient_sequence>$2 AND acknowledged_at IS NULL AND expires_at>$3 \
                     AND attempts<$4 AND sender_principal<>$5 AND (wake IS NULL OR \
                     wake->>'decision' IN ('rung','hinted') OR \
                     (wake->>'queued'='true' AND NOT EXISTS (SELECT 1 FROM \
                     coordination_wakes w WHERE w.recipient_agent_id=\
                     coordination_messages.recipient_agent_id AND \
                     w.message_id=coordination_messages.message_id AND \
                     w.decision='nudged'))) AND repudiated_at IS NULL \
                     ORDER BY recipient_sequence LIMIT $6",
                    &[&agent_id, &seq, &now, &MAX_ATTEMPTS, &DAEMON_PRINCIPAL, &limit],
                )
                .await?
        } else {
            client
                .query(
                    "SELECT * FROM coordination_messages WHERE recipient_agent_id=$1 \
                     AND recipient_sequence>$2 AND acknowledged_at IS NULL AND expires_at>$3 \
                     ORDER BY recipient_sequence LIMIT $4",
                    &[&agent_id, &seq, &now, &limit],
                )
                .await?
        }
        .iter()
        .map(MessageRow::from_row)
        .collect();
        let continuity = client
            .query_one(
                "SELECT count(*) FILTER (WHERE recipient_sequence>$1) AS retained_after,\
                 count(*) FILTER (WHERE recipient_sequence>$1 AND acknowledged_at IS NULL \
                 AND expires_at<=$2) AS expired_unacknowledged,\
                 (SELECT next_sequence FROM coordination_agents WHERE agent_id=$3) AS high_water \
                 FROM coordination_messages WHERE recipient_agent_id=$3 \
                 AND (created_at>$4 OR expires_at>$2)",
                &[&seq, &now, &agent_id, &(now - DEDUPE_RETENTION)],
            )
            .await?;
        let retained_after: i64 = continuity.get(0);
        let expired: i64 = continuity.get(1);
        let high_water: Option<i64> = continuity.get(2);
        let high_water = high_water.unwrap_or(0);
        let gap = retained_after < high_water - seq;
        let status = if !rows.is_empty() {
            "messages"
        } else if expired > 0 {
            "expired_unacknowledged"
        } else if gap {
            "retention_gap"
        } else {
            "empty"
        };
        let unread: Vec<String> = rows
            .iter()
            .filter(|r| r.first_read_at.is_none())
            .map(|r| r.message_id.clone())
            .collect();
        if !unread.is_empty() {
            let tx = client.transaction().await?;
            tx.query(
                "SELECT 1 FROM coordination_agents WHERE agent_id=$1 FOR UPDATE",
                &[&agent_id],
            )
            .await?;
            let first: std::collections::HashSet<String> = tx
                .query(
                    "UPDATE coordination_messages SET first_read_at=$1 WHERE message_id=ANY($2) \
                     AND first_read_at IS NULL RETURNING message_id",
                    &[&now, &unread],
                )
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect();
            let path = if for_delivery { "delivery" } else { "pull" };
            let events = rows
                .iter()
                .filter(|r| first.contains(&r.message_id))
                .map(|r| {
                    Event::new(
                        "read",
                        &json!({"path": path, "sender_agent_id": r.sender_agent_id}),
                    )
                    .principal(principal)
                    .agent(agent_id)
                    .recipient(agent_id)
                    .scope(&r.project, &r.task)
                    .message(&r.message_id)
                })
                .collect();
            append(&tx, events, now, None).await?;
            tx.commit().await?;
        }
        let messages: Vec<Value> = rows
            .iter()
            .map(|r| {
                let mut m = Map::new();
                m.insert("message_id".into(), json!(r.message_id));
                m.insert("sender_agent_id".into(), json!(r.sender_agent_id));
                m.insert("sender_principal".into(), json!(r.sender_principal));
                m.insert("recipient_agent_id".into(), json!(r.recipient_agent_id));
                m.insert("project".into(), json!(r.project));
                m.insert("task".into(), json!(r.task));
                m.insert("text".into(), opt_str(r.text.as_deref()));
                m.insert("reply_to".into(), opt_str(r.reply_to.as_deref()));
                m.insert("recipient_sequence".into(), json!(r.recipient_sequence));
                m.insert("hlc".into(), json!(r.hlc));
                m.insert("created_at".into(), float_value(r.created_at));
                m.insert("expires_at".into(), float_value(r.expires_at));
                for (k, v) in message_origin(r) {
                    m.insert(k, v);
                }
                Value::Object(m)
            })
            .collect();
        let last = rows.last().map_or(seq, |r| r.recipient_sequence);
        Ok(json!({
            "messages": messages,
            "after": format!("{agent_id}:{last}"),
            "continuity": {"status": status, "expired_unacknowledged": expired,
                           "metadata_gap": gap, "high_water": high_water,
                           "metadata_retention_seconds": DEDUPE_RETENTION as i64},
        }))
    }

    /// `ack`.
    pub async fn ack(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        message_id: &Value,
    ) -> Result<Value> {
        let (given, batch) = message_ids(message_id)?;
        let tx = client.transaction().await?;
        self.auth(&tx, principal, agent_id, credential, true).await?;
        let now = now();
        let prefixes: Vec<String> = given.iter().filter(|g| is_id_prefix(g)).cloned().collect();
        let mut matches: std::collections::HashMap<String, Vec<String>> =
            prefixes.iter().map(|p| (p.clone(), Vec::new())).collect();
        if !prefixes.is_empty() {
            let likes: Vec<String> = prefixes.iter().map(|p| format!("{p}%")).collect();
            for r in tx
                .query(
                    "SELECT message_id FROM coordination_messages WHERE recipient_agent_id=$1 \
                     AND message_id LIKE ANY($2) ORDER BY message_id",
                    &[&agent_id, &likes],
                )
                .await?
            {
                let id: String = r.get(0);
                for p in &prefixes {
                    if id.starts_with(p.as_str()) {
                        matches.get_mut(p).expect("prefix").push(id.clone());
                    }
                }
            }
        }
        let mut ids: Vec<String> = Vec::new();
        let mut resolved_map: std::collections::HashMap<String, String> =
            std::collections::HashMap::new();
        for one in &given {
            let full = match matches.get(one) {
                None => Some(one.clone()),
                Some(found) => resolved(
                    found.clone(),
                    if batch { None } else { Some("message_not_found") },
                    "ambiguous_message_id",
                )?,
            };
            let r = full.unwrap_or_else(|| one.clone());
            resolved_map.insert(one.clone(), r.clone());
            if !ids.contains(&r) {
                ids.push(r);
            }
        }
        let mut found: std::collections::HashMap<String, MessageRow> = tx
            .query(
                "SELECT * FROM coordination_messages WHERE recipient_agent_id=$1 \
                 AND message_id = ANY($2) FOR UPDATE",
                &[&agent_id, &ids],
            )
            .await?
            .iter()
            .map(|r| {
                let m = MessageRow::from_row(r);
                (m.message_id.clone(), m)
            })
            .collect();
        let pending: Vec<String> = ids
            .iter()
            .filter(|id| found.get(*id).is_some_and(|m| m.acknowledged_at.is_none()))
            .cloned()
            .collect();
        if !pending.is_empty() {
            tx.execute(
                "UPDATE coordination_agents SET last_activity=$1 WHERE agent_id=$2",
                &[&now, &agent_id],
            )
            .await?;
            for r in tx
                .query(
                    "UPDATE coordination_messages SET acknowledged_at=$1 \
                     WHERE message_id = ANY($2) RETURNING *",
                    &[&now, &pending],
                )
                .await?
            {
                let m = MessageRow::from_row(&r);
                found.insert(m.message_id.clone(), m);
            }
        }
        let missing: Vec<String> = given
            .iter()
            .filter(|g| !found.contains_key(&resolved_map[*g]))
            .cloned()
            .collect();
        if !batch && !missing.is_empty() {
            return Err(BoardError::code("message_not_found"));
        }
        let events = pending
            .iter()
            .map(|one| {
                let m = &found[one];
                Event::new("ack", &json!({"sender_agent_id": m.sender_agent_id}))
                    .principal(principal)
                    .agent(agent_id)
                    .recipient(agent_id)
                    .scope(&m.project, &m.task)
                    .message(one)
            })
            .collect();
        append(&tx, events, now, None).await?;
        tx.commit().await?;
        let receipts: Vec<Value> = ids
            .iter()
            .filter_map(|id| found.get(id).map(|m| Value::Object(receipt(m))))
            .collect();
        if batch {
            Ok(json!({"receipts": receipts, "missing": missing}))
        } else {
            Ok(receipts.into_iter().next().unwrap_or(Value::Null))
        }
    }

    /// `mark_attempt`.
    pub async fn mark_attempt(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        message_id: &Value,
        attachment_id: &Value,
        generation: i64,
    ) -> Result<Value> {
        let tx = client.transaction().await?;
        let agent = self.auth(&tx, principal, agent_id, credential, true).await?;
        Store::attachment(&agent, attachment_id, generation)?;
        if !agent.wake_enabled {
            return Err(BoardError::code("wake_disabled"));
        }
        // A non-string id matches no row, as the Python query would.
        let id = message_id.as_str().unwrap_or("");
        let Some(row) = tx
            .query_opt(
                "SELECT * FROM coordination_messages WHERE message_id=$1 \
                 AND recipient_agent_id=$2 AND acknowledged_at IS NULL AND expires_at>$3 \
                 FOR UPDATE",
                &[&id, &agent_id, &now()],
            )
            .await?
        else {
            return Err(BoardError::code("message_not_pending"));
        };
        let mut row = MessageRow::from_row(&row);
        if row.attempt_generation != Some(generation) {
            if row.attempts >= MAX_ATTEMPTS {
                return Err(BoardError::code("attempts_exhausted"));
            }
            let now = now();
            row = MessageRow::from_row(
                &tx.query_one(
                    "UPDATE coordination_messages SET attempt_at=$1,attempt_generation=$2,\
                     attempts=attempts+1 WHERE message_id=$3 RETURNING *",
                    &[&now, &generation, &id],
                )
                .await?,
            );
            append(
                &tx,
                vec![Event::new(
                    "attempt",
                    &json!({"generation": generation, "attempts": row.attempts,
                            "sender_agent_id": row.sender_agent_id}),
                )
                .principal(principal)
                .agent(agent_id)
                .recipient(agent_id)
                .scope(&row.project, &row.task)
                .message(id)],
                now,
                None,
            )
            .await?;
        }
        tx.commit().await?;
        Ok(Value::Object(receipt(&row)))
    }

    /// `history`.
    #[allow(clippy::too_many_arguments)]
    pub async fn history(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        after: Option<&Value>,
        limit: Option<&Value>,
        peer: Option<&Value>,
        audit_retention_days: i64,
    ) -> Result<Value> {
        let tx = client.transaction().await?;
        tx.batch_execute("SET LOCAL statement_timeout = '5s'").await?;
        self.auth(&tx, principal, agent_id, credential, false).await?;
        let limit = check_limit(limit.unwrap_or(&json!(50)))?;
        let mut seq: i64 = 0;
        if let Some(after) = after.filter(|v| !v.is_null()) {
            let parsed = after.as_str().and_then(|s| {
                let mut parts = s.splitn(3, ':');
                let mailbox = parts.next()?;
                let kind = parts.next()?;
                let n = py_int_str(parts.next()?)?;
                (mailbox == agent_id && kind == "history" && n >= 0).then_some(n)
            });
            let Some(n) = parsed else {
                return Err(BoardError::code("invalid_cursor"));
            };
            seq = n;
            let head: Option<i64> = tx
                .query_one(
                    "SELECT max(seq) AS seq FROM (\
                     (SELECT seq FROM coordination_events WHERE event='send' \
                     AND (agent_id,principal)>=($1,$2) AND (agent_id,principal)<=($1,$2) \
                     ORDER BY agent_id DESC,principal DESC,seq DESC LIMIT 1) UNION ALL \
                     (SELECT seq FROM coordination_events WHERE event='send' \
                     AND recipient_agent_id>=$1 AND recipient_agent_id<=$1 \
                     ORDER BY recipient_agent_id DESC,seq DESC LIMIT 1)) heads",
                    &[&agent_id, &principal],
                )
                .await?
                .get(0);
            if head.is_some_and(|h| seq > h) {
                return Err(BoardError::detail("invalid_cursor", "cursor_ahead"));
            }
        }
        let peer = match peer.filter(|v| !v.is_null()) {
            None => None,
            Some(v) => {
                let ok = v.as_str().filter(|s| {
                    !s.is_empty() && text::py_len(s) <= 120 && s.chars().all(|c| ID_CHARS.contains(c))
                });
                let Some(s) = ok else {
                    return Err(BoardError::code("invalid_recipient"));
                };
                Some(s.to_string())
            }
        };
        let now = now();
        let cutoff = (audit_retention_days != 0).then(|| audit_cutoff(now, audit_retention_days));
        // Each direction: (columns, values), then seq and the optional cutoff.
        let mut params: Vec<Box<dyn tokio_postgres::types::ToSql + Sync + Send>> = Vec::new();
        let mut direction = |columns: &[&str],
                             values: &[String],
                             extra_principal: bool|
         -> (String, String) {
            let prefix = format!(
                "({})",
                columns.iter().map(|c| format!("s.{c}")).collect::<Vec<_>>().join(",")
            );
            let mut slots = Vec::new();
            for v in values {
                params.push(Box::new(v.clone()));
                slots.push(format!("${}", params.len()));
            }
            let placeholders = format!("({})", slots.join(","));
            let mut clauses = vec![
                "s.event='send'".to_string(),
                format!("{prefix}>={placeholders}"),
                format!("{prefix}<={placeholders}"),
            ];
            params.push(Box::new(seq));
            clauses.push(format!("s.seq>${}", params.len()));
            if extra_principal {
                params.push(Box::new(principal.to_string()));
                clauses.push(format!("s.principal=${}", params.len()));
            }
            if let Some(cut) = cutoff {
                params.push(Box::new(cut));
                clauses.push(format!("s.created_at>=${}", params.len()));
            }
            let order = format!(
                "{},s.seq",
                columns.iter().map(|c| format!("s.{c}")).collect::<Vec<_>>().join(",")
            );
            (clauses.join(" AND "), order)
        };
        let ((outgoing, out_order), (incoming, in_order)) = match &peer {
            None => (
                direction(
                    &["agent_id", "principal"],
                    &[agent_id.to_string(), principal.to_string()],
                    false,
                ),
                direction(&["recipient_agent_id"], &[agent_id.to_string()], false),
            ),
            Some(p) => (
                direction(
                    &["agent_id", "recipient_agent_id"],
                    &[agent_id.to_string(), p.clone()],
                    true,
                ),
                direction(
                    &["agent_id", "recipient_agent_id"],
                    &[p.clone(), agent_id.to_string()],
                    false,
                ),
            ),
        };
        let page = limit + 1;
        params.push(Box::new(page));
        let page_slot = params.len();
        params.push(Box::new(REPUDIATE_EVENT.to_string()));
        let rep_slot = params.len();
        let sql = format!(
            "WITH page AS (SELECT seq FROM ((SELECT s.seq FROM coordination_events s WHERE \
             {outgoing} ORDER BY {out_order} LIMIT ${page_slot}) UNION \
             (SELECT s.seq FROM coordination_events s WHERE {incoming} \
             ORDER BY {in_order} LIMIT ${page_slot})) sends ORDER BY seq LIMIT ${page_slot}) \
             SELECT p.*,COALESCE(m.acknowledged_at,l.acknowledged_at) AS acknowledged_at,\
             COALESCE(m.first_read_at,l.first_read_at) AS first_read_at,l.redacted_at,\
             COALESCE(m.repudiated_at,l.repudiated_at) AS repudiated_at \
             FROM page ids JOIN coordination_events p ON p.seq=ids.seq \
             LEFT JOIN coordination_messages m ON m.message_id=p.message_id \
             LEFT JOIN LATERAL (SELECT \
             min(e.created_at) FILTER (WHERE e.event='ack' AND \
             e.agent_id=p.recipient_agent_id) AS acknowledged_at,\
             min(e.created_at) FILTER (WHERE e.event='read' AND \
             e.agent_id=p.recipient_agent_id) AS first_read_at,\
             min(e.created_at) FILTER (WHERE e.event='redact') AS redacted_at,\
             min(e.created_at) FILTER (WHERE e.event=${rep_slot} AND e.actor='maintainer') \
             AS repudiated_at \
             FROM coordination_events e WHERE e.message_id=p.message_id \
             AND e.event IN ('ack','read','redact',${rep_slot})) l ON true ORDER BY p.seq"
        );
        let refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
            params.iter().map(|b| b.as_ref() as _).collect();
        let rows = tx.query(&sql, &refs).await?;
        tx.commit().await?;
        let has_more = rows.len() as i64 > limit;
        let rows: Vec<&tokio_postgres::Row> = rows.iter().take(limit as usize).collect();
        let mut messages = Vec::new();
        for sent in &rows {
            let payload_text: String = sent.get("payload");
            let payload: Value = serde_json::from_str(&payload_text).unwrap_or(Value::Null);
            let redacted_at: Option<f64> = sent.get("redacted_at");
            let redacted = redacted_at.is_some();
            let body: Option<String> = sent.get("body");
            let mut text_value = if redacted { None } else { body.map(Value::String) };
            if text_value.is_none() && !redacted {
                text_value = payload.get("text").cloned().filter(|v| !v.is_null());
            }
            let mut expires_at = payload.get("expires_at").and_then(canon::as_f64);
            if let Some(r) = redacted_at {
                expires_at = Some(expires_at.map_or(r, |e| e.min(r)));
            }
            let sender_principal: String = sent.get("principal");
            let maintainer = payload.get("origin").and_then(Value::as_str) == Some(MAINTAINER_ORIGIN)
                && sender_principal == MAINTAINER_PRINCIPAL;
            let acknowledged_at: Option<f64> = sent.get("acknowledged_at");
            let first_read_at: Option<f64> = sent.get("first_read_at");
            let state = if acknowledged_at.is_some() {
                "acknowledged"
            } else if expires_at.is_some_and(|e| e <= now) {
                "expired"
            } else {
                "pending"
            };
            let agent: String = sent.get("agent_id");
            let recipient: Option<String> = sent.get("recipient_agent_id");
            let mut participants = vec![agent.clone()];
            if let Some(r) = &recipient
                && *r != agent
            {
                participants.push(r.clone());
            }
            participants.sort();
            let body_state = if redacted {
                "redacted"
            } else if text_value.is_some() {
                "retained"
            } else {
                "unavailable"
            };
            let mut m = Map::new();
            m.insert("message_id".into(), opt_str(sent.get::<_, Option<String>>("message_id").as_deref()));
            m.insert("sender_agent_id".into(), json!(agent));
            m.insert("sender_principal".into(), json!(sender_principal));
            m.insert("recipient_agent_id".into(), opt_str(recipient.as_deref()));
            m.insert("participants".into(), json!(participants));
            m.insert("project".into(), json!(sent.get::<_, String>("project")));
            m.insert("task".into(), json!(sent.get::<_, String>("task")));
            m.insert("text".into(), text_value.unwrap_or(Value::Null));
            m.insert("body_state".into(), json!(body_state));
            m.insert("reply_to".into(), payload.get("reply_to").cloned().unwrap_or(Value::Null));
            m.insert(
                "recipient_sequence".into(),
                payload.get("recipient_sequence").cloned().unwrap_or(Value::Null),
            );
            m.insert("hlc".into(), json!(sent.get::<_, String>("hlc")));
            m.insert("created_at".into(), float_value(sent.get("created_at")));
            m.insert("expires_at".into(), opt_f64(expires_at));
            m.insert("acknowledged_at".into(), opt_f64(acknowledged_at));
            m.insert("first_read_at".into(), opt_f64(first_read_at));
            m.insert("state".into(), json!(state));
            m.insert(
                "origin".into(),
                json!(if maintainer { MAINTAINER_ORIGIN } else { MESSAGE_ORIGIN }),
            );
            let repudiated_at: Option<f64> = sent.get("repudiated_at");
            if maintainer && let Some(at) = repudiated_at {
                m.insert("repudiated_at".into(), float_value(at));
                if !redacted {
                    m.insert("text".into(), json!(WITHDRAWN_TEXT));
                }
            }
            messages.push(Value::Object(m));
        }
        let last = rows.last().map_or(seq, |r| r.get::<_, i64>("seq"));
        Ok(json!({"messages": messages, "after": format!("{agent_id}:history:{last}"),
                  "has_more": has_more, "retention_days": audit_retention_days,
                  "retained_since": cutoff}))
    }

    /// `prune`.
    pub async fn prune(
        &self,
        client: &mut tokio_postgres::Client,
        audit_retention_days: i64,
    ) -> Result<Value> {
        let now = now();
        let tx = client.transaction().await?;
        let window = "CASE WHEN a.capabilities->>'resumable'='false' THEN $1::double precision \
                      ELSE $2::double precision END";
        let removable = format!(
            "(a.lease_until IS NULL OR a.lease_until<={window}) AND a.last_activity<={window} \
             AND NOT EXISTS (SELECT 1 FROM coordination_messages m \
             WHERE m.sender_agent_id=a.agent_id OR m.recipient_agent_id=a.agent_id)"
        );
        let short = now - lease::EPHEMERAL_AGENT_RETENTION;
        let long = now - lease::AGENT_RETENTION;
        let mut events = Vec::new();
        lease::settle_due(&tx, now, &mut events, None).await?;
        let mut expired: Vec<String> = tx
            .query(
                "UPDATE coordination_messages SET text=NULL \
                 WHERE expires_at<=$1 AND text IS NOT NULL RETURNING message_id",
                &[&now],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        expired.sort();
        let mut removed: Vec<String> = tx
            .query(
                "DELETE FROM coordination_messages WHERE created_at<=$1 \
                 AND expires_at<=$2 RETURNING message_id",
                &[&(now - DEDUPE_RETENTION), &now],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        removed.sort();
        tx.execute(
            "DELETE FROM coordination_wakes WHERE created_at<=$1",
            &[&(now - WAKE_RETENTION)],
        )
        .await?;
        let mut agents: Vec<String> = tx
            .query(
                &format!(
                    "DELETE FROM coordination_agents a WHERE {removable} \
                     AND NOT EXISTS (SELECT 1 FROM coordination_leases l \
                     WHERE l.holder_agent_id=a.agent_id) RETURNING a.agent_id"
                ),
                &[&short, &long],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        agents.sort();
        if !agents.is_empty() {
            tx.execute(
                "UPDATE coordination_agents SET parent_agent_id=NULL \
                 WHERE parent_agent_id=ANY($1)",
                &[&agents],
            )
            .await?;
        }
        tx.execute(
            "DELETE FROM coordination_lease_waiters w WHERE NOT EXISTS \
             (SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id)",
            &[],
        )
        .await?;
        let mut leases: Vec<String> = tx
            .query(
                "DELETE FROM coordination_leases l WHERE l.holder_agent_id IS NULL \
                 AND l.freed_at<=$1 AND NOT EXISTS (SELECT 1 FROM coordination_lease_waiters w \
                 WHERE w.name=l.name) RETURNING l.name",
                &[&(now - DEDUPE_RETENTION)],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        leases.sort();
        if !expired.is_empty() {
            events.push(Event::new("expire", &json!({"message_ids": expired})).actor("daemon"));
        }
        if !removed.is_empty() || !agents.is_empty() || !leases.is_empty() {
            let mut pruned = json!({"message_ids": removed, "agent_ids": agents});
            if !leases.is_empty() {
                pruned["leases"] = json!(leases);
            }
            events.push(Event::new("prune", &pruned).actor("daemon"));
        }
        let mut head = None;
        let mut audit_removed: u64 = 0;
        if audit_retention_days != 0 {
            let cutoff = audit_cutoff(now, audit_retention_days);
            head = Some(chain_head(&tx).await?);
            let cut = tx
                .query_opt(
                    "WITH retained AS (SELECT min(seq) AS first_seq FROM coordination_events \
                     WHERE created_at>=$1) SELECT seq,hash FROM coordination_events,retained \
                     WHERE retained.first_seq IS NULL OR seq<retained.first_seq \
                     ORDER BY seq DESC LIMIT 1",
                    &[&cutoff],
                )
                .await?;
            if let Some(cut) = cut {
                let through_seq: i64 = cut.get(0);
                let through_hash: String = cut.get(1);
                audit_removed = tx
                    .execute(
                        "DELETE FROM coordination_events WHERE seq<=$1",
                        &[&through_seq],
                    )
                    .await?;
                events.push(
                    Event::new(
                        "audit_prune",
                        &json!({"through_seq": through_seq, "through_hash": through_hash,
                                "removed": audit_removed, "cutoff": cutoff_value(cutoff),
                                "retention_days": audit_retention_days}),
                    )
                    .actor("daemon"),
                );
            }
        }
        append(&tx, events, now, head).await?;
        tx.commit().await?;
        Ok(json!({"bodies_expired": expired.len(), "removed": removed.len(),
                  "agents_removed": agents.len(), "audit_removed": audit_removed}))
    }
}

/// `audit_cutoff`: the UTC day boundary at or before `now - days`.
pub fn audit_cutoff(now: f64, retention_days: i64) -> f64 {
    ((now - retention_days as f64 * 86400.0) / 86400.0).floor() * 86400.0
}

/// `math.floor(...) * 86400` is a Python int.
fn cutoff_value(cutoff: f64) -> Value {
    json!(cutoff as i64)
}

/// Python `int(raw)` on a cursor's tail: optional sign and surrounding
/// whitespace, digits with single underscores between them.
fn py_int_str(raw: &str) -> Option<i64> {
    let t = text::py_strip(raw);
    let chars: Vec<char> = t.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_'
            && !(i > 0
                && i + 1 < chars.len()
                && chars[i - 1].is_ascii_digit()
                && chars[i + 1].is_ascii_digit())
        {
            return None;
        }
    }
    t.replace('_', "").parse::<i64>().ok()
}
