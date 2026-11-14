//! The Codex rows of `connect`: Codex's `config.toml` is read and written
//! only through Codex's own versioned app-server (`setup::codex`), never as
//! text, so a project or managed layer is never flattened into the user
//! layer. Two rows: the registration (`[mcp_servers.pseudolife-memory.env]`)
//! and the hooks' connection file beside it.
use super::discover::{self, Ctx, Edits, Row};
use super::pyjson::{Dict, J};
use super::{Defer, FILE_KEY, SERVER, Step, TOKEN_KEY, WriteFailed, files, paths};
use crate::setup::codex::{self as setup, Rpc, SetupError};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// Opens Codex's app-server for a Codex home, from an empty directory.
#[allow(async_fn_in_trait)]
pub(super) trait Opener {
    type Client: Rpc;
    async fn open(&self, home: &Path, cwd: &Path) -> Result<Self::Client, SetupError>;
    /// Stop the app-server and wait for it, so its cwd can be removed.
    async fn close(&self, client: Self::Client) {
        drop(client);
    }
}

/// The real Codex on PATH (or `CODEX_CLI_PATH`).
pub(super) struct AppServer;
impl Opener for AppServer {
    type Client = setup::Client;
    async fn open(&self, home: &Path, cwd: &Path) -> Result<setup::Client, SetupError> {
        let executable = setup::resolve_codex()?;
        setup::Client::open(&executable, home, cwd).await
    }
    async fn close(&self, mut client: setup::Client) {
        client.close().await;
    }
}

/// The replace plan, and the environment to show and confirm. Before a
/// pairing names its token file, the plan shows the placeholder as written
/// (Python's `str(token_file)`), not resolved against the working directory.
fn plan_for(
    ctx: &Ctx,
    config: &Value,
    home: &Path,
) -> Result<(setup::ConnectionPlan, serde_json::Map<String, Value>), SetupError> {
    let placeholder = ctx.token_file.as_deref() == Some(super::PAIRED_TOKEN_FILE);
    let token_file = ctx
        .token_file
        .as_deref()
        .filter(|_| !placeholder)
        .map(Path::new);
    let plan = setup::plan_replace(config, home, &ctx.url, token_file, ctx.remote)?;
    let mut shown = plan.env().clone();
    if placeholder {
        shown.insert(FILE_KEY.to_owned(), json!(super::PAIRED_TOKEN_FILE));
        shown.remove(TOKEN_KEY);
    }
    Ok((plan, shown))
}

/// An empty directory for the app-server's `cwd`, so no project layer
/// applies; removed when dropped.
struct EmptyDir(PathBuf);
impl EmptyDir {
    fn new() -> Result<Self, SetupError> {
        let path =
            std::env::temp_dir().join(format!("pseudolife-connect-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&path)
            .map_err(|_| SetupError("A temporary directory for Codex could not be created."))?;
        Ok(Self(path))
    }
}
impl Drop for EmptyDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// The Codex home as Codex spells it: the directory of `config.toml`,
/// resolved through links.
fn home_of(config: &Path) -> PathBuf {
    let parent = config.parent().unwrap_or(config).to_path_buf();
    match std::fs::canonicalize(&parent) {
        Ok(resolved) => paths::strip_verbatim(resolved),
        Err(_) => parent,
    }
}

fn to_j(value: &Value) -> J {
    match value {
        Value::Null => J::Null,
        Value::Bool(b) => J::Bool(*b),
        Value::Number(n) if n.is_i64() || n.is_u64() => J::Int(n.to_string()),
        Value::Number(n) => J::Float(n.as_f64().unwrap_or(0.0)),
        Value::String(s) => J::Str(s.clone()),
        Value::Array(items) => J::List(items.iter().map(to_j).collect()),
        Value::Object(map) => J::Dict(map.iter().map(|(k, v)| (k.clone(), to_j(v))).collect()),
    }
}

fn dict(map: &serde_json::Map<String, Value>) -> Dict {
    map.iter().map(|(k, v)| (k.clone(), to_j(v))).collect()
}

fn text(value: Option<&Value>) -> Option<String> {
    value.map(|v| v.as_str().map_or_else(|| v.to_string(), str::to_owned))
}

/// The registration's edits: every key whose value differs, in key order.
fn edits(before: &serde_json::Map<String, Value>, after: &serde_json::Map<String, Value>) -> Edits {
    let mut keys: Vec<&String> = before.keys().chain(after.keys()).collect();
    keys.sort();
    keys.dedup();
    keys.into_iter()
        .filter(|key| before.get(*key) != after.get(*key))
        .map(|key| (key.clone(), text(after.get(key))))
        .collect()
}

/// `connect_cli._codex_rows`.
pub(super) async fn rows(ctx: &Ctx, opener: &impl Opener) -> Result<Vec<Row>, Defer> {
    let path = paths::codex_config()?;
    // Only Codex's own file: another client's unreadable configuration must
    // not read as "no Codex registration" (Codex review).
    let found =
        crate::runtime::registrations::find_selected_registrations(|client| client == "codex");
    let command = match found {
        Ok(found) => found
            .into_iter()
            .find(|registration| registration.client == "codex")
            .map(|registration| registration.command),
        Err(cause) => {
            return Ok(vec![discover::manual(
                "codex",
                "registration",
                Some(&path),
                Some(SERVER),
                format!(
                    "the Codex configuration could not be read ({cause}); repair {} and run connect again",
                    paths::show(&path)?
                ),
            )?]);
        }
    };
    rows_for(ctx, &path, command, opener).await
}

/// The rows for the Codex `config.toml` at `path`, whose shim registration
/// (if any) runs `command`.
pub(super) async fn rows_for(
    ctx: &Ctx,
    path: &Path,
    command: Option<String>,
    opener: &impl Opener,
) -> Result<Vec<Row>, Defer> {
    let path = path.to_path_buf();
    let Some(command) = command else {
        return unregistered(ctx, &path);
    };
    let home = home_of(&path);
    let connection = setup::connection_path(&home);
    let manual = |reason: &str| -> Result<Vec<Row>, Defer> {
        Ok(vec![discover::manual(
            "codex",
            "registration",
            Some(&path),
            Some(SERVER),
            format!(
                "{reason} Edit [mcp_servers.{SERVER}.env] where it is set, and {} to match",
                paths::show(&connection)?
            ),
        )?])
    };
    let planned = async {
        let cwd = EmptyDir::new()?;
        let mut client = opener.open(&home, &cwd.0).await?;
        let result = async {
            let config = client
                .rpc("config/read", json!({"includeLayers": true, "cwd": cwd.0}))
                .await?;
            let (plan, shown) = plan_for(ctx, &config, &home)?;
            let recorded = setup::read_connection(&connection)?;
            Ok::<_, SetupError>((plan, shown, recorded))
        }
        .await;
        opener.close(client).await;
        result
    };
    let (plan, shown, recorded) = match planned.await {
        Ok(found) => found,
        Err(error) => return manual(&error.to_string()),
    };
    let before = dict(plan.previous_env());
    let after = dict(&shown);
    let row_edits = edits(plan.previous_env(), &shown);
    let mut notes = discover::notes(&before, ctx, &row_edits, Some(&command));
    let literal = plan
        .previous_env()
        .get(TOKEN_KEY)
        .and_then(Value::as_str)
        .filter(|token| !token.is_empty() && !plan.previous_env().contains_key(FILE_KEY))
        .filter(|_| ctx.token_file.is_none())
        .map(str::to_owned);
    if literal.is_some() {
        notes.push(format!(
            "its literal {TOKEN_KEY} moves into Codex's token copy {}, as the Codex credential writer does",
            paths::show(&setup::token_copy_path(&home))?
        ));
    }
    let mut registration_row = discover::row(
        "codex",
        "registration",
        Some(&path),
        Some(SERVER),
        if row_edits.is_empty() {
            "current"
        } else {
            "change"
        },
    )?;
    registration_row.changes = discover::changes(&before, &row_edits)?;
    registration_row.notes = notes;
    registration_row.codex = Some(home.clone());
    registration_row.codex_after = Some(shown.clone());
    registration_row.credential = Some(match &literal {
        Some(token) => Some(super::Cred::Literal(token.clone())),
        None => discover::credential(&after)?,
    });
    let target = shown
        .get(FILE_KEY)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let (old_url, old_file) = recorded
        .clone()
        .map_or((None, None), |(u, f)| (Some(u), Some(f)));
    let mut connection_changes = Vec::new();
    if old_url.as_deref() != Some(ctx.url.as_str()) {
        let shown = old_url.as_deref().map(super::url::shown).transpose()?;
        connection_changes.push(("daemon_url".to_owned(), shown, Some(ctx.url.clone())));
    }
    if old_file.as_deref() != Some(target.as_str()) {
        connection_changes.push(("token_file".to_owned(), old_file, Some(target.clone())));
    }
    let mut connection_row = discover::row(
        "codex",
        "connection",
        Some(&connection),
        None,
        if connection_changes.is_empty() {
            "current"
        } else {
            "change"
        },
    )?;
    connection_row.changes = connection_changes;
    connection_row.notes = vec!["read by the plugin hooks in Codex context".to_owned()];
    connection_row.codex = Some(home);
    Ok(vec![registration_row, connection_row])
}

/// No Codex registration of the shim: a url server is manual, an
/// unreadable file that names the server is manual, anything else absent.
fn unregistered(ctx: &Ctx, path: &Path) -> Result<Vec<Row>, Defer> {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    if text.is_empty() {
        return Ok(vec![discover::absent("codex", ctx)?]);
    }
    match text.parse::<toml::Table>() {
        Ok(data) => {
            let server = data
                .get("mcp_servers")
                .and_then(toml::Value::as_table)
                .and_then(|servers| servers.get(SERVER));
            if server.is_some_and(toml::Value::is_table) {
                return Ok(vec![discover::manual(
                    "codex",
                    "registration",
                    Some(path),
                    Some(SERVER),
                    "not a stdio registration of the shim (a url server); connect rewrites only the shim's own. Re-register it with codex mcp add".to_owned(),
                )?]);
            }
        }
        Err(_) if text.contains(&format!("mcp_servers.{SERVER}")) => {
            return Ok(vec![discover::manual(
                "codex",
                "registration",
                Some(path),
                Some(SERVER),
                format!(
                    "config.toml is not valid TOML here; edit [mcp_servers.{SERVER}.env] by hand"
                ),
            )?]);
        }
        Err(_) => {}
    }
    Ok(vec![discover::absent("codex", ctx)?])
}

/// `connect_cli._apply_codex`: one transaction through the app-server for
/// both Codex rows; the three files it may write are recorded for the run's
/// rollback, with what each held before and after.
pub(super) async fn apply(
    ctx: &Ctx,
    rows: &mut [&mut Row],
    steps: &mut Vec<Step>,
    opener: &impl Opener,
) -> Result<(), WriteFailed> {
    let home = rows[0].codex.clone().expect("a Codex row");
    let config = home.join("config.toml");
    let watched = [
        config.clone(),
        setup::connection_path(&home),
        setup::token_copy_path(&home),
    ];
    let mut originals = Vec::new();
    for path in &watched {
        // A file that cannot be read now must not be recorded as absent:
        // the rollback would then remove it (Opus review).
        match files::bytes(path) {
            Ok(original) => originals.push(original),
            Err(error) => {
                return Err(WriteFailed {
                    path: path.clone(),
                    reason: format!(
                        "{} while reading it before the Codex write; nothing of Codex was written",
                        files::os_error_name(&error)
                    ),
                    events: Vec::new(),
                });
            }
        }
    }
    let expected = rows
        .iter()
        .find_map(|row| row.codex_after.clone())
        .expect("the registration row's planned environment");
    let first = steps.len();
    for (path, original) in watched.iter().zip(originals) {
        steps.push(Step {
            path: path.clone(),
            original,
            written: None,
            backup: None,
        });
    }
    let mut failure_events = Vec::new();
    let outcome = async {
        let cwd = EmptyDir::new().map_err(|e| (e.to_string(), json!({})))?;
        let mut client = opener
            .open(&home, &cwd.0)
            .await
            .map_err(|e| (e.to_string(), json!({})))?;
        let read = client
            .rpc("config/read", json!({"includeLayers": true, "cwd": cwd.0}))
            .await
            .map_err(|e| (e.to_string(), json!({})))?;
        let (plan, shown) =
            plan_for(ctx, &read, &home).map_err(|e| (e.to_string(), json!({})))?;
        // What was shown, confirmed and verified is what is written: a
        // configuration changed since the plan is refused untouched.
        if shown != expected {
            opener.close(client).await;
            return Err((
                "Codex's configuration changed after the plan was shown, so none of it was written; run connect again".to_owned(),
                json!({}),
            ));
        }
        let applied = setup::apply_replace_detailed(&mut client, &cwd.0, plan)
            .await
            .map(|applied| applied.report().clone())
            .map_err(|failure| {
                failure_events = failure.rollback.iter().map(to_j).collect();
                (failure.reason.to_string(), failure.report)
            });
        opener.close(client).await;
        applied
    }
    .await;
    for (offset, path) in watched.iter().enumerate() {
        steps[first + offset].written = files::bytes(path).ok().flatten();
    }
    let report = match &outcome {
        Ok(report) => report.clone(),
        Err((_, report)) => report.clone(),
    };
    let backup = |key: &str| report[key].as_str().map(str::to_owned);
    steps[first].backup = backup("backup");
    steps[first + 1].backup = backup("connection_backup");
    steps[first + 2].backup = backup("token_backup");
    if let Err((reason, _)) = outcome {
        // The app-server transaction compensated itself and kept what it
        // could not safely undo: none of its files is this run's to restore
        // from a snapshot (Codex review), so they leave the rollback list.
        steps.truncate(first);
        return Err(WriteFailed {
            path: config,
            reason,
            events: failure_events,
        });
    }
    let created: Vec<String> = report["created"]
        .as_array()
        .map(|items| items.iter().filter_map(text_of).collect())
        .unwrap_or_default();
    let connection_shown = setup::connection_path(&home).to_string_lossy().into_owned();
    for row in rows.iter_mut() {
        if row.place == "registration" {
            row.backup = backup("backup");
            if let Some(token_backup) = backup("token_backup") {
                row.notes.push(format!("token copy backup: {token_backup}"));
            }
        } else {
            row.backup = backup("connection_backup");
            row.created = created.contains(&connection_shown);
        }
    }
    Ok(())
}

fn text_of(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}
