//! Operator role grants use the same lease-first locking order as the store.
use super::{Error, Event, append, audit_timestamp, settle, vacate, validate_name};
use crate::pg::{Row, Session, Transaction};
use serde_json::{Value, json};

const DELEGATE: &str = "delegate:";
const COORDINATOR: &str = "coordinator:";

fn project_name(project: &str) -> Result<String, Error> {
    let name = format!("{DELEGATE}{project}");
    if project.is_empty()
        || project.trim_matches(super::super::super::whitespace) != project
        || name.chars().count() > 120
    {
        return Err(Error::Refused("invalid_project"));
    }
    Ok(name)
}

fn id_prefix(value: &str) -> bool {
    (8..32).contains(&value.len())
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

async fn role_agent(tx: &Transaction<'_>, value: &str) -> Result<Row, Error> {
    let id = if id_prefix(value) {
        let ids = tx
            .query(
                "SELECT agent_id FROM coordination_agents WHERE agent_id LIKE $1 ORDER BY agent_id LIMIT 9",
                &[&format!("{value}%")],
            )
            .await?;
        match ids.as_slice() {
            [] => return Err(Error::Refused("instance_not_found")),
            [row] => row.try_get::<_, String>("agent_id")?,
            _ => return Err(Error::Refused("ambiguous_agent")),
        }
    } else {
        value.to_owned()
    };
    let agent = tx
        .query_opt(
            "SELECT *,capabilities::text AS capabilities_text FROM coordination_agents WHERE agent_id=$1",
            &[&id],
        )
        .await?
        .ok_or(Error::Refused("instance_not_found"))?;
    if agent
        .try_get::<_, Option<String>>("credential_hash")?
        .is_none()
    {
        return Err(Error::Refused("agent_revoked"));
    }
    Ok(agent)
}

fn listener_reason(
    attached: bool,
    wake_enabled: bool,
    capabilities: &Value,
    now: f64,
) -> Option<&'static str> {
    if attached && wake_enabled {
        return None;
    }
    let ring = capabilities.get("ring") == Some(&Value::Bool(true));
    let deadline = capabilities
        .get("ring_armed_until")
        .filter(|v| !v.is_null());
    if attached
        && ring
        && deadline
            .and_then(Value::as_f64)
            .is_some_and(|v| v.is_finite() && v > now)
    {
        return None;
    }
    if deadline.is_some() || wake_enabled {
        Some("listener_expired")
    } else if ring {
        Some("listener_unknown")
    } else {
        Some("wake_disabled")
    }
}

fn warning(reason: Option<&str>) -> Option<String> {
    let text = match reason? {
        "listener_expired" => "its listener lapsed",
        "listener_unknown" => "its listener has not reported yet",
        "wake_disabled" => "it has no wake listener",
        _ => unreachable!("listener reason"),
    };
    Some(format!(
        "The new delegate has no live wake path now ({text}): maintainer mail to it waits for its next turn. A Claude Code session listens again when its turn ends, a Codex thread once its doorbell re-arms; a client older than the always-listening plugin stops 59 minutes after a turn until it is updated."
    ))
}

pub struct Granted {
    pub name: String,
    pub agent_id: String,
    pub fence: i64,
    pub expires_at: f64,
    pub replaced: Option<String>,
    pub also_broken: Option<String>,
    pub reason: Option<&'static str>,
}
impl Granted {
    pub fn warning(&self) -> Option<String> {
        warning(self.reason)
    }
    pub fn json_line(&self) -> Result<String, Error> {
        let ascii = crate::board::identity::ascii_json;
        let mut text = format!(
            "{{\"name\": {}, \"agent_id\": {}, \"fence\": {}, \"expires_at\": {}, \"replaced\": {}",
            ascii(&json!(self.name)),
            ascii(&json!(self.agent_id)),
            self.fence,
            audit_timestamp(self.expires_at)?,
            ascii(&json!(self.replaced)),
        );
        if let Some(name) = &self.also_broken {
            text.push_str(&format!(", \"also_broken\": {}", ascii(&json!(name))));
        }
        text.push_str(&format!(
            ", \"reachable\": {}, \"reason\": {}",
            self.reason.is_none(),
            ascii(&json!(self.reason))
        ));
        if let Some(warning) = self.warning() {
            text.push_str(&format!(", \"warning\": {}", ascii(&json!(warning))));
        }
        text.push_str("}\n");
        Ok(text)
    }
}

/// Resolve a registered recipient after locking both role leases, then commit
/// the replacement, queue changes and chained audit rows in one transaction.
pub async fn grant_delegate(
    session: &mut Session,
    project: &str,
    agent: &str,
    hold: u64,
    clock: impl FnOnce() -> f64,
) -> Result<Granted, Error> {
    let name = project_name(project)?;
    if !(60..=604800).contains(&hold) {
        return Err(Error::Refused("invalid_ttl"));
    }
    validate_name(&name)?;
    let tx = session.client_mut().transaction().await?;
    let now = clock();
    audit_timestamp(now)?;
    let coordinator = format!("{COORDINATOR}{project}");
    let names: Vec<String> = [coordinator.clone(), name.clone()]
        .into_iter()
        .filter(|name| name.chars().count() <= 120)
        .collect();
    // coordinator: sorts before delegate:, including when only the latter fits.
    for role in &names {
        tx.execute(
            "INSERT INTO coordination_leases (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            &[role],
        )
        .await?;
    }
    tx.query(
        "SELECT name FROM coordination_leases WHERE name=ANY($1) ORDER BY name FOR UPDATE",
        &[&names],
    )
    .await?;
    let agent = role_agent(&tx, agent).await?;
    let agent_id: String = agent.try_get("agent_id")?;
    let principal: String = agent.try_get("principal")?;
    let mut events = Vec::new();
    if tx
        .query_opt(
            "DELETE FROM coordination_lease_waiters WHERE name=$1 AND agent_id=$2 RETURNING ticket",
            &[&coordinator, &agent_id],
        )
        .await?
        .is_some()
    {
        events.push(Event::new(
            "lease_dequeue",
            "daemon",
            agent_id.clone(),
            json!({"name": coordinator, "reason": "one_role"}),
        ));
    }
    let mut also_broken = None;
    if let Some(row) = tx
        .query_opt(
            "SELECT * FROM coordination_leases WHERE name=$1",
            &[&coordinator],
        )
        .await?
    {
        let holder: Option<String> = row.try_get("holder_agent_id")?;
        let expiry: Option<f64> = row.try_get("expires_at")?;
        if holder.as_deref() == Some(agent_id.as_str()) && expiry.is_some_and(|until| until > now) {
            let fence: i64 = row.try_get("fence")?;
            vacate(&tx, &coordinator, now).await?;
            events.push(Event::new(
                "lease_break",
                "operator",
                agent_id.clone(),
                json!({"fence": fence, "name": coordinator, "reason": "one_role"}),
            ));
            settle(&tx, &coordinator, now, &mut events).await?;
            also_broken = Some(coordinator);
        }
    }
    for row in tx
        .query(
            "DELETE FROM coordination_lease_waiters WHERE name=$1 RETURNING agent_id",
            &[&name],
        )
        .await?
    {
        events.push(Event::new(
            "lease_dequeue",
            "daemon",
            row.try_get("agent_id")?,
            json!({"name": name, "reason": "reserved"}),
        ));
    }
    let row = tx
        .query_one(
            "SELECT * FROM coordination_leases WHERE name=$1 FOR UPDATE",
            &[&name],
        )
        .await?;
    let mut replaced: Option<String> = row.try_get("holder_agent_id")?;
    let expiry: Option<f64> = row.try_get("expires_at")?;
    if let Some(holder) = &replaced
        && expiry.is_some_and(|until| until <= now)
    {
        let fence: i64 = row.try_get("fence")?;
        events.push(Event::new(
            "lease_expire",
            "daemon",
            holder.clone(),
            json!({"fence": fence, "name": name}),
        ));
        vacate(&tx, &name, now).await?;
        replaced = None;
    }
    if replaced.is_none() {
        settle(&tx, &name, now, &mut events).await?;
    }
    let expires_at = now + hold as f64;
    let attachment: Option<String> = agent.try_get("attachment_id")?;
    let until: Option<f64> = agent.try_get("lease_until")?;
    let wake_enabled: bool = agent.try_get("wake_enabled")?;
    let capabilities: String = agent.try_get("capabilities_text")?;
    let capabilities: Value = serde_json::from_str(&capabilities).map_err(|_| Error::Database)?;
    let reason = listener_reason(
        attachment.is_some() && until.unwrap_or(0.0) > now,
        wake_enabled,
        &capabilities,
        now,
    );
    let row = tx.query_one("UPDATE coordination_leases SET holder_agent_id=$1,holder_principal=$2,purpose='granted by the operator',fence=nextval('coordination_lease_fence'),acquired_at=$3,expires_at=$4,expect=NULL,expected_end=NULL,freed_at=NULL WHERE name=$5 RETURNING fence", &[&agent_id, &principal, &now, &expires_at, &name]).await?;
    let fence: i64 = row.try_get("fence")?;
    events.push(
        Event::new(
            "lease_delegate",
            "operator",
            agent_id.clone(),
            json!({"fence": fence, "hold": hold, "name": name, "replaced": replaced}),
        )
        .scoped(agent.try_get("project")?, agent.try_get("task")?),
    );
    append(&tx, events, now).await?;
    tx.commit().await?;
    Ok(Granted {
        name,
        agent_id,
        fence,
        expires_at,
        replaced,
        also_broken,
        reason,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_namespace_boundaries_preserve_unicode_and_error_priority() {
        assert_eq!(project_name("资源").unwrap(), "delegate:资源");
        assert!(project_name(&"a".repeat(111)).is_ok());
        for value in ["", " a", "a\u{001c}", &"a".repeat(112)] {
            assert!(matches!(
                project_name(value),
                Err(Error::Refused("invalid_project"))
            ));
        }
        assert!(matches!(
            validate_name(&project_name("a\0").unwrap()),
            Err(Error::Refused("invalid_lease"))
        ));
    }

    #[test]
    fn prefix_resolution_is_only_lowercase_hex_eight_to_thirty_one() {
        for value in ["01234567", "abcdef0123456789", &"a".repeat(31)] {
            assert!(id_prefix(value));
        }
        for value in ["abcdef0", "ABCDEF00", "abcdefgh", "资源", &"a".repeat(32)] {
            assert!(!id_prefix(value));
        }
    }

    #[test]
    fn listener_deadlines_require_live_attachment_and_strictly_future_number() {
        let now = 1000.0;
        assert_eq!(listener_reason(true, true, &json!({}), now), None);
        assert_eq!(
            listener_reason(
                true,
                false,
                &json!({"ring": true, "ring_armed_until": 1001}),
                now
            ),
            None
        );
        for deadline in [json!(1000), json!(999), json!(true), json!("1001")] {
            assert_eq!(
                listener_reason(
                    true,
                    false,
                    &json!({"ring": true, "ring_armed_until": deadline}),
                    now
                ),
                Some("listener_expired")
            );
        }
        assert_eq!(
            listener_reason(false, true, &json!({}), now),
            Some("listener_expired")
        );
        assert_eq!(
            listener_reason(
                false,
                false,
                &json!({"ring": true, "ring_armed_until": 1001}),
                now
            ),
            Some("listener_expired")
        );
        assert_eq!(
            listener_reason(false, false, &json!({"ring": true}), now),
            Some("listener_unknown")
        );
        assert_eq!(
            listener_reason(
                false,
                false,
                &json!({"ring": 1, "ring_armed_until": null}),
                now
            ),
            Some("wake_disabled")
        );
    }

    #[test]
    fn grant_output_retains_order_float_and_ascii_with_optional_role_break() {
        let mut grant = Granted {
            name: "delegate:资源".into(),
            agent_id: "a".repeat(32),
            fence: 7,
            expires_at: 1000.0,
            replaced: None,
            also_broken: Some("coordinator:资源".into()),
            reason: None,
        };
        assert_eq!(
            grant.json_line().unwrap(),
            "{\"name\": \"delegate:\\u8d44\\u6e90\", \"agent_id\": \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\", \"fence\": 7, \"expires_at\": 1000.0, \"replaced\": null, \"also_broken\": \"coordinator:\\u8d44\\u6e90\", \"reachable\": true, \"reason\": null}\n"
        );
        grant.reason = Some("wake_disabled");
        let text = grant.json_line().unwrap();
        assert!(
            text.contains("\"reachable\": false, \"reason\": \"wake_disabled\", \"warning\": ")
        );
        assert!(grant.warning().unwrap().contains("it has no wake listener"));
    }
}
