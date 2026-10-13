//! The board's own Postgres session and its row shapes.
//!
//! `CoordinationConnection` (`storage/coordination.py:48-117`): a dedicated
//! connection, never the writer's, with `lock_timeout` 5 s and the public
//! search path; healed on next use after a restart, never mid-transaction
//! (a transaction holds the client, so a broken one fails with it).

use super::canon::float_value;
use super::error::{BoardError, Result};
use serde_json::{Map, Value, json};
use tokio_postgres::{Client, NoTls, Row};

/// Finish a transaction on the dedicated board client, independently of the
/// writer's global recovery marker. The driver rolls back on drop (including
/// cancellation), so the next operation cannot commit an abandoned body.
/// PostgreSQL silently turns COMMIT of an aborted block into ROLLBACK; probe
/// first so a swallowed statement failure cannot be reported as success.
pub async fn commit(tx: tokio_postgres::Transaction<'_>) -> Result<()> {
    tx.batch_execute("SELECT 1").await?;
    tx.commit().await?;
    Ok(())
}

#[cfg(test)]
#[path = "db_tests.rs"]
mod tests;

pub struct Conn {
    dsn: String,
    client: Option<Client>,
}

impl Conn {
    pub fn new(dsn: String) -> Conn {
        Conn { dsn, client: None }
    }

    async fn connect(&self) -> Result<Client> {
        let config: tokio_postgres::Config = self
            .dsn
            .parse()
            .map_err(|e: tokio_postgres::Error| BoardError::Internal(e.to_string()))?;
        let mut config = config;
        config.connect_timeout(std::time::Duration::from_secs(10));
        let (client, connection) = config.connect(NoTls).await?;
        tokio::spawn(async move {
            let _ = connection.await;
        });
        client
            .batch_execute("SET lock_timeout = '5s'; SET search_path TO public")
            .await?;
        Ok(client)
    }

    /// The live client, reconnecting when the previous one closed.
    pub async fn client(&mut self) -> Result<&mut Client> {
        if self.client.as_ref().is_none_or(Client::is_closed) {
            self.client = Some(self.connect().await?);
        }
        Ok(self.client.as_mut().expect("connected above"))
    }
}

pub fn opt_str(v: Option<&str>) -> Value {
    v.map_or(Value::Null, |s| Value::String(s.to_string()))
}

pub fn opt_f64(v: Option<f64>) -> Value {
    v.map_or(Value::Null, float_value)
}

/// One `coordination_agents` row (`SELECT *`).
#[derive(Debug, Clone)]
pub struct AgentRow {
    pub agent_id: String,
    pub principal: String,
    pub credential_hash: Option<String>,
    pub label: String,
    pub project: String,
    pub task: String,
    pub status: String,
    pub episode: String,
    pub capabilities: Value,
    pub wake_enabled: bool,
    pub created_at: f64,
    pub last_activity: f64,
    pub lifecycle: String,
    pub next_sequence: i64,
    pub attachment_id: Option<String>,
    pub generation: i64,
    pub lease_until: Option<f64>,
    pub status_expires_at: Option<f64>,
    pub children: Value,
    pub park_reason: Option<String>,
    pub park_needs: String,
    pub park_clear_by: String,
    pub park_resume: String,
    pub park_expires: Option<f64>,
    pub park_set_at: Option<f64>,
    pub parent_agent_id: Option<String>,
    pub parent_thread: Option<String>,
    pub name: String,
    pub name_source: String,
    pub name_set_at: Option<f64>,
    pub harness_name: String,
}

impl AgentRow {
    pub fn from_row(r: &Row) -> AgentRow {
        AgentRow {
            agent_id: r.get("agent_id"),
            principal: r.get("principal"),
            credential_hash: r.get("credential_hash"),
            label: r.get("label"),
            project: r.get("project"),
            task: r.get("task"),
            status: r.get("status"),
            episode: r.get("episode"),
            capabilities: r.get("capabilities"),
            wake_enabled: r.get("wake_enabled"),
            created_at: r.get("created_at"),
            last_activity: r.get("last_activity"),
            lifecycle: r.get("lifecycle"),
            next_sequence: r.get("next_sequence"),
            attachment_id: r.get("attachment_id"),
            generation: r.get("generation"),
            lease_until: r.get("lease_until"),
            status_expires_at: r.get("status_expires_at"),
            children: r.get("children"),
            park_reason: r.get("park_reason"),
            park_needs: r.get("park_needs"),
            park_clear_by: r.get("park_clear_by"),
            park_resume: r.get("park_resume"),
            park_expires: r.get("park_expires"),
            park_set_at: r.get("park_set_at"),
            parent_agent_id: r.get("parent_agent_id"),
            parent_thread: r.get("parent_thread"),
            name: r.get("name"),
            name_source: r.get("name_source"),
            name_set_at: r.get("name_set_at"),
            harness_name: r.get("harness_name"),
        }
    }

    pub fn capability(&self, key: &str) -> Option<&Value> {
        self.capabilities.as_object().and_then(|m| m.get(key))
    }

    pub fn has_capability(&self, key: &str) -> bool {
        self.capabilities
            .as_object()
            .is_some_and(|m| m.contains_key(key))
    }

    pub fn children_list(&self) -> Vec<Value> {
        self.children.as_array().cloned().unwrap_or_default()
    }

    /// `row[key]` as Python reads it (the update event's `before`).
    pub fn column(&self, key: &str) -> Value {
        match key {
            "label" => json!(self.label),
            "project" => json!(self.project),
            "task" => json!(self.task),
            "status" => json!(self.status),
            "episode" => json!(self.episode),
            "capabilities" => self.capabilities.clone(),
            "wake_enabled" => json!(self.wake_enabled),
            "children" => self.children.clone(),
            "park_reason" => opt_str(self.park_reason.as_deref()),
            "park_needs" => json!(self.park_needs),
            "park_clear_by" => json!(self.park_clear_by),
            "park_resume" => json!(self.park_resume),
            "park_expires" => opt_f64(self.park_expires),
            "park_set_at" => opt_f64(self.park_set_at),
            "status_expires_at" => opt_f64(self.status_expires_at),
            "name" => json!(self.name),
            "name_source" => json!(self.name_source),
            "name_set_at" => opt_f64(self.name_set_at),
            "parent_agent_id" => opt_str(self.parent_agent_id.as_deref()),
            "parent_thread" => opt_str(self.parent_thread.as_deref()),
            _ => Value::Null,
        }
    }
}

/// One `coordination_messages` row (`SELECT *`).
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct MessageRow {
    pub message_id: String,
    pub sender_agent_id: String,
    pub recipient_agent_id: String,
    pub sender_principal: String,
    pub project: String,
    pub task: String,
    pub text: Option<String>,
    pub reply_to: Option<String>,
    pub request_id: String,
    pub fingerprint: String,
    pub recipient_sequence: i64,
    pub hlc: String,
    pub created_at: f64,
    pub expires_at: f64,
    pub attempt_at: Option<f64>,
    pub attempt_generation: Option<i64>,
    pub attempts: i32,
    pub acknowledged_at: Option<f64>,
    pub first_read_at: Option<f64>,
    pub wake: Option<Value>,
    pub origin: String,
    pub maintainer_proof: Option<Value>,
    pub repudiated_at: Option<f64>,
}

impl MessageRow {
    pub fn from_row(r: &Row) -> MessageRow {
        MessageRow {
            message_id: r.get("message_id"),
            sender_agent_id: r.get("sender_agent_id"),
            recipient_agent_id: r.get("recipient_agent_id"),
            sender_principal: r.get("sender_principal"),
            project: r.get("project"),
            task: r.get("task"),
            text: r.get("text"),
            reply_to: r.get("reply_to"),
            request_id: r.get("request_id"),
            fingerprint: r.get("fingerprint"),
            recipient_sequence: r.get("recipient_sequence"),
            hlc: r.get("hlc"),
            created_at: r.get("created_at"),
            expires_at: r.get("expires_at"),
            attempt_at: r.get("attempt_at"),
            attempt_generation: r.get("attempt_generation"),
            attempts: r.get("attempts"),
            acknowledged_at: r.get("acknowledged_at"),
            first_read_at: r.get("first_read_at"),
            wake: r.get("wake"),
            origin: r.get("origin"),
            maintainer_proof: r.get("maintainer_proof"),
            repudiated_at: r.get("repudiated_at"),
        }
    }
}

/// One `coordination_leases` row, with the holder's label and name when
/// the query joins them.
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct LeaseRow {
    pub name: String,
    pub holder_agent_id: Option<String>,
    pub holder_principal: String,
    pub purpose: String,
    pub fence: i64,
    pub acquired_at: Option<f64>,
    pub expires_at: Option<f64>,
    pub expect: Option<i32>,
    pub expected_end: Option<f64>,
    pub freed_at: Option<f64>,
    pub label: Option<String>,
    pub holder_name: Option<String>,
}

impl LeaseRow {
    pub fn from_row(r: &Row) -> LeaseRow {
        let joined = r.columns().iter().any(|c| c.name() == "holder_name");
        LeaseRow {
            name: r.get("name"),
            holder_agent_id: r.get("holder_agent_id"),
            holder_principal: r.get("holder_principal"),
            purpose: r.get("purpose"),
            fence: r.get("fence"),
            acquired_at: r.get("acquired_at"),
            expires_at: r.get("expires_at"),
            expect: r.get("expect"),
            expected_end: r.get("expected_end"),
            freed_at: r.get("freed_at"),
            label: if joined { r.get("label") } else { None },
            holder_name: if joined { r.get("holder_name") } else { None },
        }
    }
}

/// A JSON object from key/value pairs, in the given order.
pub fn obj(pairs: Vec<(&str, Value)>) -> Value {
    let mut m = Map::new();
    for (k, v) in pairs {
        m.insert(k.to_string(), v);
    }
    Value::Object(m)
}
