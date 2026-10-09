//! `perform_import` into a bank already at this build's schema, where the
//! oracle's `ensure_schema` changes nothing. A dry pass over the archive runs
//! before the transaction and defers every unported value; the transaction
//! pass then issues the oracle's statements in its order, so a refusal lands
//! at the same row with the same rolled-back work.
use super::json::{self, Value};
use super::{EXCLUDED_TABLES, EXPORTED_TABLES, Outcome, skip_meta_key, sql};
use crate::pg::{Client, Dsn, Session};
use std::{
    collections::HashSet,
    fmt::Write as _,
    fs::File,
    io::{BufReader, Read},
    path::Path,
};
use zip::{CompressionMethod, ZipArchive};

/// `SCHEMA_META_VERSION` at the pinned oracle, as meta's jsonb text.
const CURRENT_SCHEMA: &str = "55";
/// `schema._EXPECTED_EMBEDDING_DIM`: ensure_schema refuses any other live dim.
const EXPECTED_DIM: i64 = 1024;
/// `transfer_cli._BATCH_ROWS`: rows grouped per executemany flush.
const BATCH_ROWS: usize = 500;
/// Statements sent per simple-protocol message; one transaction either way.
const STATEMENTS_PER_MESSAGE: usize = 100;
const GUARD: &str = "SELECT count(*) FROM pg_stat_activity \
    WHERE datname = current_database() \
    AND pid <> pg_backend_pid() AND backend_type = 'client backend'";
const BACKFILL: &str = "INSERT INTO memory_trace_invalidations \
    (entity_norm, attribute_norm, source_entry_id, invalidated_at, cause) \
    SELECT t.entity_norm, t.attribute_norm, t.entry_id, \
    e.superseded_at, 'source_superseded' \
    FROM memory_traces t JOIN entries e ON e.id = t.entry_id \
    WHERE e.superseded_at IS NOT NULL \
    ON CONFLICT (entity_norm, attribute_norm, source_entry_id) DO NOTHING";
const ENTRY_HIGHWATER: &str = "SELECT GREATEST(\
    COALESCE((SELECT MAX(id) FROM entries), 0), \
    COALESCE((SELECT MAX(source_entry_id) FROM memory_trace_invalidations), 0), \
    COALESCE((SELECT MAX(entry_id) FROM entry_reinstatement_decisions), 0))";

enum Stop {
    Defer,
    Refuse(String),
    Fail,
}

/// One target column: the oracle's `information_schema` udt_name (which
/// selects its placeholder) and the full type a value is checked against.
pub(super) struct Column {
    name: String,
    udt: String,
    full: String,
}

/// The dry pass checks values against the server's input functions and
/// writes nothing; the real pass writes and checks nothing.
#[derive(Clone, Copy)]
enum Pass<'a> {
    Dry(&'a Client),
    Real(&'a Client),
}

/// The tables ensure_schema's v11 block backfills on every call (tx_time,
/// valid_time from asserted_at, writer_id 'legacy'; schema.py:361-376).
const STAMPED_TABLES: [&str; 4] = ["facts", "world_facts", "lessons", "edges"];

struct Archive {
    zip: ZipArchive<BufReader<File>>,
    names: HashSet<String>,
}

impl Archive {
    fn open(path: &Path) -> Option<Self> {
        let mut zip = ZipArchive::new(BufReader::new(File::open(path).ok()?)).ok()?;
        let mut names = HashSet::new();
        for index in 0..zip.len() {
            let member = zip.by_index_raw(index).ok()?;
            if member.encrypted()
                || !matches!(
                    member.compression(),
                    CompressionMethod::Stored | CompressionMethod::Deflated
                )
                || !names.insert(member.name().to_owned())
            {
                return None;
            }
        }
        Some(Self { zip, names })
    }

    fn read(&mut self, name: &str) -> Option<Vec<u8>> {
        let mut member = self.zip.by_name(name).ok()?;
        let mut bytes = Vec::new();
        member.read_to_end(&mut bytes).ok()?;
        Some(bytes)
    }

    /// A member as `TextIOWrapper(encoding="utf-8", newline="\n")` lines.
    fn text(&mut self, name: &str) -> Option<String> {
        String::from_utf8(self.read(name)?).ok()
    }
}

fn list_repr(names: &[&String]) -> String {
    let items: Vec<String> = names
        .iter()
        .map(|name| crate::cli::mode_repr(name))
        .collect();
    format!("[{}]", items.join(", "))
}

/// `export format {manifest.get('format_version')!r}` unless it equals 1.
fn format_refusal(value: Option<&Value>) -> Result<Option<String>, Stop> {
    let spelled = match value {
        None | Some(Value::Null) => "None".to_owned(),
        Some(Value::Int(digits)) if digits == "1" => return Ok(None),
        Some(Value::Int(digits)) => digits.clone(),
        Some(Value::Bool(true)) => return Ok(None),
        Some(Value::Bool(false)) => "False".to_owned(),
        Some(Value::Float(value)) if *value == 1.0 => return Ok(None),
        Some(Value::Float(value)) => json::float_repr(*value),
        Some(Value::Str(text)) => crate::cli::mode_repr(text),
        Some(_) => return Err(Stop::Defer),
    };
    Ok(Some(format!(
        "export format {spelled} is not the format 1 this build reads — upgrade pseudolife-mcp and retry."
    )))
}

/// `live and exported and live != exported`, with `str(exported)`.
fn dim_refusal(value: Option<&Value>, live: Option<i64>) -> Result<Option<String>, Stop> {
    let (spelled, equal) = match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => return Ok(None),
        Some(Value::Int(digits)) if digits == "0" => return Ok(None),
        Some(Value::Int(digits)) => (
            digits.clone(),
            live.is_some_and(|live| digits.parse::<i64>() == Ok(live)),
        ),
        Some(Value::Bool(true)) => ("True".to_owned(), live == Some(1)),
        Some(Value::Float(value)) if *value == 0.0 => return Ok(None),
        Some(Value::Float(value)) => (
            json::float_repr(*value),
            live.is_some_and(|live| live as f64 == *value),
        ),
        Some(Value::Str(text)) if text.is_empty() => return Ok(None),
        Some(Value::Str(text)) => (text.clone(), false),
        Some(_) => return Err(Stop::Defer),
    };
    Ok(match live {
        Some(live) if !equal => Some(format!(
            "export embeddings are {spelled}-dimensional but this bank's columns are vector({live}) — the vectors cannot load verbatim. Import on a matching build, or re-embed via the migration path (ops/migrate_embeddings.py) after importing there."
        )),
        _ => None,
    })
}

/// `int(manifest.get("schema_version") or 0) < 38`.
fn legacy_entries(value: Option<&Value>) -> Result<bool, Stop> {
    Ok(match value {
        None | Some(Value::Null | Value::Bool(_)) => true,
        Some(Value::Int(digits)) => match digits.parse::<i128>() {
            Ok(number) => number < 38,
            Err(_) => digits.starts_with('-'),
        },
        Some(Value::Float(value)) if value.is_finite() => value.trunc() < 38.0,
        Some(Value::Str(text)) if text.is_empty() => true,
        Some(Value::Array(items)) if items.is_empty() => true,
        Some(Value::Object(members)) if members.is_empty() => true,
        Some(_) => return Err(Stop::Defer),
    })
}

fn manifest(archive: &mut Archive, display: &str) -> Result<Value, Stop> {
    if !archive.names.contains("manifest.json") {
        return Err(Stop::Refuse(format!(
            "{display} carries no manifest.json — not a pseudolife-mcp export."
        )));
    }
    let bytes = archive.read("manifest.json").ok_or(Stop::Defer)?;
    // json.loads(bytes) sniffs BOMs and UTF-16/32; only plain UTF-8 is ported.
    if bytes.starts_with(&[0xef, 0xbb, 0xbf])
        || bytes.starts_with(&[0xff, 0xfe])
        || bytes.starts_with(&[0xfe, 0xff])
        || bytes.iter().take(4).any(|byte| *byte == 0)
    {
        return Err(Stop::Defer);
    }
    let text = String::from_utf8(bytes).map_err(|_| Stop::Defer)?;
    let value = json::loads(&text).ok_or(Stop::Defer)?;
    if !matches!(value, Value::Object(_)) {
        return Err(Stop::Defer);
    }
    Ok(value)
}

fn int_type(digits: &str) -> &'static str {
    match digits.parse::<i128>() {
        Ok(n) if (-(1 << 15)..(1 << 15)).contains(&n) => "int2",
        Ok(n) if (-(1 << 31)..(1 << 31)).contains(&n) => "int4",
        Ok(n) if (-(1 << 63)..(1 << 63)).contains(&n) => "int8",
        _ => "numeric",
    }
}

fn int_fits(digits: &str, udt: &str) -> bool {
    let Ok(n) = digits.parse::<i128>() else {
        return false;
    };
    match udt {
        "int2" => i16::try_from(n).is_ok(),
        "int4" => i32::try_from(n).is_ok(),
        "int8" => i64::try_from(n).is_ok(),
        _ => false,
    }
}

fn float8(value: f64) -> String {
    if value.is_nan() {
        "'NaN'::float8".into()
    } else if value.is_infinite() {
        if value < 0.0 {
            "'-Infinity'::float8".into()
        } else {
            "'Infinity'::float8".into()
        }
    } else {
        format!("'{}'::float8", json::float_repr(value))
    }
}

/// float8 -> float4 refuses overflow and underflow (`float8_to_float4`).
fn float4_loads(value: f64) -> bool {
    let narrowed = value as f32;
    !(narrowed.is_infinite() && value.is_finite() || narrowed == 0.0 && value != 0.0)
}

/// The SQL psycopg sends for one value: str is untyped, int is int2/int4/int8/
/// numeric by range, float is float8, bool is bool, and jsonb columns carry
/// `json.dumps(value)`. Values the oracle's export never writes for the
/// column's type, or that would fail inside the transaction, are `None`.
fn literal(value: &Value, udt: &str) -> Option<String> {
    let literal = match value {
        Value::Null => "NULL".to_owned(),
        _ if udt == "jsonb" => {
            if json::jsonb_unloadable(value) {
                return None;
            }
            sql::text(&json::dumps(value, true, None))
        }
        // The export writes strings only for these types; the server's input
        // function is checked in the dry pass for the last three.
        Value::Str(text)
            if !text.contains('\0')
                && matches!(udt, "text" | "uuid" | "vector" | "timestamptz") =>
        {
            sql::text(text)
        }
        Value::Int(digits) => {
            let loads = match udt {
                "int2" | "int4" | "int8" => int_fits(digits, udt),
                "float8" => digits.parse::<f64>().is_ok_and(f64::is_finite),
                "float4" => digits
                    .parse::<f64>()
                    .is_ok_and(|v| v.is_finite() && float4_loads(v)),
                _ => false,
            };
            if !loads {
                return None;
            }
            format!("'{digits}'::{}", int_type(digits))
        }
        Value::Float(value) if udt == "float8" || (udt == "float4" && float4_loads(*value)) => {
            float8(*value)
        }
        Value::Bool(value) if udt == "bool" => value.to_string(),
        _ => return None,
    };
    Some(match udt {
        "vector" | "jsonb" | "timestamptz" => format!("{literal}::{udt}"),
        _ => literal,
    })
}

async fn send(pass: Pass<'_>, statements: &[String]) -> Result<u64, Stop> {
    let Pass::Real(client) = pass else {
        return Ok(0);
    };
    let mut total = 0;
    for chunk in statements.chunks(STATEMENTS_PER_MESSAGE) {
        total += sql::affected(client, &chunk.join("; "))
            .await
            .map_err(|_| Stop::Fail)?;
    }
    Ok(total)
}

fn lines(text: &str) -> impl Iterator<Item = &str> {
    text.split('\n').filter(|line| !line.is_empty())
}

fn record(line: &str) -> Result<Vec<(String, Value)>, Stop> {
    match json::loads(line) {
        Some(Value::Object(members)) => Ok(members),
        _ => Err(Stop::Defer),
    }
}

async fn import_meta(pass: Pass<'_>, text: &str) -> Result<u64, Stop> {
    let mut count = 0;
    for line in lines(text) {
        let rec = record(line)?;
        let mut unknown: Vec<&String> = rec
            .iter()
            .map(|(key, _)| key)
            .filter(|key| *key != "key" && *key != "value")
            .collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(Stop::Refuse(format!(
                "meta rows in the export carry column(s) {} this build does not know — the export came from a newer pseudolife-mcp; upgrade this install and retry.",
                list_repr(&unknown)
            )));
        }
        let field = |name: &str| rec.iter().find(|(key, _)| key == name).map(|(_, v)| v);
        let Some(Value::Str(key)) = field("key") else {
            return Err(Stop::Defer);
        };
        if skip_meta_key(key) {
            continue;
        }
        let value = field("value").ok_or(Stop::Defer)?;
        if key.contains('\0') || json::jsonb_unloadable(value) {
            return Err(Stop::Defer);
        }
        let statement = format!(
            "INSERT INTO meta (key, value) VALUES ({}, {}::jsonb) \
             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            sql::text(key),
            sql::text(&json::dumps(value, true, None))
        );
        send(pass, &[statement]).await?;
        count += 1;
    }
    Ok(count)
}

/// Dry pass: every string headed for an input function that can refuse it
/// (`vector(n)`, uuid, timestamptz in this session) is cast by the server
/// first, so the transaction never meets a value error.
async fn check(pass: Pass<'_>, checks: &mut Vec<String>) -> Result<(), Stop> {
    if let Pass::Dry(client) = pass
        && !checks.is_empty()
    {
        for chunk in checks.chunks(STATEMENTS_PER_MESSAGE) {
            sql::execute(client, &chunk.join("; "))
                .await
                .map_err(|_| Stop::Defer)?;
        }
    }
    checks.clear();
    Ok(())
}

async fn import_table(
    pass: Pass<'_>,
    table: &str,
    columns: &[Column],
    text: &str,
    legacy: bool,
) -> Result<u64, Stop> {
    let on_conflict = if table == "relations" {
        " ON CONFLICT (name) DO NOTHING"
    } else {
        ""
    };
    let column = |name: &str| columns.iter().find(|column| column.name == name);
    let udt = |name: &str| column(name).map(|column| column.udt.as_str());
    let mut checks = Vec::new();
    let mut groups: Vec<(Vec<String>, Vec<String>)> = Vec::new();
    let mut pending = 0;
    let mut inserted = 0;
    let mut inverses = Vec::new();
    async fn flush(
        pass: Pass<'_>,
        table: &str,
        on_conflict: &str,
        groups: &mut Vec<(Vec<String>, Vec<String>)>,
    ) -> Result<u64, Stop> {
        let mut total = 0;
        for (columns, tuples) in groups.drain(..) {
            let statements: Vec<String> = tuples
                .iter()
                .map(|tuple| {
                    format!(
                        "INSERT INTO {table} ({}) VALUES ({tuple}){on_conflict}",
                        columns.join(", ")
                    )
                })
                .collect();
            total += send(pass, &statements).await?;
        }
        Ok(total)
    }
    for line in lines(text) {
        let mut rec = record(line)?;
        if table == "entries" && legacy {
            match rec.iter_mut().find(|(key, _)| key == "dream_state") {
                Some((_, value)) => *value = Value::Null,
                None => rec.push(("dream_state".into(), Value::Null)),
            }
        }
        let mut unknown: Vec<&String> = rec
            .iter()
            .map(|(key, _)| key)
            .filter(|key| udt(key).is_none())
            .collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(Stop::Refuse(format!(
                "table {} in the export carries column(s) {} this build does not know — the export came from a newer pseudolife-mcp; upgrade this install and retry (importing here would silently drop that data).",
                crate::cli::mode_repr(table),
                list_repr(&unknown)
            )));
        }
        if table == "relations"
            && let Some((_, inverse)) = rec.iter_mut().find(|(key, _)| key == "inverse_of")
            && *inverse != Value::Null
        {
            let Value::Str(target) = std::mem::replace(inverse, Value::Null) else {
                return Err(Stop::Defer);
            };
            let Some((_, Value::Str(name))) = rec.iter().find(|(key, _)| key == "name") else {
                return Err(Stop::Defer);
            };
            if target.contains('\0') || name.contains('\0') {
                return Err(Stop::Defer);
            }
            inverses.push(format!(
                "UPDATE relations SET inverse_of = {} WHERE name = {}",
                sql::text(&target),
                sql::text(name)
            ));
        }
        if rec.is_empty() {
            return Err(Stop::Defer);
        }
        rec.sort_by(|a, b| a.0.cmp(&b.0));
        let mut values = Vec::with_capacity(rec.len());
        for (key, value) in &rec {
            let target = column(key).ok_or(Stop::Defer)?;
            values.push(literal(value, &target.udt).ok_or(Stop::Defer)?);
            if let Value::Str(text) = value
                && matches!(target.udt.as_str(), "uuid" | "vector" | "timestamptz")
            {
                checks.push(format!(
                    "SELECT CAST({} AS {})",
                    sql::text(text),
                    target.full
                ));
                if checks.len() >= STATEMENTS_PER_MESSAGE {
                    check(pass, &mut checks).await?;
                }
            }
        }
        let keys: Vec<String> = rec.into_iter().map(|(key, _)| key).collect();
        let tuple = values.join(", ");
        match groups.iter_mut().find(|(columns, _)| *columns == keys) {
            Some((_, tuples)) => tuples.push(tuple),
            None => groups.push((keys, vec![tuple])),
        }
        pending += 1;
        if pending >= BATCH_ROWS {
            inserted += flush(pass, table, on_conflict, &mut groups).await?;
            pending = 0;
        }
    }
    check(pass, &mut checks).await?;
    inserted += flush(pass, table, on_conflict, &mut groups).await?;
    send(pass, &inverses).await?;
    Ok(inserted)
}

/// The per-table loop of `perform_import`, dry or for real.
async fn process(
    pass: Pass<'_>,
    archive: &mut Archive,
    columns: &[Vec<Column>],
    legacy: bool,
    counts: &mut Vec<(&'static str, u64)>,
) -> Result<(), Stop> {
    for (table, columns) in EXPORTED_TABLES.iter().zip(columns) {
        let member = format!("{table}.jsonl");
        if !archive.names.contains(&member) {
            continue;
        }
        let text = archive.text(&member).ok_or(Stop::Defer)?;
        let count = if *table == "meta" {
            import_meta(pass, &text).await?
        } else {
            import_table(pass, table, columns, &text, legacy && *table == "entries").await?
        };
        counts.push((table, count));
    }
    Ok(())
}

async fn transaction(
    client: &Client,
    archive: &mut Archive,
    columns: &[Vec<Column>],
    legacy: bool,
) -> Result<Vec<(&'static str, u64)>, Stop> {
    let fail = |_| Stop::Fail;
    sql::execute(
        client,
        &format!(
            "BEGIN; SET LOCAL lock_timeout = '5s'; LOCK TABLE {} IN EXCLUSIVE MODE",
            EXPORTED_TABLES.join(", ")
        ),
    )
    .await
    .map_err(fail)?;
    let mut held = Vec::new();
    for table in EXPORTED_TABLES {
        if matches!(table, "meta" | "relations") {
            continue;
        }
        let count = sql::scalar(client, &format!("SELECT count(*) FROM {table}"))
            .await
            .map_err(fail)?
            .ok_or(Stop::Fail)?;
        if count != "0" {
            held.push(format!("{table}={count}"));
        }
    }
    if !held.is_empty() {
        return Err(Stop::Refuse(format!(
            "target bank is not empty ({}) — import only fills a fresh bank. Point PSEUDOLIFE_MCP_DATABASE_URL at a new database (or start a fresh lite data dir) and retry.",
            held.join(", ")
        )));
    }
    sql::execute(
        client,
        "DELETE FROM meta WHERE key = 'curation_listing_spelling_v2'",
    )
    .await
    .map_err(fail)?;
    let mut counts = Vec::new();
    process(Pass::Real(client), archive, columns, legacy, &mut counts)
        .await
        .map_err(|stop| match stop {
            Stop::Defer => Stop::Fail,
            other => other,
        })?;
    if !archive.names.contains("memory_trace_invalidations.jsonl") {
        let count = sql::affected(client, BACKFILL).await.map_err(fail)?;
        counts.push(("memory_trace_invalidations", count));
    }
    for (table, columns) in EXPORTED_TABLES.iter().zip(columns) {
        if !columns.iter().any(|column| column.name == "id") {
            continue;
        }
        let sequence = sql::scalar(
            client,
            &format!("SELECT pg_get_serial_sequence('{table}', 'id')"),
        )
        .await
        .map_err(fail)?;
        let Some(sequence) = sequence else { continue };
        let statement = if *table == "entries" {
            let highwater = sql::scalar(client, ENTRY_HIGHWATER)
                .await
                .map_err(fail)?
                .ok_or(Stop::Fail)?;
            if highwater == "0" {
                continue;
            }
            format!(
                "SELECT setval({}, '{highwater}'::{}, true)",
                sql::text(&sequence),
                int_type(&highwater)
            )
        } else {
            format!(
                "SELECT setval({}, (SELECT MAX(id) FROM {table}), true) \
                 WHERE EXISTS (SELECT 1 FROM {table})",
                sql::text(&sequence)
            )
        };
        sql::execute(client, &statement).await.map_err(fail)?;
    }
    sql::execute(client, "COMMIT").await.map_err(fail)?;
    Ok(counts)
}

/// Everything before the transaction: guard, schema scope, the lock probe
/// standing in for ensure_schema's DDL locks, the dimension check and the
/// dry pass. `Ok(None)` is the named schema deferral.
async fn admit(
    client: &Client,
    archive: &mut Archive,
    manifest: &Value,
    force: bool,
    legacy: bool,
) -> Result<Option<Vec<Vec<Column>>>, Stop> {
    let defer = |_| Stop::Defer;
    sql::execute(client, "RESET lock_timeout")
        .await
        .map_err(defer)?;
    let others = sql::scalar(client, GUARD)
        .await
        .map_err(defer)?
        .ok_or(Stop::Defer)?;
    if others != "0" && !force {
        return Err(Stop::Refuse(format!(
            "{others} other connection(s) hold this database — a running daemon? Stop it first (Docker tier: `docker compose -f ops/docker-compose.yml stop pseudolife-daemon`; pip tiers: stop the serve process), then retry. Pass --force only if you know the connections are inert."
        )));
    }
    for table in EXPORTED_TABLES.iter().chain(EXCLUDED_TABLES.iter()) {
        let exists = sql::scalar(
            client,
            &format!("SELECT to_regclass('public.{table}') IS NOT NULL"),
        )
        .await
        .map_err(defer)?;
        if exists.as_deref() != Some("t") {
            return Ok(None);
        }
    }
    let version = sql::scalar(
        client,
        "SELECT value::text FROM meta WHERE key = 'schema_version'",
    )
    .await
    .map_err(defer)?;
    if version.as_deref() != Some(CURRENT_SCHEMA) {
        return Ok(None);
    }
    // psycopg decodes in the server's encoding; the native client is UTF-8.
    let encoding = sql::scalar(client, "SHOW server_encoding")
        .await
        .map_err(defer)?;
    if encoding.as_deref() != Some("UTF8") {
        return Err(Stop::Defer);
    }
    // ensure_schema's ALTER/CREATE INDEX need table ownership and its CREATE
    // TABLE IF NOT EXISTS needs CREATE on public; without them the oracle
    // fails before importing.
    let owner = sql::scalar(
        client,
        "SELECT coalesce(bool_and(pg_has_role(c.relowner, 'USAGE')), true) \
         AND has_schema_privilege('public', 'CREATE') \
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')",
    )
    .await
    .map_err(defer)?;
    if owner.as_deref() != Some("t") {
        return Err(Stop::Defer);
    }
    // ensure_schema commits its stamp backfill before the dimension and
    // emptiness checks, so a bank holding NULL stamps would change.
    let stamps = STAMPED_TABLES
        .iter()
        .map(|table| {
            format!(
                "EXISTS (SELECT 1 FROM {table} WHERE tx_time IS NULL \
                 OR valid_time IS NULL OR writer_id IS NULL)"
            )
        })
        .collect::<Vec<_>>()
        .join(" OR ");
    let unstamped = sql::scalar(client, &format!("SELECT {stamps}"))
        .await
        .map_err(defer)?;
    if unstamped.as_deref() != Some("f") {
        return Err(Stop::Defer);
    }
    let live = sql::scalar(
        client,
        "SELECT atttypmod FROM pg_attribute \
         WHERE attrelid = to_regclass('public.entries') \
         AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped",
    )
    .await
    .map_err(defer)?
    .map(|text| text.parse::<i64>())
    .transpose()
    .map_err(|_| Stop::Defer)?;
    if live.is_some_and(|live| live > 0 && live != EXPECTED_DIM) {
        return Err(Stop::Defer);
    }
    // ensure_schema's DDL waits up to 5 s for every table it alters; a holder
    // that would make it fail makes this probe fail first.
    let tables = sql::scalar(
        client,
        "SELECT string_agg(quote_ident(tablename), ', ' ORDER BY tablename) \
         FROM pg_tables WHERE schemaname = 'public'",
    )
    .await
    .map_err(defer)?
    .ok_or(Stop::Defer)?;
    let probe = format!(
        "BEGIN; SET LOCAL lock_timeout = '5s'; LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE"
    );
    let locked = sql::execute(client, &probe).await;
    sql::execute(client, "ROLLBACK").await.map_err(defer)?;
    locked.map_err(defer)?;
    if let Some(message) =
        dim_refusal(manifest.get("embedding_dim"), live.filter(|live| *live > 0))?
    {
        return Err(Stop::Refuse(message));
    }
    let mut columns = Vec::new();
    for table in EXPORTED_TABLES {
        let rows = sql::rows(
            client,
            &format!(
                "SELECT c.column_name, c.udt_name, format_type(a.atttypid, a.atttypmod) \
                 FROM information_schema.columns c JOIN pg_attribute a \
                 ON a.attrelid = 'public.{table}'::regclass AND a.attname = c.column_name \
                 WHERE c.table_schema = 'public' AND c.table_name = '{table}'"
            ),
        )
        .await
        .map_err(defer)?;
        let mut found = Vec::new();
        for row in rows {
            let [Some(name), Some(udt), Some(full)] =
                <[Option<String>; 3]>::try_from(row).map_err(|_| Stop::Defer)?
            else {
                return Err(Stop::Defer);
            };
            found.push(Column { name, udt, full });
        }
        columns.push(found);
    }
    match process(
        Pass::Dry(client),
        archive,
        &columns,
        legacy,
        &mut Vec::new(),
    )
    .await
    {
        Ok(()) | Err(Stop::Refuse(_)) => Ok(Some(columns)),
        Err(other) => Err(other),
    }
}

pub(super) async fn run(dsn: &Dsn, path: &Path, display: &str, force: bool) -> Outcome {
    let Some(mut archive) = Archive::open(path) else {
        return Outcome::Deferred;
    };
    let manifest = match manifest(&mut archive, display) {
        Ok(value) => value,
        Err(stop) => return outcome(stop),
    };
    match format_refusal(manifest.get("format_version")) {
        Ok(Some(message)) => return Outcome::Refused(message),
        Ok(None) => {}
        Err(stop) => return outcome(stop),
    }
    // Validated before any connection; dim_refusal re-reads it after the guard.
    if let Err(stop) = dim_refusal(manifest.get("embedding_dim"), Some(0)) {
        return outcome(stop);
    }
    let legacy = if archive.names.contains("entries.jsonl") {
        match legacy_entries(manifest.get("schema_version")) {
            Ok(legacy) => legacy,
            Err(stop) => return outcome(stop),
        }
    } else {
        false
    };
    let Ok(session) = Session::open(dsn).await else {
        return Outcome::Deferred;
    };
    let result = match admit(session.client(), &mut archive, &manifest, force, legacy).await {
        Ok(None) => Err(None),
        Ok(Some(columns)) => {
            let result = transaction(session.client(), &mut archive, &columns, legacy).await;
            if result.is_err() {
                let _ = sql::execute(session.client(), "ROLLBACK").await;
            }
            result.map_err(Some)
        }
        Err(stop) => Err(Some(stop)),
    };
    let _ = session.close().await;
    match result {
        Ok(counts) => {
            let mut text = format!("imported: {display}\n");
            for (table, count) in counts {
                if count > 0 {
                    let _ = writeln!(text, "  {table}: {count}");
                }
            }
            Outcome::Done(text)
        }
        Err(None) => Outcome::SchemaDeferred,
        Err(Some(stop)) => outcome(stop),
    }
}

fn outcome(stop: Stop) -> Outcome {
    match stop {
        Stop::Defer => Outcome::Deferred,
        Stop::Refuse(message) => Outcome::Refused(message),
        Stop::Fail => Outcome::Failed,
    }
}
