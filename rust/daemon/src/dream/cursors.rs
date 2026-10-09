//! Exact PostgreSQL acknowledgement (`service_dream.py:959-1216`).

use super::token;
use crate::storage::Storage;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio_postgres::Client;

/// Python distinguishes ValueError refusals from operational failures; an
/// exception's text is never evidence for its class (`service.py:3736`).
#[derive(Debug)]
enum Failure {
    Refusal(String),
    Operational(String),
}

impl Failure {
    fn text(&self) -> &str {
        match self {
            Self::Refusal(s) | Self::Operational(s) => s,
        }
    }
}

impl From<String> for Failure {
    fn from(value: String) -> Self {
        Self::Refusal(value)
    }
}

impl From<&str> for Failure {
    fn from(value: &str) -> Self {
        Self::Refusal(value.into())
    }
}

#[derive(Clone)]
pub struct Policy {
    pub eligible_sources: Vec<String>,
    pub exclude_sources: Vec<String>,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            eligible_sources: Vec::new(),
            exclude_sources: ["consolidation", "reflection", "status", "log", "digest"]
                .map(String::from)
                .to_vec(),
        }
    }
}

pub struct Cursors {
    storage: Arc<Storage>,
    resident: Mutex<Resident>,
}

#[derive(Default)]
struct Resident {
    loaded: bool,
    initialized: bool,
    cursor: f64,
    secret: Option<String>,
    error: Option<String>,
    retryable: bool,
    entries: Vec<Entry>,
    policy: Policy,
}

struct Entry {
    id: i64,
    ts: f64,
    source: String,
    state: Option<String>,
    payload: Value,
}

impl Cursors {
    pub fn new(storage: Arc<Storage>) -> Arc<Self> {
        Arc::new(Self {
            storage,
            resident: Mutex::new(Resident::default()),
        })
    }

    /// Like Python's service thread, an admitted write owns its completion even
    /// when its request stops waiting. No cancellation can strand a BEGIN on
    /// the shared connection or release its writer guard before COMMIT/ROLLBACK.
    pub async fn execute(self: &Arc<Self>, action: Value) -> Value {
        if crate::mutants::active("dream-cancel-drop-task") {
            return self.execute_inner(action).await;
        }
        let owned = self.clone();
        tokio::spawn(async move { owned.execute_inner(action).await })
            .await
            .unwrap_or_else(|_| json!({"error":"dream operation task failed"}))
    }

    async fn execute_inner(&self, action: Value) -> Value {
        let mut resident = self.resident.lock().await;
        let _writer = self.storage.writer_guard().await;
        let client = self.storage.client();
        let result = match action["op"].as_str().unwrap_or("") {
            "initialize" => {
                let policy = policy(&action, false);
                match initialize(client, &policy).await {
                    Ok(value) => match load(client, &mut resident).await {
                        Ok(()) => {
                            apply_initialization(&mut resident, &value);
                            resident.policy = policy;
                            Ok(value)
                        }
                        Err(e) => Err(e),
                    },
                    Err(e) => Err(e),
                }
            }
            "acknowledge" => match acknowledgement_args(&action) {
                Ok((ids, timestamp)) => acknowledge(client, &ids, timestamp).await,
                Err(e) => Err(e),
            },
            "pull" => {
                resident.policy = policy(&action, true);
                match ensure_initialized(client, &mut resident).await {
                    Ok(()) => {
                        // dream_pull retries even a transient failure from its
                        // first lazy init; dream_commit never performs this retry.
                        if resident.error.is_some() && resident.retryable {
                            initialize_resident(client, &mut resident).await;
                        }
                        pull(&resident, action["limit"].as_i64().unwrap_or(20))
                    }
                    Err(e) => Err(e),
                }
            }
            "commit" => match ensure_initialized(client, &mut resident).await {
                Ok(()) => Ok(commit(client, &mut resident, &action["commit_token"]).await),
                Err(e) => Err(e),
            },
            op => Err(format!("unknown operation: {op}").into()),
        };
        result.unwrap_or_else(|e| json!({"error":e.text()}))
    }
}

fn policy(action: &Value, service_defaults: bool) -> Policy {
    let strings = |key: &str| {
        action[key].as_array().map(|a| {
            a.iter()
                .filter_map(|s| s.as_str().map(String::from))
                .collect::<Vec<_>>()
        })
    };
    Policy {
        eligible_sources: strings("eligible_sources").unwrap_or_default(),
        exclude_sources: strings("exclude_sources").unwrap_or_else(|| {
            if service_defaults {
                ["consolidation", "reflection", "status", "log", "digest"]
                    .map(String::from)
                    .to_vec()
            } else {
                Vec::new()
            }
        }),
    }
}

fn db_error(e: tokio_postgres::Error) -> Failure {
    let text = if let Some(db) = e.as_db_error() {
        let mut text = db.message().to_string();
        for (name, part) in [
            ("DETAIL", db.detail()),
            ("HINT", db.hint()),
            ("CONTEXT", db.where_()),
        ] {
            if let Some(part) = part {
                text.push_str(&format!("\n{name}:  {part}"));
            }
        }
        text
    } else {
        e.to_string()
    };
    Failure::Operational(text)
}

async fn meta(client: &Client, key: &str) -> Result<Option<Value>, Failure> {
    Ok(client
        .query_opt("SELECT value FROM meta WHERE key = $1", &[&key])
        .await
        .map_err(db_error)?
        .map(|r| r.get(0)))
}

fn cursor_value(value: Option<Value>) -> Result<f64, Failure> {
    match value {
        None => Ok(0.0),
        Some(Value::Number(n)) => n.as_f64().ok_or_else(|| "invalid display cursor".into()),
        Some(Value::String(s)) => s.parse().map_err(|_| {
            format!(
                "could not convert string to float: {}",
                crate::storage::py_repr(&s)
            )
            .into()
        }),
        _ => Err(Failure::Operational("invalid display cursor type".into())),
    }
}

async fn finish(client: &Client, result: Result<Value, Failure>) -> Result<Value, Failure> {
    match result {
        Ok(value) => {
            client.batch_execute("COMMIT").await.map_err(db_error)?;
            Ok(value)
        }
        Err(e) => {
            client.batch_execute("ROLLBACK").await.map_err(db_error)?;
            Err(e)
        }
    }
}

async fn initialize(client: &Client, policy: &Policy) -> Result<Value, Failure> {
    client.batch_execute("BEGIN").await.map_err(db_error)?;
    let result = async {
        let unclassified = client.query("SELECT id FROM entries WHERE dream_state IS NULL ORDER BY id FOR UPDATE",&[]).await.map_err(db_error)?;
        let cursor = cursor_value(meta(client,"cortex_dream_cursor").await?)?;
        if !unclassified.is_empty() && !cursor.is_finite() {
            return Err("invalid_legacy_dream_cursor: unclassified entries require a finite cortex_dream_cursor".into());
        }
        let secret = match meta(client,"dream_ack_secret_v1").await? {
            None => {
                let mut bytes = [0u8;32];
                getrandom::fill(&mut bytes).map_err(|_| Failure::Operational("dream secret generation failed".into()))?;
                let secret = hex::encode(bytes);
                client.execute("INSERT INTO meta (key,value) VALUES ('dream_ack_secret_v1',$1::jsonb)", &[&json!(secret)]).await.map_err(db_error)?;
                secret
            },
            Some(value) => {
                let secret = value.as_str().filter(|s| s.len()==64 && token::generation(s).is_ok())
                    .ok_or_else(|| "invalid_dream_ack_secret: dream_ack_secret_v1 must be a 64-character hexadecimal string".to_string())?;
                secret.to_string()
            },
        };
        let mut updated = serde_json::Map::new();
        if !unclassified.is_empty() || crate::mutants::active("dream-init-classify-explicit") {
            let predicate = if crate::mutants::active("dream-init-classify-explicit") {"TRUE"} else {"dream_state IS NULL"};
            let (source_op, sources) = if policy.eligible_sources.is_empty() {
                ("<> ALL", &policy.exclude_sources)
            } else { ("= ANY", &policy.eligible_sources) };
            let statement = format!("UPDATE entries SET dream_state='legacy-covered' WHERE {predicate} AND source {source_op}($1) AND ts <= $2 RETURNING id");
            for r in client.query(&statement,&[sources,&cursor]).await.map_err(db_error)? {
                updated.insert(r.get::<_,i64>(0).to_string(),json!("legacy-covered"));
            }
            for r in client.query("UPDATE entries SET dream_state='pending' WHERE dream_state IS NULL RETURNING id",&[]).await.map_err(db_error)? {
                updated.insert(r.get::<_,i64>(0).to_string(),json!("pending"));
            }
        }
        Ok(json!({"secret":secret,"dream_cursor":cursor,"updated_states":updated}))
    }.await;
    finish(client, result).await
}

async fn load(client: &Client, resident: &mut Resident) -> Result<(), Failure> {
    let rows = client.query("SELECT id,text,ts,episode_id,source,authority,distortion_tolerance,dream_state FROM entries ORDER BY id", &[]).await.map_err(db_error)?;
    let entries = rows.into_iter().map(|r| {
        let id: i64 = r.get(0);
        let ts: f64 = r.get(2);
        let source: String = r.get(4);
        Entry { id, ts, source: source.clone(), state: r.get(7), payload: json!({
            "text":r.get::<_,String>(1),"timestamp":ts,"episode_id":r.get::<_,Option<String>>(3),
            "db_id":id,"dream_id":null,"source":source,"authority":r.get::<_,Option<String>>(5),
            "distortion_tolerance":r.get::<_,Option<String>>(6),
        }) }
    }).collect();
    resident.entries = entries;
    resident.loaded = true;
    Ok(())
}

fn apply_initialization(resident: &mut Resident, value: &Value) {
    resident.secret = value["secret"].as_str().map(String::from);
    resident.cursor = value["dream_cursor"].as_f64().unwrap_or(0.0);
    for entry in &mut resident.entries {
        if let Some(state) = value["updated_states"][entry.id.to_string()].as_str() {
            entry.state = Some(state.into());
        }
    }
    resident.initialized = true;
    resident.error = None;
    resident.retryable = false;
}

async fn ensure_initialized(client: &Client, resident: &mut Resident) -> Result<(), Failure> {
    if !resident.loaded {
        load(client, resident).await?;
    }
    if !resident.initialized {
        initialize_resident(client, resident).await;
    }
    Ok(())
}

async fn initialize_resident(client: &Client, resident: &mut Resident) {
    match initialize(client, &resident.policy).await {
        Ok(value) => apply_initialization(resident, &value),
        Err(error) => {
            resident.retryable = matches!(error, Failure::Operational(_));
            resident.error = Some(
                if error.text().starts_with("invalid_legacy_dream_cursor:") {
                    error.text().to_string()
                } else {
                    format!("dream_ack_initialization_failed: {}", error.text())
                },
            );
            resident.initialized = true;
        }
    }
}

fn safe_cursor(resident: &Resident) -> f64 {
    if resident.cursor.is_finite() {
        resident.cursor
    } else {
        0.0
    }
}

fn pull(resident: &Resident, limit: i64) -> Result<Value, Failure> {
    let cursor = safe_cursor(resident);
    if let Some(error) = &resident.error {
        return Ok(
            json!({"error":error.split(':').next().unwrap_or(error),"detail":error,"cursor":cursor,"count":0,"entries":[]}),
        );
    }
    let p = &resident.policy;
    let mut entries: Vec<&Entry> = resident
        .entries
        .iter()
        .filter(|e| {
            e.state.as_deref() == Some("pending")
                && (crate::mutants::active("dream-pull-ignore-source")
                    || if p.eligible_sources.is_empty() {
                        !p.exclude_sources.contains(&e.source)
                    } else {
                        p.eligible_sources.contains(&e.source)
                    })
        })
        .collect();
    entries.sort_by(|a, b| {
        a.ts.partial_cmp(&b.ts)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.id.cmp(&b.id))
    });
    if crate::mutants::active("dream-pull-reverse") {
        entries.reverse();
    }
    entries.truncate(
        usize::try_from(limit.max(0))
            .unwrap_or(usize::MAX)
            .min(token::MAX_ENTRY_IDS),
    );
    let mut out = json!({"cursor":cursor,"count":entries.len(),"entries":entries.iter().map(|e|e.payload.clone()).collect::<Vec<_>>()});
    if !entries.is_empty() {
        let secret = resident
            .secret
            .as_deref()
            .ok_or("dream acknowledgement secret is unavailable")?;
        let generation = token::generation(secret)?;
        let newest = entries
            .iter()
            .map(|e| e.ts)
            .reduce(|current, next| if next > current { next } else { current })
            .unwrap_or(cursor);
        let ids: Vec<_> = entries.iter().map(|e| e.id).collect();
        out["commit_token"] = json!(token::issue(secret, &generation, &ids, newest)?);
    }
    Ok(out)
}

fn acknowledgement_args(action: &Value) -> Result<(Vec<i64>, f64), Failure> {
    let invalid = "dream_ack_invalid_states: entry ids must be distinct positive integers";
    let ids: Vec<i64> = action["entry_ids"]
        .as_array()
        .ok_or(invalid)?
        .iter()
        .map(|i| i.as_i64().filter(|v| *v > 0).ok_or(invalid))
        .collect::<Result<_, _>>()?;
    if ids.is_empty() || ids.iter().collect::<HashSet<_>>().len() != ids.len() {
        return Err(invalid.into());
    }
    let timestamp = action["display_timestamp"]
        .as_f64()
        .filter(|v| v.is_finite())
        .ok_or("dream_ack_invalid_states: display timestamp must be finite")?;
    Ok((ids, timestamp))
}

async fn acknowledge(client: &Client, ids: &[i64], timestamp: f64) -> Result<Value, Failure> {
    client.batch_execute("BEGIN").await.map_err(db_error)?;
    let result = async {
        let rows = client.query("SELECT id,dream_state FROM entries WHERE id=ANY($1) FOR UPDATE", &[&ids]).await.map_err(db_error)?;
        let states: HashMap<i64,Option<String>> = rows.iter().map(|r|(r.get(0),r.get(1))).collect();
        let present: Vec<i64> = ids.iter().copied().filter(|i|states.contains_key(i)).collect();
        let missing: Vec<i64> = ids.iter().copied().filter(|i|!states.contains_key(i)).collect();
        let invalid: Vec<String> = present.iter().filter_map(|i| {
            let state = states[i].as_deref();
            if matches!(state,Some("pending"|"acknowledged")) {None} else {
                Some(format!("{i}={}",state.map(crate::storage::py_repr).unwrap_or_else(||"None".into())))
            }
        }).collect();
        if !invalid.is_empty() {return Err(format!("dream_ack_invalid_states: {}",invalid.join(",")).into());}
        let newly = if crate::mutants::active("dream-ack-skip-write") {0} else {
            let statement = if crate::mutants::active("dream-ack-all-pending") {
                "UPDATE entries SET dream_state='acknowledged' WHERE dream_state='pending' AND ($1::bigint[] IS NOT NULL) RETURNING id"
            } else {"UPDATE entries SET dream_state='acknowledged' WHERE id=ANY($1) AND dream_state='pending' RETURNING id"};
            client.query(statement,&[&present]).await.map_err(db_error)?.len()
        };
        let current = cursor_value(meta(client,"cortex_dream_cursor").await?)?;
        if !current.is_finite() {return Err("dream_ack_invalid_states: stored display cursor must be finite".into());}
        let cursor = if crate::mutants::active("dream-cursor-rewind") || timestamp>current {timestamp} else {current};
        client.execute("INSERT INTO meta (key,value) VALUES ('cortex_dream_cursor',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", &[&json!(cursor)]).await.map_err(db_error)?;
        Ok(json!({"acknowledged_ids":present,"missing_ids":missing,"newly_acknowledged":newly,"dream_cursor":cursor}))
    }.await;
    finish(client, result).await
}

async fn commit(client: &Client, resident: &mut Resident, input: &Value) -> Value {
    if let Some(error) = &resident.error {
        return json!({"error":error.split(':').next().unwrap_or(error),"detail":error});
    }
    let verified = (|| {
        let secret = resident
            .secret
            .as_deref()
            .ok_or("invalid_dream_commit_token")?;
        let generation = token::generation(secret).map_err(|_| "invalid_dream_commit_token")?;
        token::verify(
            input.as_str().ok_or("invalid_dream_commit_token")?,
            secret,
            &generation,
        )
        .map_err(|_| "invalid_dream_commit_token")
    })();
    let payload = match verified {
        Ok(p) => p,
        Err(e) => return json!({"error":e,"detail":e}),
    };
    match acknowledge(client, &payload.entry_ids, payload.display_timestamp).await {
        Ok(value) => {
            let settled: HashSet<i64> = ["acknowledged_ids", "missing_ids"]
                .iter()
                .flat_map(|k| {
                    value[k]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_i64)
                })
                .collect();
            for e in &mut resident.entries {
                if settled.contains(&e.id) {
                    e.state = Some("acknowledged".into());
                }
            }
            resident.cursor = value["dream_cursor"].as_f64().unwrap_or(resident.cursor);
            let mut out = json!({"dream_cursor":resident.cursor,"acknowledged":value["acknowledged_ids"].as_array().map_or(0,Vec::len),"newly_acknowledged":value["newly_acknowledged"]});
            let missing = value["missing_ids"].as_array().map_or(0, Vec::len);
            if missing > 0 {
                out["missing"] = json!(missing);
            }
            out
        }
        Err(e) => {
            let code = match &e {
                Failure::Operational(_) => "dream_ack_persist_failed",
                Failure::Refusal(text) => match text.split(':').next().unwrap_or(text) {
                    code @ ("dream_ack_invalid_states" | "dream_ack_missing_entries") => code,
                    _ => "dream_ack_rejected",
                },
            };
            json!({"error":code,"detail":e.text()})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equal_timestamps_keep_identity_order_including_signed_zero() {
        let mut resident = Resident {
            secret: Some("11".repeat(32)),
            ..Resident::default()
        };
        for (id, ts) in [(2, -0.0), (1, 0.0)] {
            resident.entries.push(Entry {
                id,
                ts,
                source: "notes".into(),
                state: Some("pending".into()),
                payload: json!({"db_id":id}),
            });
        }
        let pulled = pull(&resident, 20).unwrap();
        assert_eq!(pulled["entries"], json!([{"db_id":1},{"db_id":2}]));
    }
}
