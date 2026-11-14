//! Where `conductor-watch` looks: GitHub through `gh`, the suite results
//! directory, and the suite leases.
use super::events;
use serde_json::{Map, Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

/// gh's `--json` fields the classifiers read.
const PR_FIELDS: &str = "number,title,headRefOid,mergeable,mergeStateStatus,isDraft,statusCheckRollup";
const RUN_FIELDS: &str =
    "attempt,databaseId,workflowDatabaseId,workflowName,status,conclusion,headSha,event";
/// gh paginates up to this; a pull request missing from the list is looked
/// up by number before it counts as closed, so a full page cannot fake one.
const PR_LIMIT: &str = "500";
/// The longest suite summary a snapshot keeps (characters).
const SUMMARY: usize = 120;
/// How long one gh call may take.
const GH_TIMEOUT: Duration = Duration::from_secs(120);
/// What a person can do about gh printing something this watcher cannot read.
const GH_FIX: &str = " (check that the gh on PATH is the GitHub CLI and current: gh --version)";

/// Why a gh call gave no answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum GhError {
    /// gh is not installed, or not on an absolute `PATH` directory.
    Missing,
    /// The call failed; the text says how.
    Failed(String),
}

/// Runs gh with `argv` and returns what it printed on stdout. Async, so the
/// watcher keeps its board row renewed and sees a stop request or an
/// interrupt while gh runs.
#[allow(async_fn_in_trait)]
pub(super) trait Gh {
    async fn call(&mut self, argv: &[&str]) -> Result<String, GhError>;
}

/// The real gh: found once, run with a timeout, `-R <repo>` appended.
pub(super) struct GhProgram {
    pub(super) program: Option<PathBuf>,
    pub(super) repo: Option<String>,
}

impl Gh for GhProgram {
    async fn call(&mut self, argv: &[&str]) -> Result<String, GhError> {
        let Some(program) = &self.program else {
            return Err(GhError::Missing);
        };
        let mut command = std::process::Command::new(program);
        command.args(argv);
        if let Some(repo) = &self.repo {
            command.args(["-R", repo]);
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
        }
        let mut command = tokio::process::Command::from(command);
        // A call given up (timed out, or the watcher stopping) ends gh.
        command.kill_on_drop(true);
        let child = command.spawn().map_err(|error| match error.kind() {
            std::io::ErrorKind::NotFound => GhError::Missing,
            _ => GhError::Failed(format!("gh could not run ({error})")),
        })?;
        // The timeout covers reading the output too, so a grandchild that
        // keeps gh's pipe open cannot hold the watcher.
        let output = match tokio::time::timeout(GH_TIMEOUT, child.wait_with_output()).await {
            Err(_) => return Err(GhError::Failed("gh timed out".into())),
            Ok(Err(error)) => return Err(GhError::Failed(format!("gh could not run ({error})"))),
            Ok(Ok(output)) => output,
        };
        let status = output.status;
        let out = String::from_utf8_lossy(&output.stdout).into_owned();
        let err = String::from_utf8_lossy(&output.stderr).into_owned();
        if !status.success() {
            let text = if err.trim().is_empty() { &out } else { &err };
            return Err(GhError::Failed(text.trim().lines().last().map_or_else(
                || format!("gh exited {}", status.code().unwrap_or(-1)),
                str::to_owned,
            )));
        }
        Ok(out)
    }
}

/// A JSON kind as the refusals name it.
fn kind(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(true) => "true",
        Value::Bool(false) => "false",
        Value::Object(_) => "a JSON object",
        Value::Array(_) => "a list",
        Value::String(_) => "a string",
        Value::Number(_) => "a number",
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Allowed {
    Text,
    Int,
    IntOrText,
    List,
}

impl Allowed {
    /// Null (or absent) is allowed except for `number`.
    fn admits(self, value: Option<&Value>, required: bool) -> bool {
        match value {
            None | Some(Value::Null) => !required,
            Some(Value::String(_)) => matches!(self, Self::Text | Self::IntOrText),
            Some(Value::Number(number)) => {
                matches!(self, Self::Int | Self::IntOrText) && (number.is_i64() || number.is_u64())
            }
            Some(Value::Array(_)) => self == Self::List,
            Some(_) => false,
        }
    }
}

const PR_TYPES: [(&str, Allowed, bool); 6] = [
    ("number", Allowed::Int, true),
    ("title", Allowed::Text, false),
    ("headRefOid", Allowed::Text, false),
    ("mergeable", Allowed::Text, false),
    ("mergeStateStatus", Allowed::Text, false),
    ("statusCheckRollup", Allowed::List, false),
];
const CHECK_TYPES: [(&str, Allowed, bool); 7] = [
    ("__typename", Allowed::Text, false),
    ("state", Allowed::Text, false),
    ("context", Allowed::Text, false),
    ("status", Allowed::Text, false),
    ("conclusion", Allowed::Text, false),
    ("name", Allowed::Text, false),
    ("workflowName", Allowed::Text, false),
];
const RUN_TYPES: [(&str, Allowed, bool); 7] = [
    ("databaseId", Allowed::Int, false),
    ("workflowDatabaseId", Allowed::IntOrText, false),
    ("workflowName", Allowed::Text, false),
    ("status", Allowed::Text, false),
    ("conclusion", Allowed::Text, false),
    ("headSha", Allowed::Text, false),
    ("event", Allowed::Text, false),
];

/// `value` when it is a list of objects whose typed fields have those
/// types; otherwise the reason, naming what `what` printed instead.
fn records(value: Value, what: &str, types: &[(&str, Allowed, bool)]) -> Result<Vec<Value>, String> {
    let Value::Array(items) = value else {
        return Err(format!("{what} printed {}, not a list of objects{GH_FIX}", kind(&value)));
    };
    check_items(&items, what, types, "a list")?;
    Ok(items)
}

fn check_items(
    items: &[Value],
    what: &str,
    types: &[(&str, Allowed, bool)],
    holder: &str,
) -> Result<(), String> {
    for item in items {
        let Value::Object(fields) = item else {
            return Err(format!("{what} printed {holder} holding {}, not objects{GH_FIX}", kind(item)));
        };
        for (key, allowed, required) in types {
            let field = fields.get(*key);
            if field.is_none() && *required {
                return Err(format!("{what} printed an item with no {key}{GH_FIX}"));
            }
            if !allowed.admits(field, *required) {
                return Err(format!(
                    "{what} printed {key} as {}{GH_FIX}",
                    field.map_or("null", kind)
                ));
            }
        }
        if let Some(Value::Array(checks)) = fields.get("statusCheckRollup") {
            check_items(checks, what, &CHECK_TYPES, "a statusCheckRollup")?;
        }
    }
    Ok(())
}

fn parse(out: &str, what: &str) -> Result<Value, GhError> {
    let out = if out.trim().is_empty() { "null" } else { out };
    serde_json::from_str(out)
        .map_err(|_| GhError::Failed(format!("{what} printed output that is not JSON{GH_FIX}")))
}

/// The GitHub side of a snapshot.
pub(super) struct GitHub<'a, G: Gh> {
    pub(super) gh: &'a mut G,
    pub(super) author: Option<String>,
}

impl<G: Gh> GitHub<'_, G> {
    async fn json(&mut self, argv: &[&str], what: &str) -> Result<Value, GhError> {
        let out = self.gh.call(argv).await?;
        parse(&out, what)
    }

    async fn runs_on(&mut self, head: &str) -> Result<Vec<Value>, GhError> {
        let value = self
            .json(
                &["run", "list", "--commit", head, "--limit", "100", "--json", RUN_FIELDS],
                "gh run list",
            )
            .await?;
        records(value, "gh run list", &RUN_TYPES).map_err(GhError::Failed)
    }

    /// The open pull requests. Only a head about to be reported green or
    /// cancelled has its workflow runs looked up, so a quiet poll costs one
    /// call; the runs can only hold or worsen what the rollup says.
    pub(super) async fn prs(&mut self, old: Option<&Map<String, Value>>) -> Result<Map<String, Value>, GhError> {
        let mut argv = vec!["pr", "list", "--state", "open", "--limit", PR_LIMIT, "--json", PR_FIELDS];
        let author = self.author.clone();
        if let Some(author) = author.as_deref() {
            argv.extend(["--author", author]);
        }
        let value = self.json(&argv, "gh pr list").await?;
        let prs = records(value, "gh pr list", &PR_TYPES).map_err(GhError::Failed)?;
        let mut snap = events::pr_snapshot(old, &prs);
        for (number, pr) in snap.iter_mut() {
            let checks = pr["checks"].as_str().unwrap_or("").to_owned();
            if checks != "green" && checks != "cancelled" {
                continue;
            }
            let before = old.and_then(|old| old.get(number));
            if before.is_some_and(|before| before["head"] == pr["head"] && before["checks"] == pr["checks"]) {
                continue;
            }
            let head = pr["head"].as_str().unwrap_or("").to_owned();
            let (runs, names) = events::runs_state(&self.runs_on(&head).await?);
            if runs == "pending" {
                pr["checks"] = json!("pending");
                pr["failed"] = json!([]);
            } else if runs == "failing" || (runs == "cancelled" && checks == "green") {
                pr["checks"] = json!(runs);
                pr["failed"] = json!(names);
            }
        }
        Ok(snap)
    }

    pub(super) async fn master(&mut self, old: Option<&Map<String, Value>>) -> Result<Map<String, Value>, GhError> {
        let value = self
            .json(
                &["run", "list", "--branch", "master", "--limit", "30", "--json", RUN_FIELDS],
                "gh run list",
            )
            .await?;
        let runs = records(value, "gh run list", &RUN_TYPES).map_err(GhError::Failed)?;
        Ok(events::master_snapshot(old, &runs))
    }

    /// `merged` or `closed`, or `None` while that is not known (the lookup
    /// failed, or the pull request is still open and fell off the list).
    pub(super) async fn closed_as(&mut self, number: &str) -> Option<String> {
        let value = self
            .json(&["pr", "view", number, "--json", "state"], "gh pr view")
            .await
            .ok()?;
        match value.get("state").and_then(Value::as_str) {
            Some("MERGED") => Some("merged".into()),
            Some("CLOSED") => Some("closed".into()),
            _ => None,
        }
    }

    /// The repository's name, for the board row's project.
    pub(super) async fn repo_name(&mut self) -> Option<String> {
        let value = self
            .json(&["repo", "view", "--json", "name"], "gh repo view")
            .await
            .ok()?;
        value.get("name").and_then(Value::as_str).map(str::to_owned)
    }
}

/// `str()` of a JSON value as the Python watcher stored it.
fn python_str(value: Option<&Value>, fallback: &str) -> String {
    match value {
        None => fallback.to_owned(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) => "None".into(),
        Some(Value::Bool(flag)) => if *flag { "True" } else { "False" }.into(),
        Some(other) => other.to_string(),
    }
}

/// The readable result files in `directory`. One that does not parse yet
/// (still being written) stays out, so a later poll reports it; one that
/// parses but is not an object is noted once and stays out too.
pub(super) fn suites(
    directory: &Path,
    old: Option<&Map<String, Value>>,
    skipped: &mut Vec<String>,
    note: &mut dyn FnMut(String),
) -> Map<String, Value> {
    let mut snap = old.cloned().unwrap_or_default();
    let Ok(entries) = std::fs::read_dir(directory) else {
        return snap;
    };
    let mut names: Vec<(String, PathBuf)> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            name.ends_with(".json").then(|| (name, entry.path()))
        })
        .collect();
    names.sort();
    for (name, path) in names {
        if snap.contains_key(&name) {
            continue;
        }
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&bytes);
        let Ok(body) = serde_json::from_slice::<Value>(bytes) else {
            continue;
        };
        let Value::Object(body) = body else {
            if !skipped.contains(&name) {
                skipped.push(name.clone());
                note(format!(
                    "suite result {} is {}, not an object; skipped until it holds a result",
                    path.display(),
                    kind(&body)
                ));
            }
            continue;
        };
        let commit: String = python_str(body.get("commit"), "").chars().take(8).collect();
        let summary: String = python_str(body.get("summary"), "").chars().take(SUMMARY).collect();
        snap.insert(
            name,
            json!({
                "machine": python_str(body.get("machine"), "?"),
                "commit": commit,
                "exit": body.get("exit_code").cloned().unwrap_or(Value::Null),
                "summary": summary,
            }),
        );
    }
    snap
}

#[cfg(test)]
#[path = "sources_tests.rs"]
mod tests;
