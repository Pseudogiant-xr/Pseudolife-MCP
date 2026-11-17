//! The bank through `PSEUDOLIFE_MCP_DATABASE_URL` only, read as
//! `board_audit_cli._bank()` and `audit_events` read it. The lite tier's
//! embedded instance is separate; container forwarding lives in transport.rs.
use super::{args::Export, chain, codec};
use crate::pg;
use serde_json::{Map, Number, Value};
use tokio_postgres::{GenericClient, IsolationLevel, types::ToSql};

pub(super) const NO_LOG: &str =
    "this bank has no audit log yet (schema older than v42); start a v42 daemon once to create it";
const COLUMNS: &str = "seq,event,actor,principal,agent_id,recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash";

/// A refusal never claims the audit was checked or the operation completed.
pub(super) enum Ending {
    Exit(u8),
    Refused(&'static str),
}

/// None selects container transport, whose preflight rejects an invalid
/// explicit setting before it can select a different bank.
pub(super) fn dsn() -> Option<pg::Dsn> {
    let value = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL").ok()?;
    if value.is_empty() {
        return None;
    }
    pg::Dsn::parse_with_connection_options(&value).ok()
}

pub(super) fn dsn_refusal() -> u8 {
    let error = match std::env::var("PSEUDOLIFE_MCP_DATABASE_URL") {
        Ok(value) => pg::Dsn::parse_with_connection_options(&value).err(),
        Err(_) => None,
    };
    match error {
        Some(error) => connection_error(error),
        None => super::stats::error(
            "PSEUDOLIFE_MCP_DATABASE_URL is missing or is not Unicode; set it to a valid PostgreSQL database URL",
        ),
    }
}

pub(super) fn connection_error(error: pg::Error) -> u8 {
    let message = match error {
        pg::Error::UnsupportedOption(name) if name == "sslmode" => "PSEUDOLIFE_MCP_DATABASE_URL sslmode is unsupported; choose disable, prefer, require, verify-ca or verify-full".to_owned(),
        pg::Error::UnsupportedOption(_) => "PSEUDOLIFE_MCP_DATABASE_URL contains an unsupported option name; remove that option (supported names: host, port, user, password, dbname, sslmode, sslrootcert, application_name, connect_timeout)".to_owned(),
        pg::Error::InvalidOption(name) => format!("PSEUDOLIFE_MCP_DATABASE_URL has an invalid {name} option; correct that option"),
        pg::Error::InvalidDsn => "PSEUDOLIFE_MCP_DATABASE_URL is not a supported PostgreSQL URL or keyword connection string; correct that setting".to_owned(),
        pg::Error::AmbientControl(name) if matches!(name.as_str(), "postgresql.crt" | "postgresql.key" | "root.crl") => {
            format!("unsupported implicit PostgreSQL TLS file {name}; move that file out of the default PostgreSQL directory before retrying")
        }
        pg::Error::AmbientControl(name) => {
            // Unknown PGSSL/PGTLS names may themselves contain pasted data.
            let name = if name.to_ascii_uppercase().starts_with("PGSSL") { "PGSSL*" }
                else if name.to_ascii_uppercase().starts_with("PGTLS") { "PGTLS*" }
                else { &name };
            format!("PostgreSQL ambient setting {name} is unsupported; remove that setting for this command and configure PSEUDOLIFE_MCP_DATABASE_URL explicitly")
        }
        pg::Error::RootCertificate => "PSEUDOLIFE_MCP_DATABASE_URL sslrootcert cannot be read as a trusted certificate; set sslrootcert to a readable PEM certificate".to_owned(),
        pg::Error::HostnameRules => "PSEUDOLIFE_MCP_DATABASE_URL host does not match the TLS certificate's subject alternative names; use the host named by the server certificate".to_owned(),
        pg::Error::SessionSetup => "PostgreSQL audit session setup failed; restore the session permissions for the login in PSEUDOLIFE_MCP_DATABASE_URL".to_owned(),
        pg::Error::Connection | pg::Error::Shutdown => "PostgreSQL audit connection failed; restore connectivity and authentication for PSEUDOLIFE_MCP_DATABASE_URL".to_owned(),
    };
    super::stats::error(message)
}

const SNAPSHOT_ERROR: &str = "cannot start a read-only audit snapshot; restore database connectivity and transaction permissions for PSEUDOLIFE_MCP_DATABASE_URL";
const LOG_ERROR: &str = "cannot inspect the audit schema; restore database connectivity and catalog permissions for PSEUDOLIFE_MCP_DATABASE_URL";
const READ_ERROR: &str = "cannot read audit rows; restore database connectivity and SELECT permissions on coordination_events";
const DECODE_ERROR: &str = "audit columns have unsupported database types or nulls; restore the coordination_events schema from a trusted backup";

fn read_error(error: tokio_postgres::Error) -> &'static str {
    use tokio_postgres::error::SqlState;
    match error.code() {
        Some(&SqlState::UNDEFINED_COLUMN | &SqlState::UNDEFINED_TABLE) => {
            "audit schema is missing a required table or column; restore the coordination_events schema from a trusted backup"
        }
        Some(&SqlState::INSUFFICIENT_PRIVILEGE) => {
            "audit SELECT permission was denied; grant the configured database login SELECT on coordination_events"
        }
        _ => READ_ERROR,
    }
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
pub(super) async fn audit_events(
    client: &impl GenericClient,
    filters: &Filters<'_>,
) -> Result<Vec<(Map<String, Value>, f64)>, &'static str> {
    let body = if has_body_column(client).await.map_err(|_| LOG_ERROR)? {
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
    let rows = client.query(&sql, &params).await.map_err(read_error)?;
    let text = |row: &tokio_postgres::Row, name: &str| -> Result<Value, &'static str> {
        let value: Option<String> = row.try_get(name).map_err(|_| DECODE_ERROR)?;
        Ok(value.map_or(Value::Null, Value::String))
    };
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let seq: i64 = row.try_get("seq").map_err(|_| DECODE_ERROR)?;
        let created_at: f64 = row.try_get("created_at").map_err(|_| DECODE_ERROR)?;
        if !created_at.is_finite() {
            return Err(
                "audit created_at is not finite; restore valid audit rows from a trusted backup",
            );
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
pub(super) async fn snapshot(session: &mut pg::Session) -> Result<pg::Transaction<'_>, ()> {
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

pub(super) async fn verify(dsn: &pg::Dsn, expect: Option<super::args::Head>) -> Ending {
    let mut session = match pg::Session::open(dsn).await {
        Ok(session) => session,
        Err(error) => return Ending::Exit(connection_error(error)),
    };
    let ending = verify_in(&mut session, expect).await;
    let _ = session.close().await;
    ending
}

async fn verify_in(session: &mut pg::Session, expect: Option<super::args::Head>) -> Ending {
    let Ok(tx) = snapshot(session).await else {
        return Ending::Refused(SNAPSHOT_ERROR);
    };
    match log_present(&tx).await {
        Ok(true) => {}
        Ok(false) => return no_log(),
        Err(()) => return Ending::Refused(LOG_ERROR),
    }
    let rows = match audit_events(&tx, &Filters::default()).await {
        Ok(rows) => rows,
        Err(message) => return Ending::Refused(message),
    };
    let _ = tx.rollback().await;
    let mut walker = chain::Walker::new(expect);
    for (map, created_at) in rows {
        let seq = map["seq"].as_i64().expect("bigint seq");
        let row = chain::Row {
            value: Value::Object(map),
            pending_payload: None,
            seq,
            created_at,
        };
        match walker.push(row) {
            Ok(None) => {}
            Ok(Some(report)) => return Ending::Exit(super::report(report, 1)),
            Err(chain::Failure::Unsupported(message)) => return Ending::Refused(message),
            Err(chain::Failure::Python) => return Ending::Exit(super::failed()),
        }
    }
    match walker.finish() {
        Ok((report, ok)) => Ending::Exit(super::report(report, if ok { 0 } else { 1 })),
        Err(chain::Failure::Unsupported(message)) => Ending::Refused(message),
        Err(chain::Failure::Python) => Ending::Exit(super::failed()),
    }
}

/// Both event and wake reads share one read-only repeatable-read snapshot.
pub(super) async fn stats(
    dsn: &pg::Dsn,
    first: f64,
    until: f64,
    since: f64,
) -> Result<(Vec<Value>, Option<Vec<Value>>), Ending> {
    let mut session = pg::Session::open(dsn)
        .await
        .map_err(|error| Ending::Exit(connection_error(error)))?;
    let result = async {
        let tx = snapshot(&mut session).await.map_err(|_| Ending::Exit(super::failed()))?;
        if !log_present(&tx).await.map_err(|_| Ending::Exit(super::failed()))? { return Err(no_log()); }
        let rows = audit_events(&tx,&Filters { since:Some(first),until:Some(until),..Filters::default() })
            .await.map_err(Ending::Refused)?;
        let mut events = Vec::new();
        for (mut row,_) in rows {
            let payload = row["payload"].as_str().ok_or_else(|| Ending::Exit(super::failed()))?;
            let parsed = codec::python_loads(payload).map_err(|_| Ending::Exit(super::failed()))?;
            row.insert("payload".into(),super::stats::payload(parsed)
                .map_err(|_| Ending::Refused("audit payload contains a value stats cannot represent; use an archive whose payload strings are valid Unicode"))?);
            events.push(Value::Object(row));
        }
        let present: bool = tx.query_one("SELECT to_regclass('public.coordination_wakes') IS NOT NULL",&[])
            .await.and_then(|r| r.try_get(0)).map_err(|_| Ending::Exit(super::failed()))?;
        let wakes = if present {
            let rows = tx.query("SELECT recipient_agent_id,decision,created_at,served_at FROM coordination_wakes WHERE created_at >= $1 AND created_at < $2 ORDER BY created_at,wake_id",&[&since,&until])
                .await.map_err(|_| Ending::Exit(super::failed()))?;
            let mut wakes = Vec::new();
            for row in rows {
                let recipient: String = row.try_get(0).map_err(|_| Ending::Exit(super::failed()))?;
                let decision: String = row.try_get(1).map_err(|_| Ending::Exit(super::failed()))?;
                let created: f64 = row.try_get(2).map_err(|_| Ending::Exit(super::failed()))?;
                let served: Option<f64> = row.try_get(3).map_err(|_| Ending::Exit(super::failed()))?;
                wakes.push(serde_json::json!({"recipient_agent_id":recipient,"decision":decision,"created_at":created,"served_at":served}));
            }
            Some(wakes)
        } else { None };
        tx.rollback().await.map_err(|_| Ending::Exit(super::failed()))?;
        Ok((events,wakes))
    }.await;
    let _ = session.close().await;
    result
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

fn agent_query_error(error: tokio_postgres::Error) -> &'static str {
    use tokio_postgres::error::SqlState;
    match error.code() {
        Some(&SqlState::UNDEFINED_COLUMN | &SqlState::UNDEFINED_TABLE) => {
            "--agent lookup schema is missing a required table or column; restore the coordination_agents and coordination_events schema from a trusted backup"
        }
        Some(&SqlState::INSUFFICIENT_PRIVILEGE) => {
            "--agent lookup SELECT permission was denied; grant the configured database login SELECT on coordination_agents and coordination_events"
        }
        _ if error.is_closed() => {
            "--agent lookup lost its database connection; restore connectivity to PSEUDOLIFE_MCP_DATABASE_URL"
        }
        _ => "--agent database lookup failed; report this database lookup failure",
    }
}

/// `resolve_agent_id(conn, value)`: the value itself, or the one id it is a
/// prefix of among registered addresses and the log's actors and recipients.
async fn resolve_agent(client: &impl GenericClient, value: &str) -> Result<Resolved, &'static str> {
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
        .map_err(agent_query_error)?;
    let ids: Vec<String> = rows
        .iter()
        .map(|row| row.try_get::<_, String>(0))
        .collect::<Result<_, _>>()
        .map_err(|_| "--agent lookup columns have unsupported types or nulls; restore the coordination_agents and coordination_events schema from a trusted backup")?;
    Ok(match ids.len() {
        0 => Resolved::Missing,
        1 => Resolved::Id(ids[0].clone()),
        _ => Resolved::Ambiguous(distinguishing_prefixes(&ids[..ids.len().min(8)]).join(", ")),
    })
}

/// One export line: `json.dumps(row, ensure_ascii=True, separators=(",", ":"))`
/// with the payload parsed by `json.loads`.
fn export_line(map: Map<String, Value>) -> Option<String> {
    let mut line = String::from("{");
    for (index, (key, value)) in map.iter().enumerate() {
        if index > 0 {
            line.push(',');
        }
        codec::ascii_compact(&Value::String(key.clone()), &mut line)?;
        line.push(':');
        if key == "payload" {
            let payload = codec::python_loads(value.as_str()?).ok()?;
            codec::python_compact(&payload, true, false, &mut line)?;
        } else {
            codec::ascii_compact(value, &mut line)?;
        }
    }
    line.push_str("}\n");
    Some(line)
}

pub(super) async fn export(dsn: &pg::Dsn, args: &Export) -> Ending {
    let mut session = match pg::Session::open(dsn).await {
        Ok(session) => session,
        Err(error) => return Ending::Exit(connection_error(error)),
    };
    let ending = export_in(&mut session, args).await;
    let _ = session.close().await;
    ending
}

async fn export_in(session: &mut pg::Session, args: &Export) -> Ending {
    let Ok(tx) = snapshot(session).await else {
        return Ending::Refused(SNAPSHOT_ERROR);
    };
    match log_present(&tx).await {
        Ok(true) => {}
        Ok(false) => return no_log(),
        Err(()) => return Ending::Refused(LOG_ERROR),
    }
    // Reserve a private staging file; the destination remains absent until success.
    let output = match Output::create(args) {
        Ok(output) => output,
        Err(ending) => return ending,
    };
    let outcome = match &args.agent {
        None => None,
        Some(agent) => match resolve_agent(&tx, agent).await {
            Err(message) => return Ending::Refused(message),
            Ok(Resolved::Id(id)) => Some(id),
            Ok(Resolved::Missing) => {
                crate::stderrln!("board-audit: no agent id starts with {agent}");
                return Ending::Exit(2);
            }
            Ok(Resolved::Ambiguous(listed)) => {
                crate::stderrln!(
                    "board-audit: --agent {agent} matches several ids: {listed}; give a longer prefix"
                );
                return Ending::Exit(2);
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
    let rows = match audit_events(&tx, &filters).await {
        Ok(rows) => rows,
        Err(message) => return Ending::Refused(message),
    };
    let _ = tx.rollback().await;
    for (map, _) in rows {
        let Some(line) = export_line(map) else {
            return Ending::Exit(super::stats::error(
                "audit payload cannot be encoded for export; restore a readable audit payload from a trusted backup; stdout output is incomplete and no file archive was published",
            ));
        };
        if let Err(code) = output.write(&line) {
            return Ending::Exit(code);
        }
    }
    Ending::Exit(output.finish())
}

/// `OSError.strerror` of a failed file write in CPython. POSIX: the C
/// library's `strerror` text, which std prints before ` (os error N)`.
/// Windows: the C runtime maps Win32 file errors to errno, including
/// missing parents, access denial, sharing violations and invalid names.
pub(super) fn write_strerror(error: &std::io::Error) -> Option<String> {
    let code = error.raw_os_error()?;
    if cfg!(windows) {
        return match code {
            2 | 3 | 206 => Some("No such file or directory".into()),
            5 | 32 => Some("Permission denied".into()),
            80 | 183 => Some("File exists".into()),
            112 => Some("No space left on device".into()),
            123 => Some("Invalid argument".into()),
            267 => Some("Not a directory".into()),
            _ => None,
        };
    }
    let text = error.to_string();
    text.strip_suffix(&format!(" (os error {code})"))
        .map(str::to_owned)
}

/// A file archive is private until complete; stdout keeps streaming rows.
struct Output {
    file: Option<super::output::Staged>,
}
impl Output {
    fn create(args: &Export) -> Result<Self, Ending> {
        let file = args
            .out
            .as_ref()
            .map(|out| {
                super::output::Staged::create(std::path::Path::new(out))
                    .map_err(|error| Ending::Exit(super::output::failure("staging", &error)))
            })
            .transpose()?;
        Ok(Self { file })
    }

    fn write(&self, line: &str) -> Result<(), u8> {
        use std::io::Write;
        if let Some(file) = &self.file {
            file.file()
                .write_all(line.as_bytes())
                .map_err(|e| super::output::failure("write", &e))
        } else {
            let mut stdout = std::io::stdout().lock();
            stdout.write_all(&super::super::text_bytes(line)).and_then(|()| stdout.flush())
                .map_err(|_| super::stats::error("stdout output is incomplete because the output was closed; rerun export with --out to a writable file"))
        }
    }

    fn finish(self) -> u8 {
        self.file.map_or(0, |file| {
            file.publish()
                .map_or_else(|e| super::output::failure("publication", &e), |()| 0)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_failures_name_cpython_strerror() {
        let full = std::io::Error::from_raw_os_error(if cfg!(windows) { 112 } else { 28 });
        assert_eq!(
            write_strerror(&full).as_deref(),
            Some("No space left on device")
        );
        if cfg!(windows) {
            assert_eq!(
                write_strerror(&std::io::Error::from_raw_os_error(5)).as_deref(),
                Some("Permission denied")
            );
            assert_eq!(
                write_strerror(&std::io::Error::from_raw_os_error(3)).as_deref(),
                Some("No such file or directory")
            );
            assert_eq!(
                write_strerror(&std::io::Error::from_raw_os_error(206)).as_deref(),
                Some("No such file or directory")
            );
            // ERROR_IO_DEVICE: the C runtime's errno for it is not pinned.
            assert_eq!(
                write_strerror(&std::io::Error::from_raw_os_error(1117)),
                None
            );
        } else {
            assert_eq!(
                write_strerror(&std::io::Error::from_raw_os_error(5)).as_deref(),
                Some("Input/output error")
            );
        }
        assert_eq!(write_strerror(&std::io::Error::other("no code")), None);
    }

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
