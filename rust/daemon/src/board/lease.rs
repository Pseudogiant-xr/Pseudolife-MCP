//! Resource leases (spec BS8, `storage/coordination.py:1936-2298`): one row
//! per name kept across holds so the fence (one sequence for all) keeps
//! rising, a ticket-ordered queue per name, and settling (expire a lapsed
//! hold, grant a free lease to its queue's head) on every lease call,
//! listing and prune. Lock order: lease row, waiter rows, agent row, chain.

use super::canon::float_value;
use super::db::{LeaseRow, obj, opt_f64};
use super::error::{BoardError, Result};
use super::store::{Event, Store, append, check_limit, now, refuse_secret};
use super::text;
use serde_json::{Value, json};
use tokio_postgres::GenericClient;

pub const MAX_LEASE_NAME: usize = 120;
pub const LEASE_TTL_MIN: i64 = 30;
pub const LEASE_TTL_MAX: i64 = 86400;
pub const LEASE_EXPECT_MAX: i64 = 7 * 86400;
pub const LEASE_QUEUE_MAX: i64 = 64;
pub const LEASE_GRANT_WINDOW: i32 = 300;
pub const LEASE_LIST_QUEUE: i64 = 10;
pub const DELEGATE_PREFIX: &str = "delegate:";
pub const RESERVED_LEASE_PREFIXES: [&str; 2] = [DELEGATE_PREFIX, "designated:"];
pub const COORDINATOR_PREFIX: &str = "coordinator:";
/// `EPHEMERAL_AGENT_RETENTION` and `AGENT_RETENTION`.
pub const EPHEMERAL_AGENT_RETENTION: f64 = 3600.0;
pub const AGENT_RETENTION: f64 = 7.0 * 86400.0;

fn reserved(name: &str) -> bool {
    RESERVED_LEASE_PREFIXES.iter().any(|p| name.starts_with(p))
}

/// `_lease_args`: the name (no control/format/surrogate/unassigned
/// character, no surrounding whitespace, at most 120), ttl, expect, purpose.
pub fn lease_args(
    name: &Value,
    ttl: Option<&Value>,
    expect: Option<&Value>,
    purpose: Option<&Value>,
) -> Result<(String, Option<i64>, Option<i64>, Option<String>)> {
    let Some(name) = name.as_str() else {
        return Err(BoardError::code("invalid_lease"));
    };
    if name.is_empty()
        || name != text::py_strip(name)
        || text::py_len(name) > MAX_LEASE_NAME
        || name.chars().any(text::category_c)
    {
        return Err(BoardError::code("invalid_lease"));
    }
    refuse_secret(name, "lease")?;
    let ttl = match ttl.filter(|v| !v.is_null()) {
        None => None,
        Some(v) => match super::canon::py_int_i64(v) {
            Some(t) if (LEASE_TTL_MIN..=LEASE_TTL_MAX).contains(&t) => Some(t),
            _ => return Err(BoardError::code("invalid_ttl")),
        },
    };
    let expect = match expect.filter(|v| !v.is_null()) {
        None => None,
        Some(v) => match super::canon::py_int_i64(v) {
            Some(e) if (1..=LEASE_EXPECT_MAX).contains(&e) => Some(e),
            _ => return Err(BoardError::code("invalid_expect")),
        },
    };
    let purpose = match purpose.filter(|v| !v.is_null()) {
        None => None,
        Some(v) => {
            let p = text::check_value(v, 240, "purpose", true)?;
            refuse_secret(p, "purpose")?;
            Some(p.to_string())
        }
    };
    Ok((name.to_string(), ttl, expect, purpose))
}

/// `_grant`.
#[allow(clippy::too_many_arguments)]
async fn grant(
    c: &impl GenericClient,
    name: &str,
    agent_id: &str,
    principal: &str,
    now: f64,
    hold: f64,
    expect: Option<i32>,
    purpose: &str,
) -> Result<LeaseRow> {
    let expected_end = expect.map(|e| now + f64::from(e));
    let row = c
        .query_one(
            "UPDATE coordination_leases SET holder_agent_id=$1,holder_principal=$2,purpose=$3,\
             fence=nextval('coordination_lease_fence'),acquired_at=$4,expires_at=$5,expect=$6,\
             expected_end=$7,freed_at=NULL WHERE name=$8 RETURNING *",
            &[
                &agent_id,
                &principal,
                &purpose,
                &now,
                &(now + hold),
                &expect,
                &expected_end,
                &name,
            ],
        )
        .await?;
    Ok(LeaseRow::from_row(&row))
}

/// `_vacate`.
async fn vacate(c: &impl GenericClient, name: &str, now: f64) -> Result<LeaseRow> {
    let row = c
        .query_one(
            "UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',\
             purpose='',acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,\
             freed_at=$1 WHERE name=$2 RETURNING *",
            &[&now, &name],
        )
        .await?;
    Ok(LeaseRow::from_row(&row))
}

/// `_drop_reserved_waiters`.
async fn drop_reserved_waiters(
    c: &impl GenericClient,
    name: &str,
    events: &mut Vec<Event>,
) -> Result<()> {
    for r in c
        .query(
            "DELETE FROM coordination_lease_waiters WHERE name=$1 RETURNING agent_id",
            &[&name],
        )
        .await?
    {
        let agent: String = r.get(0);
        events.push(
            Event::new("lease_dequeue", &json!({"name": name, "reason": "reserved"}))
                .actor("daemon")
                .agent(&agent),
        );
    }
    Ok(())
}

/// `_settle`: the locked row after expiry and grant, or None.
pub async fn settle(
    c: &impl GenericClient,
    name: &str,
    now: f64,
    events: &mut Vec<Event>,
) -> Result<Option<LeaseRow>> {
    let Some(row) = c
        .query_opt(
            "SELECT * FROM coordination_leases WHERE name=$1 FOR UPDATE",
            &[&name],
        )
        .await?
    else {
        return Ok(None);
    };
    let mut row = LeaseRow::from_row(&row);
    if let Some(holder) = &row.holder_agent_id
        && row.expires_at.is_some_and(|e| e <= now)
        && !crate::mutants::active("board-lease-never-expires")
    {
        events.push(
            Event::new("lease_expire", &json!({"name": name, "fence": row.fence}))
                .actor("daemon")
                .agent(holder),
        );
        row = vacate(c, name, now).await?;
    }
    if row.holder_agent_id.is_none() {
        let window = "CASE WHEN a.capabilities->>'resumable'='false' THEN $2::double precision \
                      ELSE $3::double precision END";
        let sql = format!(
            "DELETE FROM coordination_lease_waiters w WHERE w.name=$1 AND (NOT EXISTS \
             (SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id) OR EXISTS \
             (SELECT 1 FROM coordination_agents a WHERE a.agent_id=w.agent_id \
             AND (a.lease_until IS NULL OR a.lease_until<={window}) \
             AND a.last_activity<={window})) RETURNING w.agent_id"
        );
        let short = now - EPHEMERAL_AGENT_RETENTION;
        let long = now - AGENT_RETENTION;
        for r in c.query(&sql, &[&name, &short, &long]).await? {
            let agent: String = r.get(0);
            events.push(
                Event::new("lease_dequeue", &json!({"name": name, "reason": "departed"}))
                    .actor("daemon")
                    .agent(&agent),
            );
        }
        if reserved(name) {
            drop_reserved_waiters(c, name, events).await?;
            return Ok(Some(row));
        }
        let waiter = c
            .query_opt(
                "SELECT w.* FROM coordination_lease_waiters w JOIN coordination_agents a \
                 ON a.agent_id=w.agent_id AND a.credential_hash IS NOT NULL \
                 WHERE w.name=$1 ORDER BY w.ticket LIMIT 1 FOR UPDATE OF w",
                &[&name],
            )
            .await?;
        if let Some(w) = waiter {
            let agent: String = w.get("agent_id");
            let principal: String = w.get("principal");
            let ttl: i32 = w.get("ttl");
            let expect: Option<i32> = w.get("expect");
            let purpose: String = w.get("purpose");
            c.execute(
                "DELETE FROM coordination_lease_waiters WHERE name=$1 AND agent_id=$2",
                &[&name, &agent],
            )
            .await?;
            let hold = ttl.min(LEASE_GRANT_WINDOW);
            row = grant(c, name, &agent, &principal, now, f64::from(hold), expect, &purpose).await?;
            events.push(
                Event::new(
                    "lease_grant",
                    &json!({"name": name, "fence": row.fence, "window": hold,
                            "expect": expect, "purpose": purpose}),
                )
                .actor("daemon")
                .agent(&agent),
            );
        }
    }
    Ok(Some(row))
}

/// `_settle_due`: every lapsed hold, and every free lease with waiters, in
/// name order.
pub async fn settle_due(
    c: &impl GenericClient,
    now: f64,
    events: &mut Vec<Event>,
    name: Option<&str>,
) -> Result<()> {
    let base = "SELECT name FROM coordination_leases l WHERE ((l.holder_agent_id IS NOT NULL \
                AND l.expires_at<=$1) OR (l.holder_agent_id IS NULL AND EXISTS (SELECT 1 FROM \
                coordination_lease_waiters w WHERE w.name=l.name)))";
    let rows = match name {
        Some(n) => {
            c.query(
                &format!("{base} AND l.name=$2 ORDER BY l.name FOR UPDATE"),
                &[&now, &n],
            )
            .await?
        }
        None => {
            c.query(&format!("{base} ORDER BY l.name FOR UPDATE"), &[&now])
                .await?
        }
    };
    for r in rows {
        let n: String = r.get(0);
        settle(c, &n, now, events).await?;
    }
    Ok(())
}

/// `_holder`.
fn holder(row: &LeaseRow) -> Value {
    let Some(agent) = &row.holder_agent_id else {
        return Value::Null;
    };
    let mut h = obj(vec![
        ("agent_id", json!(agent)),
        ("label", json!(row.label.clone().unwrap_or_default())),
        (
            "name",
            json!(text::board_name(
                agent,
                row.label.as_deref(),
                row.holder_name.as_deref()
            )),
        ),
        ("principal", json!(row.holder_principal)),
        ("purpose", json!(row.purpose)),
        ("acquired_at", opt_f64(row.acquired_at)),
        ("expires_at", opt_f64(row.expires_at)),
        ("expected_end", opt_f64(row.expected_end)),
    ]);
    if row.name.starts_with("claim:file:") {
        h["fence"] = json!(row.fence);
    }
    h
}

async fn lease_row(c: &impl GenericClient, name: &str) -> Result<LeaseRow> {
    let row = c
        .query_one(
            "SELECT l.*,a.label,a.name AS holder_name FROM coordination_leases l \
             LEFT JOIN coordination_agents a \
             ON a.agent_id=l.holder_agent_id WHERE l.name=$1",
            &[&name],
        )
        .await?;
    Ok(LeaseRow::from_row(&row))
}

/// `_lease_view`.
async fn lease_view(c: &impl GenericClient, name: &str, agent_id: &str) -> Result<Value> {
    let row = lease_row(c, name).await?;
    let queued: i64 = c
        .query_one(
            "SELECT count(*) AS n FROM coordination_lease_waiters WHERE name=$1",
            &[&name],
        )
        .await?
        .get(0);
    let mine = row.holder_agent_id.as_deref() == Some(agent_id);
    let mut position = Value::Null;
    if !mine {
        let ahead: i64 = c
            .query_one(
                "SELECT count(*) AS n FROM coordination_lease_waiters w, \
                 coordination_lease_waiters me WHERE me.name=$1 AND me.agent_id=$2 \
                 AND w.name=me.name AND w.ticket<=me.ticket",
                &[&name, &agent_id],
            )
            .await?
            .get(0);
        if ahead != 0 {
            position = json!(ahead);
        }
    }
    Ok(json!({
        "name": name,
        "state": if mine { "held" } else { "queued" },
        "fence": if mine { json!(row.fence) } else { Value::Null },
        "expires_at": if mine { opt_f64(row.expires_at) } else { Value::Null },
        "expected_end": if mine { opt_f64(row.expected_end) } else { Value::Null },
        "position": position,
        "queued": queued,
        "holder": holder(&row),
    }))
}

/// `_lock_role_leases`.
async fn lock_role_leases(c: &impl GenericClient, project: &str) -> Result<()> {
    let mut names: Vec<String> = [
        format!("{COORDINATOR_PREFIX}{project}"),
        format!("{DELEGATE_PREFIX}{project}"),
    ]
    .into_iter()
    .filter(|n| text::py_len(n) <= MAX_LEASE_NAME)
    .collect();
    names.sort();
    for n in &names {
        c.execute(
            "INSERT INTO coordination_leases (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            &[n],
        )
        .await?;
    }
    c.query(
        "SELECT name FROM coordination_leases WHERE name=ANY($1) ORDER BY name FOR UPDATE",
        &[&names],
    )
    .await?;
    Ok(())
}

impl Store {
    /// `acquire_lease`.
    #[allow(clippy::too_many_arguments)]
    pub async fn acquire_lease(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        name: &Value,
        ttl: Option<&Value>,
        expect: Option<&Value>,
        purpose: Option<&Value>,
    ) -> Result<Value> {
        let default_ttl = json!(120);
        let ttl = ttl.or(Some(&default_ttl));
        let (name, ttl, expect, purpose) = lease_args(name, ttl, expect, purpose)?;
        // `ttl=None` passed explicitly reaches _lease_args as None and is then
        // used as `now + None`: a TypeError, public `invalid_request`.
        let Some(ttl) = ttl else {
            return Err(BoardError::code("invalid_request_type_error"));
        };
        if reserved(&name) {
            return Err(BoardError::code("reserved_lease"));
        }
        let tx = client.transaction().await?;
        let agent = self.auth(&tx, principal, agent_id, credential, false).await?;
        let now = now();
        if let Some(project) = name.strip_prefix(COORDINATOR_PREFIX) {
            lock_role_leases(&tx, project).await?;
            let live = "SELECT 1 AS d FROM coordination_leases WHERE name=$1 \
                        AND holder_agent_id=$2 AND expires_at>$3";
            let delegate = tx
                .query_opt(live, &[&format!("{DELEGATE_PREFIX}{project}"), &agent_id, &now])
                .await?;
            let coordinator = tx.query_opt(live, &[&name, &agent_id, &now]).await?;
            if delegate.is_some() && coordinator.is_none() {
                return Err(BoardError::code("already_delegate"));
            }
        }
        tx.execute(
            "INSERT INTO coordination_leases (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            &[&name],
        )
        .await?;
        let mut events = Vec::new();
        let row = settle(&tx, &name, now, &mut events)
            .await?
            .expect("lease row inserted above");
        let scoped = |e: Event| {
            e.principal(principal)
                .agent(agent_id)
                .scope(&agent.project, &agent.task)
        };
        let expect32 = expect.map(|e| e as i32);
        let purpose_or_empty = purpose.clone().unwrap_or_default();
        if row.holder_agent_id.as_deref() == Some(agent_id) {
            let mut changed = serde_json::Map::new();
            let new_expect = expect32.filter(|e| Some(*e) != row.expect);
            let new_purpose = purpose.clone().filter(|p| *p != row.purpose);
            tx.execute(
                "UPDATE coordination_leases SET expires_at=$1 WHERE name=$2",
                &[&(now + ttl as f64), &name],
            )
            .await?;
            if let Some(e) = new_expect {
                tx.execute(
                    "UPDATE coordination_leases SET expect=$1,expected_end=$2 WHERE name=$3",
                    &[&e, &(now + f64::from(e)), &name],
                )
                .await?;
                changed.insert("expect".into(), json!(e));
            }
            if let Some(p) = &new_purpose {
                tx.execute(
                    "UPDATE coordination_leases SET purpose=$1 WHERE name=$2",
                    &[p, &name],
                )
                .await?;
                changed.insert("purpose".into(), json!(p));
            }
            if !changed.is_empty() {
                let mut payload = json!({"name": name, "fence": row.fence});
                for (k, v) in changed {
                    payload[k] = v;
                }
                events.push(scoped(Event::new("lease_update", &payload)));
            }
        } else if row.holder_agent_id.is_none() {
            let granted = grant(
                &tx,
                &name,
                agent_id,
                principal,
                now,
                ttl as f64,
                expect32,
                &purpose_or_empty,
            )
            .await?;
            events.push(scoped(Event::new(
                "lease_acquire",
                &json!({"name": name, "fence": granted.fence, "ttl": ttl,
                        "expect": expect, "purpose": purpose_or_empty}),
            )));
        } else if tx
            .query_opt(
                "SELECT 1 AS q FROM coordination_lease_waiters WHERE name=$1 AND agent_id=$2",
                &[&name, &agent_id],
            )
            .await?
            .is_none()
        {
            let n: i64 = tx
                .query_one(
                    "SELECT count(*) AS n FROM coordination_lease_waiters WHERE name=$1",
                    &[&name],
                )
                .await?
                .get(0);
            if n >= LEASE_QUEUE_MAX {
                return Err(BoardError::code("lease_queue_full"));
            }
            tx.execute(
                "INSERT INTO coordination_lease_waiters (name,agent_id,principal,purpose,ttl,\
                 expect,enqueued_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                &[
                    &name,
                    &agent_id,
                    &principal,
                    &purpose_or_empty,
                    &(ttl as i32),
                    &expect32,
                    &now,
                ],
            )
            .await?;
            events.push(scoped(Event::new(
                "lease_queue",
                &json!({"name": name, "ttl": ttl, "expect": expect,
                        "purpose": purpose_or_empty}),
            )));
        }
        tx.execute(
            "UPDATE coordination_agents SET last_activity=$1 WHERE agent_id=$2",
            &[&now, &agent_id],
        )
        .await?;
        append(&tx, events, now, None).await?;
        let view = lease_view(&tx, &name, agent_id).await?;
        tx.commit().await?;
        Ok(view)
    }

    /// `release_lease`.
    pub async fn release_lease(
        &self,
        client: &mut tokio_postgres::Client,
        principal: &str,
        agent_id: &str,
        credential: &str,
        name: &Value,
    ) -> Result<Value> {
        let (name, ..) = lease_args(name, None, None, None)?;
        let tx = client.transaction().await?;
        let agent = self.auth(&tx, principal, agent_id, credential, false).await?;
        let now = now();
        let scoped = |e: Event| {
            e.principal(principal)
                .agent(agent_id)
                .scope(&agent.project, &agent.task)
        };
        let row = tx
            .query_opt(
                "SELECT * FROM coordination_leases WHERE name=$1 FOR UPDATE",
                &[&name],
            )
            .await?
            .map(|r| LeaseRow::from_row(&r));
        let mut events = Vec::new();
        let result;
        if let Some(row) = row.filter(|r| r.holder_agent_id.as_deref() == Some(agent_id)) {
            vacate(&tx, &name, now).await?;
            events.push(scoped(Event::new(
                "lease_release",
                &json!({"name": name, "fence": row.fence}),
            )));
            settle(&tx, &name, now, &mut events).await?;
            result = json!({"name": name, "released": true, "dequeued": false});
        } else if tx
            .query_opt(
                "DELETE FROM coordination_lease_waiters WHERE name=$1 AND agent_id=$2 \
                 RETURNING ticket",
                &[&name, &agent_id],
            )
            .await?
            .is_some()
        {
            events.push(scoped(Event::new("lease_dequeue", &json!({"name": name}))));
            result = json!({"name": name, "released": false, "dequeued": true});
        } else {
            return Err(BoardError::code("lease_not_held"));
        }
        tx.execute(
            "UPDATE coordination_agents SET last_activity=$1 WHERE agent_id=$2",
            &[&now, &agent_id],
        )
        .await?;
        append(&tx, events, now, None).await?;
        tx.commit().await?;
        Ok(result)
    }
}

/// `list_leases`: held and queued leases, resource leases before claims,
/// each with its first LEASE_LIST_QUEUE waiters and an exact count.
pub async fn list_leases(
    client: &mut tokio_postgres::Client,
    name: Option<&str>,
    limit: i64,
    settle_due_first: bool,
) -> Result<Value> {
    let now = now();
    if settle_due_first {
        let tx = client.transaction().await?;
        let mut events = Vec::new();
        settle_due(&tx, now, &mut events, name).await?;
        append(&tx, events, now, None).await?;
        tx.commit().await?;
    }
    lease_page(&*client, name, limit, now).await
}

/// The read half of `list_leases`: one page of held and queued leases.
pub async fn lease_page(
    client: &impl GenericClient,
    name: Option<&str>,
    limit: i64,
    now: f64,
) -> Result<Value> {
    let busy = "(l.holder_agent_id IS NOT NULL OR EXISTS (SELECT 1 FROM \
                coordination_lease_waiters w WHERE w.name=l.name))";
    let page = limit + 1;
    let rows: Vec<LeaseRow> = match name {
        Some(n) => {
            client
                .query(
                    &format!(
                        "SELECT l.*,a.label,a.name AS holder_name FROM coordination_leases l \
                         LEFT JOIN coordination_agents a ON a.agent_id=l.holder_agent_id WHERE {busy} \
                         AND l.name=$1 ORDER BY l.name LIKE 'claim:%', l.name LIMIT $2"
                    ),
                    &[&n, &page],
                )
                .await?
        }
        None => {
            client
                .query(
                    &format!(
                        "SELECT l.*,a.label,a.name AS holder_name FROM coordination_leases l \
                         LEFT JOIN coordination_agents a ON a.agent_id=l.holder_agent_id WHERE {busy} \
                         ORDER BY l.name LIKE 'claim:%', l.name LIMIT $1"
                    ),
                    &[&page],
                )
                .await?
        }
    }
    .iter()
    .map(LeaseRow::from_row)
    .collect();
    let page_rows: Vec<&LeaseRow> = rows.iter().take(limit as usize).collect();
    let names: Vec<String> = page_rows.iter().map(|r| r.name.clone()).collect();
    let mut queues: std::collections::HashMap<String, Vec<Value>> =
        names.iter().map(|n| (n.clone(), Vec::new())).collect();
    let mut counts: std::collections::HashMap<String, i64> = std::collections::HashMap::new();
    if !names.is_empty() {
        for w in client
            .query(
                "SELECT wanted.name,w.agent_id,coalesce(a.label,'') AS label,a.name AS agent_name,\
                 w.enqueued_at,w.purpose \
                 FROM unnest($1::text[]) AS wanted(name) CROSS JOIN LATERAL \
                 (SELECT agent_id,enqueued_at,purpose,ticket FROM coordination_lease_waiters \
                 WHERE name=wanted.name ORDER BY ticket LIMIT $2) w \
                 LEFT JOIN coordination_agents a ON a.agent_id=w.agent_id \
                 ORDER BY wanted.name,w.ticket",
                &[&names, &LEASE_LIST_QUEUE],
            )
            .await?
        {
            let lease: String = w.get(0);
            let agent: String = w.get(1);
            let label: String = w.get(2);
            let agent_name: Option<String> = w.get(3);
            let enqueued_at: f64 = w.get(4);
            let purpose: String = w.get(5);
            queues.entry(lease).or_default().push(obj(vec![
                ("agent_id", json!(agent)),
                ("label", json!(label)),
                ("enqueued_at", float_value(enqueued_at)),
                ("purpose", json!(purpose)),
                (
                    "name",
                    json!(text::board_name(&agent, Some(&label), agent_name.as_deref())),
                ),
            ]));
        }
        for r in client
            .query(
                "SELECT name,count(*) AS n FROM coordination_lease_waiters \
                 WHERE name=ANY($1::text[]) GROUP BY name",
                &[&names],
            )
            .await?
        {
            counts.insert(r.get(0), r.get(1));
        }
    }
    let leases: Vec<Value> = page_rows
        .iter()
        .map(|row| {
            json!({
                "name": row.name,
                "holder": holder(row),
                "fence": row.fence,
                "expires_at": opt_f64(row.expires_at),
                "expected_end": opt_f64(row.expected_end),
                "stale": row.expected_end.is_some_and(|e| now > e),
                "queued": counts.get(&row.name).copied().unwrap_or(0),
                "queue": queues.remove(&row.name).unwrap_or_default(),
            })
        })
        .collect();
    Ok(json!({"leases": leases, "truncated": rows.len() as i64 > limit}))
}

/// `list_leases` behind the `leases` action: `name` and `limit` checked first.
pub async fn leases_action(
    client: &mut tokio_postgres::Client,
    params: &serde_json::Map<String, Value>,
) -> Result<Value> {
    let name = match params.get("name").filter(|v| !v.is_null()) {
        Some(v) => Some(lease_args(v, None, None, None)?.0),
        None => None,
    };
    let limit = check_limit(params.get("limit").unwrap_or(&json!(super::store::MAX_PAGE)))?;
    list_leases(client, name.as_deref(), limit, true).await
}
