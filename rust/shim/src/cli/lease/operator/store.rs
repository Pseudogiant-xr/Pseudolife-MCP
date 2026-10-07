//! Offline operator mutations mirror CoordinationStore's transaction boundaries.
use crate::{
    board::identity::hex_hash,
    pg::{Session, Transaction},
};
use serde_json::{Value, json};

#[derive(Debug)]
pub enum Error {
    Refused(&'static str),
    Database,
    Clock,
}
impl From<tokio_postgres::Error> for Error {
    fn from(_: tokio_postgres::Error) -> Self {
        Self::Database
    }
}
pub fn validate_name(name: &str) -> Result<(), Error> {
    if name.is_empty()
        || name.trim_matches(super::super::whitespace) != name
        || name.chars().count() > 120
        || name.chars().any(crate::board::claims::forbidden)
    {
        return Err(Error::Refused("invalid_lease"));
    }
    if super::secrets::refused(name) {
        return Err(Error::Refused("secret_like_body"));
    }
    Ok(())
}
#[derive(Debug, PartialEq, Eq)]
pub struct Broken {
    pub name: String,
    pub broken: bool,
    pub was_held_by: Option<String>,
}
impl Broken {
    /// Python json.dumps uses insertion order, ASCII strings and spaced separators.
    pub fn json_line(&self) -> String {
        let name = crate::board::identity::ascii_json(&json!(self.name));
        let holder = crate::board::identity::ascii_json(&json!(self.was_held_by));
        format!(
            "{{\"name\": {name}, \"broken\": {}, \"was_held_by\": {holder}}}\n",
            self.broken
        )
    }
}
struct Event {
    event: &'static str,
    actor: &'static str,
    agent: String,
    payload: String,
}
impl Event {
    fn new(event: &'static str, actor: &'static str, agent: String, payload: Value) -> Self {
        // Construct each payload below in sorted key order, as the producer's
        // canonical UTF-8 JSON does. TEXT is hashed and stored without JSONB.
        Self {
            event,
            actor,
            agent,
            payload: payload.to_string(),
        }
    }
}
async fn vacate(tx: &Transaction<'_>, name: &str, now: f64) -> Result<(), Error> {
    tx.execute("UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',purpose='',acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,freed_at=$1 WHERE name=$2", &[&now, &name]).await?;
    Ok(())
}
async fn settle(
    tx: &Transaction<'_>,
    name: &str,
    now: f64,
    events: &mut Vec<Event>,
) -> Result<(), Error> {
    // Called after vacate under the same lease-row lock. The lease therefore
    // cannot expire here; it is already free, exactly as after Python break.
    let short = now - 3600.0;
    let retained = now - 604800.0;
    let departed = tx.query("DELETE FROM coordination_lease_waiters w WHERE w.name=$1 AND (NOT EXISTS (SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id) OR EXISTS (SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id AND (a.lease_until IS NULL OR a.lease_until<=CASE WHEN a.capabilities->>'resumable'='false' THEN $2 ELSE $3 END) AND a.last_activity<=CASE WHEN a.capabilities->>'resumable'='false' THEN $2 ELSE $3 END)) RETURNING w.agent_id", &[&name, &short, &retained]).await?;
    for row in departed {
        events.push(Event::new(
            "lease_dequeue",
            "daemon",
            row.try_get("agent_id")?,
            json!({"name": name, "reason": "departed"}),
        ));
    }
    if name.starts_with("delegate:") || name.starts_with("designated:") {
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
        return Ok(());
    }
    let Some(waiter) = tx.query_opt("SELECT w.* FROM coordination_lease_waiters w JOIN coordination_agents a ON a.agent_id=w.agent_id AND a.credential_hash IS NOT NULL WHERE w.name=$1 ORDER BY w.ticket LIMIT 1 FOR UPDATE OF w", &[&name]).await? else { return Ok(()); };
    let agent: String = waiter.try_get("agent_id")?;
    let principal: String = waiter.try_get("principal")?;
    let purpose: String = waiter.try_get("purpose")?;
    let hold = waiter.try_get::<_, i32>("ttl")?.min(300);
    let expect: Option<i32> = waiter.try_get("expect")?;
    let expiry = now + f64::from(hold);
    let expected = expect.map(|expect| now + f64::from(expect));
    tx.execute(
        "DELETE FROM coordination_lease_waiters WHERE name=$1 AND agent_id=$2",
        &[&name, &agent],
    )
    .await?;
    let grant = tx.query_one("UPDATE coordination_leases SET holder_agent_id=$1,holder_principal=$2,purpose=$3,fence=nextval('coordination_lease_fence'),acquired_at=$4,expires_at=$5,expect=$6,expected_end=$7,freed_at=NULL WHERE name=$8 RETURNING fence", &[&agent, &principal, &purpose, &now, &expiry, &expect, &expected, &name]).await?;
    let fence: i64 = grant.try_get("fence")?;
    events.push(Event::new(
        "lease_grant",
        "daemon",
        agent,
        json!({"expect": expect, "fence": fence, "name": name, "purpose": purpose, "window": hold}),
    ));
    Ok(())
}

/// The audit format uses the clock's finite binary64 value, including its
/// shortest decimal spelling. This affects chain hashes, never display output.
pub fn audit_timestamp(now: f64) -> Result<String, Error> {
    if !now.is_finite() {
        return Err(Error::Clock);
    }
    let text = serde_json::to_string(&now).map_err(|_| Error::Clock)?;
    if let Some((mantissa, exponent)) = text.split_once('e') {
        let exponent: i32 = exponent.parse().map_err(|_| Error::Clock)?;
        if (-4..16).contains(&exponent) {
            let digits = mantissa.trim_start_matches('-').replace('.', "");
            let point = 1 + exponent;
            let sign = if now.is_sign_negative() { "-" } else { "" };
            let decimal = if point <= 0 {
                format!("0.{}{}", "0".repeat((-point) as usize), digits)
            } else if point as usize >= digits.len() {
                format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
            } else {
                let (whole, fraction) = digits.split_at(point as usize);
                format!("{whole}.{fraction}")
            };
            return Ok(format!("{sign}{decimal}"));
        }
        let mantissa = mantissa.strip_suffix(".0").unwrap_or(mantissa);
        return Ok(format!("{mantissa}e{exponent:+03}"));
    }
    // serde_json chooses fixed notation for some small values where the audit
    // producer uses scientific notation (for example 0.00001 -> 1e-05).
    if now != 0.0 && now.abs() < 0.0001 {
        let unsigned = text.trim_start_matches('-').trim_start_matches("0.");
        let zeros = unsigned.bytes().take_while(|c| *c == b'0').count();
        let digits = unsigned[zeros..].trim_end_matches('0');
        let (first, rest) = digits.split_at(1);
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let sign = if now.is_sign_negative() { "-" } else { "" };
        return Ok(format!("{sign}{first}{fraction}e-{:02}", zeros + 1));
    }
    Ok(text)
}
async fn append(tx: &Transaction<'_>, events: Vec<Event>, now: f64) -> Result<(), Error> {
    tx.query_one(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        &[&"coordination-audit-chain"],
    )
    .await?;
    let head = tx
        .query_opt(
            "SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1",
            &[],
        )
        .await?;
    let (mut seq, mut prev) = if let Some(head) = head {
        (
            head.try_get::<_, i64>("seq")?,
            head.try_get::<_, String>("hash")?,
        )
    } else {
        (0, "0".repeat(64))
    };
    let stamp = audit_timestamp(now)?;
    for event in events {
        seq = seq.checked_add(1).ok_or(Error::Database)?;
        let before = json!([
            "pseudolife-coordination-audit-v1",
            seq,
            event.event,
            event.actor,
            "",
            event.agent,
            Value::Null,
            "",
            "",
            Value::Null
        ]);
        let after = json!(["", event.payload]);
        let before = before.to_string();
        let after = after.to_string();
        let material = format!("{},{},{}", &before[..before.len() - 1], stamp, &after[1..]);
        let hash = hex_hash(format!("{prev}{material}"));
        tx.execute("INSERT INTO coordination_events (seq,event,actor,principal,agent_id,recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash) VALUES ($1,$2,$3,'',$4,NULL,'','',NULL,$5,$6,'',$7,$8)", &[&seq, &event.event, &event.actor, &event.agent, &event.payload, &now, &prev, &hash]).await?;
        prev = hash;
    }
    Ok(())
}
/// The clock is read inside BEGIN, matching the existing Python store seam.
/// Fixtures can inject the same clock into both stores without a production
/// Python change. Callers must compare all rows and hash bytes exactly.
pub async fn break_lease(
    session: &mut Session,
    name: &str,
    clock: impl FnOnce() -> f64,
) -> Result<Broken, Error> {
    validate_name(name)?;
    let tx = session.client_mut().transaction().await?;
    let now = clock();
    audit_timestamp(now)?;
    let row = tx
        .query_opt(
            "SELECT * FROM coordination_leases WHERE name=$1 FOR UPDATE",
            &[&name],
        )
        .await?;
    let holder: Option<String> = row
        .as_ref()
        .map(|row| row.try_get("holder_agent_id"))
        .transpose()?
        .flatten();
    if let Some(holder) = holder {
        let fence: i64 = row.as_ref().expect("held lease").try_get("fence")?;
        vacate(&tx, name, now).await?;
        let mut events = vec![Event::new(
            "lease_break",
            "operator",
            holder.clone(),
            json!({"fence": fence, "name": name}),
        )];
        settle(&tx, name, now, &mut events).await?;
        append(&tx, events, now).await?;
        tx.commit().await?;
        Ok(Broken {
            name: name.to_owned(),
            broken: true,
            was_held_by: Some(holder),
        })
    } else {
        tx.commit().await?;
        Ok(Broken {
            name: name.to_owned(),
            broken: false,
            was_held_by: None,
        })
    }
}
