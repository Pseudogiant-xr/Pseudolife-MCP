//! Where the statements run: the admin URL through the crate's PostgreSQL
//! client (`AdminUrl`), or `psql` inside the Postgres container
//! (`ContainerPsql`). Each `query` answers like the oracle's: the first column
//! of the last row-returning statement's first row, or the error text.
use super::file::{py_splitlines, py_strip};
use crate::pg::{Dsn, Session, TlsEnvironment};
use std::{process::Stdio, time::Duration};
use tokio::io::AsyncWriteExt;

/// `DOCKER_TIMEOUT_S`.
const DOCKER_TIMEOUT: Duration = Duration::from_secs(60);
/// `ContainerPsql.query`'s `sh -c` script: "$1" is the database, empty
/// meaning the container's POSTGRES_DB.
const PSQL: &str =
    r#"psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${1:-$POSTGRES_DB}""#;

pub(super) enum Executor {
    Admin { url: String },
    Container { name: String },
}

/// The admin URL with `dbname` set, as `make_conninfo(url, dbname=...)` does:
/// a later `dbname` wins in both admitted DSN grammars.
fn with_dbname(url: &str, database: &str) -> String {
    if url.starts_with("postgresql://") || url.starts_with("postgres://") {
        let encoded: String =
            percent_encoding::utf8_percent_encode(database, percent_encoding::NON_ALPHANUMERIC)
                .collect();
        let separator = if url.contains('?') { '&' } else { '?' };
        format!("{url}{separator}dbname={encoded}")
    } else {
        let escaped = database.replace('\\', "\\\\").replace('\'', "\\'");
        format!("{url} dbname='{escaped}'")
    }
}

/// The admin URL parses in the admitted grammar and no ambient libpq control
/// this client refuses is set; otherwise the caller defers before any effect.
pub(super) fn admin_url_admitted(url: &str) -> bool {
    Dsn::parse(url).is_ok()
        && Dsn::parse(&with_dbname(url, "postgres")).is_ok()
        && TlsEnvironment::from_environment().validate().is_ok()
}

fn exception_name(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    }
}

impl Executor {
    pub(super) fn description(&self) -> String {
        match self {
            Self::Admin { .. } => "the Postgres at the admin URL".into(),
            Self::Container { name } => format!("the Postgres in container {name}"),
        }
    }

    pub(super) fn is_container(&self) -> bool {
        matches!(self, Self::Container { .. })
    }

    pub(super) async fn query(
        &self,
        database: Option<&str>,
        statements: &[String],
    ) -> Result<String, String> {
        match self {
            Self::Admin { url } => {
                let text = match database {
                    None => url.clone(),
                    Some(database) => with_dbname(url, database),
                };
                admin_query(&text, statements)
                    .await
                    .map_err(|error| super::redacted(&error, Some(url)))
            }
            Self::Container { name } => container_query(name, database, statements).await,
        }
    }
}

async fn admin_query(url: &str, statements: &[String]) -> Result<String, String> {
    let dsn = Dsn::parse(url).map_err(|error| format!("ProgrammingError: {error}"))?;
    let session = Session::open(&dsn)
        .await
        .map_err(|error| format!("OperationalError: {error}"))?;
    let result = async {
        // The shared client's session settings, back to the server's own
        // defaults as a plain psycopg connection has them.
        session
            .client()
            .batch_execute(
                "RESET lock_timeout; RESET search_path; SET client_min_messages = warning",
            )
            .await
            .map_err(database_error)?;
        let mut result = String::new();
        for statement in statements {
            let messages = session
                .client()
                .simple_query(statement)
                .await
                .map_err(database_error)?;
            let mut described = false;
            let mut first: Option<Option<String>> = None;
            for message in messages {
                match message {
                    tokio_postgres::SimpleQueryMessage::RowDescription(_) => described = true,
                    tokio_postgres::SimpleQueryMessage::Row(row) if first.is_none() => {
                        described = true;
                        first = Some(row.get(0).map(str::to_owned));
                    }
                    _ => {}
                }
            }
            if described {
                result = first.flatten().unwrap_or_default();
            }
        }
        Ok(result)
    }
    .await;
    let _ = session.close().await;
    result
}

fn database_error(error: tokio_postgres::Error) -> String {
    match error.as_db_error() {
        Some(db) => format!("DatabaseError: {}", db.message()),
        None => "OperationalError: the connection was lost".into(),
    }
}

/// Python's text-mode pipe: universal newlines on the way in.
fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

async fn container_query(
    container: &str,
    database: Option<&str>,
    statements: &[String],
) -> Result<String, String> {
    let mut script = String::from("SET client_min_messages = warning;\n");
    for statement in statements {
        script.push_str(statement);
        script.push_str(";\n");
    }
    // Python's text-mode stdin writes os.linesep for each newline.
    let script = super::super::text_bytes(&script);
    let mut command = tokio::process::Command::new("docker");
    command
        .args(["exec", "-i", container, "sh", "-c", PSQL, "sh"])
        .arg(database.unwrap_or(""))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let (code, stdout, stderr) = match command.spawn() {
        Err(error) => (127, String::new(), exception_name(&error).to_owned()),
        Ok(mut child) => {
            let mut stdin = child.stdin.take().expect("piped stdin");
            let run = async move {
                let feed = async move {
                    let _ = stdin.write_all(&script).await;
                    drop(stdin);
                };
                let (_, output) = tokio::join!(feed, child.wait_with_output());
                output
            };
            match tokio::time::timeout(DOCKER_TIMEOUT, run).await {
                Err(_) => (127, String::new(), "TimeoutExpired".to_owned()),
                Ok(Err(error)) => (127, String::new(), exception_name(&error).to_owned()),
                Ok(Ok(output)) => (
                    output.status.code().unwrap_or(1),
                    text(&output.stdout),
                    text(&output.stderr),
                ),
            }
        }
    };
    if code != 0 {
        let message = if !stderr.is_empty() {
            stderr
        } else if !stdout.is_empty() {
            stdout
        } else {
            format!("exit {code}")
        };
        return Err(py_strip(&message).to_owned());
    }
    Ok(py_splitlines(&stdout)
        .into_iter()
        .rfind(|line| !py_strip(line).is_empty())
        .map(|line| py_strip(line).to_owned())
        .unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_database_is_set_in_both_grammars() {
        assert_eq!(
            with_dbname("postgresql://u@127.0.0.1:5/postgres", "template1"),
            "postgresql://u@127.0.0.1:5/postgres?dbname=template1"
        );
        assert_eq!(
            with_dbname("postgresql://u@h/x?sslmode=disable", "a b"),
            "postgresql://u@h/x?sslmode=disable&dbname=a%20b"
        );
        assert_eq!(
            with_dbname("host=h user=u", "it's"),
            "host=h user=u dbname='it\\'s'"
        );
    }
}
