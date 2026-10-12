//! Simple-protocol helpers: text-format results like psycopg's default cursor,
//! and escape-string literals for the oracle's parameters.
use crate::pg::Client;
use tokio_postgres::{SimpleQueryMessage, error::Severity};

pub(super) type Rows = Vec<Vec<Option<String>>>;

/// One statement's text-format rows.
pub(super) async fn rows(client: &Client, sql: &str) -> Result<Rows, ()> {
    let messages = client.simple_query(sql).await.map_err(|_| ())?;
    Ok(messages
        .iter()
        .filter_map(|message| match message {
            SimpleQueryMessage::Row(row) => Some(
                (0..row.len())
                    .map(|index| row.get(index).map(str::to_owned))
                    .collect(),
            ),
            _ => None,
        })
        .collect())
}

/// The first column of the first row.
pub(super) async fn scalar(client: &Client, sql: &str) -> Result<Option<String>, ()> {
    Ok(rows(client, sql)
        .await?
        .into_iter()
        .next()
        .and_then(|row| row.into_iter().next().flatten()))
}

/// Row counts of every command in a (possibly multi-statement) query.
pub(super) async fn affected(client: &Client, sql: &str) -> Result<u64, ()> {
    let messages = client.simple_query(sql).await.map_err(|_| ())?;
    Ok(messages
        .iter()
        .map(|message| match message {
            SimpleQueryMessage::CommandComplete(count) => *count,
            _ => 0,
        })
        .sum())
}

pub(super) async fn execute(client: &Client, sql: &str) -> Result<(), ()> {
    client.batch_execute(sql).await.map_err(|_| ())
}

/// How a COMMIT failed.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum CommitFailure {
    /// The server answered with an ERROR: it rolled the transaction back.
    RolledBack,
    /// A FATAL or PANIC answer, or none (the connection was lost): the
    /// transaction may have become durable before the session ended.
    Unknown,
}

/// `COMMIT`, telling a refusal the server rolled back from an answer that
/// leaves the outcome open.
pub(super) async fn commit(client: &Client) -> Result<(), CommitFailure> {
    match client.batch_execute("COMMIT").await {
        Ok(()) => Ok(()),
        Err(error) => Err(match error.as_db_error() {
            Some(db) if db.parsed_severity() == Some(Severity::Error) => CommitFailure::RolledBack,
            _ => CommitFailure::Unknown,
        }),
    }
}

/// An escape-string literal: independent of `standard_conforming_strings`.
/// Callers have already refused U+0000, which no PostgreSQL text can hold.
pub(super) fn text(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 3);
    out.push_str("E'");
    for character in value.chars() {
        match character {
            '\'' => out.push_str("''"),
            '\\' => out.push_str("\\\\"),
            other => out.push(other),
        }
    }
    out.push('\'');
    out
}
