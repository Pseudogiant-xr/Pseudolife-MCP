//! `perform_export`: one REPEATABLE READ READ ONLY snapshot streamed into
//! `<out>.part` as one JSONL member per exported table plus `manifest.json`.
use super::json::{self, Value};
use super::{EXCLUDED_TABLES, EXPORTED_TABLES, Outcome, canonical_path, skip_meta_key, sql};
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
/// Session zones whose offset is zero in every tz database, and the zone
/// psycopg falls back to when it cannot load one: the only sessions in which
/// PostgreSQL's rendered wall time is exactly what psycopg's loader returns.
const UTC_ZONES: [&str; 18] = [
    "UTC",
    "Etc/UTC",
    "UCT",
    "Etc/UCT",
    "Universal",
    "Etc/Universal",
    "Zulu",
    "Etc/Zulu",
    "GMT",
    "Etc/GMT",
    "GMT0",
    "Etc/GMT0",
    "GMT+0",
    "Etc/GMT+0",
    "GMT-0",
    "Etc/GMT-0",
    "Greenwich",
    "Etc/Greenwich",
];

pub(super) struct Destination {
    path: PathBuf,
    display: String,
}

/// The oracle's `args.out or Path.cwd() / "pseudolife-export-<ts>.zip"`, with
/// every precondition whose failure would be a Python traceback deferred.
pub(super) fn destination(out: Option<String>) -> Option<Destination> {
    let display = match out {
        Some(value) => value,
        None => {
            // MSVCRT's strftime honours TZ; the native local clock does not.
            if cfg!(windows) && std::env::var_os("TZ").is_some() {
                return None;
            }
            let cwd = std::env::current_dir()
                .ok()?
                .into_os_string()
                .into_string()
                .ok()?;
            let name = format!(
                "pseudolife-export-{}.zip",
                chrono::Local::now().format("%Y%m%d-%H%M%S")
            );
            let separator = if cfg!(windows) { '\\' } else { '/' };
            if cwd.ends_with(separator) {
                format!("{cwd}{name}")
            } else {
                format!("{cwd}{separator}{name}")
            }
        }
    };
    if !canonical_path(&display) {
        return None;
    }
    let path = PathBuf::from(&display);
    let partial = PathBuf::from(format!("{display}.part"));
    for target in [&path, &partial] {
        match fs::symlink_metadata(target) {
            Ok(meta) if meta.is_dir() => return None,
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return None,
        }
    }
    // mkdir(parents=True, exist_ok=True) fails on a non-directory ancestor.
    for ancestor in path.ancestors().skip(1) {
        if ancestor.as_os_str().is_empty() {
            break;
        }
        match fs::metadata(ancestor) {
            Ok(meta) if meta.is_dir() => break,
            Ok(_) => return None,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return None,
        }
    }
    Some(Destination { path, display })
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

enum Stop {
    /// Unported data: leave the filesystem exactly as it was.
    Defer,
    /// A server or filesystem failure: the oracle unlinks `.part` and keeps
    /// the directories it created.
    Fail,
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
        Kind::Timestamptz => json::write_str(out, &timestamptz(text, rendering)?, false),
    }
    Some(())
}

async fn tables(client: &Client) -> Result<Option<Vec<Table>>, ()> {
    let mut found = Vec::new();
    for name in EXPORTED_TABLES {
        let exists = sql::scalar(
            client,
            &format!("SELECT to_regclass('public.{name}') IS NOT NULL"),
        )
        .await?;
        if exists.as_deref() != Some("t") {
            return Ok(None);
        }
        let statement = client
            .prepare(&format!("SELECT * FROM {name} ORDER BY 1"))
            .await
            .map_err(|_| ())?;
        let mut columns = Vec::new();
        for column in statement.columns() {
            let Some(kind) = kind(column.type_()) else {
                return Ok(None);
            };
            columns.push((column.name().to_owned(), kind));
        }
        found.push(Table { name, columns });
    }
    Ok(Some(found))
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
        .map_err(|_| Stop::Fail)?;
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
                cell(&mut line, *kind, value.as_deref(), rendering).ok_or(Stop::Defer)?;
            }
            line.push_str("}\n");
            zip.write_all(line.as_bytes()).map_err(|_| Stop::Fail)?;
            count += 1;
        }
    }
    sql::execute(client, &format!("CLOSE {cursor}"))
        .await
        .map_err(|_| Stop::Fail)?;
    Ok(count)
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
    created: &mut Vec<PathBuf>,
) -> Result<Vec<(&'static str, u64)>, Stop> {
    // The oracle's session has no lock_timeout; the shared session sets one.
    sql::execute(client, "RESET lock_timeout")
        .await
        .map_err(|_| Stop::Defer)?;
    let setup = "BEGIN; \
        SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; \
        SET LOCAL extra_float_digits = 3";
    sql::execute(client, setup).await.map_err(|_| Stop::Defer)?;
    let zone = sql::scalar(client, "SHOW TimeZone").await;
    let style = sql::scalar(client, "SHOW DateStyle").await;
    let (Ok(Some(zone)), Ok(Some(style))) = (zone, style) else {
        return Err(Stop::Defer);
    };
    let rendering = Rendering {
        utc: UTC_ZONES.contains(&zone.as_str()),
        iso: style.starts_with("ISO"),
    };
    let tables = tables(client)
        .await
        .map_err(|_| Stop::Defer)?
        .ok_or(Stop::Defer)?;
    let version = sql::rows(
        client,
        "SELECT value FROM meta WHERE key = 'schema_version'",
    )
    .await
    .map_err(|_| Stop::Defer)?;
    let version = match version.first().and_then(|row| row.first()) {
        None => Value::Null,
        Some(None) => Value::Null,
        Some(Some(text)) => json::loads(text).ok_or(Stop::Defer)?,
    };

    // First filesystem effect: the oracle's out.parent.mkdir(parents=True).
    let mut missing: Vec<&Path> = Vec::new();
    for ancestor in destination.path.ancestors().skip(1) {
        if ancestor.as_os_str().is_empty() || ancestor.is_dir() {
            break;
        }
        missing.push(ancestor);
    }
    for directory in missing.into_iter().rev() {
        fs::create_dir(directory).map_err(|_| Stop::Defer)?;
        created.push(directory.to_path_buf());
    }
    let file = File::create(partial).map_err(|_| Stop::Fail)?;
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
        .map_err(|_| Stop::Fail)?;
    zip.write_all(json::dumps(&manifest, true, Some(2)).as_bytes())
        .map_err(|_| Stop::Fail)?;
    let mut writer = zip.finish().map_err(|_| Stop::Fail)?;
    writer.flush().map_err(|_| Stop::Fail)?;
    drop(writer);
    sql::execute(client, "COMMIT")
        .await
        .map_err(|_| Stop::Fail)?;
    Ok(counts)
}

pub(super) async fn run(dsn: &Dsn, destination: &Destination) -> Outcome {
    let Ok(session) = Session::open(dsn).await else {
        return Outcome::Deferred;
    };
    let partial = PathBuf::from(format!("{}.part", destination.display));
    let mut created = Vec::new();
    let result = snapshot(session.client(), destination, &partial, &mut created).await;
    let _ = session.close().await;
    let counts = match result {
        Ok(counts) => counts,
        Err(stop) => {
            let _ = fs::remove_file(&partial);
            if matches!(stop, Stop::Fail) {
                return Outcome::Failed;
            }
            for directory in created.iter().rev() {
                let _ = fs::remove_dir(directory);
            }
            return Outcome::Deferred;
        }
    };
    if fs::rename(&partial, &destination.path).is_err() {
        return Outcome::Failed;
    }
    let mut text = format!("exported to: {}\n", destination.display);
    for (table, count) in counts {
        if count > 0 {
            let _ = writeln!(text, "  {table}: {count}");
        }
    }
    Outcome::Done(text)
}
