//! Wake decisions at send and ring offers at attach and heartbeat (spec
//! BS7): `_wake_decision`, `_maintainer_wake`, `_serve_wake`, `_live_park`,
//! `_authority`, `_lease_clearer` and `_need_matches`.

use super::canon::{self, float_value};
use super::db::AgentRow;
use super::error::Result;
use super::store::{WakePolicy, listener_path};
use super::text;
use serde_json::{Map, Value, json};
use tokio_postgres::GenericClient;

pub const AUTHORITY_REASONS: [&str; 2] = ["maintainer", "delegate"];
const AUTHORITY_ROW_REASONS: [&str; 3] = ["maintainer", "delegate", "coordinator"];
const REOPEN_ROW_REASONS: [&str; 4] = ["maintainer", "delegate", "clearer", "coordinator"];
pub const MAINTAINER_WAKE_REASON: &str = "maintainer_message";
pub const DELEGATE_PREFIX: &str = "delegate:";
const ROLE_ACTORS: [&str; 2] = ["operator", "maintainer"];
const WAKE_SERVE_REPEAT: f64 = 25.0;
const LEASE_CLEAR_GRACE: f64 = 60.0;
const DONE_REOPEN_BY: &str = "urgent mail from the maintainer, the maintainer's delegate for the project, \
     or the park's named clearer";
const WITHHELD_RETRY: &str = "urgent=true, or clears=<its park_needs>, rings this park";
pub const FALLBACK_CODEX: &str = "Mail is queued for receive on the thread's next turn and rings once its Codex \
     doorbell re-arms (the shim watches a thread from its first Pseudolife call after \
     the MCP server starts). For an immediate response, the maintainer types into the \
     session's window.";
pub const FALLBACK_CLAUDE: &str = "Mail is queued for receive on the session's next turn and rings once its Stop-hook \
     listener re-arms at a turn end. Only if it is a Claude Desktop Code-tab session (the \
     board cannot tell) does its host's session send_message reach it now; a Claude Code \
     CLI session is reached by the maintainer typing into its window.";
pub const FALLBACK_PATHS_CODEX: [&str; 2] = ["codex_doorbell", "maintainer_types"];
pub const FALLBACK_PATHS_CLAUDE: [&str; 2] = ["claude_desktop_send_message", "maintainer_types"];
const ATTENTION_FALLBACK: &str = "One capped bell may queue with turn state unknown; acceptance is not dispatch, reading, or native steering.";

/// A standing park record.
#[derive(Debug, Clone)]
pub struct Park {
    pub park_reason: String,
    pub park_needs: String,
    pub park_clear_by: String,
    pub park_set_at: Option<f64>,
}

/// `_live_park`: a reason set and no `park_expires` in the past.
pub fn live_park(row: &AgentRow, now: f64) -> Option<Park> {
    let reason = row.park_reason.as_ref()?;
    if row.park_expires.is_some_and(|e| e <= now) {
        return None;
    }
    Some(Park {
        park_reason: reason.clone(),
        park_needs: row.park_needs.clone(),
        park_clear_by: row.park_clear_by.clone(),
        park_set_at: row.park_set_at,
    })
}

/// `re.findall(r"\w+", s.lower())`.
fn words(s: &str) -> Vec<String> {
    let lower = s.to_lowercase();
    let mut out = Vec::new();
    let mut current = String::new();
    for c in lower.chars() {
        if text::alnum(c) || c == '_' {
            current.push(c);
        } else if !current.is_empty() {
            out.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

/// `_need_matches`.
pub fn need_matches(clears: &str, needs: &str) -> bool {
    let left = words(clears);
    let right = words(needs);
    if left.is_empty() || right.is_empty() {
        return false;
    }
    let (short, long) = if left.len() <= right.len() {
        (left, right)
    } else {
        (right, left)
    };
    if !short.iter().any(|w| w.chars().count() >= 4) {
        return false;
    }
    (0..=long.len() - short.len()).any(|i| long[i..i + short.len()] == short[..])
}

fn is_hex(s: &str) -> bool {
    s.bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// `_lease_clearer`.
async fn lease_clearer(
    c: &impl GenericClient,
    agent_id: &str,
    name: &str,
    now: f64,
) -> Result<bool> {
    let len = name.chars().count();
    if name == "maintainer" || ((8..=32).contains(&len) && is_hex(name)) {
        return Ok(false);
    }
    Ok(c.query_opt(
        "SELECT 1 AS hit FROM coordination_events WHERE created_at>$1 AND agent_id=$2 \
             AND CASE WHEN event IN ('lease_release','lease_expire') \
             THEN payload::jsonb->>'name'=$3 ELSE false END LIMIT 1",
        &[&(now - LEASE_CLEAR_GRACE), &agent_id, &name],
    )
    .await?
    .is_some())
}

/// `_authority`.
async fn authority(
    c: &impl GenericClient,
    maintainer_principals: &[String],
    sender: &AgentRow,
    recipient: &AgentRow,
    now: f64,
) -> Result<Option<&'static str>> {
    if maintainer_principals.contains(&sender.principal) {
        return Ok(Some("maintainer"));
    }
    if recipient.project.is_empty() {
        return Ok(None);
    }
    let actors: Vec<String> = ROLE_ACTORS.iter().map(|s| s.to_string()).collect();
    let hit = c
        .query_opt(
            "SELECT 1 AS hit FROM coordination_leases l WHERE l.name=$1 \
             AND l.holder_agent_id=$2 AND l.expires_at>$3 AND EXISTS (SELECT 1 \
             FROM coordination_events e WHERE e.event='lease_delegate' \
             AND e.actor=ANY($4) AND e.agent_id=l.holder_agent_id \
             AND CASE WHEN e.event='lease_delegate' THEN \
             e.payload::jsonb->>'name'=l.name \
             AND (e.payload::jsonb->>'fence')::bigint=l.fence ELSE false END)",
            &[
                &format!("{DELEGATE_PREFIX}{}", recipient.project),
                &sender.agent_id,
                &now,
                &actors,
            ],
        )
        .await?;
    Ok(hit.map(|_| "delegate"))
}

fn count(row: &tokio_postgres::Row) -> i64 {
    row.get::<_, i64>(0)
}

fn no_path(recipient: &AgentRow, missing: &str) -> Map<String, Value> {
    let codex = recipient.has_capability("codex");
    let mut m = Map::new();
    m.insert("decision".into(), json!("no_path"));
    m.insert("reason".into(), json!(missing));
    m.insert("queued".into(), json!(true));
    m.insert("last_activity".into(), float_value(recipient.last_activity));
    m.insert(
        "fallback".into(),
        json!(if codex {
            FALLBACK_CODEX
        } else {
            FALLBACK_CLAUDE
        }),
    );
    m.insert(
        "fallback_paths".into(),
        json!(if codex {
            FALLBACK_PATHS_CODEX.to_vec()
        } else {
            FALLBACK_PATHS_CLAUDE.to_vec()
        }),
    );
    m
}

/// `_wake_decision`. The returned object may carry `_ring`, which `send`
/// pops into the audit payload before storing the decision.
#[allow(clippy::too_many_arguments)]
pub async fn wake_decision(
    c: &impl GenericClient,
    policy: &WakePolicy,
    maintainer_principals: &[String],
    sender: &AgentRow,
    recipient: &AgentRow,
    now: f64,
    clears: Option<&str>,
    urgent: bool,
    message_id: &str,
    maintainer: bool,
) -> Result<Map<String, Value>> {
    if maintainer {
        return maintainer_wake(c, policy, sender, recipient, now, message_id).await;
    }
    let park = live_park(recipient, now);
    let done = park.as_ref().is_some_and(|p| p.park_reason == "done");
    let mut need = Map::new();
    if let Some(p) = park.as_ref().filter(|_| !done) {
        need.insert("park_needs".into(), json!(p.park_needs));
        need.insert("park_clear_by".into(), json!(p.park_clear_by));
    }
    let with_need = |mut m: Map<String, Value>, need: &Map<String, Value>| {
        for (k, v) in need {
            m.insert(k.clone(), v.clone());
        }
        m
    };
    let attention = park.is_none() && urgent && recipient.has_capability("codex");
    if !attention && park.is_none() && recipient.last_activity > now - policy.active_seconds as f64
    {
        return Ok(obj_map(json!({"decision": "hinted", "reason": "active"})));
    }
    let auth = if urgent {
        authority(c, maintainer_principals, sender, recipient, now).await?
    } else {
        None
    };
    let hour = now - 3600.0;
    let day = now - 86400.0;
    let how: &str;
    if done {
        let park = park.as_ref().expect("done park");
        let mut h = auth;
        if h.is_none()
            && urgent
            && !park.park_clear_by.is_empty()
            && (park.park_clear_by == sender.agent_id
                || lease_clearer(c, &sender.agent_id, &park.park_clear_by, now).await?)
        {
            h = Some("clearer");
        }
        let Some(h) = h else {
            return Ok(obj_map(
                json!({"decision": "not_needed", "reason": "parked_done",
                                     "reopen_by": DONE_REOPEN_BY}),
            ));
        };
        how = h;
    } else if let Some(park) = park.as_ref() {
        let mut h: Option<&str> = None;
        if park.park_clear_by == sender.agent_id {
            h = Some("clearer");
        } else if park.park_clear_by == "anyone" {
            h = Some("anyone");
        } else if !park.park_clear_by.is_empty()
            && lease_clearer(c, &sender.agent_id, &park.park_clear_by, now).await?
        {
            h = Some("clearer");
        } else if clears.is_some_and(|cl| need_matches(cl, &park.park_needs)) {
            h = Some("clears");
        } else if urgent {
            h = Some(auth.unwrap_or("urgent"));
        }
        let Some(h) = h else {
            let mut m = obj_map(json!({"decision": "withheld", "reason": "need_not_cleared"}));
            m = with_need(m, &need);
            m.insert("retry".into(), json!(WITHHELD_RETRY));
            return Ok(m);
        };
        how = h;
    } else if urgent {
        how = auth.unwrap_or("urgent");
    } else {
        return Ok(obj_map(
            json!({"decision": "not_needed", "reason": "no_park"}),
        ));
    }
    let counts_urgent = how == "urgent" || (done && how == "clearer");
    if counts_urgent && !crate::mutants::active("board-urgent-cap-off") {
        let sent = count(
            &c.query_one(
                "SELECT count(*) AS n FROM coordination_wakes WHERE \
                 sender_agent_id=$1 AND urgent AND created_at>$2",
                &[&sender.agent_id, &hour],
            )
            .await?,
        );
        if sent >= policy.urgent_per_sender_per_hour {
            return Ok(with_need(
                obj_map(json!({"decision": "capped", "reason": "urgent_sender_hour"})),
                &need,
            ));
        }
    } else if AUTHORITY_REASONS.contains(&how) {
        let reasons: Vec<String> = AUTHORITY_ROW_REASONS
            .iter()
            .map(|s| s.to_string())
            .collect();
        let sent = count(
            &c.query_one(
                "SELECT count(*) AS n FROM coordination_wakes WHERE \
                 sender_agent_id=$1 AND reason=ANY($2) AND created_at>$3",
                &[&sender.agent_id, &reasons, &hour],
            )
            .await?,
        );
        if sent >= policy.authority_per_sender_per_hour {
            return Ok(with_need(
                obj_map(json!({"decision": "capped", "reason": "authority_sender_hour"})),
                &need,
            ));
        }
    }
    let decision = if attention { "attention" } else { "rung" };
    let reason = how;
    let stagger_floor = now - policy.fan_out_stagger_seconds as f64;
    let counts = c
        .query_one(
            "SELECT count(*) FILTER (WHERE recipient_agent_id=$1 AND created_at>$2) AS recipient,\
             count(*) FILTER (WHERE created_at>$3) AS night,\
             max(ring_at) FILTER (WHERE sender_agent_id=$4 AND ring_at>$5) AS burst \
             FROM coordination_wakes",
            &[
                &recipient.agent_id,
                &hour,
                &day,
                &sender.agent_id,
                &stagger_floor,
            ],
        )
        .await?;
    let recipient_count: i64 = counts.get(0);
    let night: i64 = counts.get(1);
    let burst: Option<f64> = counts.get(2);
    if recipient_count >= policy.per_recipient_per_hour {
        return Ok(with_need(
            obj_map(json!({"decision": "capped", "reason": "recipient_hour"})),
            &need,
        ));
    }
    if night >= policy.nightly_total {
        return Ok(with_need(
            obj_map(json!({"decision": "capped", "reason": "nightly"})),
            &need,
        ));
    }
    let mut ring_at = now;
    if let Some(burst) = burst {
        ring_at = now.max(burst + policy.fan_out_stagger_seconds as f64);
    }
    c.execute(
        "INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,\
         decision,reason,urgent,ring_at,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        &[
            &recipient.agent_id,
            &sender.agent_id,
            &message_id,
            &decision,
            &reason,
            &counts_urgent,
            &ring_at,
            &now,
        ],
    )
    .await?;
    if attention {
        let mut m = obj_map(json!({
            "decision": "no_path", "reason": "no_steer_path", "attention": true,
            "delivery": "queue_pending", "recipient_state": "unknown", "queue_allowed": true,
        }));
        m.insert("ring_at".into(), float_value(ring_at));
        m.insert("fallback".into(), json!(ATTENTION_FALLBACK));
        m.insert("_ring".into(), json!(reason));
        return Ok(m);
    }
    if let Some(missing) = listener_path(recipient, now) {
        let mut m = with_need(no_path(recipient, missing), &need);
        if done {
            m.insert("reopened".into(), json!(true));
        }
        m.insert("_ring".into(), json!(reason));
        return Ok(m);
    }
    let mut m = obj_map(json!({"decision": decision, "reason": reason}));
    m.insert("ring_at".into(), float_value(ring_at));
    if done {
        m.insert("reopened".into(), json!(true));
    }
    m.insert("_ring".into(), json!(reason));
    Ok(m)
}

/// `_maintainer_wake`.
async fn maintainer_wake(
    c: &impl GenericClient,
    policy: &WakePolicy,
    sender: &AgentRow,
    recipient: &AgentRow,
    now: f64,
    message_id: &str,
) -> Result<Map<String, Value>> {
    let sent = count(
        &c.query_one(
            "SELECT count(*) AS n FROM coordination_wakes WHERE \
             recipient_agent_id=$1 AND reason=$2 AND created_at>$3",
            &[
                &recipient.agent_id,
                &MAINTAINER_WAKE_REASON,
                &(now - 3600.0),
            ],
        )
        .await?,
    );
    if sent >= policy.maintainer_per_recipient_per_hour {
        return Ok(obj_map(
            json!({"decision": "capped", "reason": "maintainer_hour"}),
        ));
    }
    c.execute(
        "INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,\
         decision,reason,urgent,ring_at,created_at) VALUES ($1,$2,$3,'rung',$4,false,$5,$6)",
        &[
            &recipient.agent_id,
            &sender.agent_id,
            &message_id,
            &MAINTAINER_WAKE_REASON,
            &now,
            &now,
        ],
    )
    .await?;
    if let Some(missing) = listener_path(recipient, now) {
        let mut m = no_path(recipient, missing);
        m.insert("_ring".into(), json!(MAINTAINER_WAKE_REASON));
        return Ok(m);
    }
    let mut m = obj_map(json!({"decision": "rung", "reason": MAINTAINER_WAKE_REASON}));
    m.insert("ring_at".into(), float_value(now));
    m.insert("_ring".into(), json!(MAINTAINER_WAKE_REASON));
    Ok(m)
}

fn obj_map(v: Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m,
        _ => Map::new(),
    }
}

#[derive(Clone)]
struct Offerable {
    decision: String,
    reason: String,
    ring_at: f64,
    created_at: f64,
    wake_id: i64,
    served_at: Option<f64>,
    expires_at: f64,
    queue_allowed: bool,
    queued: bool,
    park_authorized: bool,
}

/// `_serve_wake`: the newest pending ring for this address, or null.
pub async fn serve_wake(c: &impl GenericClient, agent_id: &str, now: f64) -> Result<Value> {
    let row = c
        .query_one(
            "SELECT * FROM coordination_agents WHERE agent_id=$1",
            &[&agent_id],
        )
        .await?;
    let row = AgentRow::from_row(&row);
    let codex = row.has_capability("codex");
    let park = if codex { live_park(&row, now) } else { None };
    let reopen_only = park.as_ref().is_some_and(|p| p.park_reason == "done");
    let park_since = park.as_ref().and_then(|p| p.park_set_at);
    let mut park_seq: Option<i64> = None;
    if park.is_some() {
        let event = c
            .query_opt(
                "SELECT seq,payload::jsonb->'fields'->'park_set_at' AS park_set_at \
                 FROM coordination_events WHERE agent_id=$1 AND event='update' \
                 AND payload::jsonb->'fields' ? 'park_set_at' ORDER BY seq DESC LIMIT 1",
                &[&agent_id],
            )
            .await?;
        if let Some(event) = event {
            let stamp: Option<Value> = event.get(1);
            let stamp = stamp.as_ref().and_then(canon::as_f64);
            if stamp == park_since {
                park_seq = Some(event.get(0));
            }
        }
    }
    let listening = listener_path(&row, now).is_none();
    let ring_allowed = park_seq.is_some() && listening;
    if !codex && !listening {
        return Ok(Value::Null);
    }
    let seq_floor = park_seq.unwrap_or(0);
    let reopen: Vec<String> = REOPEN_ROW_REASONS
        .iter()
        .map(|s| s.to_string())
        .chain(std::iter::once(MAINTAINER_WAKE_REASON.to_string()))
        .collect();
    let base = "UPDATE coordination_wakes SET served_at=$1 WHERE recipient_agent_id=$2 \
                AND decision IN ('rung','attention') \
                AND served_at IS NULL AND EXISTS (SELECT 1 FROM coordination_messages m \
                WHERE m.message_id=coordination_wakes.message_id AND m.recipient_agent_id=$2 \
                AND m.acknowledged_at IS NULL AND m.expires_at>$1)";
    if codex {
        let guard = " AND (decision='attention' OR ($3 AND reason=$4) OR ($5 AND EXISTS \
                     (SELECT 1 FROM coordination_events s WHERE s.event='send' \
                     AND s.message_id=coordination_wakes.message_id AND s.seq>$6)))";
        if reopen_only {
            let sql = format!("{base}{guard} AND decision='rung' AND reason=ANY($7)");
            c.execute(
                &sql,
                &[
                    &now,
                    &agent_id,
                    &listening,
                    &MAINTAINER_WAKE_REASON,
                    &ring_allowed,
                    &seq_floor,
                    &reopen,
                ],
            )
            .await?;
        } else {
            let sql = format!("{base}{guard}");
            c.execute(
                &sql,
                &[
                    &now,
                    &agent_id,
                    &listening,
                    &MAINTAINER_WAKE_REASON,
                    &ring_allowed,
                    &seq_floor,
                ],
            )
            .await?;
        }
    } else {
        c.execute(base, &[&now, &agent_id]).await?;
    }
    let mut rows: Vec<Offerable> = c
        .query(
            "SELECT w.decision,w.reason,w.ring_at,w.created_at,w.wake_id,w.served_at,m.expires_at,\
             coalesce(m.wake->>'queue_allowed'='true',false) AS queue_allowed,\
             coalesce(m.wake->>'queued'='true',false) AS queued,\
             EXISTS (SELECT 1 FROM coordination_events s WHERE s.event='send' \
             AND s.message_id=w.message_id AND s.seq>$1) AS park_authorized FROM coordination_wakes w \
             JOIN coordination_messages m ON m.message_id=w.message_id \
             AND m.recipient_agent_id=w.recipient_agent_id WHERE w.recipient_agent_id=$2 \
             AND w.decision IN ('rung','attention') \
             AND (w.served_at IS NOT NULL OR $3) AND m.acknowledged_at IS NULL AND m.expires_at>$4",
            &[&seq_floor, &agent_id, &codex, &now],
        )
        .await?
        .iter()
        .map(|r| Offerable {
            decision: r.get(0),
            reason: r.get(1),
            ring_at: r.get(2),
            created_at: r.get(3),
            wake_id: r.get(4),
            served_at: r.get(5),
            expires_at: r.get(6),
            queue_allowed: r.get(7),
            queued: r.get(8),
            park_authorized: r.get(9),
        })
        .collect();
    if reopen_only {
        rows.retain(|item| {
            item.reason == MAINTAINER_WAKE_REASON
                || (park_seq.is_some()
                    && item.decision == "rung"
                    && REOPEN_ROW_REASONS.contains(&item.reason.as_str())
                    && item.park_authorized)
        });
    }
    if rows.is_empty() {
        return Ok(Value::Null);
    }
    let max_ring = |rows: &[Offerable]| rows.iter().map(|r| r.ring_at).fold(f64::MIN, f64::max);
    if codex {
        let eligible: Vec<Offerable> = rows
            .iter()
            .filter(|item| {
                item.decision == "attention"
                    || (listening && item.reason == MAINTAINER_WAKE_REASON)
                    || (ring_allowed && item.park_authorized)
            })
            .cloned()
            .collect();
        if eligible.is_empty() {
            return Ok(json!({"decision": "attention", "reason": "no_steer_path",
                             "ring_at": float_value(max_ring(&rows))}));
        }
        rows = eligible;
    }
    if !listening {
        rows.retain(|item| item.decision == "attention");
        if rows.is_empty() {
            return Ok(Value::Null);
        }
    }
    let mut newest = &rows[0];
    for r in &rows[1..] {
        if (r.created_at, r.wake_id) > (newest.created_at, newest.wake_id) {
            newest = r;
        }
    }
    if !codex
        && !rows.iter().any(|r| r.queued)
        && newest.served_at.unwrap_or(f64::NEG_INFINITY) <= now - WAKE_SERVE_REPEAT
    {
        return Ok(Value::Null);
    }
    let mut offer = json!({"decision": newest.decision, "reason": newest.reason,
                           "ring_at": float_value(max_ring(&rows))});
    if codex {
        offer["message_expires_at"] = float_value(newest.expires_at);
        if newest.decision == "attention" {
            offer["queue_allowed"] = json!(newest.queue_allowed && park.is_none() && listening);
            offer["recipient_state"] = json!("unknown");
        }
    }
    Ok(offer)
}
