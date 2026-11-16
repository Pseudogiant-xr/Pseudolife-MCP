//! `perform_export`: one REPEATABLE READ READ ONLY snapshot streamed into
//! `<out>.part` as one JSONL member per exported table plus `manifest.json`.
use super::json::{self, Value};
use super::{EXCLUDED_TABLES, EXPORTED_TABLES, Outcome, skip_meta_key, sql};
use crate::pg::{Client, Dsn, Session, types::Type};
use std::{
    fmt::Write as _,
    fs::{self, File},
    io::{BufWriter, Write},
    path::{Path, PathBuf},
};
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

/// Streamed in batches like the oracle's named cursor.
const FETCH_ROWS: usize = 1000;
pub(super) struct Destination {
    path: PathBuf,
    display: String,
}

/// Validate the destination without creating directories or touching files.
pub(super) fn destination(out: Option<String>) -> Result<Destination, String> {
    let path = match out {
        Some(value) => PathBuf::from(value),
        None => std::env::current_dir().map_err(|e|
            format!("cannot resolve the current directory: {e} — pass --out with an absolute archive path."))?
            .join(format!("pseudolife-export-{}.zip", chrono::Local::now().format("%Y%m%d-%H%M%S"))),
    };
    let display = path
        .to_str()
        .ok_or_else(|| {
            "--out path is not valid Unicode — choose a Unicode archive path.".to_owned()
        })?
        .to_owned();
    match fs::symlink_metadata(&path) {
        Ok(meta) if meta.is_dir() => {
            return Err(format!(
                "--out {display:?} is a directory — choose an archive filename."
            ));
        }
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            return Err(format!(
                "cannot inspect --out {display:?}: {e} — choose an accessible archive path."
            ));
        }
    }
    match fs::symlink_metadata(format!("{display}.part")) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Ok(_) => {
            return Err(format!(
                "--out temporary file {display}.part already exists — choose a different --out filename."
            ));
        }
        Err(e) => {
            return Err(format!(
                "cannot inspect --out temporary file {display}.part: {e} — choose an accessible --out path."
            ));
        }
    }
    for ancestor in path.ancestors().skip(1) {
        if ancestor.as_os_str().is_empty() {
            break;
        }
        match fs::metadata(ancestor) {
            Ok(meta) if meta.is_dir() => break,
            Ok(_) => {
                return Err(format!(
                    "--out parent {} is not a directory — choose an archive path beneath a directory.",
                    ancestor.display()
                ));
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => {
                return Err(format!(
                    "cannot inspect --out parent {}: {e} — choose an accessible archive path.",
                    ancestor.display()
                ));
            }
        }
    }
    Ok(Destination { path, display })
}

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    /// text, uuid and pgvector: psycopg returns the server's text unchanged.
    Text,
    Int,
    Float,
    Bool,
    Jsonb,
    Timestamptz,
}

fn kind(ty: &Type) -> Option<Kind> {
    Some(match *ty {
        Type::TEXT | Type::UUID => Kind::Text,
        Type::INT2 | Type::INT4 | Type::INT8 => Kind::Int,
        Type::FLOAT4 | Type::FLOAT8 => Kind::Float,
        Type::BOOL => Kind::Bool,
        Type::JSONB => Kind::Jsonb,
        Type::TIMESTAMPTZ => Kind::Timestamptz,
        _ if ty.name() == "vector" => Kind::Text,
        _ => return None,
    })
}

struct Table {
    name: &'static str,
    columns: Vec<(String, Kind)>,
}

/// Admission refusals precede filesystem changes.
enum Stop {
    Refuse(String),
    /// PostgreSQL failed during the snapshot.
    Fail,
    /// The filesystem.
    FailIo,
}

struct Rendering {
    utc: bool,
    iso: bool,
}

fn timestamptz(text: &str, rendering: &Rendering) -> Option<String> {
    if !rendering.utc || !rendering.iso {
        return None;
    }
    let body = text.strip_suffix("+00")?;
    let bytes = body.as_bytes();
    let shape = |at: usize, separator: u8| bytes.get(at) == Some(&separator);
    if bytes.len() < 19
        || !bytes[..4].iter().all(u8::is_ascii_digit)
        || &body[..4] == "0000"
        || !shape(4, b'-')
        || !shape(7, b'-')
        || !shape(10, b' ')
        || !shape(13, b':')
        || !shape(16, b':')
        || ![5, 6, 8, 9, 11, 12, 14, 15, 17, 18]
            .iter()
            .all(|&at| bytes[at].is_ascii_digit())
    {
        return None;
    }
    let mut out = format!("{}T{}", &body[..10], &body[11..19]);
    if bytes.len() > 19 {
        let fraction = body[19..].strip_prefix('.')?;
        if fraction.is_empty()
            || fraction.len() > 6
            || !fraction.bytes().all(|b| b.is_ascii_digit())
        {
            return None;
        }
        let micros = format!("{fraction:0<6}");
        if micros != "000000" {
            out.push('.');
            out.push_str(&micros);
        }
    }
    out.push_str("+00:00");
    Some(out)
}

/// One column value as `json.dumps` writes psycopg's decoded value.
fn cell(out: &mut String, kind: Kind, text: Option<&str>, rendering: &Rendering) -> Option<()> {
    let Some(text) = text else {
        out.push_str("null");
        return Some(());
    };
    match kind {
        Kind::Text => json::write_str(out, text, false),
        Kind::Int => {
            let digits = text.strip_prefix('-').unwrap_or(text);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            out.push_str(text);
        }
        Kind::Float => {
            let value = match text {
                "NaN" => f64::NAN,
                "Infinity" => f64::INFINITY,
                "-Infinity" => f64::NEG_INFINITY,
                _ => text.parse::<f64>().ok().filter(|v| v.is_finite())?,
            };
            out.push_str(&json::json_float(value));
        }
        Kind::Bool => out.push_str(match text {
            "t" => "true",
            "f" => "false",
            _ => return None,
        }),
        Kind::Jsonb => out.push_str(&json::dumps(&json::loads(text)?, false, None)),
        Kind::Timestamptz => json::write_str(
            out,
            &timestamptz(text, rendering).unwrap_or_else(|| text.to_owned()),
            false,
        ),
    }
    Some(())
}

/// `meta.schema_version` as this build's `ensure_schema` writes it.
const BUILD_SCHEMA: &str = "59";

async fn tables(client: &Client) -> Result<Vec<Table>, Stop> {
    // Every public table must be classified, as transfer_cli's roster test
    // demands of BENCH_RESET_TABLES; an unknown one would be dropped silently.
    let present = sql::rows(
        client,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    )
    .await
    .map_err(|error| Stop::Refuse(sql::refusal(error, "read the public table roster")))?;
    for row in &present {
        let Some(Some(name)) = row.first() else {
            return Err(query_refusal("read the public table roster"));
        };
        if !EXPORTED_TABLES.contains(&name.as_str()) && !EXCLUDED_TABLES.contains(&name.as_str()) {
            return Err(Stop::Refuse(format!(
                "public table {name:?} has no export policy in this build — use a build that supports this table before exporting."
            )));
        }
    }
    let mut found = Vec::new();
    for name in EXPORTED_TABLES {
        let exists = sql::scalar(
            client,
            &format!("SELECT to_regclass('public.{name}') IS NOT NULL"),
        )
        .await
        .map_err(|error| Stop::Refuse(sql::refusal(error, "inspect export tables")))?;
        if exists.as_deref() != Some("t") {
            return Err(Stop::Refuse(format!(
                "required export table {name} is missing — start the matching daemon to initialize the bank before exporting."
            )));
        }
        let statement = client
            .prepare(&format!("SELECT * FROM {name} LIMIT 0"))
            .await
            .map_err(|error| Stop::Refuse(sql::refusal(error, "inspect export columns")))?;
        client.query(&statement, &[]).await.map_err(|error| {
            Stop::Refuse(sql::refusal(error, &format!("read export table {name}")))
        })?;
        let mut columns = Vec::new();
        for column in statement.columns() {
            let Some(kind) = kind(column.type_()) else {
                return Err(Stop::Refuse(format!(
                    "{}.{} has unsupported export type {} — use a build that supports this column type.",
                    name,
                    column.name(),
                    column.type_().name()
                )));
            };
            columns.push((column.name().to_owned(), kind));
        }
        found.push(Table { name, columns });
    }
    Ok(found)
}

type Archive = ZipWriter<BufWriter<File>>;

async fn write_table(
    client: &Client,
    zip: &mut Archive,
    table: &Table,
    rendering: &Rendering,
) -> Result<u64, Stop> {
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .large_file(true);
    zip.start_file(format!("{}.jsonl", table.name), options)
        .map_err(|_| Stop::FailIo)?;
    let cursor = format!("pl_export_{}", table.name);
    sql::execute(
        client,
        &format!(
            "DECLARE {cursor} CURSOR FOR SELECT * FROM {} ORDER BY 1",
            table.name
        ),
    )
    .await
    .map_err(|_| Stop::Fail)?;
    let key_column = table.columns.iter().position(|(name, _)| name == "key");
    let dropped = (table.name == "outcome_signals")
        .then(|| {
            table
                .columns
                .iter()
                .position(|(name, _)| name == "used_ids")
        })
        .flatten();
    let mut count = 0;
    let mut line = String::new();
    loop {
        let rows = sql::rows(client, &format!("FETCH FORWARD {FETCH_ROWS} FROM {cursor}"))
            .await
            .map_err(|_| Stop::Fail)?;
        if rows.is_empty() {
            break;
        }
        for row in &rows {
            if row.len() != table.columns.len() {
                return Err(Stop::Fail);
            }
            if table.name == "meta"
                && let Some(Some(key)) = key_column.map(|index| &row[index])
                && skip_meta_key(key)
            {
                continue;
            }
            line.clear();
            line.push('{');
            let mut first = true;
            for (index, ((name, kind), value)) in table.columns.iter().zip(row).enumerate() {
                if Some(index) == dropped {
                    continue;
                }
                if !first {
                    line.push_str(", ");
                }
                first = false;
                json::write_str(&mut line, name, false);
                line.push_str(": ");
                cell(&mut line, *kind, value.as_deref(), rendering).ok_or(Stop::Fail)?;
            }
            line.push_str("}\n");
            zip.write_all(line.as_bytes()).map_err(|_| Stop::FailIo)?;
            count += 1;
        }
    }
    sql::execute(client, &format!("CLOSE {cursor}"))
        .await
        .map_err(|_| Stop::Fail)?;
    Ok(count)
}

fn quoted(identifier: &str) -> String {
    format!("\"{}\"", identifier.replace('"', "\"\""))
}

fn query_refusal(operation: &str) -> Stop {
    Stop::Refuse(format!(
        "PostgreSQL could not {operation} for PSEUDOLIFE_MCP_DATABASE_URL — restore database access and retry."
    ))
}

/// Check JSON limits in the same snapshot before creating the archive.
async fn admit_values(
    client: &Client,
    tables: &[Table],
    _rendering: &Rendering,
) -> Result<(), Stop> {
    for table in tables {
        let documents: Vec<_> = table
            .columns
            .iter()
            .filter(|(_, kind)| *kind == Kind::Jsonb)
            .map(|(name, _)| name)
            .collect();
        if documents.is_empty() {
            continue;
        }
        let cursor = format!("pl_admit_{}", table.name);
        sql::execute(
            client,
            &format!(
                "DECLARE {cursor} CURSOR FOR SELECT {} FROM {}",
                documents
                    .iter()
                    .map(|name| quoted(name))
                    .collect::<Vec<_>>()
                    .join(", "),
                table.name
            ),
        )
        .await
        .map_err(|error| Stop::Refuse(sql::refusal(error, "open the JSON validation cursor")))?;
        loop {
            let rows = sql::rows(client, &format!("FETCH FORWARD {FETCH_ROWS} FROM {cursor}"))
                .await
                .map_err(|error| Stop::Refuse(sql::refusal(error, "read JSON values")))?;
            if rows.is_empty() {
                break;
            }
            for row in rows {
                for (name, text) in documents.iter().zip(row) {
                    if let Some(text) = text {
                        json::read(&text).map_err(|reason| {
                            Stop::Refuse(format!(
                                "{}.{}: {reason} — simplify this JSON value before exporting.",
                                table.name, name
                            ))
                        })?;
                    }
                }
            }
        }
        sql::execute(client, &format!("CLOSE {cursor}"))
            .await
            .map_err(|error| {
                Stop::Refuse(sql::refusal(error, "close the JSON validation cursor"))
            })?;
    }
    Ok(())
}

fn created_at() -> String {
    let now = chrono::Utc::now();
    let mut text = now.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = now.timestamp_subsec_micros();
    if micros != 0 {
        let _ = write!(text, ".{micros:06}");
    }
    text.push_str("+00:00");
    text
}

async fn snapshot(
    client: &Client,
    destination: &Destination,
    partial: &Path,
) -> Result<Vec<(&'static str, u64)>, Stop> {
    let setup = "BEGIN; SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; \
        SET LOCAL extra_float_digits = 3; SET LOCAL TimeZone = 'UTC'; SET LOCAL DateStyle = 'ISO, YMD'";
    sql::execute(client, setup)
        .await
        .map_err(|error| Stop::Refuse(sql::refusal(error, "start a read-only snapshot")))?;
    let encoding = sql::scalar(client, "SHOW server_encoding")
        .await
        .map_err(|error| Stop::Refuse(sql::refusal(error, "read server_encoding")))?;
    if encoding.as_deref() != Some("UTF8") {
        return Err(Stop::Refuse(
            "server_encoding is not UTF8 — migrate this database to UTF8 before exporting.".into(),
        ));
    }
    let rendering = Rendering {
        utc: true,
        iso: true,
    };
    let tables = tables(client).await?;
    let version = sql::scalar(
        client,
        "SELECT value FROM meta WHERE key = 'schema_version'",
    )
    .await
    .map_err(|error| Stop::Refuse(sql::refusal(error, "read meta.schema_version")))?;
    if version.as_deref() != Some(BUILD_SCHEMA) {
        let stamp = version
            .as_deref()
            .and_then(|value| value.parse::<i64>().ok());
        let supported: i64 = BUILD_SCHEMA.parse().expect("build schema version");
        let (cause, fix) = match stamp {
            Some(stamp) if stamp > supported => (
                "newer than this client",
                "run pseudolife-mcp update and export with a release that supports the bank",
            ),
            Some(stamp) if stamp > 0 && stamp < supported => (
                "older than this client",
                "use the matching daemon release to upgrade the bank before exporting",
            ),
            _ => (
                "missing or not understood",
                "restore the bank from a backup with a valid schema stamp",
            ),
        };
        return Err(Stop::Refuse(format!(
            "meta.schema_version is {cause} (this client supports {BUILD_SCHEMA}) — {fix}."
        )));
    }
    let version = Value::Int(BUILD_SCHEMA.into());
    admit_values(client, &tables, &rendering).await?;

    // First filesystem effect: the oracle's out.parent.mkdir(parents=True).
    let mut missing: Vec<&Path> = Vec::new();
    for ancestor in destination.path.ancestors().skip(1) {
        if ancestor.as_os_str().is_empty() || ancestor.is_dir() {
            break;
        }
        missing.push(ancestor);
    }
    for directory in missing.into_iter().rev() {
        fs::create_dir(directory).map_err(|_| Stop::FailIo)?;
    }
    let file = File::create(partial).map_err(|_| Stop::FailIo)?;
    let mut zip = ZipWriter::new(BufWriter::new(file));
    let mut counts = Vec::new();
    for table in &tables {
        counts.push((
            table.name,
            write_table(client, &mut zip, table, &rendering).await?,
        ));
    }
    let dim = sql::scalar(
        client,
        "SELECT atttypmod FROM pg_attribute \
         WHERE attrelid = to_regclass('public.entries') \
         AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped",
    )
    .await
    .map_err(|_| Stop::Fail)?;
    let dim = match dim.map(|text| text.parse::<i64>()) {
        Some(Ok(value)) if value > 0 => Value::Int(value.to_string()),
        Some(Err(_)) => return Err(Stop::Fail),
        _ => Value::Null,
    };
    let manifest = Value::Object(vec![
        ("format_version".into(), Value::Int("1".into())),
        ("created_at".into(), Value::Str(created_at())),
        ("schema_version".into(), version),
        ("embedding_dim".into(), dim),
        (
            "pseudolife_version".into(),
            Value::Str(env!("CARGO_PKG_VERSION").into()),
        ),
        (
            "counts".into(),
            Value::Object(
                counts
                    .iter()
                    .map(|(name, count)| ((*name).to_owned(), Value::Int(count.to_string())))
                    .collect(),
            ),
        ),
        (
            "excluded_tables".into(),
            Value::Array(
                EXCLUDED_TABLES
                    .iter()
                    .map(|name| Value::Str((*name).into()))
                    .collect(),
            ),
        ),
    ]);
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    zip.start_file("manifest.json", options)
        .map_err(|_| Stop::FailIo)?;
    zip.write_all(json::dumps(&manifest, true, Some(2)).as_bytes())
        .map_err(|_| Stop::FailIo)?;
    let mut writer = zip.finish().map_err(|_| Stop::FailIo)?;
    writer.flush().map_err(|_| Stop::FailIo)?;
    drop(writer);
    sql::execute(client, "COMMIT")
        .await
        .map_err(|_| Stop::Fail)?;
    Ok(counts)
}

pub(super) async fn run(dsn: &Dsn, destination: &Destination) -> Outcome {
    let session = match Session::open(dsn).await {
        Ok(session) => session,
        Err(error) => return Outcome::Refused(super::database_refusal(error)),
    };
    let partial = PathBuf::from(format!("{}.part", destination.display));
    let result = snapshot(session.client(), destination, &partial).await;
    let _ = session.close().await;
    let counts = match result {
        Ok(counts) => counts,
        // Decided before any effect: nothing to undo.
        Err(Stop::Refuse(message)) => return Outcome::Refused(message),
        Err(stop) => {
            // destination() refused a pre-existing `.part`: this one is ours.
            let _ = fs::remove_file(&partial);
            return match stop {
                Stop::FailIo => Outcome::FailedIo,
                _ => Outcome::Failed,
            };
        }
    };
    // The oracle's replace failing leaves `.part` in place, as here.
    if fs::rename(&partial, &destination.path).is_err() {
        return Outcome::FailedIo;
    }
    let mut text = format!("exported to: {}\n", destination.display);
    for (table, count) in counts {
        if count > 0 {
            let _ = writeln!(text, "  {table}: {count}");
        }
    }
    Outcome::Done(text)
}
