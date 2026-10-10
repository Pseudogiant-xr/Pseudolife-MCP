//! PostgresStorage constructor, ping and bank id (spec sections P, H8-H9).
//!
//! Python oracle: `pseudolife_memory/storage/postgres.py` at master 35b8f5d2
//! (`PostgresStorage.__init__`, `_open_session`, `_connect`,
//! `_acquire_writer_lease`, `_describe_lease_holder`, `_seed_relations`,
//! `_bump_lease_epoch`, `ping`, `cached_bank_id`) and `service.py`'s
//! `_assert_public_search_path`.
//!
//! Declared differences from psycopg/libpq, none of which changes a database
//! write: no TLS (`NoTls`; a DSN that requires TLS fails to connect); DSN
//! options tokio-postgres does not know are a connect failure; the
//! `application_name` fallback names this binary instead of Python's argv.

// Wired by main in W1-A.
#![allow(dead_code)]

pub mod graph;
pub mod schema;

use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tokio::task::JoinHandle;
use tokio_postgres::error::SqlState;
use tokio_postgres::{Client, Config, NoTls};

use schema::{SchemaError, simple_value};

/// `WRITER_LEASE_KEY` (postgres.py:244).
pub const WRITER_LEASE_KEY: &str = "pseudolife-bank-writer";
/// `_LEASE_KEEPALIVE` (postgres.py:254-258), in order.
const LEASE_KEEPALIVE: [(&str, i64); 3] = [
    ("tcp_keepalives_idle", 60),
    ("tcp_keepalives_interval", 10),
    ("tcp_keepalives_count", 3),
];
/// `_LEASE_WAIT` (postgres.py:266).
const LEASE_WAIT: &str = "2s";
/// `BANK_ID_RETRY_SECONDS` (postgres.py:286).
pub const BANK_ID_RETRY: Duration = Duration::from_secs(60);
/// `_LEASE_EPOCH_KEY` (postgres.py:305).
const LEASE_EPOCH_KEY: &str = "writer_lease_epoch";
/// `BANK_ID_META_KEY` (storage/coordination.py:366).
pub const BANK_ID_META_KEY: &str = "coordination_bank_id";

/// `_BUILTIN_RELATIONS` (postgres.py:191-211): (name, description,
/// transitive, inverse_of). Referenced inverses come first (FK).
const BUILTIN_RELATIONS: [(&str, &str, bool, Option<&str>); 11] = [
    ("depends-on", "src requires dst to function", true, None),
    ("part-of", "src is a component of dst", true, None),
    ("hosts", "src is the host/platform for dst", false, None),
    (
        "runs-on",
        "src executes on host/platform dst",
        false,
        Some("hosts"),
    ),
    ("uses", "src makes use of dst", false, None),
    ("configures", "src sets configuration for dst", false, None),
    (
        "stores-data-in",
        "src persists its data in dst",
        false,
        None,
    ),
    (
        "implements",
        "src (a source file) realizes concept/role dst",
        false,
        None,
    ),
    ("related-to", "untyped catch-all association", false, None),
    (
        "prefers",
        "src (a task-type) prefers approach/tool dst (positive lesson)",
        false,
        None,
    ),
    (
        "avoids",
        "src (a task-type) should avoid dead-end dst (negative lesson)",
        false,
        None,
    ),
];

/// Why [`Storage::open`] failed, in the three classes `service.py`
/// (`_ensure_postgres_storage`, lines 1234-1251) treats differently.
#[derive(Debug, Clone, PartialEq)]
pub enum OpenError {
    /// `WriterLeaseHeld`: another session holds the bank's writer lease.
    /// The caller records it as `not_ready` and arms the backoff.
    LeaseHeld(String),
    /// A `RuntimeError` from the constructor (the embedding-dimension
    /// refusal). The caller records it as `init_refusal`, no backoff.
    Refused(String),
    /// Any other error (connect, SQL). Python records nothing.
    Failed(String),
}

impl std::fmt::Display for OpenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpenError::LeaseHeld(m) | OpenError::Refused(m) | OpenError::Failed(m) => {
                f.write_str(m)
            }
        }
    }
}

impl From<tokio_postgres::Error> for OpenError {
    fn from(e: tokio_postgres::Error) -> Self {
        OpenError::Failed(error_text(&e))
    }
}

/// An error's text: the server's message for a database error, else the
/// client's description. Free per spec (H8 `db` text, L3).
pub fn error_text(e: &tokio_postgres::Error) -> String {
    match e.as_db_error() {
        Some(db) => db.to_string(),
        None => e.to_string(),
    }
}

/// The open writer session plus what `ping` and `cached_bank_id` need.
pub struct Storage {
    client: Client,
    conn_task: JoinHandle<()>,
    /// The DSN as parsed, without the application-name fallback: Python's
    /// `ping` and `cached_bank_id` connections pass none.
    base: Config,
    lease_epoch: i64,
    /// `_lease_lost`: set only by Python's reconnect path when the lease was
    /// lost to another writer (postgres.py `conn`). This slice has no
    /// reconnect, so it stays `None`; kept so `ping` carries the check.
    lease_lost: Option<String>,
    bank_id: Mutex<BankIdCache>,
}

struct BankIdCache {
    id: Option<String>,
    /// `_bank_id_retry_at`; `None` is Python's initial 0.0 (always due).
    retry_at: Option<Instant>,
}

/// Parse a DSN in URL or `key=value` form.
pub fn parse_dsn(dsn: &str) -> Result<Config, String> {
    dsn.parse::<Config>()
        .map_err(|e| format!("invalid connection string: {e}"))
}

/// `_application_name()` (postgres.py:369-380): how this process appears in
/// `pg_stat_activity`. Python derives the program from argv; here it is the
/// daemon's own name (free: never compared).
pub fn application_name() -> String {
    truncate_bytes(
        &format!(
            "pseudolife-mcp pid={} pseudolife-daemon serve",
            std::process::id()
        ),
        63,
    )
}

fn truncate_bytes(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_string()
}

/// `_LOCAL_PORT_ERRORS` (postgres.py:63): WSAEADDRINUSE / WSAENOBUFS.
fn local_port_error(e: &tokio_postgres::Error) -> bool {
    if e.as_db_error().is_some() {
        return false;
    }
    let mut source: Option<&(dyn std::error::Error + 'static)> = std::error::Error::source(e);
    while let Some(s) = source {
        if let Some(io) = s.downcast_ref::<std::io::Error>()
            && matches!(io.raw_os_error(), Some(10048 | 10055))
        {
            return true;
        }
        source = s.source();
    }
    false
}

/// A jitter factor in [0.5, 1.5) without a random-number dependency.
fn jitter() -> f64 {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    0.5 + f64::from(nanos % 1_000_000) / 1_000_000.0
}

/// `connect_retrying_local_ports` (postgres.py:81-101) with libpq's
/// `connect_timeout` semantics: the timeout bounds the whole connection
/// attempt (socket and startup), not only the socket connect.
pub async fn connect(
    config: &Config,
    timeout: Duration,
) -> Result<(Client, JoinHandle<()>), String> {
    const ATTEMPTS: u32 = 4;
    let mut config = config.clone();
    config.connect_timeout(timeout);
    let mut attempt = 1;
    loop {
        let result = tokio::time::timeout(timeout, config.connect(NoTls)).await;
        match result {
            Err(_) => return Err("connection timeout expired".into()),
            Ok(Ok((client, connection))) => {
                let task = tokio::spawn(async move {
                    let _ = connection.await;
                });
                return Ok((client, task));
            }
            Ok(Err(e)) if attempt < ATTEMPTS && local_port_error(&e) => {
                let backoff = 0.05 * f64::from(1u32 << (attempt - 1)) * jitter();
                tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
                attempt += 1;
            }
            Ok(Err(e)) => return Err(error_text(&e)),
        }
    }
}

/// Python's `repr()` of a `str`, for the lease refusal's `{bank!r}`.
/// Exact for ASCII; for other characters it escapes the C1 controls and the
/// common non-printing separators and format characters (unassigned code
/// points, which Python also escapes, are passed through).
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if py_printable(c) => out.push(c),
            c => {
                let n = c as u32;
                if n <= 0xff {
                    out.push_str(&format!("\\x{n:02x}"));
                } else if n <= 0xffff {
                    out.push_str(&format!("\\u{n:04x}"));
                } else {
                    out.push_str(&format!("\\U{n:08x}"));
                }
            }
        }
    }
    out.push(quote);
    out
}

fn py_printable(c: char) -> bool {
    let n = c as u32;
    if n < 0x80 {
        return (0x20..0x7f).contains(&n);
    }
    !matches!(n,
        0x80..=0xa0 | 0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x1680
        | 0x180e | 0x2000..=0x200f | 0x2028..=0x202f | 0x205f..=0x2064
        | 0x2066..=0x206f | 0x3000 | 0xd800..=0xf8ff | 0xfeff | 0xfff9..=0xfffb
        | 0xe0001 | 0xe0020..=0xe007f | 0xf0000..=0x10ffff)
}

/// `_describe_lease_holder`'s message (postgres.py:584-601), from the
/// holders' `(pid, application_name)` rows and the bank name.
pub fn lease_holder_message(bank: &str, holders: &[(i32, Option<String>)]) -> String {
    let holder = if holders.is_empty() {
        "a session that released it a moment ago (retry)".to_string()
    } else {
        holders
            .iter()
            .map(|(pid, app)| {
                format!(
                    "backend pid {pid} (application_name {})",
                    py_repr(app.as_deref().unwrap_or(""))
                )
            })
            .collect::<Vec<_>>()
            .join(", ")
    };
    format!(
        "another process holds the writer lease on bank {}: \
         {holder}. A bank has exactly one writer, because two would \
         each save slots from their own resident copy and erase each \
         other's history. Stop that process first (for the Docker \
         daemon: `docker compose -f ops/docker-compose.yml stop \
         pseudolife-daemon`), or point this one at a different database.",
        py_repr(bank)
    )
}

/// `_lease_holders` (postgres.py:561-582).
async fn lease_holders(
    client: &Client,
) -> Result<Vec<(i32, Option<String>)>, tokio_postgres::Error> {
    let rows = client
        .query(
            "
            WITH k AS (SELECT hashtextextended($1, 0) AS key)
            SELECT a.pid, a.application_name
            FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            CROSS JOIN k
            WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
              AND l.database = (SELECT oid FROM pg_database
                                WHERE datname = current_database())
              AND l.classid = ((k.key >> 32) & 4294967295)::oid
              AND l.objid = (k.key & 4294967295)::oid
            ",
            &[&WRITER_LEASE_KEY],
        )
        .await?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
}

/// `_describe_lease_holder` (postgres.py:584-601).
async fn describe_lease_holder(client: &Client) -> Result<String, tokio_postgres::Error> {
    let holders = lease_holders(client).await?;
    let bank = simple_value(client, "SELECT current_database()")
        .await?
        .flatten()
        .unwrap_or_default();
    Ok(lease_holder_message(&bank, &holders))
}

/// `_connect` (postgres.py:603-624): autocommit (tokio-postgres never opens
/// an implicit transaction), `connect_timeout` 10, the application-name
/// fallback, then the two session settings.
async fn connect_session(base: &Config) -> Result<(Client, JoinHandle<()>), OpenError> {
    let mut config = base.clone();
    if config.get_application_name().is_none() {
        config.application_name(application_name());
    }
    let (client, task) = connect(&config, Duration::from_secs(10))
        .await
        .map_err(OpenError::Failed)?;
    let configure = async {
        client.batch_execute("SET lock_timeout = '5s'").await?;
        client.batch_execute("SET search_path TO public").await
    };
    if let Err(e) = configure.await {
        drop(client);
        let _ = task.await;
        return Err(e.into());
    }
    Ok((client, task))
}

/// `_acquire_writer_lease` (postgres.py:447-479).
async fn acquire_writer_lease(client: &Client) -> Result<(), OpenError> {
    for (name, value) in LEASE_KEEPALIVE {
        client
            .batch_execute(&format!("SET {name} = {value}"))
            .await?;
    }
    let previous = simple_value(client, "SHOW lock_timeout")
        .await?
        .flatten()
        .unwrap_or_default();
    client
        .execute(
            "SELECT set_config('lock_timeout', $1, false)",
            &[&LEASE_WAIT],
        )
        .await?;
    match client
        .execute(
            "SELECT pg_advisory_lock(hashtextextended($1, 0))",
            &[&WRITER_LEASE_KEY],
        )
        .await
    {
        Ok(_) => {}
        Err(e) if e.code() == Some(&SqlState::LOCK_NOT_AVAILABLE) => {
            return Err(OpenError::LeaseHeld(describe_lease_holder(client).await?));
        }
        Err(e) => return Err(e.into()),
    }
    client
        .execute("SELECT set_config('lock_timeout', $1, false)", &[&previous])
        .await?;
    Ok(())
}

/// `ensure_schema` (schema.py:1145-1664): one explicit transaction. A
/// failure rolls back before the session is dropped, as psycopg's
/// `conn.transaction()` does on the way out.
async fn ensure_schema(client: &Client) -> Result<(), OpenError> {
    client.batch_execute("BEGIN").await?;
    match schema::ensure_schema_in_transaction(client).await {
        Ok(()) => {
            client.batch_execute("COMMIT").await?;
            Ok(())
        }
        Err(e) => {
            let _ = client.batch_execute("ROLLBACK").await;
            Err(match e {
                SchemaError::Refused(m) => OpenError::Refused(m),
                SchemaError::Db(e) => e.into(),
            })
        }
    }
}

fn wall_clock() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `_seed_relations` (postgres.py:754-766): one transaction, one insert per
/// builtin, each stamped with the wall clock when it runs.
async fn seed_relations(client: &Client) -> Result<(), OpenError> {
    client.batch_execute("BEGIN").await?;
    let inserts = async {
        let stmt = "
                    INSERT INTO relations
                      (name, description, transitive, inverse_of, builtin,
                       created_at)
                    VALUES ($1, $2, $3, $4, TRUE, $5)
                    ON CONFLICT (name) DO NOTHING
                    ";
        for (name, desc, transitive, inverse) in BUILTIN_RELATIONS {
            let now = if crate::mutants::active("seed-clock-zero") {
                0.0
            } else {
                wall_clock()
            };
            client
                .execute(stmt, &[&name, &desc, &transitive, &inverse, &now])
                .await?;
        }
        client.batch_execute("COMMIT").await
    };
    if let Err(e) = inserts.await {
        let _ = client.batch_execute("ROLLBACK").await;
        return Err(e.into());
    }
    Ok(())
}

/// `_bump_lease_epoch` (postgres.py:482-498).
async fn bump_lease_epoch(client: &Client) -> Result<i64, OpenError> {
    let row = client
        .query_one(
            "
            INSERT INTO meta (key, value) VALUES ($1, '1'::jsonb)
            ON CONFLICT (key) DO UPDATE
              SET value = to_jsonb(COALESCE(
                CASE WHEN jsonb_typeof(meta.value) = 'number'
                     THEN (meta.value #>> '{}')::numeric::bigint END, 0) + 1)
            RETURNING (value #>> '{}')::bigint
            ",
            &[&LEASE_EPOCH_KEY],
        )
        .await?;
    Ok(row.get(0))
}

/// `_assert_public_search_path`'s rule (service.py:1047-1065) over a
/// `SHOW search_path` value: `Err` carries the Python `RuntimeError` text.
pub fn search_path_refusal(path: &str) -> Option<String> {
    let schemas: Vec<&str> = path
        .split(',')
        .map(|s| py_strip(s).trim_matches('"'))
        .collect();
    let Some(public) = schemas.iter().position(|s| *s == "public") else {
        return Some(format!(
            "search_path must include 'public' (got {}); the real bank \
             lives in public — refusing to run against a shadow schema.",
            py_repr(path)
        ));
    };
    if let Some(user) = schemas.iter().position(|s| *s == "$user")
        && user < public
    {
        return Some(format!(
            "search_path resolves $user (role 'pseudolife', which is also a \
             schema name) ahead of public (got {}) — this shadows the \
             real bank. Pin search_path to public first.",
            py_repr(path)
        ));
    }
    None
}

/// Python's `str.strip()` with no argument: Unicode whitespace plus the
/// four information separators U+001C-U+001F, which `str.isspace` counts.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

impl Storage {
    /// `PostgresStorage.__init__` (postgres.py:383-430), statement for
    /// statement: session (P1), writer lease (P2), `ensure_schema` (P3),
    /// relation seeds (P4), lease epoch (P5). `register_vector` only reads
    /// the `vector` type's oid and is not reproduced. On any failure after
    /// the connect the session is closed (releasing the lease) before the
    /// error returns.
    pub async fn open(dsn: &str) -> Result<Storage, OpenError> {
        let base = parse_dsn(dsn).map_err(OpenError::Failed)?;
        let (client, conn_task) = connect_session(&base).await?;
        let built = async {
            acquire_writer_lease(&client).await?;
            ensure_schema(&client).await?;
            if !crate::mutants::active("skip-relation-seed") {
                seed_relations(&client).await?;
            }
            if crate::mutants::active("skip-lease-epoch") {
                return Ok(0);
            }
            bump_lease_epoch(&client).await
        };
        match built.await {
            Ok(lease_epoch) => Ok(Storage {
                client,
                conn_task,
                base,
                lease_epoch,
                lease_lost: None,
                bank_id: Mutex::new(BankIdCache {
                    id: None,
                    retry_at: None,
                }),
            }),
            Err(e) => {
                // Close now, not at some later drop: the caller's retry
                // would otherwise refuse against this attempt's lease.
                drop(client);
                let _ = conn_task.await;
                Err(e)
            }
        }
    }

    /// `service.py` `_assert_public_search_path` (P7), on the writer session.
    ///
    /// On `Err` the caller closes this storage and records nothing: Python
    /// raises the `RuntimeError` after `_ensure_postgres_storage` has already
    /// cleared `_init_refusal` and `_not_ready` (service.py:1250-1265), so
    /// neither flag is set and no backoff is armed. (Spec P7 says
    /// `init_refusal`; the Python sets neither.) A failed `SHOW` returns its
    /// error text the same way.
    pub async fn check_search_path(&self) -> Result<(), String> {
        let path = simple_value(&self.client, "SHOW search_path")
            .await
            .map_err(|e| error_text(&e))?
            .flatten()
            .unwrap_or_default();
        match search_path_refusal(&path) {
            Some(msg) => Err(msg),
            None => Ok(()),
        }
    }

    /// `ping` (postgres.py:702-719, spec H8): a fresh connection (2 s
    /// connect timeout, no application-name fallback) runs `SELECT 1`. The
    /// lease-lost branch can only fire after a reconnect, which this slice
    /// does not have; `resident_invalidated` likewise (always unset).
    pub async fn ping(&self) -> Result<(), String> {
        let (client, task) = connect(&self.base, Duration::from_secs(2)).await?;
        let probe = async {
            client.batch_execute("SELECT 1").await?;
            if let Some(lost) = &self.lease_lost
                && !lease_holders(&client).await?.is_empty()
            {
                return Ok(Err(lost.clone()));
            }
            Ok::<_, tokio_postgres::Error>(Ok(()))
        };
        let result = probe.await;
        drop(client);
        let _ = task.await;
        match result {
            Ok(r) => r,
            Err(e) => Err(error_text(&e)),
        }
    }

    /// `cached_bank_id` (postgres.py:721-752, spec H9): the coordination
    /// bank id, read on a fresh connection at most once per 60 s (the
    /// window is armed before the attempt) until found, then never again.
    /// Never fails: any error is `None`.
    pub async fn cached_bank_id(&self) -> Option<String> {
        {
            let mut cache = self.bank_id.lock().unwrap();
            if cache.id.is_some() {
                return cache.id.clone();
            }
            let now = Instant::now();
            if matches!(cache.retry_at, Some(at) if now < at) {
                return None;
            }
            cache.retry_at = Some(now + BANK_ID_RETRY);
        }
        let read = async {
            let (client, task) = connect(&self.base, Duration::from_secs(2)).await.ok()?;
            let value = async {
                client.batch_execute("SET statement_timeout = '2s'").await?;
                client
                    .query_opt(
                        "SELECT value FROM public.meta WHERE key = $1",
                        &[&BANK_ID_META_KEY],
                    )
                    .await
            }
            .await;
            drop(client);
            let _ = task.await;
            value.ok()
        };
        let row = read.await?;
        let value: Option<serde_json::Value> = row.map(|r| r.get(0));
        let mut cache = self.bank_id.lock().unwrap();
        if let Some(serde_json::Value::String(id)) = value
            && !id.is_empty()
        {
            cache.id = Some(id);
        }
        cache.id.clone()
    }

    /// The lease epoch this session wrote (`_lease_epoch`).
    pub fn lease_epoch(&self) -> i64 {
        self.lease_epoch
    }

    /// The writer session, for later slices.
    pub fn client(&self) -> &Client {
        &self.client
    }

    /// `close()`: end the session and wait until the connection task has
    /// sent its terminate message, so the lease is released when this
    /// returns (the server may still take a moment to exit the backend).
    pub async fn close(self) {
        let Storage {
            client, conn_task, ..
        } = self;
        drop(client);
        let _ = conn_task.await;
    }
}

#[cfg(test)]
mod tests;
