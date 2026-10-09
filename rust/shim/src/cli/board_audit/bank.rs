//! The bank through `PSEUDOLIFE_MCP_DATABASE_URL` only, read as
//! `board_audit_cli._bank()` and `audit_events` read it. The lite tier's
//! embedded instance and the daemon-container transport defer.
use super::{args::Export, chain, codec};
use crate::pg;
use serde_json::{Map, Number, Value};
use tokio_postgres::{GenericClient, IsolationLevel, types::ToSql};

pub(super) const NO_LOG: &str =
    "this bank has no audit log yet (schema older than v42); start a v42 daemon once to create it";
const COLUMNS: &str = "seq,event,actor,principal,agent_id,recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash";

/// What a bank action ends in; `Deferred` happens before any effect.
pub(super) enum Ending {
    Exit(u8),
    Deferred,
}

/// The configured DSN, or `None` (no or empty variable, an unsupported DSN
/// spelling, or a value that is not Unicode): those paths defer.
pub(super) fn dsn() -> Option<pg::Dsn> {
    let value = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL").ok()?;
    if value.is_empty() {
        return None;
    }
    pg::Dsn::parse(&value).ok()
}

pub(super) async fn has_body_column(client: &impl GenericClient) -> Result<bool, ()> {
    client
        .query_one(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('coordination_events') AND attname='body' AND attnum>0 AND NOT attisdropped)",
            &[],
        )
        .await
        .and_then(|row| row.try_get(0))
        .map_err(|_| ())
}

pub(super) async fn log_present(client: &impl GenericClient) -> Result<bool, ()> {
    client
        .query_one(
            "SELECT to_regclass('public.coordination_events') IS NOT NULL",
            &[],
        )
        .await
        .and_then(|row| row.try_get(0))
        .map_err(|_| ())
}

#[derive(Default)]
pub(super) struct Filters<'a> {
    pub project: Option<&'a str>,
    pub task: Option<&'a str>,
    pub agent: Option<String>,
    pub since: Option<f64>,
    pub until: Option<f64>,
}

/// `audit_events(conn, ...)` in chain order, each row as the dict the
/// Python generator yields (`payload` still the stored text).
async fn audit_events(
    client: &impl GenericClient,
    filters: &Filters<'_>,
) -> Result<Vec<(Map<String, Value>, f64)>, ()> {
    let body = if has_body_column(client).await? {
        "body,body_salt"
    } else {
        "NULL::text AS body,NULL::text AS body_salt"
    };
    let mut clauses = Vec::new();
    let mut params: Vec<&(dyn ToSql + Sync)> = Vec::new();
    for (column, value) in [("project", &filters.project), ("task", &filters.task)] {
        if let Some(value) = value {
            params.push(value);
            clauses.push(format!("{column}=${}", params.len()));
        }
    }
    if let Some(agent) = &filters.agent {
        params.push(agent);
        clauses.push(format!(
            "(agent_id=${0} OR recipient_agent_id=${0})",
            params.len()
        ));
    }
    if let Some(since) = &filters.since {
        params.push(since);
        clauses.push(format!("created_at>=${}", params.len()));
    }
    if let Some(until) = &filters.until {
        params.push(until);
        clauses.push(format!("created_at<${}", params.len()));
    }
    let filter = if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    };
    let sql = format!("SELECT {COLUMNS},{body} FROM coordination_events{filter} ORDER BY seq");
    let rows = client.query(&sql, &params).await.map_err(|_| ())?;
    let text = |row: &tokio_postgres::Row, name: &str| -> Result<Value, ()> {
        let value: Option<String> = row.try_get(name).map_err(|_| ())?;
        Ok(value.map_or(Value::Null, Value::String))
    };
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let seq: i64 = row.try_get("seq").map_err(|_| ())?;
        let created_at: f64 = row.try_get("created_at").map_err(|_| ())?;
        if !created_at.is_finite() {
            // Python would hash and print NaN/Infinity spellings: deferred.
            return Err(());
        }
        let mut map = Map::new();
        map.insert("seq".into(), Value::Number(seq.into()));
        for name in [
            "event",
            "actor",
            "principal",
            "agent_id",
            "recipient_agent_id",
            "project",
            "task",
            "message_id",
            "payload",
        ] {
            map.insert(name.into(), text(row, name)?);
        }
        map.insert(
            "created_at".into(),
            Number::from_f64(created_at).map_or(Value::Null, Value::Number),
        );
        for name in ["hlc", "prev_hash", "hash", "body", "body_salt"] {
            map.insert(name.into(), text(row, name)?);
        }
        out.push((map, created_at));
    }
    Ok(out)
}

/// A read-only repeatable-read snapshot, as `_bank()` opens for reads.
async fn snapshot(session: &mut pg::Session) -> Result<pg::Transaction<'_>, ()> {
    session
        .client_mut()
        .build_transaction()
        .isolation_level(IsolationLevel::RepeatableRead)
        .read_only(true)
        .start()
        .await
        .map_err(|_| ())
}

pub(super) fn no_log() -> Ending {
    crate::stderrln!("board-audit: {NO_LOG}");
    Ending::Exit(2)
}

pub(super) async fn verify(dsn: &pg::Dsn, expect: Option<(i64, String)>) -> Ending {
    let Ok(mut session) = pg::Session::open(dsn).await else {
        return Ending::Deferred;
    };
    let ending = verify_in(&mut session, expect).await;
    let _ = session.close().await;
    ending
}

async fn verify_in(session: &mut pg::Session, expect: Option<(i64, String)>) -> Ending {
    let Ok(tx) = snapshot(session).await else {
        return Ending::Deferred;
    };
    match log_present(&tx).await {
        Ok(true) => {}
        Ok(false) => return no_log(),
        Err(()) => return Ending::Deferred,
    }
    let Ok(rows) = audit_events(&tx, &Filters::default()).await else {
        return Ending::Deferred;
    };
    let _ = tx.rollback().await;
    let mut walker = chain::Walker::new(expect);
    for (map, created_at) in rows {
        let seq = map["seq"].as_i64().expect("bigint seq");
        let row = chain::Row {
            value: Value::Object(map),
            seq,
            created_at,
        };
        match walker.push(row) {
            Ok(None) => {}
            Ok(Some(report)) => return Ending::Exit(super::report(report, 1)),
            Err(chain::Deferred) => return Ending::Deferred,
        }
    }
    match walker.finish() {
        Ok((report, ok)) => Ending::Exit(super::report(report, if ok { 0 } else { 1 })),
        Err(chain::Deferred) => Ending::Deferred,
    }
}

fn is_id_prefix(value: &str) -> bool {
    (8..32).contains(&value.len())
        && value
            .bytes()
            .all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}

/// `distinguishing_prefixes`: each candidate cut to the shortest length that
/// tells them apart, never under eight characters.
pub(super) fn distinguishing_prefixes(ids: &[String]) -> Vec<String> {
    let mut ordered: Vec<Vec<char>> = ids.iter().map(|id| id.chars().collect()).collect();
    ordered.sort();
    let shared = ordered
        .windows(2)
        .map(|pair| {
            pair[0]
                .iter()
                .zip(&pair[1])
                .take_while(|(a, b)| a == b)
                .count()
        })
        .max()
        .unwrap_or(0);
    let cut = 8.max(shared + 1);
    ordered
        .iter()
        .map(|id| id.iter().take(cut).collect())
        .collect()
}

enum Resolved {
    Id(String),
    Missing,
    Ambiguous(String),
}

/// `resolve_agent_id(conn, value)`: the value itself, or the one id it is a
/// prefix of among registered addresses and the log's actors and recipients.
async fn resolve_agent(client: &impl GenericClient, value: &str) -> Result<Resolved, ()> {
    if !is_id_prefix(value) {
        return Ok(Resolved::Id(value.to_owned()));
    }
    let like = format!("{value}%");
    let rows = client
        .query(
            "SELECT agent_id FROM (SELECT agent_id FROM coordination_agents WHERE agent_id LIKE $1 UNION SELECT agent_id FROM coordination_events WHERE agent_id LIKE $1 UNION SELECT recipient_agent_id FROM coordination_events WHERE recipient_agent_id LIKE $1) AS ids ORDER BY agent_id LIMIT 9",
            &[&like],
        )
        .await
        .map_err(|_| ())?;
    let ids: Vec<String> = rows
        .iter()
        .map(|row| row.try_get::<_, String>(0))
        .collect::<Result<_, _>>()
        .map_err(|_| ())?;
    Ok(match ids.len() {
        0 => Resolved::Missing,
        1 => Resolved::Id(ids[0].clone()),
        _ => Resolved::Ambiguous(distinguishing_prefixes(&ids[..ids.len().min(8)]).join(", ")),
    })
}

/// One export line: `json.dumps(row, ensure_ascii=True, separators=(",", ":"))`
/// with the payload parsed by `json.loads`.
fn export_line(mut map: Map<String, Value>) -> Option<String> {
    let payload = match map.get("payload") {
        Some(Value::String(text)) => codec::python_loads(text)?,
        _ => return None,
    };
    map.insert("payload".into(), payload);
    let mut line = String::new();
    codec::ascii_compact(&Value::Object(map), &mut line)?;
    line.push('\n');
    Some(line)
}

pub(super) async fn export(dsn: &pg::Dsn, args: &Export) -> Ending {
    let Ok(mut session) = pg::Session::open(dsn).await else {
        return Ending::Deferred;
    };
    let ending = export_in(&mut session, args).await;
    let _ = session.close().await;
    ending
}

enum Outcome {
    Lines(String),
    AgentRefused(String),
}

async fn export_in(session: &mut pg::Session, args: &Export) -> Ending {
    let Ok(tx) = snapshot(session).await else {
        return Ending::Deferred;
    };
    match log_present(&tx).await {
        Ok(true) => {}
        Ok(false) => return no_log(),
        Err(()) => return Ending::Deferred,
    }
    // Everything is read and rendered before the first byte is written, so
    // any deferral leaves no file and no partial stdout behind.
    let outcome = match &args.agent {
        None => None,
        Some(agent) => match resolve_agent(&tx, agent).await {
            Err(()) => return Ending::Deferred,
            Ok(Resolved::Id(id)) => Some(id),
            Ok(Resolved::Missing) => {
                return write_refusal(args, format!("no agent id starts with {agent}"));
            }
            Ok(Resolved::Ambiguous(listed)) => {
                return write_refusal(
                    args,
                    format!("--agent {agent} matches several ids: {listed}; give a longer prefix"),
                );
            }
        },
    };
    let filters = Filters {
        project: args.project.as_deref(),
        task: args.task.as_deref(),
        agent: outcome,
        since: args.since,
        until: args.until,
    };
    let Ok(rows) = audit_events(&tx, &filters).await else {
        return Ending::Deferred;
    };
    let _ = tx.rollback().await;
    let mut text = String::new();
    for (map, _) in rows {
        let Some(line) = export_line(map) else {
            return Ending::Deferred;
        };
        text.push_str(&line);
    }
    write_lines(args, Outcome::Lines(text))
}

/// Python creates `--out` before resolving `--agent`, so a refused agent
/// leaves that new empty file behind; stdout gets nothing.
fn write_refusal(args: &Export, message: String) -> Ending {
    write_lines(args, Outcome::AgentRefused(message))
}

fn write_lines(args: &Export, outcome: Outcome) -> Ending {
    use std::io::Write;
    let (text, refusal) = match outcome {
        Outcome::Lines(text) => (text, None),
        Outcome::AgentRefused(message) => (String::new(), Some(message)),
    };
    if let Some(out) = &args.out {
        let Ok(mut file) = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(out)
        else {
            return Ending::Deferred;
        };
        if file
            .write_all(text.as_bytes())
            .and_then(|()| file.flush())
            .is_err()
        {
            drop(file);
            let _ = std::fs::remove_file(out);
            return Ending::Deferred;
        }
    } else if !text.is_empty() {
        let mut stdout = std::io::stdout().lock();
        if stdout
            .write_all(&super::super::text_bytes(&text))
            .and_then(|()| stdout.flush())
            .is_err()
        {
            crate::stderrln!("board-audit: the output was closed before the export finished");
            return Ending::Exit(2);
        }
    }
    if let Some(message) = refusal {
        crate::stderrln!("board-audit: {message}");
        return Ending::Exit(2);
    }
    Ending::Exit(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefixes_follow_the_producer() {
        let ids = [
            "abcdef0123456789abcdef0123456789".to_owned(),
            "abcdef0123450000abcdef0123456789".to_owned(),
        ];
        assert_eq!(
            distinguishing_prefixes(&ids),
            ["abcdef0123450", "abcdef0123456"]
        );
        assert!(is_id_prefix("abcdef01"));
        assert!(!is_id_prefix("abcdef0"));
        assert!(!is_id_prefix("ABCDEF01"));
    }
}
