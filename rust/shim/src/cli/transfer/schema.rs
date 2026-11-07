//! Import uses the daemon's generated Python schema plan, SQL and metadata.
//! No DDL is maintained here; this adapter supplies the CLI's connection and
//! commit handling without depending on the daemon's serving/runtime crate.
use crate::pg::Client;
use serde_json::Value;
use std::collections::HashMap;
use tokio_postgres::{
    SimpleQueryMessage,
    types::{ToSql, Type},
};

include!("../../../../daemon/src/storage/schema_meta.rs");
const SQL: &str = include_str!("../../../../daemon/src/storage/schema.sql");
const PLAN: &str = include_str!("../../../../daemon/src/storage/schema_plan.json");
const RESTORE: &str = "(`pseudolife-mcp restore --backup-file <dump> --state-archive <state.tgz>`; from a checkout, `ops/restore.ps1 -Apply -BackupFile <dump> -StateArchive <state.tgz>` or `ops/restore.sh --apply --backup-file <dump> --state-archive <state.tgz>`)";

pub(super) enum Error {
    Refused(String),
    Db,
    CommitUnknown,
}
impl From<tokio_postgres::Error> for Error {
    fn from(_: tokio_postgres::Error) -> Self {
        Self::Db
    }
}

async fn first(client: &Client, sql: &str) -> Result<Option<Option<String>>, Error> {
    for message in client.simple_query(sql).await? {
        if let SimpleQueryMessage::Row(row) = message {
            return Ok(Some(row.get(0).map(str::to_owned)));
        }
    }
    Ok(None)
}
fn test(p: &Value, last: &Option<Option<String>>, variables: &HashMap<&str, bool>) -> bool {
    if let Some(not) = p.get("not") {
        return !test(not, last, variables);
    }
    if let Some(name) = p.get("variable") {
        return variables[name.as_str().unwrap()];
    }
    match p["last"].as_str().unwrap() {
        "row" => last.is_some(),
        "value" => matches!(last, Some(Some(_))),
        _ => panic!("unknown generated schema predicate"),
    }
}
fn version_refusal(value: &str) -> Option<String> {
    let version = serde_json::from_str::<Value>(value)
        .ok()
        .and_then(|v| v.as_i64());
    match version {
        Some(v) if v > SCHEMA_META_VERSION => Some(format!(
            "Refusing to start: bank schema version {v} is newer than this build's schema version {SCHEMA_META_VERSION}. A newer Pseudolife release upgraded this bank and this build cannot serve it safely, so the bank was left untouched. Run that release or a newer one (`pseudolife-mcp update`); to roll back to this build instead, first restore the backup taken before that upgrade {RESTORE}."
        )),
        Some(v) if v >= 1 => None,
        _ => Some(format!(
            "Refusing to start: bank schema_version must be a positive integer, but this bank's is {value}. Pseudolife never writes that value, so the bank was left untouched. Restore the bank from a backup {RESTORE}."
        )),
    }
}
async fn execute(client: &Client) -> Result<(), Error> {
    let plan: Value = serde_json::from_str(PLAN).expect("generated schema plan");
    assert_eq!(plan["format"], 1);
    assert_eq!(plan["schema_version"], SCHEMA_META_VERSION);
    let mut pending: Vec<_> = plan["steps"].as_array().unwrap().iter().rev().collect();
    let mut variables = HashMap::new();
    let mut last = None;
    while let Some(step) = pending.pop() {
        match step["op"].as_str().unwrap() {
            "version_guard" => {
                if matches!(
                    first(client, "SELECT to_regclass('public.meta')").await?,
                    Some(Some(_))
                ) && let Some(Some(value)) = first(
                    client,
                    "SELECT value::text FROM public.meta WHERE key = 'schema_version'",
                )
                .await?
                    && let Some(message) = version_refusal(&value)
                {
                    return Err(Error::Refused(message));
                }
            }
            "embedding_guard" => {
                if let Some(Some(value)) =
                    first(client, plan["embedding_probe"].as_str().unwrap()).await?
                {
                    let dim: i32 = value.parse().unwrap_or(-1);
                    if dim > 0 && dim != EXPECTED_EMBEDDING_DIM {
                        return Err(Error::Refused(
                            plan["embedding_refusal"]
                                .as_str()
                                .unwrap()
                                .replace("{live_dim}", &dim.to_string()),
                        ));
                    }
                }
            }
            "base_schema" => {
                last = first(client, SQL.split_once('\n').expect("schema header").1).await?;
            }
            "sql" => {
                let sql = step["sql"].as_str().unwrap();
                let params = step["parameters"].as_array().unwrap();
                if params.is_empty() {
                    last = first(client, sql).await?;
                } else {
                    let strings: Vec<&str> = params.iter().map(|p| p.as_str().unwrap()).collect();
                    let binds: Vec<(&(dyn ToSql + Sync), Type)> = strings
                        .iter()
                        .map(|p| (p as &(dyn ToSql + Sync), Type::TEXT))
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

/// Python commits ensure_schema separately, even if a later import refuses.
pub(super) async fn ensure(client: &Client) -> Result<(), Error> {
    client.batch_execute("BEGIN").await?;
    if let Err(error) = execute(client).await {
        let _ = client.batch_execute("ROLLBACK").await;
        return Err(error);
    }
    super::sql::commit(client)
        .await
        .map_err(|error| match error {
            super::sql::CommitFailure::RolledBack => Error::Db,
            super::sql::CommitFailure::Unknown => Error::CommitUnknown,
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_artifacts_match_and_future_versions_refuse() {
        let plan: Value = serde_json::from_str(PLAN).unwrap();
        assert_eq!(plan["schema_version"], SCHEMA_META_VERSION);
        assert!(SQL.starts_with(&format!("-- schema_version {SCHEMA_META_VERSION}\n")));
        for value in ["0", "null", "true", "1.5", "\"58\"", "999999"] {
            assert!(version_refusal(value).is_some());
        }
        assert!(version_refusal(&SCHEMA_META_VERSION.to_string()).is_none());
    }
}
