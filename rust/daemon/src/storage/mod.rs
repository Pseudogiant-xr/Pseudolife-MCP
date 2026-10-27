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
/// `_LEASE_RETRY_SECONDS` (postgres.py:295): after a reconnect is refused
/// the lease, calls inside this window get the cached refusal instead of
/// each waiting out `LEASE_WAIT` under the service lock.
const LEASE_RETRY: Duration = Duration::from_secs(5);
/// `_RECONNECT_RETRY_SECONDS` (postgres.py:301): after a reconnect cannot
/// reach the server, calls inside this window skip another attempt. Not
/// measured in Python either: it bounds a burst to one attempt.
const RECONNECT_RETRY: Duration = Duration::from_secs(5);
/// `_SESSION_PROBE_INTERVAL` (postgres.py:310-320): how long a writer
/// session verified alive stays trusted. Python measured it 2026-09-23 on
/// the bench PostgreSQL (Windows host to Docker): the `SELECT 1` probe costs
/// 0.72 ms median, 1.8 ms p95; probing on every call cost +1.0 ms (+17%) on
/// `cortex_lookup`, so at most one probe per interval keeps a burst of cheap
/// calls cheap. Writes are not exposed: every reconnect re-checks the epoch.
const SESSION_PROBE_INTERVAL: Duration = Duration::from_secs(1);
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

/// Whether psycopg would raise `OperationalError` (or a subclass) for this
/// error, the class `verify_writer_session` tolerates: every client-side
/// failure (a closed connection, I/O, a timeout) and the server SQLSTATE
/// classes psycopg 3 maps to it (`psycopg.errors.lookup`, checked against
/// the installed psycopg).
pub fn operational(e: &tokio_postgres::Error) -> bool {
    let Some(code) = e.code() else { return true };
    matches!(
        code.code().get(..2),
        Some(
            "08" | "27"
                | "28"
                | "2F"
                | "38"
                | "39"
                | "3B"
                | "40"
                | "53"
                | "54"
                | "55"
                | "57"
                | "58"
                | "F0"
                | "HV"
        )
    )
}

/// Why a reconnect failed, in the classes `PostgresStorage.conn` and
/// `verify_writer_session` treat differently.
#[derive(Debug)]
enum Reopen {
    /// `WriterLeaseHeld`.
    LeaseHeld(String),
    /// `psycopg.OperationalError`: the server could not be reached.
    Unreachable(String),
    /// Any other error, which Python lets propagate to the caller.
    Failed(String),
}

impl From<tokio_postgres::Error> for Reopen {
    fn from(e: tokio_postgres::Error) -> Self {
        if operational(&e) {
            Reopen::Unreachable(error_text(&e))
        } else {
            Reopen::Failed(error_text(&e))
        }
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
    /// The reconnect bookkeeping `PostgresStorage` keeps beside its session.
    session: Mutex<SessionState>,
    bank_id: Mutex<BankIdCache>,
}

/// `PostgresStorage`'s session bookkeeping (postgres.py:424-437). A
/// reconnect builds a new [`Storage`]; these fields stay on the one whose
/// session died, which `/health` keeps probing until the replacement exists.
#[derive(Default)]
struct SessionState {
    /// `_lease_lost`: the refusal a reconnect got because another writer
    /// took the lease; `ping` reports it while that writer is still there.
    lease_lost: Option<String>,
    /// `_lease_retry_at`; `None` is Python's initial 0.0 (always due).
    lease_retry_at: Option<Instant>,
    /// `_reconnect_error`: the last reconnect's connection failure.
    reconnect_error: Option<String>,
    reconnect_retry_at: Option<Instant>,
    /// `_session_ok_at`; `None` is 0.0 (never verified).
    ok_at: Option<Instant>,
    /// `resident_invalidated`: set on the replacement session when another
    /// writer held the bank in the gap, until the owner re-reads the bank.
    resident_invalidated: Option<String>,
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

/// Python's `repr()` of a `str`: the quote choice, the backslash escapes and
/// the hex escapes for every code point `str.isprintable()` rejects (CPython
/// 3.11, Unicode 14: [`py_printable`]).
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

/// `str.isprintable()` for one code point, from CPython 3.11's Unicode 14
/// data (`py_repr_unicode14.rs`).
pub(crate) fn py_printable(c: char) -> bool {
    let n = c as u32;
    if n < 0x80 {
        return (0x20..0x7f).contains(&n);
    }
    let table = crate::py_repr_unicode14::NONPRINTABLE;
    let i = table.partition_point(|&(_, hi)| hi < n);
    table.get(i).is_none_or(|&(lo, _)| lo > n)
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
    match take_writer_lease(client).await {
        Ok(()) => Ok(()),
        Err(LeaseError::Held(m)) => Err(OpenError::LeaseHeld(m)),
        Err(LeaseError::Db(e)) => Err(e.into()),
    }
}

enum LeaseError {
    Held(String),
    Db(tokio_postgres::Error),
}

impl From<tokio_postgres::Error> for LeaseError {
    fn from(e: tokio_postgres::Error) -> Self {
        LeaseError::Db(e)
    }
}

async fn take_writer_lease(client: &Client) -> Result<(), LeaseError> {
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
            return Err(LeaseError::Held(describe_lease_holder(client).await?));
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
    Ok(bump_lease_epoch_raw(client).await?)
}

async fn bump_lease_epoch_raw(client: &Client) -> Result<i64, tokio_postgres::Error> {
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

/// `_read_lease_epoch` (postgres.py:523-530): `None` when the row is
/// missing or holds no number.
async fn read_lease_epoch(client: &Client) -> Result<Option<i64>, tokio_postgres::Error> {
    let row = client
        .query_opt(
            "SELECT CASE WHEN jsonb_typeof(value) = 'number' \
             THEN (value #>> '{}')::numeric::bigint END \
             FROM meta WHERE key = $1",
            &[&LEASE_EPOCH_KEY],
        )
        .await?;
    Ok(row.and_then(|r| r.get(0)))
}

/// Python's `str()` of an `int | None`.
fn py_epoch(epoch: Option<i64>) -> String {
    epoch.map_or_else(|| "None".into(), |e| e.to_string())
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
                session: Mutex::new(SessionState {
                    ok_at: Some(Instant::now()),
                    ..SessionState::default()
                }),
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

    /// `ping` (postgres.py:725-742, spec H8): a fresh connection (2 s
    /// connect timeout, no application-name fallback) runs `SELECT 1`; then
    /// it fails while a reconnect was refused the lease and a holder is
    /// still there, and while the owner has yet to re-read a bank another
    /// writer held in the meantime.
    pub async fn ping(&self) -> Result<(), String> {
        let (client, task) = connect(&self.base, Duration::from_secs(2)).await?;
        let probe = async {
            client.batch_execute("SELECT 1").await?;
            let lost = self.session.lock().unwrap().lease_lost.clone();
            if let Some(lost) = lost
                && !lease_holders(&client).await?.is_empty()
            {
                return Ok(Err(lost));
            }
            Ok::<_, tokio_postgres::Error>(Ok(()))
        };
        let result = probe.await;
        drop(client);
        let _ = task.await;
        match result {
            Ok(Ok(())) => match self.resident_invalidated() {
                // `BankChangedHands` until the owner re-reads the bank.
                Some(m) => Err(m),
                None => Ok(()),
            },
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

    /// `verify_writer_session` (postgres.py:533-568): round-trip on the
    /// writer session before its owner serves from a resident copy, at most
    /// once per [`SESSION_PROBE_INTERVAL`]. A dead session is replaced here
    /// (`Ok(Some(replacement))`), which runs the handover check, so the
    /// owner learns from [`Storage::resident_invalidated`] before it serves
    /// a copy another writer may have made stale. An unreachable server is
    /// tolerated (`Ok(None)`): nothing else can write a bank this process
    /// cannot reach either, and every storage call still fails on its own.
    /// `Err` only when another writer holds the lease, or when the
    /// reconnect hits an error Python does not catch here.
    ///
    /// Python skips the probe while a transaction pins its connection
    /// (`_pinned`); here that is [`crate::txn::try_hold`] failing, and its
    /// guard keeps any transaction from starting until this returns.
    pub async fn verify_writer_session(&self) -> Result<Option<Storage>, String> {
        let Some(_hold) = crate::txn::try_hold() else {
            return Ok(None);
        };
        if crate::mutants::active("writer-no-reconnect") {
            return Ok(None);
        }
        let closed = self.client.is_closed();
        let fresh = matches!(self.session.lock().unwrap().ok_at,
            Some(at) if at.elapsed() < SESSION_PROBE_INTERVAL);
        if !closed && (fresh || crate::txn::recovery_pending()) {
            return Ok(None);
        }
        if !closed {
            match self.client.batch_execute("SELECT 1").await {
                Ok(()) => {
                    self.session.lock().unwrap().ok_at = Some(Instant::now());
                    return Ok(None);
                }
                // `except psycopg.OperationalError`: with a reconnect
                // already failing, the server is unreachable; with the
                // session still open, `self.conn` keeps it (nothing to heal).
                Err(e) if operational(&e) => {
                    let failing = self.session.lock().unwrap().reconnect_error.is_some();
                    if failing || !self.client.is_closed() {
                        return Ok(None);
                    }
                }
                Err(e) => return Err(error_text(&e)),
            }
        }
        self.heal().await
    }

    /// The reconnect half of `PostgresStorage.conn` (postgres.py:675-722):
    /// the cached refusal or failure inside its retry window, else a new
    /// session that must win the lease back before it serves anything.
    async fn heal(&self) -> Result<Option<Storage>, String> {
        {
            let state = self.session.lock().unwrap();
            let now = Instant::now();
            if let (Some(lost), Some(at)) = (&state.lease_lost, state.lease_retry_at)
                && now < at
            {
                return Err(lost.clone());
            }
            if state.reconnect_error.is_some()
                && state.reconnect_retry_at.is_some_and(|at| now < at)
            {
                return Ok(None);
            }
        }
        eprintln!("postgres connection lost (closed=True broken=False); reconnecting");
        match self.reopen().await {
            Ok(fresh) => Ok(Some(fresh)),
            Err(Reopen::LeaseHeld(m)) => {
                let mut state = self.session.lock().unwrap();
                state.lease_lost = Some(m.clone());
                state.lease_retry_at = Some(Instant::now() + LEASE_RETRY);
                Err(m)
            }
            Err(Reopen::Unreachable(m)) => {
                let mut state = self.session.lock().unwrap();
                state.reconnect_error = Some(format!("reconnect failed: {m}"));
                state.reconnect_retry_at = Some(Instant::now() + RECONNECT_RETRY);
                Ok(None)
            }
            Err(Reopen::Failed(m)) => Err(m),
        }
    }

    /// `_open_session` plus the epoch check of `PostgresStorage.conn`: no
    /// `ensure_schema` (the schema exists) and no relation seeds. A
    /// different stored epoch means another writer held the bank in the
    /// gap: the replacement carries the `resident_invalidated` refusal and
    /// starts with no cached bank id. Closed again on any failure.
    async fn reopen(&self) -> Result<Storage, Reopen> {
        let (client, conn_task) = connect_session(&self.base).await.map_err(|e| match e {
            OpenError::LeaseHeld(m) => Reopen::LeaseHeld(m),
            // `_connect`'s failures are psycopg OperationalErrors (a refused
            // connect, a timeout, a dropped session during its two SETs).
            OpenError::Failed(m) | OpenError::Refused(m) => Reopen::Unreachable(m),
        })?;
        let built = async {
            match take_writer_lease(&client).await {
                Ok(()) => {}
                Err(LeaseError::Held(m)) => return Err(Reopen::LeaseHeld(m)),
                Err(LeaseError::Db(e)) => return Err(e.into()),
            }
            let found = read_lease_epoch(&client).await?;
            let epoch = bump_lease_epoch_raw(&client).await?;
            Ok::<_, Reopen>((found, epoch))
        };
        let (found, epoch) = match built.await {
            Ok(v) => v,
            Err(e) => {
                drop(client);
                let _ = conn_task.await;
                return Err(e);
            }
        };
        let moved =
            found != Some(self.lease_epoch) && !crate::mutants::active("writer-epoch-unchecked");
        let handed_over = moved.then(|| {
            format!(
                "another writer held this bank while this process was \
                 disconnected (lease epoch {} -> {}); its resident copy may \
                 be stale, so nothing is served or saved until it re-reads \
                 the bank",
                self.lease_epoch,
                py_epoch(found)
            )
        });
        if let Some(m) = &handed_over {
            eprintln!("{m}");
        }
        // Another writer may have restored a different bank here: the
        // cached bank id goes with the resident copy.
        let bank_id = if handed_over.is_some() {
            BankIdCache {
                id: None,
                retry_at: None,
            }
        } else {
            let cache = self.bank_id.lock().unwrap();
            BankIdCache {
                id: cache.id.clone(),
                retry_at: cache.retry_at,
            }
        };
        Ok(Storage {
            client,
            conn_task,
            base: self.base.clone(),
            lease_epoch: epoch,
            session: Mutex::new(SessionState {
                ok_at: Some(Instant::now()),
                resident_invalidated: handed_over,
                ..SessionState::default()
            }),
            bank_id: Mutex::new(bank_id),
        })
    }

    /// `resident_invalidated`.
    pub fn resident_invalidated(&self) -> Option<String> {
        self.session.lock().unwrap().resident_invalidated.clone()
    }

    /// `acknowledge_rehydration` (postgres.py:579-582).
    pub fn acknowledge_rehydration(&self) {
        self.session.lock().unwrap().resident_invalidated = None;
    }

    /// `_session_ok_at = 0.0`: the next verify probes even inside the
    /// interval (the board's clock retry, `coordination.dispatch`).
    pub fn forget_probe(&self) {
        self.session.lock().unwrap().ok_at = None;
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
