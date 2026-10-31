//! Signed role changes, inside the assertion's transaction.

use serde_json::{Map, Value, json};
use tokio_postgres::Transaction;

use super::{Outcome, hold, project, refuse, role_holder};
use crate::board::db::{AgentRow, LeaseRow, opt_f64};
use crate::board::error::BoardError;
use crate::board::lease::{
    CONDUCTOR_EVENT, CONDUCTOR_PREFIX, CONDUCTOR_PREFIXES, COORDINATOR_PREFIX,
    LEGACY_CONDUCTOR_PREFIX, MAX_LEASE_NAME, lease_args, settle,
};
use crate::board::store::{Event, RESERVED_SENDERS, Store, append, listener_path, now};

/// Coordination refusals have the Console's `_board_error` vocabulary.
fn board_error(error: BoardError) -> super::Refusal {
    match error {
        BoardError::Refused(error) => match error.code.as_str() {
            "recipient_not_found" | "instance_not_found" | "agent_revoked" => {
                refuse("recipient_unknown", "board_role")
            }
            "recipient_reserved" => refuse("recipient_reserved", "board_role"),
            "rate_limited" | "queue_full" => refuse("rate_capped", "board_role"),
            _ => refuse("invalid_request", "board_role"),
        },
        error => error.into(),
    }
}

/// `_lock_role_leases`: lease rows in name order, before any waiter or agent.
async fn lock_role_leases(tx: &Transaction<'_>, project: &str) -> Outcome<()> {
    let mut names: Vec<String> = [
        format!("{COORDINATOR_PREFIX}{project}"),
        format!("{CONDUCTOR_PREFIX}{project}"),
        format!("{LEGACY_CONDUCTOR_PREFIX}{project}"),
    ]
    .into_iter()
    .filter(|name| name.chars().count() <= MAX_LEASE_NAME)
    .collect();
    names.sort();
    for name in &names {
        tx.execute(
            "INSERT INTO coordination_leases (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            &[name],
        )
        .await?;
    }
    tx.query(
        "SELECT name FROM coordination_leases WHERE name=ANY($1) ORDER BY name FOR UPDATE",
        &[&names],
    )
    .await?;
    Ok(())
}

/// `_role_recipient`, retaining the complete row for reachability and audit.
async fn recipient(tx: &Transaction<'_>, fields: &Map<String, Value>) -> Outcome<AgentRow> {
    let Some(Value::String(id)) = fields.get("agent_id") else {
        return Err(refuse("recipient_unknown", "agent_id"));
    };
    if id.len() != 32 || !id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(refuse("recipient_unknown", "agent_id"));
    }
    let row = tx
        .query_opt(
            "SELECT * FROM coordination_agents WHERE agent_id=$1 AND credential_hash IS NOT NULL",
            &[id],
        )
        .await?
        .ok_or_else(|| refuse("recipient_unknown", "agent_id"))?;
    let agent = AgentRow::from_row(&row);
    if RESERVED_SENDERS.contains(&agent.principal.as_str()) || agent.parent_thread.is_some() {
        return Err(refuse("recipient_reserved", "agent_id"));
    }
    if fields.get("project").and_then(Value::as_str) != Some(agent.project.as_str()) {
        return Err(refuse("invalid_request", "project_mismatch"));
    }
    Ok(agent)
}

async fn vacate(tx: &Transaction<'_>, name: &str, now: f64) -> Outcome<()> {
    tx.execute(
        "UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',\
         purpose='',acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,\
         freed_at=$1 WHERE name=$2",
        &[&now, &name],
    )
    .await?;
    Ok(())
}

async fn grant(
    tx: &Transaction<'_>,
    name: &str,
    agent: &AgentRow,
    now: f64,
    hold: i64,
    purpose: &str,
) -> Outcome<LeaseRow> {
    let row = tx
        .query_one(
            "UPDATE coordination_leases SET holder_agent_id=$1,holder_principal=$2,purpose=$3,\
             fence=nextval('coordination_lease_fence'),acquired_at=$4,expires_at=$5,expect=NULL,\
             expected_end=NULL,freed_at=NULL WHERE name=$6 RETURNING *",
            &[
                &agent.agent_id,
                &agent.principal,
                &purpose,
                &now,
                &(now + hold as f64),
                &name,
            ],
        )
        .await?;
    Ok(LeaseRow::from_row(&row))
}

async fn dequeue(
    tx: &Transaction<'_>,
    name: &str,
    agent_id: &str,
    reason: &str,
    events: &mut Vec<Event>,
) -> Outcome<()> {
    if tx
        .query_opt(
            "DELETE FROM coordination_lease_waiters WHERE name=$1 AND agent_id=$2 RETURNING ticket",
            &[&name, &agent_id],
        )
        .await?
        .is_some()
    {
        events.push(
            Event::new("lease_dequeue", &json!({"name": name, "reason": reason}))
                .actor("daemon")
                .agent(agent_id),
        );
    }
    Ok(())
}

async fn drop_reserved_waiters(
    tx: &Transaction<'_>,
    name: &str,
    events: &mut Vec<Event>,
) -> Outcome<()> {
    for row in tx
        .query(
            "DELETE FROM coordination_lease_waiters WHERE name=$1 RETURNING agent_id",
            &[&name],
        )
        .await?
    {
        events.push(
            Event::new(
                "lease_dequeue",
                &json!({"name": name, "reason": "reserved"}),
            )
            .actor("daemon")
            .agent(&row.get::<_, String>(0)),
        );
    }
    Ok(())
}

/// `_break_held`: break only this recipient's live hold, then settle its queue.
async fn break_held(
    tx: &Transaction<'_>,
    name: &str,
    agent_id: &str,
    now: f64,
    events: &mut Vec<Event>,
) -> Outcome<Option<String>> {
    let Some(row) = tx
        .query_opt("SELECT * FROM coordination_leases WHERE name=$1", &[&name])
        .await?
    else {
        return Ok(None);
    };
    let row = LeaseRow::from_row(&row);
    if row.holder_agent_id.as_deref() != Some(agent_id)
        || !row.expires_at.is_some_and(|expires| expires > now)
    {
        return Ok(None);
    }
    vacate(tx, name, now).await?;
    events.push(
        Event::new(
            "lease_break",
            &json!({"name": name, "fence": row.fence, "reason": "one_role"}),
        )
        .actor("maintainer")
        .agent(agent_id),
    );
    settle(tx, name, now, events).await.map_err(board_error)?;
    Ok(Some(name.to_string()))
}

/// `_retire_legacy_conductor`: old queued grants cannot resurrect the role.
async fn retire_legacy_conductor(
    tx: &Transaction<'_>,
    project: &str,
    now: f64,
    events: &mut Vec<Event>,
) -> Outcome<Option<String>> {
    let name = format!("{LEGACY_CONDUCTOR_PREFIX}{project}");
    drop_reserved_waiters(tx, &name, events).await?;
    let row = settle(tx, &name, now, events).await.map_err(board_error)?;
    let Some(row) = row else {
        return Ok(None);
    };
    let Some(holder) = row.holder_agent_id else {
        return Ok(None);
    };
    vacate(tx, &name, now).await?;
    events.push(
        Event::new(
            "lease_break",
            &json!({"name": name, "fence": row.fence, "reason": "renamed"}),
        )
        .actor("maintainer")
        .agent(&holder),
    );
    Ok(Some(holder))
}

async fn break_lease(tx: &Transaction<'_>, name: &str) -> Outcome<Value> {
    lease_args(&json!(name), None, None, None).map_err(board_error)?;
    let now = now();
    let row = tx
        .query_opt(
            "SELECT * FROM coordination_leases WHERE name=$1 FOR UPDATE",
            &[&name],
        )
        .await?;
    let row = row.map(|row| LeaseRow::from_row(&row));
    let Some(row) = row.filter(|row| row.holder_agent_id.is_some()) else {
        return Ok(json!({"name": name, "broken": false, "was_held_by": null}));
    };
    let holder = row.holder_agent_id.expect("checked holder");
    vacate(tx, name, now).await?;
    let mut events = vec![
        Event::new("lease_break", &json!({"name": name, "fence": row.fence}))
            .actor("maintainer")
            .agent(&holder),
    ];
    settle(tx, name, now, &mut events)
        .await
        .map_err(board_error)?;
    append(tx, events, now, None).await.map_err(board_error)?;
    Ok(json!({"name": name, "broken": true, "was_held_by": holder}))
}

fn reachability(out: &mut Value, agent: &AgentRow, now: f64, role: &str) {
    let reason = listener_path(agent, now);
    out["reachable"] = json!(reason.is_none());
    out["reason"] = json!(reason);
    if let Some(reason) = reason {
        let reason = match reason {
            "listener_expired" => "its listener lapsed",
            "listener_unknown" => "its listener has not reported yet",
            "wake_disabled" => "it has no wake listener",
            reason => reason,
        };
        out["warning"] = json!(format!(
            "The new {role} has no live wake path now ({reason}): maintainer mail to it \
             waits for its next turn. A Claude Code session listens again when its turn \
             ends, a Codex thread once its doorbell re-arms; a client older than the \
             always-listening plugin stops 59 minutes after a turn until it is updated."
        ));
    }
}

/// `maintainer._role`'s action; the caller owns signature, nonce and commit.
pub(super) async fn role(
    _store: &Store,
    tx: &Transaction<'_>,
    fields: &Map<String, Value>,
) -> Outcome<Value> {
    let requested = fields.get("purpose").and_then(Value::as_str).unwrap_or("");
    let purpose = super::LEGACY_ROLE_PURPOSES
        .iter()
        .find(|(old, _)| *old == requested)
        .map_or(requested, |(_, purpose)| *purpose);
    if !super::ROLE_PURPOSES.contains(&purpose) {
        return Err(refuse("assertion_invalid", "purpose"));
    }
    let project = project(fields.get("project"))?;
    let conductor = purpose.ends_with("conductor");
    let prefix = if conductor {
        CONDUCTOR_PREFIX
    } else {
        COORDINATOR_PREFIX
    };
    let name = format!("{prefix}{project}");
    lock_role_leases(tx, &project).await?;
    if role_holder(tx, &name, now()).await? != *fields.get("holder").unwrap_or(&Value::Null) {
        return Err(refuse("role_changed", "holder"));
    }
    if purpose.starts_with("revoke") {
        let out = break_lease(tx, &name).await?;
        if !conductor {
            return Ok(out);
        }
        let legacy = break_lease(tx, &format!("{LEGACY_CONDUCTOR_PREFIX}{project}")).await?;
        return Ok(if out["broken"] == true || legacy["broken"] != true {
            out
        } else {
            legacy
        });
    }
    let other = if conductor {
        COORDINATOR_PREFIX
    } else {
        CONDUCTOR_PREFIX
    };
    if role_holder(tx, &format!("{other}{project}"), now()).await?
        != *fields.get("other_holder").unwrap_or(&Value::Null)
    {
        return Err(refuse("role_changed", "other_holder"));
    }
    let agent = recipient(tx, fields).await?;
    let hold = hold(fields.get("hold"))?;
    lease_args(&json!(name), None, None, None).map_err(board_error)?;
    let now = now();
    let mut events = Vec::new();
    let mut also = None;
    let replaced;
    if conductor {
        let other_name = format!("{COORDINATOR_PREFIX}{project}");
        dequeue(tx, &other_name, &agent.agent_id, "one_role", &mut events).await?;
        also = break_held(tx, &other_name, &agent.agent_id, now, &mut events).await?;
        drop_reserved_waiters(tx, &name, &mut events).await?;
        let legacy = retire_legacy_conductor(tx, &project, now, &mut events).await?;
        let row = settle(tx, &name, now, &mut events)
            .await
            .map_err(board_error)?
            .expect("locked role lease");
        replaced = row.holder_agent_id.or(legacy);
    } else {
        for prefix in CONDUCTOR_PREFIXES {
            also = break_held(
                tx,
                &format!("{prefix}{project}"),
                &agent.agent_id,
                now,
                &mut events,
            )
            .await?
            .or(also);
        }
        let row = tx
            .query_one("SELECT * FROM coordination_leases WHERE name=$1", &[&name])
            .await?;
        let row = LeaseRow::from_row(&row);
        if let Some(holder) = row.holder_agent_id.as_deref()
            && row.expires_at.is_some_and(|expires| expires <= now)
        {
            events.push(
                Event::new("lease_expire", &json!({"name": name, "fence": row.fence}))
                    .actor("daemon")
                    .agent(holder),
            );
            replaced = None;
        } else {
            replaced = row.holder_agent_id;
        }
        dequeue(tx, &name, &agent.agent_id, "assigned", &mut events).await?;
    }
    let row = grant(
        tx,
        &name,
        &agent,
        now,
        hold,
        if conductor {
            "granted by the maintainer"
        } else {
            "assigned by the maintainer"
        },
    )
    .await?;
    events.push(
        Event::new(
            if conductor {
                CONDUCTOR_EVENT
            } else {
                "lease_assign"
            },
            &json!({"name": name, "fence": row.fence, "hold": hold, "replaced": replaced}),
        )
        .actor("maintainer")
        .agent(&agent.agent_id)
        .scope(&agent.project, &agent.task),
    );
    append(tx, events, now, None).await.map_err(board_error)?;
    let mut out = json!({
        "name": name, "agent_id": agent.agent_id, "fence": row.fence,
        "expires_at": opt_f64(row.expires_at), "replaced": replaced,
    });
    if let Some(also) = also {
        out["also_broken"] = json!(also);
    }
    reachability(
        &mut out,
        &agent,
        now,
        if conductor {
            "conductor"
        } else {
            "coordinator"
        },
    );
    Ok(out)
}
