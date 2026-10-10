//! `GET /api/agents?view=coordination` (`coordination.console_snapshot`,
//! `storage/coordination.py:1831-1899`): one read-only, repeatable-read
//! snapshot of board metadata for the Console. No registration, mailbox
//! read, prune or lease settlement; mail counts and events are limited to
//! the caller's principal; no payload, body, salt or credential leaves it.

use super::Board;
use super::canon::float_value;
use super::db::{obj, opt_str};
use super::error::{BoardError, Result};
use super::store::{Store, check_limit, now};
use serde_json::{Value, json};

const DETAILS: [&str; 9] = [
    "hinted",
    "not_needed",
    "rung",
    "withheld",
    "nudged",
    "no_path",
    "capped",
    "pull",
    "delivery",
];

impl Store {
    pub async fn console_snapshot(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        limit: i64,
    ) -> Result<Value> {
        let tx = client.transaction().await?;
        tx.batch_execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY; \
             SET LOCAL statement_timeout = '5s'",
        )
        .await?;
        let limit = check_limit(&json!(limit))?;
        let mut result = self.roster_read(&tx, "", None, None, limit).await?;
        let now = now();
        let ids: Vec<String> = result["agents"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|x| x["agent_id"].as_str().unwrap_or("").to_string())
                    .collect()
            })
            .unwrap_or_default();
        let counts: std::collections::HashMap<String, i64> = tx
            .query(
                "SELECT m.recipient_agent_id,count(*) AS n FROM coordination_messages m \
                 JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id \
                 WHERE a.principal=$1 AND m.acknowledged_at IS NULL AND m.expires_at>$2 \
                 AND m.recipient_agent_id=ANY($3) GROUP BY m.recipient_agent_id",
                &[&principal, &now, &ids],
            )
            .await?
            .iter()
            .map(|r| (r.get(0), r.get(1)))
            .collect();
        if let Some(agents) = result["agents"].as_array_mut() {
            for agent in agents {
                let mine = agent["principal"].as_str() == Some(principal);
                let id = agent["agent_id"].as_str().unwrap_or("").to_string();
                agent["pending_count"] = if mine {
                    json!(counts.get(&id).copied().unwrap_or(0))
                } else {
                    Value::Null
                };
            }
        }
        if let Some(leases) = result["leases"].as_array_mut() {
            for lease in leases {
                let expired = lease["expires_at"].as_f64().is_some_and(|e| e <= now);
                lease["expired"] = json!(expired);
            }
        }
        let rows = tx
            .query(
                "WITH visible_mail AS MATERIALIZED (\
                 SELECT message_id FROM coordination_events WHERE event='send' \
                 AND principal>=$1 AND principal<=$1 UNION \
                 SELECT e.message_id FROM coordination_agents a CROSS JOIN LATERAL \
                 (SELECT message_id FROM coordination_events WHERE event='send' \
                 AND recipient_agent_id>=a.agent_id AND recipient_agent_id<=a.agent_id \
                 ORDER BY recipient_agent_id,seq) e WHERE a.principal=$1), \
                 candidates AS (\
                 (SELECT e.* FROM coordination_events e WHERE principal>=$1 AND principal<=$1 \
                 AND event IN ('send','read','ack','attempt','served','woke','lease_expire') \
                 ORDER BY principal DESC,seq DESC LIMIT 101) UNION \
                 (SELECT e.* FROM visible_mail v CROSS JOIN LATERAL \
                 (SELECT * FROM coordination_events WHERE message_id>=v.message_id AND message_id<=v.message_id \
                 AND event IN ('send','read','ack','attempt','served','woke','lease_expire') \
                 ORDER BY message_id,event,created_at OFFSET 0) e \
                 ORDER BY e.seq DESC LIMIT 101) UNION \
                 (SELECT e.* FROM coordination_events e WHERE e.event='expire' AND EXISTS \
                 (SELECT 1 FROM jsonb_array_elements_text(e.payload::jsonb->'message_ids') AS ids(id) \
                 JOIN visible_mail v ON v.message_id=ids.id) ORDER BY e.seq DESC LIMIT 101)), \
                 timeline AS (SELECT e.seq,e.event,e.created_at,e.agent_id,e.recipient_agent_id,\
                 e.message_id,e.project,e.task, \
                 CASE e.event WHEN 'send' THEN e.payload::jsonb->>'wake' \
                 WHEN 'read' THEN e.payload::jsonb->>'path' END AS detail, \
                 CASE WHEN e.event='expire' THEN (SELECT count(*) FROM \
                 jsonb_array_elements_text(e.payload::jsonb->'message_ids') AS ids(id) \
                 JOIN visible_mail v ON v.message_id=ids.id) ELSE 0 END AS expired_count \
                 FROM candidates e) SELECT * FROM timeline ORDER BY seq DESC LIMIT 101",
                &[&principal],
            )
            .await?;
        tx.commit().await?;
        let events: Vec<Value> = rows
            .iter()
            .take(100)
            .map(|r| {
                let detail: Option<String> = r.get("detail");
                obj(vec![
                    ("seq", json!(r.get::<_, i64>("seq"))),
                    ("event", json!(r.get::<_, String>("event"))),
                    ("created_at", float_value(r.get("created_at"))),
                    ("agent_id", json!(r.get::<_, String>("agent_id"))),
                    (
                        "recipient_agent_id",
                        opt_str(r.get::<_, Option<String>>("recipient_agent_id").as_deref()),
                    ),
                    (
                        "message_id",
                        opt_str(r.get::<_, Option<String>>("message_id").as_deref()),
                    ),
                    ("project", json!(r.get::<_, String>("project"))),
                    ("task", json!(r.get::<_, String>("task"))),
                    (
                        "detail",
                        opt_str(detail.as_deref().filter(|d| DETAILS.contains(d))),
                    ),
                    ("expired_count", json!(r.get::<_, i64>("expired_count"))),
                ])
            })
            .collect();
        let mut out = json!({"enabled": true, "available": true, "snapshot_at": float_value(now)});
        if let (Value::Object(o), Value::Object(r)) = (&mut out, result) {
            for (k, v) in r {
                o.insert(k, v);
            }
        }
        out["events"] = Value::Array(events);
        out["events_truncated"] = json!(rows.len() > 100);
        Ok(out)
    }
}

/// Python `int(v)` on a query value: surrounding whitespace, a sign,
/// single underscores between digits; anything else falls back.
fn py_int_param(raw: Option<&String>, default: i64) -> i64 {
    let Some(raw) = raw.filter(|s| !s.is_empty()) else {
        return default;
    };
    let t = super::text::py_strip(raw);
    let chars: Vec<char> = t.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_'
            && !(i > 0
                && i + 1 < chars.len()
                && chars[i - 1].is_ascii_digit()
                && chars[i + 1].is_ascii_digit())
        {
            return default;
        }
    }
    let digits = t.replace('_', "");
    let unsigned = digits.strip_prefix(['+', '-']).unwrap_or(&digits);
    if unsigned.is_empty() || !unsigned.bytes().all(|b| b.is_ascii_digit()) {
        return default;
    }
    // A Python int too large for i64 still reaches `_limit` and fails it.
    digits.parse::<i64>().unwrap_or(i64::MAX)
}

impl Board {
    /// `coordination.console_snapshot` for the route.
    pub async fn console(
        &self,
        principal: &str,
        params: &std::collections::HashMap<String, String>,
    ) -> Result<Value> {
        if !self.config.enabled {
            return Ok(json!({"enabled": false, "available": false, "reason": "disabled"}));
        }
        let limit = py_int_param(params.get("limit"), 50);
        if !self.admitted(principal) {
            return Err(BoardError::code("principal_not_allowed"));
        }
        if self.service.snapshot().storage.is_none() {
            return Ok(json!({"enabled": true, "available": false, "reason": "not_initialized"}));
        }
        let mut conn = self.conn.lock().await;
        let client = conn.client().await?;
        self.store.console_snapshot(client, principal, limit).await
    }
}
