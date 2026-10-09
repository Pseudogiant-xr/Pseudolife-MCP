//! `ensure_schema` (spec P3, P6): `pseudolife_memory/storage/schema.py`
//! Generated SQL and migration steps; regenerate with harness/gen_schema_sql.py.
//!
//! Every generated statement runs in the same order as the Python, inside the one
//! explicit transaction the caller opens. Order matters beyond correctness:
//! each `ADD COLUMN` appends a column, so a reordered statement changes the
//! ordinal positions the database-state harness compares. Statements without
//! parameters go over the simple query protocol (psycopg sends a
//! parameterless `execute` with `PQsendQuery`); the two parameterized ones
//! (the healing updates and the `schema_version` upsert) use the extended
//! protocol, as psycopg does for any statement with bind parameters.

use tokio_postgres::Client;
use tokio_postgres::types::Type;

// Metadata shares the generator with the SQL and migration plan.
include!("schema_meta.rs");

/// The generated copy of `SCHEMA_SQL`, with the one header line
/// `gen_schema_sql.py` prepends (`-- schema_version NN\n`).
const SCHEMA_SQL_FILE: &str = include_str!("schema.sql");

/// `SCHEMA_SQL` byte for byte: the generated file minus its header line, so
/// the batch the server receives is exactly the Python constant.
pub fn schema_sql() -> &'static str {
    match SCHEMA_SQL_FILE.strip_prefix("-- schema_version ") {
        Some(rest) => rest.split_once('\n').map_or("", |(_, body)| body),
        None => SCHEMA_SQL_FILE,
    }
}

/// The schema version the generated copy was written for (its header line).
#[cfg_attr(not(test), allow(dead_code))]
pub fn schema_sql_version() -> Option<i64> {
    SCHEMA_SQL_FILE
        .strip_prefix("-- schema_version ")?
        .split_once('\n')?
        .0
        .trim()
        .parse()
        .ok()
}

/// The `RuntimeError` text of `_refuse_on_embedding_dim_mismatch`
/// (schema.py:1131-1145), exactly.
pub fn dim_mismatch_message(live_dim: i32) -> String {
    generated_plan()["embedding_refusal"]
        .as_str()
        .unwrap()
        .replace("{live_dim}", &live_dim.to_string())
}

pub enum SchemaError {
    /// The embedding-dimension refusal (a Python `RuntimeError`).
    Refused(String),
    Db(tokio_postgres::Error),
}

impl From<tokio_postgres::Error> for SchemaError {
    fn from(e: tokio_postgres::Error) -> Self {
        SchemaError::Db(e)
    }
}

/// The first column of the first row a simple query returned, as text
/// (`None` for SQL NULL or no row).
pub(crate) async fn simple_value(
    client: &Client,
    sql: &str,
) -> Result<Option<Option<String>>, tokio_postgres::Error> {
    for msg in client.simple_query(sql).await? {
        if let tokio_postgres::SimpleQueryMessage::Row(row) = msg {
            return Ok(Some(row.get(0).map(str::to_owned)));
        }
    }
    Ok(None)
}

/// `_refuse_on_embedding_dim_mismatch` (schema.py:1105-1145).
async fn refuse_on_embedding_dim_mismatch(client: &Client) -> Result<(), SchemaError> {
    let plan = generated_plan();
    let row = simple_value(client, plan["embedding_probe"].as_str().unwrap()).await?;
    let Some(value) = row else {
        return Ok(()); // fresh install: entries doesn't exist yet
    };
    // atttypmod is int4 NOT NULL; an unparseable value cannot occur.
    let live_dim: i32 = value.as_deref().unwrap_or("-1").parse().unwrap_or(-1);
    if live_dim > 0 && live_dim != EXPECTED_EMBEDDING_DIM {
        return Err(SchemaError::Refused(dim_mismatch_message(live_dim)));
    }
    Ok(())
}

fn generated_plan() -> serde_json::Value {
    serde_json::from_str(include_str!("schema_plan.json"))
        .expect("generated schema plan is valid JSON")
}

/// Execute the generated, ordered plan inside the caller's DDL transaction.
/// Constant loops are expanded by the generator; database-dependent branches
/// are evaluated here, preserving Python's one-time repairs and healing.
pub async fn ensure_schema_in_transaction(client: &Client) -> Result<(), SchemaError> {
    let plan = generated_plan();
    assert_eq!(plan["format"], 1);
    let mut pending: Vec<&serde_json::Value> =
        plan["steps"].as_array().unwrap().iter().rev().collect();
    let mut variables = std::collections::HashMap::new();
    let mut last = None;
    while let Some(step) = pending.pop() {
        match step["op"].as_str().unwrap() {
            "embedding_guard" => {
                refuse_on_embedding_dim_mismatch(client).await?;
                if !crate::mutants::active("schema-future-allowed") {
                    refuse_future_or_malformed_version(client).await?;
                }
            }
            "base_schema" => {
                last = simple_value(client, schema_sql()).await?;
            }
            "sql" => {
                let sql = step["sql"].as_str().unwrap();
                if crate::mutants::active("drop-alter-tail")
                    && sql.contains("ADD COLUMN IF NOT EXISTS explicit_reinforcements")
                {
                    continue;
                }
                let parameters = step["parameters"].as_array().unwrap();
                if parameters.is_empty() {
                    last = simple_value(client, sql).await?;
                } else {
                    let strings: Vec<&str> =
                        parameters.iter().map(|p| p.as_str().unwrap()).collect();
                    let binds: Vec<(&(dyn tokio_postgres::types::ToSql + Sync), Type)> = strings
                        .iter()
                        .map(|p| (p as &(dyn tokio_postgres::types::ToSql + Sync), Type::TEXT))
                        .collect();
                    client.execute_typed(sql, &binds).await?;
                    last = None;
                }
            }
            "remember" => {
                variables.insert(
                    step["name"].as_str().unwrap(),
                    test(&step["test"], &last, &variables),
                );
            }
            "if" => {
                let branch = if test(&step["test"], &last, &variables) {
                    "then"
                } else {
                    "else"
                };
                pending.extend(step[branch].as_array().unwrap().iter().rev());
            }
            _ => panic!("unknown generated schema operation"),
        }
    }
    Ok(())
}

fn version_refusal(value: &str) -> Option<String> {
    let parsed = serde_json::from_str::<serde_json::Value>(value).ok();
    let version = parsed.and_then(|v| v.as_i64());
    match version {
        Some(v) if v > SCHEMA_META_VERSION => Some(format!(
            "Refusing to start: bank schema version {v} is newer than this build's schema version {SCHEMA_META_VERSION}."
        )),
        Some(v) if v >= 1 => None,
        _ => Some("Refusing to start: bank schema_version must be a positive integer.".into()),
    }
}

async fn refuse_future_or_malformed_version(client: &Client) -> Result<(), SchemaError> {
    if !matches!(
        simple_value(client, "SELECT to_regclass('public.meta')").await?,
        Some(Some(_))
    ) {
        return Ok(());
    }
    if let Some(Some(value)) = simple_value(
        client,
        "SELECT value::text FROM public.meta WHERE key = 'schema_version'",
    )
    .await?
        && let Some(message) = version_refusal(&value)
    {
        return Err(SchemaError::Refused(message));
    }
    Ok(())
}

fn test(
    predicate: &serde_json::Value,
    last: &Option<Option<String>>,
    variables: &std::collections::HashMap<&str, bool>,
) -> bool {
    if let Some(negated) = predicate.get("not") {
        return !test(negated, last, variables);
    }
    if let Some(variable) = predicate.get("variable") {
        return variables[variable.as_str().unwrap()];
    }
    match predicate["last"].as_str().unwrap() {
        "row" => last.is_some(),
        "value" => matches!(last, Some(Some(_))),
        _ => panic!("unknown generated schema predicate"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bank_version_is_never_downgraded_or_coerced() {
        for value in ["null", "true", "1.5", "\"55\"", "0", "-1"] {
            assert!(version_refusal(value).is_some(), "{value}");
        }
        assert_eq!(version_refusal("1"), None);
        assert_eq!(version_refusal(&SCHEMA_META_VERSION.to_string()), None);
        let future = SCHEMA_META_VERSION + 1;
        let message = version_refusal(&future.to_string()).unwrap();
        assert!(message.contains(&future.to_string()));
        assert!(message.contains(&SCHEMA_META_VERSION.to_string()));
    }

    #[test]
    fn schema_sql_drops_only_the_header_line() {
        assert_eq!(schema_sql_version(), Some(SCHEMA_META_VERSION));
        assert!(schema_sql().starts_with("\nCREATE TABLE IF NOT EXISTS meta ("));
        assert_eq!(
            schema_sql().len() + format!("-- schema_version {SCHEMA_META_VERSION}\n").len(),
            SCHEMA_SQL_FILE.len()
        );
    }

    #[test]
    fn dim_message_matches_python() {
        // Expected text produced by schema.py's f-string for live_dim=384.
        let want = "Refusing to start: entries.embedding is vector(384) but this build's \
schema expects vector(1024). ensure_schema is additive-only and will NOT alter vector \
dimensions in place -- that would either half-migrate four tables at startup or write \
1024-d vectors into a 384-d column, corrupting the bank. Run the human-gated migration \
first: `python ops/migrate_embeddings.py` (backs up, requires the daemon stopped, \
re-embeds every row through the real embedder, and moves all four embedding columns \
from vector(384) to vector(1024)). Never run the daemon against a bank you have not migrated.";
        assert_eq!(dim_mismatch_message(384), want);
    }
}
