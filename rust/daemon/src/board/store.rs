//! `CoordinationStore`'s identity and presence side (spec BS3-BS5, BA):
//! the audit append, `context`, register/update/authenticate, the
//! children lists and subagent hooks, the roster, attach/heartbeat/detach,
//! and the park gate and woke marker. Mail is in `mail.rs`, wake decisions
//! in `wake.rs`, resource leases in `lease.rs`.

use super::canon::{self, AuditRow, GENESIS_HASH, float_value, hash_text};
use super::db::{AgentRow, obj, opt_f64, opt_str};
use super::error::{BoardError, Result};
use super::secrets::refused as looks_like_secret;
use super::text::{self, check_string, check_value};
use super::{lease, mail, wake};
use serde_json::{Map, Value, json};
use tokio_postgres::GenericClient;

pub const MAX_LABEL: usize = 120;
pub const MAX_SCOPE: usize = 120;
pub const MAX_NAME: usize = MAX_LABEL;
pub const MAX_CHILDREN: usize = 8;
pub const MAX_CHILD_LABEL: usize = 40;
pub const MAX_HOOK_CHILDREN: usize = 8;
pub const HOOK_CHILD_KIND_CUT: usize = MAX_CHILD_LABEL - 9;
pub const PARK_REASONS: [&str; 6] = [
    "done",
    "blocked",
    "needs_approval",
    "needs_info",
    "needs_resource",
    "waiting_peer",
];
pub const MAX_PARK_NEEDS: usize = 120;
pub const MAX_PARK_RESUME: usize = 240;
pub const MAX_PARK_CLEAR_BY: usize = 120;
pub const PARK_DEFAULT_TTL: f64 = 12.0 * 3600.0;
pub const PARK_MAX_TTL: f64 = 7.0 * 86400.0;
pub const ACTIVE_WINDOW: f64 = 3600.0;
pub const STATUS_STALE_AFTER: f64 = 2.0 * 3600.0;
pub const ATTACHED_IDLE_WINDOW: f64 = STATUS_STALE_AFTER + ACTIVE_WINDOW;
pub const HOOK_CHILD_TTL: f64 = ATTACHED_IDLE_WINDOW;
pub const ATTACHMENT_LEASE: f64 = 60.0;
pub const LEASE_EXPECT_MAX: i64 = 7 * 86400;
pub const MAX_PAGE: i64 = 50;
pub const BANK_ID_META_KEY: &str = "coordination_bank_id";
pub const DAEMON_PRINCIPAL: &str = "daemon";
pub const MAINTAINER_PRINCIPAL: &str = "maintainer";
pub const RESERVED_SENDERS: [&str; 2] = [DAEMON_PRINCIPAL, MAINTAINER_PRINCIPAL];
const NAME_SOURCES: [&str; 4] = ["", "title", "harness", "agent"];
const CLIENT_NAME_SOURCES: [&str; 2] = ["agent", "harness"];
const PARK_TEXT_FIELDS: [(&str, usize); 3] = [
    ("park_needs", MAX_PARK_NEEDS),
    ("park_clear_by", MAX_PARK_CLEAR_BY),
    ("park_resume", MAX_PARK_RESUME),
];
pub const PARK_FIELDS: [&str; 5] = [
    "park_reason",
    "park_needs",
    "park_clear_by",
    "park_resume",
    "park_expires",
];
/// `DONE_STATUS`: `^\W*(done|complete|completed|finished|merged)\b`, case-insensitive.
static DONE_PREFIX: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
    // Python re.IGNORECASE also folds dotted and dotless I to ASCII i.
    regex::Regex::new(r"(?i)^(done|completed|complete|f[iİı]n[iİı]shed|merged)")
        .expect("done status")
});

fn done_status(status: &str) -> bool {
    // Python's word class is alphanumeric or underscore; regex's Unicode
    // class also includes combining marks and join controls.
    let word = |c| text::alnum(c) || c == '_';
    let status = status.trim_start_matches(|c| !word(c));
    DONE_PREFIX.find(status).is_some_and(|matched| {
        status[matched.end()..]
            .chars()
            .next()
            .is_none_or(|c| !word(c))
    })
}

pub fn now() -> f64 {
    if crate::mutants::active("board-clock-behind") {
        return std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock after epoch")
            .as_secs_f64()
            - 30.0;
    }
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock after epoch")
        .as_secs_f64()
}

fn name_rank(source: &str) -> usize {
    NAME_SOURCES.iter().position(|s| *s == source).unwrap_or(0)
}

/// `_refuse_secret`: refuse credential-shaped text, naming the field.
pub fn refuse_secret(value: &str, field: &str) -> Result<()> {
    if looks_like_secret(value) {
        return Err(BoardError::detail(
            "secret_like_body",
            format!(
                "{field} looks like a credential, which the board never keeps; remove it and retry"
            ),
        )
        .with_param(field));
    }
    Ok(())
}

// ---- the audit chain (BA1-BA4) --------------------------------------------

/// One audit row before its chain position (`_event`).
pub struct Event {
    pub event: String,
    pub actor: String,
    pub principal: String,
    pub agent_id: String,
    pub recipient: Option<String>,
    pub project: String,
    pub task: String,
    pub message_id: Option<String>,
    pub hlc: String,
    pub payload: String,
    pub body: Option<String>,
    pub body_salt: Option<String>,
}

impl Event {
    pub fn new(event: &str, payload: &Value) -> Event {
        Event {
            event: event.to_string(),
            actor: "agent".into(),
            principal: String::new(),
            agent_id: String::new(),
            recipient: None,
            project: String::new(),
            task: String::new(),
            message_id: None,
            hlc: String::new(),
            payload: canon::canonical(payload),
            body: None,
            body_salt: None,
        }
    }
    pub fn actor(mut self, actor: &str) -> Event {
        self.actor = actor.to_string();
        self
    }
    pub fn principal(mut self, principal: &str) -> Event {
        self.principal = principal.to_string();
        self
    }
    pub fn agent(mut self, agent_id: &str) -> Event {
        self.agent_id = agent_id.to_string();
        self
    }
    pub fn recipient(mut self, recipient: &str) -> Event {
        self.recipient = Some(recipient.to_string());
        self
    }
    pub fn scope(mut self, project: &str, task: &str) -> Event {
        self.project = project.to_string();
        self.task = task.to_string();
        self
    }
    pub fn message(mut self, message_id: &str) -> Event {
        self.message_id = Some(message_id.to_string());
        self
    }
    pub fn hlc(mut self, hlc: &str) -> Event {
        self.hlc = hlc.to_string();
        self
    }
    pub fn body(mut self, body: &str, salt: &str) -> Event {
        self.body = Some(body.to_string());
        self.body_salt = Some(salt.to_string());
        self
    }
}

/// `_chain_head`: take the chain lock (inside the mutation's transaction),
/// then read the head.
pub async fn chain_head(c: &impl GenericClient) -> Result<(i64, String)> {
    c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        &[&"coordination-audit-chain"],
    )
    .await?;
    let row = c
        .query_opt(
            "SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1",
            &[],
        )
        .await?;
    Ok(match row {
        Some(r) => (r.get(0), r.get(1)),
        None => (0, GENESIS_HASH.to_string()),
    })
}

/// `_append`: chain and insert `events` as the mutation's last statements.
pub async fn append(
    c: &impl GenericClient,
    events: Vec<Event>,
    now: f64,
    head: Option<(i64, String)>,
) -> Result<Option<(i64, String)>> {
    if events.is_empty() {
        return Ok(None);
    }
    let (mut seq, mut prev) = match head {
        Some(h) => h,
        None => chain_head(c).await?,
    };
    if crate::mutants::active("board-chain-skip-seq") {
        seq += 1;
    }
    let mut plain = Vec::new();
    let mut bodied = Vec::new();
    for event in events {
        seq += 1;
        let digest = canon::audit_hash(
            &prev,
            &AuditRow {
                seq,
                event: &event.event,
                actor: &event.actor,
                principal: &event.principal,
                agent_id: &event.agent_id,
                recipient_agent_id: event.recipient.as_deref(),
                project: &event.project,
                task: &event.task,
                message_id: event.message_id.as_deref(),
                created_at: now,
                hlc: &event.hlc,
                payload: &event.payload,
            },
        );
        let row = (seq, event, prev.clone(), digest.clone());
        if row.1.body.is_none() {
            plain.push(row);
        } else {
            bodied.push(row);
        }
        prev = digest;
    }
    // Python inserts the bodiless rows first, then the bodied ones.
    for (seq, e, prev_hash, hash) in &plain {
        c.execute(
            "INSERT INTO coordination_events (seq,event,actor,principal,agent_id,recipient_agent_id,\
             project,task,message_id,payload,created_at,hlc,prev_hash,hash) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
            &[
                seq, &e.event, &e.actor, &e.principal, &e.agent_id, &e.recipient, &e.project,
                &e.task, &e.message_id, &e.payload, &now, &e.hlc, prev_hash, hash,
            ],
        )
        .await?;
    }
    for (seq, e, prev_hash, hash) in &bodied {
        c.execute(
            "INSERT INTO coordination_events (seq,event,actor,principal,agent_id,recipient_agent_id,\
             project,task,message_id,payload,created_at,hlc,prev_hash,hash,body,body_salt) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)",
            &[
                seq, &e.event, &e.actor, &e.principal, &e.agent_id, &e.recipient, &e.project,
                &e.task, &e.message_id, &e.payload, &now, &e.hlc, prev_hash, hash, &e.body,
                &e.body_salt,
            ],
        )
        .await?;
    }
    Ok(Some((seq, prev)))
}

// ---- the store --------------------------------------------------------------

/// The caps the wake decision applies (`WakePolicy`).
#[derive(Debug, Clone)]
pub struct WakePolicy {
    pub per_recipient_per_hour: i64,
    pub urgent_per_sender_per_hour: i64,
    pub nightly_total: i64,
    pub fan_out_stagger_seconds: i64,
    pub active_seconds: i64,
    pub authority_per_sender_per_hour: i64,
    pub maintainer_per_recipient_per_hour: i64,
}

pub struct Store {
    pub wake: WakePolicy,
    pub maintainer_principals: Vec<String>,
}

pub fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    getrandom::fill(&mut buf).expect("system randomness");
    hex::encode(buf)
}

/// `uuid.uuid4().hex`.
pub fn uuid_hex() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// `secrets.token_urlsafe(32)`: 32 random bytes, URL-safe base64, no padding.
pub fn token_urlsafe32() -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut buf = [0u8; 32];
    getrandom::fill(&mut buf).expect("system randomness");
    let mut out = String::with_capacity(43);
    for chunk in buf.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, b)| acc | (u32::from(*b) << (16 - 8 * i)));
        let take = chunk.len() + 1;
        for i in 0..take {
            out.push(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

fn is_lower_hex(s: &str, len: usize) -> bool {
    s.len() == len
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// `_live_children`.
pub fn live_children(children: &[Value], now: f64) -> Vec<Value> {
    children
        .iter()
        .filter(|child| {
            let hooked = child
                .get("agent_id")
                .is_some_and(|a| a.as_str().is_some_and(|s| !s.is_empty()));
            !hooked
                || now - child.get("since").and_then(canon::as_f64).unwrap_or(0.0) < HOOK_CHILD_TTL
        })
        .cloned()
        .collect()
}

fn hooked(child: &Value) -> bool {
    child
        .get("agent_id")
        .is_some_and(|a| a.as_str().is_some_and(|s| !s.is_empty()))
}

/// `_listener_path`: None when a ring reaches the row now, else why not.
pub fn listener_path(row: &AgentRow, now: f64) -> Option<&'static str> {
    let attached = row.attachment_id.is_some() && row.lease_until.unwrap_or(0.0) > now;
    if attached && row.wake_enabled {
        return None;
    }
    let deadline = row.capability("ring_armed_until");
    let deadline_live = deadline
        .and_then(canon::as_f64)
        .is_some_and(|d| d.is_finite() && d > now)
        && deadline.is_some_and(|d| matches!(d, Value::Number(_)));
    if attached && row.capability("ring") == Some(&Value::Bool(true)) && deadline_live {
        return None;
    }
    if deadline.is_some_and(|d| !d.is_null()) || row.wake_enabled {
        return Some("listener_expired");
    }
    if row.capability("ring") == Some(&Value::Bool(true)) {
        Some("listener_unknown")
    } else {
        Some("wake_disabled")
    }
}

/// `_relative_time` (memory/context_builder.py:14-33).
pub fn relative_time(timestamp: f64, now: f64) -> String {
    if timestamp == 0.0 {
        return "unknown time".into();
    }
    let age = (now - timestamp).max(0.0);
    let plural = |n: i64, unit: &str| format!("{n} {unit}{} ago", if n != 1 { "s" } else { "" });
    if age < 60.0 {
        "just now".into()
    } else if age < 3600.0 {
        plural((age / 60.0).floor() as i64, "minute")
    } else if age < 86400.0 {
        plural((age / 3600.0).floor() as i64, "hour")
    } else {
        plural((age / 86400.0).floor() as i64, "day")
    }
}

/// `_limit`.
pub fn check_limit(limit: &Value) -> Result<i64> {
    match canon::py_int_i64(limit) {
        Some(n) if (1..=MAX_PAGE).contains(&n) => Ok(n),
        _ => Err(BoardError::code("invalid_limit")),
    }
}

/// A Python number that keeps its int/float type (`min(value, cap)` and
/// `deadline or 0` preserve it).
pub fn min_keep_type(value: &Value, cap: f64) -> Value {
    let v = canon::as_f64(value).unwrap_or(0.0);
    if cap < v {
        float_value(cap)
    } else if canon::is_py_int(value) {
        value.clone()
    } else {
        // json.loads makes it a float, which Jsonb spells as repr does.
        float_value(v)
    }
}

impl Store {
    // ---- context --------------------------------------------------------

    /// `context` (spec BS3).
    pub async fn context(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: Option<&str>,
        nonce: Option<&str>,
        read_only: bool,
    ) -> Result<Value> {
        let candidate = uuid::Uuid::new_v4().hyphenated().to_string();
        let tx = client.transaction().await?;
        let mut created = 0u64;
        if !read_only {
            created = tx
                .execute(
                    "INSERT INTO meta (key,value) VALUES ($1,$2) ON CONFLICT (key) DO NOTHING",
                    &[&BANK_ID_META_KEY, &Value::String(candidate)],
                )
                .await?;
        }
        let bank = tx
            .query_opt("SELECT value FROM meta WHERE key=$1", &[&BANK_ID_META_KEY])
            .await?;
        let Some(bank) = bank else {
            return Err(BoardError::code("invalid_bank_identity"));
        };
        let value: Value = bank.get(0);
        let bank_id = match value.as_str() {
            Some(s) if uuid::Uuid::parse_str(s).is_ok_and(|u| u.hyphenated().to_string() == s) => {
                s.to_string()
            }
            _ => return Err(BoardError::code("invalid_bank_identity")),
        };
        if created > 0 {
            append(
                &tx,
                vec![
                    Event::new("bank_identity", &json!({"bank_id": bank_id})).principal(principal),
                ],
                now(),
                None,
            )
            .await?;
        }
        let mut result = json!({"bank_id": bank_id, "principal": principal});
        if let (Some(agent_id), Some(nonce)) = (agent_id, nonce) {
            let row = tx
                .query_opt(
                    "SELECT principal,credential_hash FROM coordination_agents WHERE agent_id=$1",
                    &[&agent_id],
                )
                .await?;
            let ok = row.as_ref().and_then(|r| {
                let p: String = r.get(0);
                let h: Option<String> = r.get(1);
                (p == principal).then_some(h).flatten()
            });
            let Some(credential_hash) = ok.filter(|h| is_lower_hex(h, 64)) else {
                return Err(BoardError::code("invalid_credential"));
            };
            use hmac::{Hmac, Mac};
            let message = canon::compact(&json!([
                "pseudolife-context-v1",
                bank_id,
                principal,
                agent_id,
                nonce
            ]));
            // ensure_ascii=True: the inputs here are ASCII already except the
            // principal, which `\u`-escapes.
            let message = ascii_escape(&message);
            let key = hex::decode(&credential_hash).expect("validated hex");
            let mut mac = Hmac::<sha2::Sha256>::new_from_slice(&key).expect("any key length");
            mac.update(message.as_bytes());
            result["proof"] = json!(hex::encode(mac.finalize().into_bytes()));
        }
        super::db::commit(tx).await?;
        Ok(result)
    }

    // ---- identity ----------------------------------------------------------

    /// `_auth`.
    pub async fn auth(
        &self,
        c: &impl GenericClient,
        principal: &str,
        agent_id: &str,
        credential: &str,
        lock: bool,
    ) -> Result<AgentRow> {
        if credential.is_empty() || text::py_len(credential) > 256 {
            return Err(BoardError::code("invalid_credential"));
        }
        let credential_hash = hash_text(credential);
        let sql = if lock {
            "SELECT * FROM coordination_agents WHERE agent_id=$1 FOR UPDATE"
        } else {
            "SELECT * FROM coordination_agents WHERE agent_id=$1"
        };
        let Some(row) = c.query_opt(sql, &[&agent_id]).await? else {
            return Err(BoardError::code("instance_not_found"));
        };
        let row = AgentRow::from_row(&row);
        let matches = row.credential_hash.as_deref().is_some_and(|h| {
            !h.is_empty() && constant_time_eq(h.as_bytes(), credential_hash.as_bytes())
        });
        if row.principal != principal || !matches {
            return Err(BoardError::code("invalid_credential"));
        }
        Ok(row)
    }

    /// `_public`.
    pub fn public(&self, row: &AgentRow) -> Value {
        let now = now();
        let mut m = Map::new();
        m.insert("agent_id".into(), json!(row.agent_id));
        m.insert("principal".into(), json!(row.principal));
        m.insert("label".into(), json!(row.label));
        m.insert("project".into(), json!(row.project));
        m.insert("task".into(), json!(row.task));
        m.insert("status".into(), json!(row.status));
        m.insert("episode".into(), json!(row.episode));
        m.insert("capabilities".into(), row.capabilities.clone());
        m.insert("wake_enabled".into(), json!(row.wake_enabled));
        m.insert("created_at".into(), float_value(row.created_at));
        m.insert("last_activity".into(), float_value(row.last_activity));
        m.insert("lifecycle".into(), json!(row.lifecycle));
        m.insert(
            "children".into(),
            Value::Array(live_children(&row.children_list(), now)),
        );
        m.insert("park_reason".into(), opt_str(row.park_reason.as_deref()));
        m.insert("park_needs".into(), json!(row.park_needs));
        m.insert("park_clear_by".into(), json!(row.park_clear_by));
        m.insert("park_resume".into(), json!(row.park_resume));
        m.insert("park_expires".into(), opt_f64(row.park_expires));
        m.insert("park_set_at".into(), opt_f64(row.park_set_at));
        m.insert(
            "parent_agent_id".into(),
            opt_str(row.parent_agent_id.as_deref()),
        );
        m.insert("subagent".into(), json!(row.parent_thread.is_some()));
        m.insert(
            "adapter_available".into(),
            json!(
                row.attachment_id.as_deref().is_some_and(|a| !a.is_empty())
                    && row.lease_until.unwrap_or(0.0) > now
            ),
        );
        m.insert(
            "name".into(),
            json!(text::board_name(
                &row.agent_id,
                Some(&row.label),
                Some(&row.name)
            )),
        );
        m.insert("name_source".into(), json!(row.name_source));
        Value::Object(m)
    }

    pub async fn authenticate(
        &self,
        c: &impl GenericClient,
        principal: &str,
        agent_id: &str,
        credential: &str,
    ) -> Result<Value> {
        let row = self.auth(c, principal, agent_id, credential, false).await?;
        Ok(self.public(&row))
    }

    /// `_check_label`: only a set label is checked.
    fn check_label(principal: &str, label: Option<&Value>) -> Result<()> {
        if let Some(Value::String(label)) = label
            && text::reserved_name(label)
            && !(RESERVED_SENDERS.contains(&principal)
                && text::casefold(text::py_strip(label)) == principal)
        {
            return Err(BoardError::code("invalid_label"));
        }
        Ok(())
    }

    /// `_check_name`: returned stripped; empty clears.
    pub fn check_name(name: &Value) -> Result<String> {
        let name = check_value(name, MAX_NAME, "name", true)?;
        if name.chars().any(text::category_c) || text::reserved_name(name) {
            return Err(BoardError::code("invalid_name"));
        }
        refuse_secret(name, "name")?;
        Ok(text::py_strip(name).to_string())
    }

    /// `_name_change`: the columns applying `name` from `source`, or none.
    fn name_change(
        row: &AgentRow,
        name: &str,
        source: &str,
        now: f64,
    ) -> Option<(String, String, f64)> {
        let current = row.name_source.as_str();
        if name_rank(source) < name_rank(current) {
            return None;
        }
        if name.is_empty() {
            if current != source {
                return None;
            }
            let kept = if source == "agent" {
                row.harness_name.clone()
            } else {
                String::new()
            };
            let src = if kept.is_empty() { "" } else { "harness" };
            return Some((kept, src.to_string(), now));
        }
        if row.name == name && current == source {
            return None;
        }
        Some((name.to_string(), source.to_string(), now))
    }

    fn name_event(row: &AgentRow, name: &str, source: &str, principal: &str, actor: &str) -> Event {
        Event::new(
            "update",
            &json!({"fields": {"name": name, "name_source": source},
                    "before": {"name": row.name, "name_source": row.name_source}}),
        )
        .actor(actor)
        .principal(principal)
        .agent(&row.agent_id)
        .scope(&row.project, &row.task)
    }

    /// `_fields`: validate the given fields in their order, returning them
    /// with `park_reason` "" as null and `park_expires` as a float.
    pub fn fields(given: &[(String, Value)]) -> Result<Vec<(String, Value)>> {
        let mut out = Vec::new();
        for (key, value) in given {
            let limit = match key.as_str() {
                "label" => Some(MAX_LABEL),
                "project" | "task" => Some(MAX_SCOPE),
                "status" => Some(240),
                "episode" => Some(120),
                _ => None,
            };
            if let Some(limit) = limit {
                let s = check_value(value, limit, key, true)?;
                refuse_secret(s, key)?;
                out.push((key.clone(), value.clone()));
                continue;
            }
            match key.as_str() {
                "wake_enabled" => {
                    if !value.is_boolean() {
                        return Err(BoardError::code("invalid_wake_enabled"));
                    }
                }
                "capabilities" => {
                    let Some(map) = value.as_object().filter(|m| m.len() <= 8) else {
                        return Err(BoardError::code("invalid_capabilities"));
                    };
                    for (item, enabled) in map {
                        check_string(item, 40, "capabilities", false)?;
                        refuse_secret(item, "capabilities")?;
                        if !enabled.is_boolean() {
                            return Err(BoardError::code("invalid_capabilities"));
                        }
                    }
                }
                "children" => {
                    let refused = || {
                        BoardError::detail(
                            "invalid_children",
                            format!(
                                "at most {MAX_CHILDREN} distinct labels of at most {MAX_CHILD_LABEL} \
                                 characters; nothing was updated"
                            ),
                        )
                    };
                    let Some(list) = value.as_array().filter(|l| l.len() <= MAX_CHILDREN) else {
                        return Err(refused());
                    };
                    let mut seen: Vec<&str> = Vec::new();
                    for label in list {
                        let Ok(s) = check_value(label, MAX_CHILD_LABEL, "children", false) else {
                            return Err(refused());
                        };
                        refuse_secret(s, "children")?;
                        seen.push(s);
                    }
                    let mut distinct = seen.clone();
                    distinct.sort_unstable();
                    distinct.dedup();
                    if distinct.len() != seen.len() {
                        return Err(refused());
                    }
                }
                "park_reason" => {
                    if value.is_null() || value.as_str() == Some("") {
                        out.push((key.clone(), Value::Null));
                        continue;
                    }
                    if !value.as_str().is_some_and(|s| PARK_REASONS.contains(&s)) {
                        return Err(BoardError::full(
                            "invalid_park",
                            Some(format!(
                                "park_reason must be one of {}, or empty to clear the park; nothing was updated",
                                PARK_REASONS.join(", ")
                            )),
                            Some("park_reason"),
                            Some(PARK_REASONS.iter().map(|s| s.to_string()).collect()),
                            None,
                        ));
                    }
                }
                "park_needs" | "park_clear_by" | "park_resume" => {
                    let limit = PARK_TEXT_FIELDS
                        .iter()
                        .find(|(k, _)| k == key)
                        .expect("park text field")
                        .1;
                    let Ok(s) = check_value(value, limit, "park", true) else {
                        return Err(BoardError::full(
                            "invalid_park",
                            Some(format!(
                                "{key} must be text of at most {limit} characters with no control characters; nothing was updated"
                            )),
                            Some(key),
                            None,
                            None,
                        ));
                    };
                    refuse_secret(s, key)?;
                }
                "park_expires" => {
                    let ok = matches!(value, Value::Number(_))
                        && canon::as_f64(value).is_some_and(|v| v.is_finite() && v > 0.0);
                    if !ok {
                        return Err(BoardError::full(
                            "invalid_park",
                            Some(
                                "park_expires must be a positive finite Unix epoch in seconds; nothing was updated"
                                    .into(),
                            ),
                            Some("park_expires"),
                            None,
                            None,
                        ));
                    }
                    out.push((
                        key.clone(),
                        float_value(canon::as_f64(value).expect("number")),
                    ));
                    continue;
                }
                _ => return Err(BoardError::code("invalid_update")),
            }
            out.push((key.clone(), value.clone()));
        }
        Ok(out)
    }

    // ---- register ------------------------------------------------------------

    /// `register` (spec BS4). `params` are the action's parameters in
    /// their given order.
    pub async fn register(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        params: &Map<String, Value>,
    ) -> Result<Value> {
        check_string(principal, 256, "principal", false)?;
        let label = params.get("label").cloned().unwrap_or(json!(""));
        Self::check_label(principal, Some(&label))?;
        let get = |k: &str, d: Value| params.get(k).cloned().unwrap_or(d);
        let capabilities = match params.get("capabilities") {
            None | Some(Value::Null) => json!({}),
            Some(v) => v.clone(),
        };
        let mut fields = Self::fields(&[
            ("label".into(), label),
            ("project".into(), get("project", json!(""))),
            ("task".into(), get("task", json!(""))),
            ("episode".into(), get("episode", json!(""))),
            ("status".into(), get("status", json!(""))),
            ("capabilities".into(), capabilities),
            ("wake_enabled".into(), get("wake_enabled", json!(false))),
        ])?;
        let field = |fields: &[(String, Value)], k: &str| -> Value {
            fields
                .iter()
                .find(|(key, _)| key == k)
                .map(|(_, v)| v.clone())
                .unwrap_or(Value::Null)
        };
        let episode = field(&fields, "episode").as_str().unwrap_or("").to_string();
        let caps = field(&fields, "capabilities");
        let thread = (caps.as_object().is_some_and(|m| m.contains_key("codex"))
            && !episode.is_empty())
        .then(|| episode.clone());
        let name_source = params.get("name_source").filter(|v| !v.is_null());
        let name = params.get("name").cloned().unwrap_or(json!(""));
        if let Some(src) = name_source {
            let valid = src
                .as_str()
                .is_some_and(|s| CLIENT_NAME_SOURCES.contains(&s));
            if !src.is_string() && !src.is_number() && !src.is_boolean() {
                // An unhashable value: `not in frozenset` raises TypeError.
                return Err(BoardError::code("invalid_request_type_error"));
            }
            if !valid || !py_truthy(&name) {
                return Err(BoardError::code("invalid_name"));
            }
        }
        let mut name_value = name.clone();
        if py_truthy(&name) {
            name_value = json!(Self::check_name(&name)?);
        }
        if py_truthy(&name_value) {
            fields.push(("name".into(), name_value.clone()));
            fields.push((
                "name_source".into(),
                json!(name_source.and_then(Value::as_str).unwrap_or("agent")),
            ));
        }
        let parent_thread = params
            .get("parent_thread")
            .filter(|v| !v.is_null())
            .cloned();
        if let Some(pt) = &parent_thread {
            // A non-string is `canonical = None`, never equal to it (Codex
            // review finding 3).
            let canonical = pt.as_str().and_then(python_uuid_canonical);
            if !pt.is_string()
                || canonical.as_deref() != pt.as_str()
                || thread.is_none()
                || thread.as_deref() == pt.as_str()
            {
                return Err(BoardError::code("invalid_parent"));
            }
        }
        let agent_id = uuid_hex();
        let credential = token_urlsafe32();
        let now = now();
        let tx = client.transaction().await?;
        let mut parent: Option<String> = None;
        if let Some(pt) = &parent_thread {
            parent = tx
                .query_opt(
                    "SELECT agent_id FROM coordination_agents WHERE principal=$1 AND episode=$2 \
                     AND capabilities ? 'codex' AND credential_hash IS NOT NULL \
                     ORDER BY last_activity DESC, agent_id LIMIT 1",
                    &[&principal, &pt.as_str().unwrap_or("")],
                )
                .await?
                .map(|r| r.get(0));
            fields.push(("parent_thread".into(), pt.clone()));
            fields.push(("parent_agent_id".into(), opt_str(parent.as_deref())));
        }
        let s = |k: &str| field(&fields, k).as_str().unwrap_or("").to_string();
        let named = field(&fields, "name");
        let named_str = named.as_str().unwrap_or("").to_string();
        let name_src = field(&fields, "name_source")
            .as_str()
            .unwrap_or("")
            .to_string();
        let harness_name = if name_src == "harness" {
            named_str.clone()
        } else {
            String::new()
        };
        let name_set_at = (!named_str.is_empty()).then_some(now);
        let wake_enabled = field(&fields, "wake_enabled").as_bool().unwrap_or(false);
        tx.execute(
            "INSERT INTO coordination_agents (agent_id,principal,credential_hash,label,project,task,\
             episode,status,capabilities,wake_enabled,created_at,last_activity,parent_thread,\
             parent_agent_id,name,name_source,name_set_at,harness_name) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)",
            &[
                &agent_id,
                &principal,
                &hash_text(&credential),
                &s("label"),
                &s("project"),
                &s("task"),
                &s("episode"),
                &s("status"),
                &caps,
                &wake_enabled,
                &now,
                &now,
                &parent_thread.as_ref().and_then(Value::as_str),
                &parent,
                &named_str,
                &name_src,
                &name_set_at,
                &harness_name,
            ],
        )
        .await?;
        let payload = Value::Object(fields.iter().cloned().collect());
        let mut events = vec![
            Event::new("register", &payload)
                .principal(principal)
                .agent(&agent_id)
                .scope(&s("project"), &s("task")),
        ];
        if let Some(thread) = &thread {
            events.extend(
                self.link_children(&tx, principal, thread, &agent_id)
                    .await?,
            );
        }
        append(&tx, events, now, None).await?;
        let mut out = self
            .authenticate(&tx, principal, &agent_id, &credential)
            .await?;
        super::db::commit(tx).await?;
        out["credential"] = json!(credential);
        Ok(out)
    }

    /// `_link_children`.
    async fn link_children(
        &self,
        c: &impl GenericClient,
        principal: &str,
        thread: &str,
        agent_id: &str,
    ) -> Result<Vec<Event>> {
        let mut rows: Vec<(String, Option<String>, String, String)> = c
            .query(
                "UPDATE coordination_agents c SET parent_agent_id=$1 FROM coordination_agents o \
                 WHERE c.agent_id=o.agent_id AND c.principal=$2 AND c.parent_thread=$3 \
                 AND c.agent_id<>$1 AND c.parent_agent_id IS DISTINCT FROM $1 \
                 RETURNING c.agent_id, o.parent_agent_id AS before, c.project, c.task",
                &[&agent_id, &principal, &thread],
            )
            .await?
            .iter()
            .map(|r| (r.get(0), r.get(1), r.get(2), r.get(3)))
            .collect();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(rows
            .into_iter()
            .map(|(child, before, project, task)| {
                Event::new(
                    "update",
                    &json!({"fields": {"parent_agent_id": agent_id},
                            "before": {"parent_agent_id": before}}),
                )
                .actor("daemon")
                .principal(principal)
                .agent(&child)
                .scope(&project, &task)
            })
            .collect())
    }

    // ---- update ---------------------------------------------------------------

    /// `update` (spec BS4). `given` are the parameters other than `expect`
    /// and `name`, in their given order.
    #[allow(clippy::too_many_arguments)] // The board update contract's arguments.
    pub async fn update(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        given: &[(String, Value)],
        expect: Option<&Value>,
        name: Option<&Value>,
    ) -> Result<Value> {
        let mut fields = Self::fields(given)?;
        let name = match name {
            Some(n) => Some(Self::check_name(n)?),
            None => None,
        };
        Self::check_label(
            principal,
            fields.iter().find(|(k, _)| k == "label").map(|(_, v)| v),
        )?;
        let expect = match expect {
            None => None,
            Some(v) => match canon::py_int_i64(v) {
                Some(n) if (1..=LEASE_EXPECT_MAX).contains(&n) => Some(n),
                _ => return Err(BoardError::code("invalid_expect")),
            },
        };
        let has = |fields: &[(String, Value)], k: &str| fields.iter().any(|(key, _)| key == k);
        let park_update = PARK_FIELDS.iter().any(|k| has(&fields, k));
        let tx = client.transaction().await?;
        let row = self
            .auth(&tx, principal, agent_id, credential, true)
            .await?;
        let now = now();
        let recorded = row.park_reason.is_some();
        let parked = wake::live_park(&row, now).is_some();
        let set = |fields: &mut Vec<(String, Value)>, k: &str, v: Value| {
            if let Some(slot) = fields.iter_mut().find(|(key, _)| key == k) {
                slot.1 = v;
            } else {
                fields.push((k.to_string(), v));
            }
        };
        let cleared = |fields: &mut Vec<(String, Value)>| {
            for (k, v) in [
                ("park_reason", Value::Null),
                ("park_needs", json!("")),
                ("park_clear_by", json!("")),
                ("park_resume", json!("")),
                ("park_expires", Value::Null),
                ("park_set_at", Value::Null),
            ] {
                set(fields, k, v);
            }
        };
        if park_update {
            let reason = fields
                .iter()
                .find(|(k, _)| k == "park_reason")
                .map(|(_, v)| v.clone());
            if reason.as_ref().is_some_and(Value::is_null) {
                cleared(&mut fields);
            } else if reason.is_none() && !parked {
                return Err(BoardError::full(
                    "invalid_park",
                    Some(
                        "park_reason is required: no park stands to refine (a lapsed park counts as none); nothing was updated"
                            .into(),
                    ),
                    Some("park_reason"),
                    Some(PARK_REASONS.iter().map(|s| s.to_string()).collect()),
                    None,
                ));
            } else {
                if recorded && !parked {
                    for (k, _) in PARK_TEXT_FIELDS {
                        if !has(&fields, k) {
                            fields.push((k.to_string(), json!("")));
                        }
                    }
                }
                set(&mut fields, "park_set_at", float_value(now));
                let standing = if parked { row.park_expires } else { None };
                if !has(&fields, "park_expires") && standing.is_none() {
                    let ttl = if crate::mutants::active("board-park-default-ttl") {
                        PARK_DEFAULT_TTL / 2.0
                    } else {
                        PARK_DEFAULT_TTL
                    };
                    fields.push(("park_expires".into(), float_value(now + ttl)));
                }
                let expires = fields
                    .iter()
                    .find(|(k, _)| k == "park_expires")
                    .and_then(|(_, v)| canon::as_f64(v));
                if expires.is_some_and(|e| e > now + PARK_MAX_TTL) {
                    return Err(BoardError::full(
                        "invalid_park",
                        Some(format!(
                            "park_expires must be at most {} days ({} seconds) from now; nothing was updated",
                            (PARK_MAX_TTL as i64) / 86400,
                            PARK_MAX_TTL as i64
                        )),
                        Some("park_expires"),
                        None,
                        None,
                    ));
                }
            }
        } else if has(&fields, "status") && recorded {
            cleared(&mut fields);
        }
        if let Some(slot) = fields.iter_mut().find(|(k, _)| k == "children") {
            let labels: Vec<String> = slot
                .1
                .as_array()
                .expect("validated list")
                .iter()
                .map(|v| v.as_str().expect("validated str").to_string())
                .collect();
            slot.1 = Value::Array(parent_children(&row.children_list(), &labels, now));
        }
        if let Some(expect) = expect {
            set(
                &mut fields,
                "status_expires_at",
                float_value(now + expect as f64),
            );
        } else if has(&fields, "status") && row.status_expires_at.is_some() {
            set(&mut fields, "status_expires_at", Value::Null);
        }
        let naming = name
            .as_deref()
            .and_then(|n| Self::name_change(&row, n, "agent", now));
        let mut columns = fields.clone();
        if let Some((n, src, at)) = &naming {
            set(&mut columns, "name", json!(n));
            set(&mut columns, "name_source", json!(src));
            set(&mut columns, "name_set_at", float_value(*at));
            set(&mut fields, "name", json!(n));
            set(&mut fields, "name_source", json!(src));
        }
        let mut sets = Vec::new();
        let mut values: Vec<Box<dyn tokio_postgres::types::ToSql + Sync + Send>> = Vec::new();
        for (k, v) in &columns {
            sets.push(format!("{k}=${}", sets.len() + 1));
            values.push(column_param(k, v));
        }
        sets.push(format!("last_activity=${}", sets.len() + 1));
        values.push(Box::new(now));
        let id_index = values.len() + 1;
        values.push(Box::new(agent_id.to_string()));
        let sql = format!(
            "UPDATE coordination_agents SET {} WHERE agent_id=${id_index}",
            sets.join(",")
        );
        let refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
            values.iter().map(|b| b.as_ref() as _).collect();
        tx.execute(&sql, &refs).await?;
        let before: Map<String, Value> = fields
            .iter()
            .map(|(k, _)| (k.clone(), row.column(k)))
            .collect();
        let payload = json!({"fields": Value::Object(fields.iter().cloned().collect()),
                             "before": Value::Object(before)});
        let project = fields
            .iter()
            .find(|(k, _)| k == "project")
            .and_then(|(_, v)| v.as_str())
            .unwrap_or(&row.project)
            .to_string();
        let task = fields
            .iter()
            .find(|(k, _)| k == "task")
            .and_then(|(_, v)| v.as_str())
            .unwrap_or(&row.task)
            .to_string();
        append(
            &tx,
            vec![
                Event::new("update", &payload)
                    .principal(principal)
                    .agent(agent_id)
                    .scope(&project, &task),
            ],
            now,
            None,
        )
        .await?;
        let out = self
            .authenticate(&tx, principal, agent_id, credential)
            .await?;
        super::db::commit(tx).await?;
        Ok(out)
    }

    // ---- subagent hooks (v50) ------------------------------------------------

    async fn hook_children_row(
        &self,
        c: &impl GenericClient,
        agent_id: &str,
        principal: &str,
    ) -> Result<Option<AgentRow>> {
        Ok(c.query_opt(
            "SELECT * FROM coordination_agents WHERE agent_id=$1 AND principal=$2 \
                 AND credential_hash IS NOT NULL FOR UPDATE",
            &[&agent_id, &principal],
        )
        .await?
        .map(|r| AgentRow::from_row(&r)))
    }

    async fn set_hook_children(
        &self,
        c: &impl GenericClient,
        row: &AgentRow,
        children: Vec<Value>,
        principal: &str,
        now: f64,
    ) -> Result<()> {
        let list = Value::Array(children);
        c.execute(
            "UPDATE coordination_agents SET children=$1,last_activity=$2 WHERE agent_id=$3",
            &[&list, &now, &row.agent_id],
        )
        .await?;
        append(
            c,
            vec![
                Event::new(
                    "update",
                    &json!({"fields": {"children": list}, "before": {"children": row.children}}),
                )
                .actor("hook")
                .principal(principal)
                .agent(&row.agent_id)
                .scope(&row.project, &row.task),
            ],
            now,
            None,
        )
        .await?;
        Ok(())
    }

    fn hook_child_id(child: &str) -> bool {
        !child.is_empty()
            && child.len() <= 64
            && child
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    }

    fn hook_child_kind(kind: &str) -> bool {
        !kind.is_empty()
            && kind.len() <= 64
            && kind
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".:_-".contains(&b))
    }

    /// `subagent_started`.
    pub async fn subagent_started(
        &self,
        client: &mut tokio_postgres::Client,
        agent_id: &str,
        principal: &str,
        child: &str,
        kind: &str,
    ) -> Result<Value> {
        if !Self::hook_child_id(child) || (!kind.is_empty() && !Self::hook_child_kind(kind)) {
            return Err(BoardError::code("invalid_children"));
        }
        let cut: String = kind.chars().take(HOOK_CHILD_KIND_CUT).collect();
        let short: String = child.chars().take(8).collect();
        let label = format!("{}#{short}", if cut.is_empty() { "subagent" } else { &cut });
        refuse_secret(&label, "children")?;
        let tx = client.transaction().await?;
        let Some(row) = self.hook_children_row(&tx, agent_id, principal).await? else {
            return Ok(json!({"recorded": false, "reason": "unknown_agent"}));
        };
        let now = now();
        let current = live_children(&row.children_list(), now);
        if current
            .iter()
            .any(|e| e.get("agent_id").and_then(Value::as_str) == Some(child))
        {
            return Ok(json!({"recorded": true, "changed": false}));
        }
        let mut children: Vec<Value> = current
            .into_iter()
            .filter(|e| e.get("label").and_then(Value::as_str) != Some(label.as_str()))
            .collect();
        let hooked_entries: Vec<(usize, f64)> = children
            .iter()
            .enumerate()
            .filter(|(_, e)| hooked(e))
            .map(|(i, e)| (i, e.get("since").and_then(canon::as_f64).unwrap_or(0.0)))
            .collect();
        if hooked_entries.len() >= MAX_HOOK_CHILDREN {
            // `min(hooked, key=since)`: the first of the oldest.
            let mut oldest = hooked_entries[0];
            for entry in &hooked_entries[1..] {
                if entry.1 < oldest.1 {
                    oldest = *entry;
                }
            }
            children.remove(oldest.0);
        }
        children.push(json!({"label": label, "since": float_value(now), "agent_id": child}));
        self.set_hook_children(&tx, &row, children, principal, now)
            .await?;
        super::db::commit(tx).await?;
        Ok(json!({"recorded": true, "changed": true}))
    }

    /// `subagent_stopped`.
    pub async fn subagent_stopped(
        &self,
        client: &mut tokio_postgres::Client,
        agent_id: &str,
        principal: &str,
        child: &str,
    ) -> Result<Value> {
        if !Self::hook_child_id(child) {
            return Err(BoardError::code("invalid_children"));
        }
        let tx = client.transaction().await?;
        let Some(row) = self.hook_children_row(&tx, agent_id, principal).await? else {
            return Ok(json!({"recorded": false, "reason": "unknown_agent"}));
        };
        let now = now();
        let live = live_children(&row.children_list(), now);
        let children: Vec<Value> = live
            .iter()
            .filter(|e| e.get("agent_id").and_then(Value::as_str) != Some(child))
            .cloned()
            .collect();
        if children.len() == live.len() {
            return Ok(json!({"recorded": false, "reason": "not_listed"}));
        }
        self.set_hook_children(&tx, &row, children, principal, now)
            .await?;
        super::db::commit(tx).await?;
        Ok(json!({"recorded": true, "changed": true}))
    }

    // ---- roster (spec BS5) ------------------------------------------------------

    pub async fn list_agents(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        params: &Map<String, Value>,
    ) -> Result<Value> {
        self.auth(&*client, principal, agent_id, credential, false)
            .await?;
        let limit = params.get("limit").cloned().unwrap_or(json!(MAX_PAGE));
        let project = params.get("project").filter(|v| !v.is_null());
        let task = params.get("task").filter(|v| !v.is_null());
        self.roster(client, agent_id, project, task, &limit, true)
            .await
    }

    pub async fn roster(
        &self,
        client: &mut tokio_postgres::Client,
        agent_id: &str,
        project: Option<&Value>,
        task: Option<&Value>,
        limit: &Value,
        settle_leases: bool,
    ) -> Result<Value> {
        let page = check_limit(limit)?;
        if let Some(v) = project {
            check_value(v, MAX_SCOPE, "project", true)?;
        }
        if let Some(v) = task {
            check_value(v, MAX_SCOPE, "task", true)?;
        }
        if settle_leases {
            let now = now();
            let tx = client.transaction().await?;
            let mut events = Vec::new();
            lease::settle_due(&tx, now, &mut events, None).await?;
            append(&tx, events, now, None).await?;
            super::db::commit(tx).await?;
        }
        self.roster_read(&*client, agent_id, project, task, page)
            .await
    }

    /// The read half of `_roster`: agents, idle count, status ages and the
    /// lease page, all through one client (a snapshot's transaction).
    pub async fn roster_read(
        &self,
        client: &impl GenericClient,
        agent_id: &str,
        project: Option<&Value>,
        task: Option<&Value>,
        limit: i64,
    ) -> Result<Value> {
        let mut clauses = vec![
            "agent_id<>$1".to_string(),
            "credential_hash IS NOT NULL".to_string(),
        ];
        let mut values: Vec<String> = vec![agent_id.to_string()];
        for (key, value) in [("project", project), ("task", task)] {
            if let Some(v) = value {
                let s = check_value(v, MAX_SCOPE, key, true)?;
                values.push(s.to_string());
                clauses.push(format!("{key}=${}", values.len()));
            }
        }
        let now = now();
        let n = values.len();
        let leased = format!(
            "(attachment_id IS NOT NULL AND coalesce(lease_until,0)>${}::double precision)",
            n + 1
        );
        let recent = format!(
            "last_activity>CASE WHEN {leased} THEN ${}::double precision ELSE ${}::double precision END",
            n + 2,
            n + 3
        );
        let order_leased = format!(
            "(attachment_id IS NOT NULL AND coalesce(lease_until,0)>${}::double precision)",
            n + 4
        );
        let sql = format!(
            "SELECT * FROM coordination_agents WHERE {} AND {recent} ORDER BY {order_leased} DESC,\
             last_activity DESC,agent_id LIMIT ${}",
            clauses.join(" AND "),
            n + 5
        );
        let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
            values.iter().map(|v| v as _).collect();
        let windows = (now, now - ATTACHED_IDLE_WINDOW, now - ACTIVE_WINDOW);
        let page = limit + 1;
        params.push(&windows.0);
        params.push(&windows.1);
        params.push(&windows.2);
        params.push(&now);
        params.push(&page);
        let rows: Vec<AgentRow> = client
            .query(&sql, &params)
            .await?
            .iter()
            .map(AgentRow::from_row)
            .collect();
        let idle_sql = format!(
            "SELECT count(*) AS n FROM coordination_agents WHERE {} AND NOT {recent}",
            clauses.join(" AND ")
        );
        let mut idle_params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
            values.iter().map(|v| v as _).collect();
        idle_params.push(&windows.0);
        idle_params.push(&windows.1);
        idle_params.push(&windows.2);
        let idle: i64 = client.query_one(&idle_sql, &idle_params).await?.get(0);
        let mut agents = Vec::new();
        for row in rows.iter().take(limit as usize) {
            let mut agent = self.public(row);
            agent["status_expires_at"] = opt_f64(row.status_expires_at);
            agent["status_overdue"] = json!(row.status_expires_at.is_some_and(|e| now > e));
            agents.push(agent);
        }
        self.stamp_status_age(client, &mut agents, now).await?;
        let leases = lease::lease_page(client, None, MAX_PAGE, now).await?;
        Ok(
            json!({"agents": agents, "truncated": rows.len() as i64 > limit,
                  "idle_omitted": idle, "leases": leases["leases"],
                  "leases_truncated": leases["truncated"]}),
        )
    }

    /// `_stamp_status_age`.
    async fn stamp_status_age(
        &self,
        c: &impl GenericClient,
        agents: &mut [Value],
        now: f64,
    ) -> Result<()> {
        if agents.is_empty() {
            return Ok(());
        }
        let ids: Vec<String> = agents
            .iter()
            .map(|a| a["agent_id"].as_str().unwrap_or("").to_string())
            .collect();
        let set_at: std::collections::HashMap<String, f64> = c
            .query(
                "SELECT agent_id,max(created_at) AS set_at FROM coordination_events \
                 WHERE agent_id=ANY($1) AND CASE event WHEN 'register' THEN true \
                 WHEN 'update' THEN (payload::jsonb->'fields') ? 'status' ELSE false END \
                 GROUP BY agent_id",
                &[&ids],
            )
            .await?
            .iter()
            .map(|r| (r.get::<_, String>(0), r.get::<_, f64>(1)))
            .collect();
        let mut oldest: Option<f64> = None;
        if set_at.len() < agents.len() {
            oldest = c
                .query_one("SELECT min(created_at) AS t FROM coordination_events", &[])
                .await?
                .get(0);
        }
        for agent in agents.iter_mut() {
            let id = agent["agent_id"].as_str().unwrap_or("").to_string();
            let when = set_at.get(&id).copied();
            let (age, text) = if let Some(when) = when {
                (Some(now - when), relative_time(when, now))
            } else if let Some(oldest) = oldest.filter(|o| now - o >= 60.0) {
                (
                    Some(now - oldest),
                    format!("more than {}", relative_time(oldest, now)),
                )
            } else {
                (None, "unknown".to_string())
            };
            agent["status_set_at"] = opt_f64(when);
            agent["status_age"] = json!(text);
            let status = agent["status"].as_str().unwrap_or("").to_string();
            agent["status_stale"] =
                json!(!status.is_empty() && age.is_some_and(|a| a >= STATUS_STALE_AFTER));
        }
        Ok(())
    }

    // ---- attachments ------------------------------------------------------------

    /// `_listener_deadline`.
    fn listener_deadline(value: Option<&Value>, now: f64) -> Result<Option<Value>> {
        let Some(value) = value.filter(|v| !v.is_null()) else {
            return Ok(None);
        };
        let ok = matches!(value, Value::Number(_))
            && canon::as_f64(value).is_some_and(|v| v >= 0.0 && v.is_finite());
        if !ok {
            return Err(BoardError::code("invalid_ring_armed_until"));
        }
        Ok(Some(min_keep_type(value, now + ATTACHMENT_LEASE)))
    }

    /// `deadline or 0`.
    fn deadline_or_zero(deadline: &Option<Value>) -> Value {
        match deadline {
            Some(v) if canon::as_f64(v).is_some_and(|x| x != 0.0) => v.clone(),
            _ => json!(0),
        }
    }

    /// `_record_listener`.
    async fn record_listener(
        &self,
        c: &impl GenericClient,
        row: &AgentRow,
        deadline: &Option<Value>,
    ) -> Result<()> {
        if deadline.is_some() || row.has_capability("ring_armed_until") {
            let patch = json!({"ring_armed_until": Self::deadline_or_zero(deadline)});
            c.execute(
                "UPDATE coordination_agents SET capabilities=capabilities || $1 WHERE agent_id=$2",
                &[&patch, &row.agent_id],
            )
            .await?;
        }
        Ok(())
    }

    /// `attach`.
    #[allow(clippy::too_many_arguments)]
    pub async fn attach(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        attachment_id: &Value,
        wake_enabled: Option<&Value>,
        ring: Option<&Value>,
        ring_armed_until: Option<&Value>,
    ) -> Result<Value> {
        let attachment_id = check_value(attachment_id, 120, "attachment_id", false)?.to_string();
        let wake_enabled = wake_enabled.cloned().unwrap_or(json!(false));
        Self::fields(&[("wake_enabled".into(), wake_enabled.clone())])?;
        let wake_enabled = wake_enabled.as_bool().expect("validated bool");
        let ring = ring.filter(|v| !v.is_null());
        if ring.is_some_and(|r| !r.is_boolean()) {
            return Err(BoardError::code("invalid_capabilities"));
        }
        let tx = client.transaction().await?;
        let row = self
            .auth(&tx, principal, agent_id, credential, true)
            .await?;
        let now = now();
        let armed_until = Self::listener_deadline(ring_armed_until, now)?;
        let alive = row.attachment_id.is_some() && row.lease_until.unwrap_or(0.0) > now;
        if alive && row.attachment_id.as_deref() != Some(attachment_id.as_str()) {
            return Err(BoardError::code("attachment_busy"));
        }
        let generation = if alive {
            row.generation
        } else {
            row.generation + 1
        };
        let started = row.attachment_id.as_deref() != Some(attachment_id.as_str());
        let lease_until = now + ATTACHMENT_LEASE;
        tx.execute(
            "UPDATE coordination_agents SET attachment_id=$1,generation=$2,lease_until=$3,\
             last_activity=CASE WHEN $4 THEN $5 ELSE last_activity END,\
             wake_enabled=$6,lifecycle='attached' WHERE agent_id=$7",
            &[
                &attachment_id,
                &generation,
                &lease_until,
                &started,
                &now,
                &wake_enabled,
                &agent_id,
            ],
        )
        .await?;
        if let Some(ring) = ring {
            let patch = json!({"ring": ring});
            tx.execute(
                "UPDATE coordination_agents SET capabilities=capabilities || $1 WHERE agent_id=$2",
                &[&patch, &agent_id],
            )
            .await?;
        }
        self.record_listener(&tx, &row, &armed_until).await?;
        let mailbox = mail::mailbox_state(&tx, agent_id).await?;
        let offer = wake::serve_wake(&tx, agent_id, now).await?;
        append(
            &tx,
            vec![
                Event::new(
                    "attach",
                    &json!({"generation": generation, "lease_until": float_value(lease_until),
                        "wake_enabled": wake_enabled, "renewed": alive}),
                )
                .principal(principal)
                .agent(agent_id)
                .scope(&row.project, &row.task),
            ],
            now,
            None,
        )
        .await?;
        super::db::commit(tx).await?;
        let mut out = json!({"agent_id": agent_id, "generation": generation,
                             "lease_until": float_value(lease_until),
                             "ring_armed_until": Self::deadline_or_zero(&armed_until)});
        merge(&mut out, mailbox);
        out["wake"] = offer;
        Ok(out)
    }

    /// `_attachment`.
    pub fn attachment(row: &AgentRow, attachment_id: &Value, generation: i64) -> Result<()> {
        let matches = row
            .attachment_id
            .as_deref()
            .is_some_and(|a| !a.is_empty() && attachment_id.as_str() == Some(a));
        if !matches || row.generation != generation || row.lease_until.unwrap_or(0.0) <= now() {
            return Err(BoardError::code("stale_attachment"));
        }
        Ok(())
    }

    /// `check_attachment`.
    pub async fn check_attachment(
        &self,
        c: &impl GenericClient,
        principal: &str,
        agent_id: &str,
        credential: &str,
        attachment_id: &Value,
        generation: i64,
    ) -> Result<()> {
        let row = self.auth(c, principal, agent_id, credential, false).await?;
        Self::attachment(&row, attachment_id, generation)
    }

    /// `heartbeat`.
    #[allow(clippy::too_many_arguments)]
    pub async fn heartbeat(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        attachment_id: &Value,
        generation: i64,
        active: Option<&Value>,
        ring_armed_until: Option<&Value>,
        name: Option<&Value>,
    ) -> Result<Value> {
        let active = active.cloned().unwrap_or(json!(false));
        let Some(active) = active.as_bool() else {
            return Err(BoardError::code("invalid_active"));
        };
        let name = match name.filter(|v| !v.is_null()) {
            Some(n) => Some(Self::check_name(n)?),
            None => None,
        };
        let tx = client.transaction().await?;
        let row = self
            .auth(&tx, principal, agent_id, credential, true)
            .await?;
        Self::attachment(&row, attachment_id, generation)?;
        let now = now();
        let armed_until = Self::listener_deadline(ring_armed_until, now)?;
        let until = now + ATTACHMENT_LEASE;
        tx.execute(
            "UPDATE coordination_agents SET lease_until=$1,\
             last_activity=CASE WHEN $2 THEN $3 ELSE last_activity END WHERE agent_id=$4",
            &[&until, &active, &now, &agent_id],
        )
        .await?;
        if let Some(n) = &name
            && *n != row.harness_name
        {
            tx.execute(
                "UPDATE coordination_agents SET harness_name=$1 WHERE agent_id=$2",
                &[n, &agent_id],
            )
            .await?;
        }
        if let Some((n, src, at)) = name
            .as_deref()
            .and_then(|n| Self::name_change(&row, n, "harness", now))
        {
            tx.execute(
                "UPDATE coordination_agents SET name=$1,name_source=$2,name_set_at=$3 WHERE agent_id=$4",
                &[&n, &src, &at, &agent_id],
            )
            .await?;
            append(
                &tx,
                vec![Self::name_event(&row, &n, &src, principal, "agent")],
                now,
                None,
            )
            .await?;
        }
        self.record_listener(&tx, &row, &armed_until).await?;
        let mailbox = mail::mailbox_state(&tx, agent_id).await?;
        let offer = wake::serve_wake(&tx, agent_id, now).await?;
        super::db::commit(tx).await?;
        let mut out = json!({"agent_id": agent_id, "generation": generation,
                             "lease_until": float_value(until),
                             "ring_armed_until": Self::deadline_or_zero(&armed_until)});
        merge(&mut out, mailbox);
        out["wake"] = offer;
        Ok(out)
    }

    /// `detach`.
    pub async fn detach(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        attachment_id: &Value,
        generation: i64,
    ) -> Result<Value> {
        let tx = client.transaction().await?;
        let row = self
            .auth(&tx, principal, agent_id, credential, true)
            .await?;
        Self::attachment(&row, attachment_id, generation)?;
        tx.execute(
            "UPDATE coordination_agents SET attachment_id=NULL,lease_until=NULL,\
             generation=generation+1,lifecycle='detached' WHERE agent_id=$1",
            &[&agent_id],
        )
        .await?;
        let mut events = vec![
            Event::new("detach", &json!({"generation": generation}))
                .principal(principal)
                .agent(agent_id)
                .scope(&row.project, &row.task),
        ];
        let all = row.children_list();
        let named: Vec<Value> = all.iter().filter(|c| !hooked(c)).cloned().collect();
        if named.len() != all.len() {
            let list = Value::Array(named);
            tx.execute(
                "UPDATE coordination_agents SET children=$1 WHERE agent_id=$2",
                &[&list, &agent_id],
            )
            .await?;
            events.push(
                Event::new(
                    "update",
                    &json!({"fields": {"children": list}, "before": {"children": row.children}}),
                )
                .actor("daemon")
                .principal(principal)
                .agent(agent_id)
                .scope(&row.project, &row.task),
            );
        }
        append(&tx, events, now(), None).await?;
        super::db::commit(tx).await?;
        Ok(json!({"detached": true}))
    }

    // ---- the Stop hook's questions ---------------------------------------------

    /// `park_gate`.
    pub async fn park_gate(
        &self,
        c: &impl GenericClient,
        agent_id: &str,
        principal: &str,
        since: Option<f64>,
    ) -> Result<Value> {
        let Some(row) = c
            .query_opt(
                "SELECT * FROM coordination_agents WHERE agent_id=$1 AND principal=$2 \
                 AND credential_hash IS NOT NULL",
                &[&agent_id, &principal],
            )
            .await?
            .map(|r| AgentRow::from_row(&r))
        else {
            return Ok(json!({"gate": "allow", "reason": "unknown_agent"}));
        };
        let now = now();
        if let Some(park) = wake::live_park(&row, now) {
            if let Some(since) = since {
                let latest: Option<f64> = c
                    .query_one(
                        "SELECT max(delivered_at) AS created_at FROM (SELECT CASE WHEN \
                         m.wake->>'queued'='true' THEN w.served_at ELSE w.created_at END AS delivered_at \
                         FROM coordination_wakes w LEFT JOIN coordination_messages m \
                         ON m.message_id=w.message_id AND m.recipient_agent_id=w.recipient_agent_id \
                         WHERE w.recipient_agent_id=$1 AND w.decision='rung') delivered WHERE delivered_at>$2",
                        &[&agent_id, &since],
                    )
                    .await?
                    .get(0);
                if let Some(latest) = latest
                    && park.park_set_at.is_none_or(|s| s <= latest)
                {
                    return Ok(json!({"gate": "block", "reason": "not_updated_this_turn"}));
                }
            }
            return Ok(json!({"gate": "allow", "reason": "parked"}));
        }
        if let Some(since) = since {
            let updated = c
                .query_opt(
                    "SELECT 1 AS hit FROM coordination_events WHERE agent_id=$1 AND created_at>$2 \
                     AND CASE event WHEN 'register' THEN true WHEN 'update' THEN \
                     (payload::jsonb->'fields') ?| array['status','park_reason','park_needs',\
                     'park_clear_by','park_resume','park_expires'] ELSE false END LIMIT 1",
                    &[&agent_id, &since],
                )
                .await?;
            if updated.is_none() {
                return Ok(json!({"gate": "block", "reason": "not_updated_this_turn"}));
            }
        }
        if done_status(&row.status) {
            return Ok(json!({"gate": "allow", "reason": "done"}));
        }
        Ok(json!({"gate": "block", "reason": "no_park"}))
    }

    /// `woke`.
    pub async fn woke(
        &self,
        client: &mut tokio_postgres::Client,
        agent_id: &str,
        principal: &str,
    ) -> Result<Value> {
        let tx = client.transaction().await?;
        let Some(row) = tx
            .query_opt(
                "SELECT * FROM coordination_agents WHERE agent_id=$1 AND principal=$2 \
                 AND credential_hash IS NOT NULL FOR UPDATE",
                &[&agent_id, &principal],
            )
            .await?
            .map(|r| AgentRow::from_row(&r))
        else {
            return Ok(json!({"recorded": false, "reason": "unknown_agent"}));
        };
        let now = now();
        let rings: i64 = tx
            .query_one(
                "SELECT count(*) AS n FROM coordination_wakes WHERE recipient_agent_id=$1 \
                 AND served_at IS NOT NULL AND served_at>$2",
                &[&agent_id, &(now - 3600.0)],
            )
            .await?
            .get(0);
        append(
            &tx,
            vec![
                Event::new("woke", &json!({"rings": rings}))
                    .principal(principal)
                    .agent(agent_id)
                    .recipient(agent_id)
                    .scope(&row.project, &row.task),
            ],
            now,
            None,
        )
        .await?;
        super::db::commit(tx).await?;
        Ok(json!({"recorded": true, "rings": rings}))
    }
}

/// `_parent_children`.
pub fn parent_children(current: &[Value], labels: &[String], now: f64) -> Vec<Value> {
    let hooked_live: Vec<Value> = live_children(current, now)
        .into_iter()
        .filter(hooked)
        .collect();
    let taken: Vec<&str> = hooked_live
        .iter()
        .filter_map(|c| c.get("label").and_then(Value::as_str))
        .collect();
    let mut since: Map<String, Value> = Map::new();
    for child in current.iter().filter(|c| !hooked(c)) {
        if let (Some(label), Some(s)) = (
            child.get("label").and_then(Value::as_str),
            child.get("since"),
        ) {
            since.insert(label.to_string(), s.clone());
        }
    }
    let mut named: Vec<Value> = labels
        .iter()
        .filter(|l| !taken.contains(&l.as_str()))
        .map(|l| {
            obj(vec![
                ("label", json!(l)),
                (
                    "since",
                    since.get(l).cloned().unwrap_or_else(|| float_value(now)),
                ),
            ])
        })
        .collect();
    named.extend(hooked_live);
    named
}

/// Python truthiness for a JSON value.
pub fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => canon::as_f64(&Value::Number(n.clone())).is_some_and(|x| x != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `str(uuid.UUID(s))` when `s` parses as Python's uuid.UUID does, else None.
pub fn python_uuid_canonical(s: &str) -> Option<String> {
    // uuid.UUID strips "urn:", "uuid:", braces and hyphens, then needs 32 hex.
    let mut hex: &str = s;
    hex = hex.strip_prefix("urn:").unwrap_or(hex);
    hex = hex.strip_prefix("uuid:").unwrap_or(hex);
    let hex = hex.trim_matches(|c| c == '{' || c == '}').replace('-', "");
    if hex.len() != 32 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    uuid::Uuid::parse_str(&hex)
        .ok()
        .map(|u| u.hyphenated().to_string())
}

/// `json.dumps(..., ensure_ascii=True)` escaping applied to already-dumped text.
fn ascii_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if (' '..='~').contains(&c) || (c.is_ascii() && (c as u32) < 0x20) {
            out.push(c);
        } else {
            let mut buf = [0u16; 2];
            for unit in c.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub fn merge(target: &mut Value, extra: Value) {
    if let (Value::Object(t), Value::Object(e)) = (target, extra) {
        for (k, v) in e {
            t.insert(k, v);
        }
    }
}

/// A SET parameter for one agent column, typed as its column.
fn column_param(key: &str, value: &Value) -> Box<dyn tokio_postgres::types::ToSql + Sync + Send> {
    match key {
        "capabilities" | "children" => Box::new(value.clone()),
        "wake_enabled" => Box::new(value.as_bool()),
        "park_expires" | "park_set_at" | "status_expires_at" | "name_set_at" => {
            Box::new(canon::as_f64(value))
        }
        "park_reason" => Box::new(value.as_str().map(str::to_string)),
        _ => Box::new(value.as_str().unwrap_or("").to_string()),
    }
}

#[cfg(test)]
mod proof_tests {
    #[test]
    fn ensure_ascii_escapes_del_and_non_ascii() {
        // CPython 3.11: json.dumps(["a\x7f\u00e9\U0001f600"], ensure_ascii=True).
        let input = "[\"a\u{7f}\u{e9}\u{1f600}\"]";
        let want = r#"["a\u007f\u00e9\ud83d\ude00"]"#;
        assert_eq!(super::ascii_escape(input), want);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn done_status_matches_python_word_boundaries() {
        // Python DONE_STATUS accepts combining marks and join controls as
        // non-word characters, unlike regex's Unicode \w class.
        for status in [
            "Done\u{200c}",
            "Done\u{200d}",
            "\u{301}Done",
            "Done\u{301}",
            "Completed!",
            "fınished",
            "fİnished",
        ] {
            assert!(done_status(status), "Python accepts {status:?}");
        }
        for status in ["_Done", "Done_", "Done2", "Done²", "2Done", "undone"] {
            assert!(!done_status(status), "Python rejects {status:?}");
        }
    }
}
