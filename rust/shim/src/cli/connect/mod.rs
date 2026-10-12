//! `pseudolife-mcp connect` (PARITY CLI-CONNECT) at Python oracle
//! `pseudolife_memory/connect_cli.py`: re-point this machine's existing
//! JSON-file client registrations (Claude Code with its settings copy,
//! Claude Desktop, Gemini CLI) at a daemon, after an unauthenticated health
//! probe, a plan, and verification of every credential against the target;
//! writes are all or nothing with backups beside each file.
//!
//! Deferred before any effect: pairing (`--code` / `--read-code`), a Codex
//! `config.toml` (its app-server writes Codex's config), an interactive
//! confirmation or token prompt, a Windows User-scope daemon URL, an
//! unattended-update schedule, and every input whose Python reading this
//! module cannot reproduce exactly (see `pyjson`, `url`, `paths`).
//!
//! A refused stdout (a closed pipe) changes only the final status: the
//! oracle's stdout is block-buffered, so its prints succeed, every step
//! runs, and CPython's interpreter-shutdown flush then fails and exits 120.
//! That holds while the buffered output stays within CPython's 8192-byte
//! buffer (measured 2026-10-10, CPython 3.11.9 on Windows: one print of
//! more than 8192 encoded bytes raises at that print instead). connect's
//! plan and JSON report are far smaller; a longer refused output is not
//! modelled: this leaf still runs every step and exits 120.
use std::cell::Cell;
use std::ffi::OsString;
use std::io::Read as _;
use std::path::PathBuf;
use std::process::ExitCode;

mod args;
mod discover;
mod files;
mod net;
mod paths;
mod procs;
mod pyjson;
mod url;

use discover::{Ctx, Row};
use pyjson::J;

/// This run is outside the native domain: nothing has been printed or
/// written, and the dispatcher reports the deferral.
#[derive(Debug)]
pub(super) struct Defer;

const URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";
const FILE_KEY: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
const TOKEN_KEY: &str = "PSEUDOLIFE_MCP_TOKEN";
const NO_SPAWN_KEY: &str = "PSEUDOLIFE_MCP_NO_SPAWN";
const STATE_KEY: &str = "PSEUDOLIFE_AGENT_STATE";
const SERVER: &str = "pseudolife-memory";
const DESKTOP_SERVER: &str = "pseudolife-desktop";
const DEFAULT_URL: &str = "http://127.0.0.1:8765";
const CLIENTS: [&str; 4] = ["claude-code", "codex", "claude-desktop", "gemini"];

const EXIT_OK: u8 = 0;
const EXIT_FAILED: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_NOTHING: u8 = 3;
const EXIT_REFUSED: u8 = 4;
const EXIT_UNVERIFIED: u8 = 5;
/// CPython's exit when a print to stderr raised: the traceback cannot reach
/// stderr either, and the interpreter-shutdown flush fails.
const EXIT_STDERR_REFUSED: u8 = 120;
/// CPython's exit when stdout refused its buffered prints at the
/// interpreter-shutdown flush, whatever `main` returned.
const EXIT_STDOUT_REFUSED: u8 = 120;

const BOARD_HINT: &str = "to admit this principal, list it under coordination.allowed_principals in the daemon's config.yaml, or invite this machine with `pseudolife-mcp invite <machine>` on the daemon host (an invited principal is admitted to the board)";

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Cred {
    File(String),
    Literal(String),
    None,
}

/// `line` and a newline, flushed; false when the stream refused either.
fn emit(stream: &mut dyn std::io::Write, line: &str) -> bool {
    stream
        .write_all(&super::text_bytes(&format!("{line}\n")))
        .and_then(|()| stream.flush())
        .is_ok()
}

struct Report {
    json: bool,
    url: Option<String>,
    remote: Option<bool>,
    dry_run: bool,
    daemon: Option<(J, J)>,
    rows: Vec<J>,
    verification: Vec<J>,
    warnings: Vec<String>,
    notes: Vec<String>,
    restart: Option<J>,
    rollback: Vec<J>,
    error: Option<String>,
    /// A stdout write or flush failed: the run carries on, and the final
    /// status becomes `EXIT_STDOUT_REFUSED`.
    stdout_refused: Cell<bool>,
}

impl Report {
    fn new(json: bool) -> Self {
        Self {
            json,
            url: None,
            remote: None,
            dry_run: false,
            daemon: None,
            rows: Vec::new(),
            verification: Vec::new(),
            warnings: Vec::new(),
            notes: Vec::new(),
            restart: None,
            rollback: Vec::new(),
            error: None,
            stdout_refused: Cell::new(false),
        }
    }

    /// Write `line` to stdout, remembering a refusal for the final status.
    fn out(&self, line: &str) {
        if !emit(&mut std::io::stdout().lock(), line) {
            self.stdout_refused.set(true);
        }
    }

    /// `print(line)`. The oracle's stdout is block-buffered on a pipe, so a
    /// refused stdout raises only at its exit, after every step has run:
    /// this run carries on too, and `finish` returns the refusal's status.
    fn say(&self, line: &str) {
        if !self.json {
            self.out(line);
        }
    }

    /// `print(line, file=sys.stderr)`. The oracle's stderr is line-buffered,
    /// so a refused stderr raises right here, before any later step; false
    /// tells the caller to stop at once with `EXIT_STDERR_REFUSED`.
    #[must_use]
    fn warn(&mut self, line: String) -> bool {
        if !self.json && !emit(&mut std::io::stderr().lock(), &line) {
            return false;
        }
        self.warnings.push(line);
        true
    }

    fn note(&mut self, line: String) {
        self.say(&line);
        self.notes.push(line);
    }

    fn fail(&mut self, code: u8, line: String) -> u8 {
        if !self.json && !emit(&mut std::io::stderr().lock(), &format!("connect: {line}")) {
            return EXIT_STDERR_REFUSED;
        }
        self.error = Some(line);
        self.finish(code)
    }

    fn finish(&self, code: u8) -> u8 {
        if self.json {
            let text = |value: &Option<String>| value.clone().map_or(J::Null, J::Str);
            let strings = |values: &[String]| J::List(values.iter().cloned().map(J::Str).collect());
            let data = J::Dict(vec![
                ("url".into(), text(&self.url)),
                ("remote".into(), self.remote.map_or(J::Null, J::Bool)),
                ("dry_run".into(), J::Bool(self.dry_run)),
                (
                    "daemon".into(),
                    self.daemon.clone().map_or(J::Null, |(version, auth)| {
                        J::Dict(vec![("version".into(), version), ("auth".into(), auth)])
                    }),
                ),
                ("rows".into(), J::List(self.rows.clone())),
                ("verification".into(), J::List(self.verification.clone())),
                ("warnings".into(), strings(&self.warnings)),
                ("notes".into(), strings(&self.notes)),
                ("restart".into(), self.restart.clone().unwrap_or(J::Null)),
                ("rollback".into(), J::List(self.rollback.clone())),
                ("error".into(), text(&self.error)),
                ("exit".into(), J::Int(code.to_string())),
            ]);
            self.out(&pyjson::dumps(&data, true));
        }
        if self.stdout_refused.get() {
            EXIT_STDOUT_REFUSED
        } else {
            code
        }
    }

    fn plan(&mut self, rows: &[Row]) {
        self.rows = rows.iter().map(Row::public).collect();
        for row in rows {
            let key = row.key.as_ref().map(|key| format!("[{key}]"));
            let where_ = [row.file.as_deref(), key.as_deref()]
                .into_iter()
                .flatten()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join(" ");
            let mut line = format!("  {:<8} {} {}", row.state, row.client, row.place);
            if !where_.is_empty() {
                line.push_str(&format!(": {where_}"));
            }
            self.say(&line);
            for (key, old, new) in &row.changes {
                self.say(&format!(
                    "             {key}: {} -> {}",
                    old.as_deref().unwrap_or("(unset)"),
                    new.as_deref().unwrap_or("(removed)")
                ));
            }
            if !row.detail.is_empty() {
                self.say(&format!("             {}", row.detail));
            }
            for note in &row.notes {
                self.say(&format!("             note: {note}"));
            }
        }
    }
}

fn places(count: usize) -> String {
    format!(
        "{count} place{} need{}",
        if count != 1 { "s" } else { "" },
        if count == 1 { "s" } else { "" }
    )
}

/// `runtimes.home`-relative `--token-file` as `os.path.abspath(os.path.expanduser(...))`.
fn token_file_path(value: &str) -> Result<String, Defer> {
    let path =
        crate::credentials::absolute_expanded(std::path::Path::new(value)).map_err(|_| Defer)?;
    path.to_str().map(str::to_owned).ok_or(Defer)
}

/// `_clients(value)`: `Ok(None)` is the usage refusal.
fn clients(value: &str) -> Option<Vec<&'static str>> {
    let names: Vec<&str> = value
        .split(',')
        .map(pyjson::py_strip)
        .filter(|name| !name.is_empty())
        .collect();
    if names.is_empty() || names == ["all"] {
        return Some(CLIENTS.to_vec());
    }
    if names.iter().any(|name| !CLIENTS.contains(name)) {
        return None;
    }
    Some(
        CLIENTS
            .iter()
            .copied()
            .filter(|client| names.contains(client))
            .collect(),
    )
}

/// Run the canonical shapes natively; `None` defers before any effect.
pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    let args = args::parse(&values)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    match runtime.block_on(connect(args)) {
        Ok(code) => Some(ExitCode::from(code)),
        Err(Defer) => None,
    }
}

/// A Windows User-scope `PSEUDOLIFE_MCP_DAEMON_URL` (`HKCU\Environment`).
fn user_environment_url_set() -> Result<bool, Defer> {
    if !cfg!(windows) {
        return Ok(false);
    }
    let root = paths::env_truthy("SystemRoot")?.ok_or(Defer)?;
    let output = std::process::Command::new(PathBuf::from(root).join("System32").join("reg.exe"))
        .args(["query", r"HKCU\Environment", "/v", URL_KEY])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .map_err(|_| Defer)?;
    Ok(output.status.success())
}

/// `connect_cli.scheduled_update()` found a schedule on this machine.
fn schedule_found() -> Result<bool, Defer> {
    if cfg!(windows) {
        let status = std::process::Command::new("schtasks")
            .args(["/Query", "/TN", "Pseudolife Unattended Update", "/XML"])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
        return Ok(status.is_ok_and(|status| status.success()));
    }
    if cfg!(target_os = "linux") {
        let base = match paths::env_truthy("XDG_CONFIG_HOME")? {
            Some(value) => PathBuf::from(value),
            None => PathBuf::from(paths::env_truthy("HOME")?.ok_or(Defer)?).join(".config"),
        };
        let unit = base
            .join("systemd")
            .join("user")
            .join("pseudolife-update.service");
        return Ok(std::fs::read_to_string(unit).is_ok());
    }
    Ok(false)
}

/// urllib sends a DEL (0x7F) inside a bearer; reqwest's header type refuses
/// it, so a real run whose credentials hold one defers. Reading a token
/// file early is unobservable; a file that fails the oracle's own check
/// is left to verification, which reports it exactly.
fn tokens_sendable(rows: &[Row]) -> bool {
    rows.iter()
        .filter(|row| matches!(row.state, "current" | "change"))
        .filter_map(|row| row.credential.clone().flatten())
        .all(|credential| match credential {
            Cred::Literal(token) => !token.contains('\u{7f}'),
            Cred::File(path) => {
                crate::credentials::CredentialProvider::new(None, Some(path.into()))
                    .and_then(|provider| provider.snapshot())
                    .map_or(true, |snapshot| {
                        !snapshot.token().unwrap_or_default().contains('\u{7f}')
                    })
            }
            Cred::None => true,
        })
}

fn shown_credential(credential: &Cred, label: &str) -> String {
    match credential {
        Cred::File(value) => format!("token file {value}"),
        Cred::Literal(_) => format!("the literal {TOKEN_KEY} of {label}"),
        Cred::None => "no token".to_owned(),
    }
}

/// `client_config.check_token_file`: the token, or the recovery text.
fn check_token_file(value: &str) -> Result<String, String> {
    let snapshot = crate::credentials::CredentialProvider::new(None, Some(value.into()))
        .and_then(|provider| provider.snapshot())
        .map_err(|error| error.0.to_owned())?;
    let token = snapshot.token().unwrap_or_default().to_owned();
    if token.starts_with('\u{feff}') {
        return Err("the file starts with a UTF-8 byte-order mark, which the shim would send as part of the token; write it without one".to_owned());
    }
    Ok(token)
}

/// `connect_cli.verify`: `(results, warnings)` or the first refusal.
async fn verify(ctx: &Ctx, rows: &[Row], auth: bool) -> Result<(Vec<J>, Vec<String>), String> {
    let mut credentials: Vec<(Cred, String)> = Vec::new();
    for row in rows {
        if !matches!(row.state, "current" | "change") {
            continue;
        }
        let Some(credential) = &row.credential else {
            continue;
        };
        let Some(credential) = credential else {
            if auth && row.place == "registration" {
                return Err(format!(
                    "{}: the registration in {} has no credential and the daemon requires one; pass --token-file <owner-only file holding its token>",
                    row.client,
                    row.file.as_deref().unwrap_or("None")
                ));
            }
            continue;
        };
        if !credentials.iter().any(|(known, _)| known == credential) {
            credentials.push((
                credential.clone(),
                format!(
                    "{} {} ({})",
                    row.client,
                    row.place,
                    row.file.as_deref().unwrap_or("None")
                ),
            ));
        }
    }
    if credentials.is_empty() {
        credentials.push((Cred::None, "no credential".to_owned()));
    }
    let mut results = Vec::new();
    let mut warnings = Vec::new();
    for (credential, label) in credentials {
        let shown = shown_credential(&credential, &label);
        let token = match &credential {
            Cred::File(value) => {
                Some(check_token_file(value).map_err(|recovery| format!("{shown}: {recovery}"))?)
            }
            Cred::Literal(value) => {
                if crate::credentials::decode_token(value.as_bytes()).is_err() {
                    return Err(format!("{shown} is not a well-formed token"));
                }
                Some(value.clone())
            }
            Cred::None => None,
        };
        if let Some(token) = &token
            && !net::credential_valid(&ctx.url, token).await
        {
            return Err(format!(
                "the daemon at {} refused {shown} (an authenticated request that follows no redirects did not succeed)",
                ctx.url
            ));
        }
        let tools = net::handshake(&ctx.url, &credential).await.map_err(|error| {
            format!(
                "the MCP handshake with {shown} failed ({error}); check the daemon's MCP access and compare daemon and shim versions"
            )
        })?;
        let board = net::board_line(&ctx.url, token.as_deref()).await;
        if !board.starts_with("on") {
            warnings.push(format!(
                "the agent board for {shown}: {board}. Memory works without the board; {BOARD_HINT}"
            ));
        }
        results.push(J::Dict(vec![
            ("credential".into(), J::Str(shown)),
            ("tools".into(), J::Int(tools.to_string())),
            ("board".into(), J::Str(board)),
        ]));
    }
    Ok((results, warnings))
}

struct Step {
    path: PathBuf,
    original: Option<Vec<u8>>,
    written: Option<Vec<u8>>,
    backup: Option<String>,
}

struct WriteFailed {
    path: PathBuf,
    reason: String,
}

/// `_apply_json`'s read-back: `Ok(Some(reason))` raises `_WriteFailed`
/// as it stands; `Err` takes the except branch, which refreshes `written`.
fn read_back(path: &std::path::Path, data: &pyjson::Dict) -> Result<Option<String>, files::Fail> {
    let differs = || {
        Ok(Some(
            "the read-back differs from what was written".to_owned(),
        ))
    };
    match files::load_json(path) {
        Ok(files::Read::Ok(read)) => {
            if pyjson::py_eq(&J::Dict(read), &J::Dict(data.clone())) {
                Ok(None)
            } else {
                differs()
            }
        }
        // HelperError from _load_json is caught like any write failure.
        Ok(files::Read::Error(message)) => Err(files::Fail::Helper(message)),
        // A missing file reads as {}; Python's reading of what this reader
        // cannot hold (a lone surrogate, deep nesting) is not what was written.
        Ok(files::Read::Missing) | Err(Defer) => differs(),
    }
}

/// `connect_cli._apply_json`.
fn apply_json(row: &mut Row, steps: &mut Vec<Step>) -> Result<(), WriteFailed> {
    let spec = row.json.as_ref().expect("a change row of a JSON file");
    let path = spec.path.clone();
    // The preflight read every target before any effect; a read error now
    // is a change under the run (where the oracle's `_bytes` would raise).
    let original = match files::bytes(&path) {
        Ok(original) => original,
        Err(error) => {
            return Err(WriteFailed {
                reason: files::os_error_name(&error).to_owned(),
                path,
            });
        }
    };
    steps.push(Step {
        path: path.clone(),
        original,
        written: None,
        backup: None,
    });
    let record = steps.len() - 1;
    let pointer = spec.pointer.clone();
    let edits = spec.edits.clone();
    let mut backup = None;
    let mut created = false;
    let outcome = (|| -> Result<Option<String>, files::Fail> {
        let mut data = match files::load_json(&path) {
            Ok(files::Read::Ok(data)) => data,
            Ok(files::Read::Error(message)) => return Err(files::Fail::Helper(message)),
            Ok(files::Read::Missing) => Vec::new(),
            Err(Defer) => return Err(files::Fail::Name("ValueError")),
        };
        let (last, parents) = pointer.split_last().expect("a pointer");
        let mut node = &mut data;
        for part in parents {
            node = match pyjson::get_mut(node, part) {
                Some(J::Dict(child)) => child,
                Some(_) => return Err(files::Fail::Name("TypeError")),
                None => return Err(files::Fail::Name("KeyError")),
            };
        }
        let env = match node.iter().find(|(k, _)| k == last).map(|(_, v)| v) {
            None | Some(J::Null) => Vec::new(),
            Some(J::Dict(env)) => env.clone(),
            Some(_) => {
                return Err(files::Fail::Helper(format!(
                    "{} is not a JSON object",
                    pointer.join(".")
                )));
            }
        };
        pyjson::set(node, last, J::Dict(discover::after(&env, &edits)));
        if steps[record].original.is_some() {
            backup = Some(files::backup(&path)?);
        } else {
            created = true;
        }
        files::write_json(&path, &data)?;
        steps[record].written = files::bytes(&path).ok().flatten();
        read_back(&path, &data)
    })();
    steps[record].backup = backup.clone();
    row.backup = backup;
    row.created |= created;
    match outcome {
        Ok(None) => Ok(()),
        Ok(Some(reason)) => Err(WriteFailed { path, reason }),
        Err(fail) => {
            steps[record].written = files::bytes(&path).ok().flatten();
            Err(WriteFailed {
                path,
                reason: match fail {
                    files::Fail::Helper(message) => message,
                    files::Fail::Name(name) => name.to_owned(),
                },
            })
        }
    }
}

fn show(path: &std::path::Path) -> String {
    path.to_string_lossy().into_owned()
}

/// `connect_cli._rollback`.
fn rollback(steps: &[Step], created: &[PathBuf]) -> Vec<J> {
    let mut outcome = Vec::new();
    let text = |value: &Option<String>| value.clone().map_or(J::Null, J::Str);
    for record in steps.iter().rev() {
        let file = J::Str(show(&record.path));
        if record.written == record.original {
            if record.backup.is_some() {
                outcome.push(J::Dict(vec![
                    ("file".into(), file),
                    ("state".into(), J::Str("unchanged".into())),
                    ("backup".into(), text(&record.backup)),
                    (
                        "detail".into(),
                        J::Str("not changed by this run; its backup can be deleted".into()),
                    ),
                ]));
            }
            continue;
        }
        // A file that cannot be read now is left as it is, with its backup.
        if !matches!(files::bytes(&record.path), Ok(current) if current == record.written) {
            outcome.push(J::Dict(vec![
                ("file".into(), file),
                ("state".into(), J::Str("left".into())),
                ("backup".into(), text(&record.backup)),
                (
                    "detail".into(),
                    J::Str(
                        "changed under the edit since this run wrote it; left as it is now, backup kept"
                            .into(),
                    ),
                ),
            ]));
            continue;
        }
        let restored = match &record.original {
            None => std::fs::remove_file(&record.path)
                .map(|()| {
                    J::Dict(vec![
                        ("file".into(), file.clone()),
                        ("state".into(), J::Str("removed".into())),
                    ])
                })
                .map_err(|error| files::os_error_name(&error)),
            Some(original) => files::write_private(&record.path, original)
                .map(|()| {
                    J::Dict(vec![
                        ("file".into(), file.clone()),
                        ("state".into(), J::Str("restored".into())),
                        ("backup".into(), text(&record.backup)),
                    ])
                })
                .map_err(|fail| match fail {
                    files::Fail::Name(name) => name,
                    files::Fail::Helper(_) => "OSError",
                }),
        };
        outcome.push(restored.unwrap_or_else(|name| {
            J::Dict(vec![
                ("file".into(), file),
                ("state".into(), J::Str("failed".into())),
                ("backup".into(), text(&record.backup)),
                (
                    "detail".into(),
                    J::Str(format!(
                        "could not be restored ({name}); restore it from its backup"
                    )),
                ),
            ])
        }));
    }
    for path in created {
        match std::fs::remove_file(path) {
            Ok(()) => outcome.push(J::Dict(vec![
                ("file".into(), J::Str(show(path))),
                ("state".into(), J::Str("removed".into())),
            ])),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => outcome.push(J::Dict(vec![
                ("file".into(), J::Str(show(path))),
                ("state".into(), J::Str("failed".into())),
                (
                    "detail".into(),
                    J::Str(format!(
                        "could not be removed ({})",
                        files::os_error_name(&error)
                    )),
                ),
            ])),
        }
    }
    outcome
}

/// `connect_cli._restart`.
fn restart(ctx: &Ctx, rows: &[Row]) -> J {
    let desktop = rows
        .iter()
        .any(|row| row.client == "claude-desktop" && row.state == "change");
    let table = procs::list().ok().flatten();
    let (pids, detail) = match (table, &ctx.layout) {
        (Some(table), Some(layout)) => {
            let held = paths::forms(&layout.root);
            let own = [Some(std::process::id()), procs::parent_pid(&table)];
            let mut pids: Vec<u32> = table
                .iter()
                .filter(|(pid, _, image)| {
                    !own.contains(&Some(*pid)) && paths::inside(image, &held)
                })
                .map(|(pid, _, _)| *pid)
                .collect();
            pids.sort_unstable();
            let detail = if pids.is_empty() {
                "no process runs a shim runtime".to_owned()
            } else {
                format!(
                    "{} process{} run a shim runtime ({}): restart those sessions; each keeps the old daemon until it restarts",
                    pids.len(),
                    if pids.len() != 1 { "es" } else { "" },
                    pids.iter().map(u32::to_string).collect::<Vec<_>>().join(", ")
                )
            };
            (pids, detail)
        }
        _ => (
            Vec::new(),
            "the process table could not be read: restart every running Claude Code, Codex and Gemini CLI session".to_owned(),
        ),
    };
    J::Dict(vec![
        (
            "pids".into(),
            J::List(pids.iter().map(|pid| J::Int(pid.to_string())).collect()),
        ),
        ("claude_desktop".into(), J::Bool(desktop)),
        ("detail".into(), J::Str(detail)),
    ])
}

fn health_ok(health: &Option<J>) -> bool {
    health
        .as_ref()
        .and_then(J::as_dict)
        .and_then(|health| pyjson::get(health, "status"))
        .and_then(J::as_str)
        == Some("ok")
}

/// Read stdin's first bytes the way `stream.read(MAX_TOKEN_BYTES + 3)` does.
fn stdin_token() -> Vec<u8> {
    let mut data = Vec::new();
    let _ = std::io::stdin()
        .lock()
        .take((crate::credentials::MAX_TOKEN_BYTES + 3) as u64)
        .read_to_end(&mut data);
    data
}

async fn connect(args: args::Args) -> Result<u8, Defer> {
    // An interactive question or token prompt is never answered natively.
    if (!args.dry_run && !args.yes || args.read_token) && procs::stdin_is_tty() {
        return Err(Defer);
    }
    // ---- the plan: nothing below may defer once anything is printed ----
    let url = match url::validated(&args.url)? {
        Ok(url) => url,
        Err(()) => {
            let shown = url::shown(&args.url)?;
            return Ok(Report::new(args.json).fail(
                EXIT_USAGE,
                format!(
                    "the daemon URL given ({shown}) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials"
                ),
            ));
        }
    };
    let client_value = args.client.clone().unwrap_or_else(|| "all".to_owned());
    let Some(selected) = clients(&client_value) else {
        let shown = pyjson::py_repr(&client_value);
        return Ok(Report::new(args.json).fail(
            EXIT_USAGE,
            format!("--client takes {} or all (got {shown})", CLIENTS.join(", ")),
        ));
    };
    if args.read_token && args.token_file.is_none() {
        return Ok(Report::new(args.json).fail(
            EXIT_USAGE,
            "--read-token creates the file --token-file names; pass both".to_owned(),
        ));
    }
    let token_file = args
        .token_file
        .as_deref()
        .map(token_file_path)
        .transpose()?;
    if args.read_token
        && let Some(token_file) = &token_file
        && std::fs::symlink_metadata(token_file).is_ok()
    {
        return Ok(Report::new(args.json).fail(
            EXIT_USAGE,
            format!(
                "{token_file} already exists; --read-token creates a token file but never replaces one"
            ),
        ));
    }
    let remote = !url::is_loopback(&url)?;
    let all_clients = selected.len() == CLIENTS.len();
    if user_environment_url_set()? || (all_clients && schedule_found()?) {
        return Err(Defer);
    }
    if selected.contains(&"codex") && std::fs::read(paths::codex_config()?).is_ok() {
        return Err(Defer);
    }
    // Discovery reads only files and prints nothing, so it runs (and may
    // defer) before any request; the oracle runs it after /health.
    let ctx = Ctx {
        url: url.clone(),
        remote,
        token_file: token_file.clone(),
        clients: selected.clone(),
        layout: paths::layout()?,
    };
    let mut rows = discover::discover(&ctx)?;
    // `_bytes` raises on anything but FileNotFoundError; read every target
    // now so such a file defers before any effect.
    for row in rows.iter().filter(|row| row.state == "change") {
        if let Some(spec) = &row.json
            && files::bytes(&spec.path).is_err()
        {
            return Err(Defer);
        }
    }

    if !args.dry_run && !tokens_sendable(&rows) {
        return Err(Defer);
    }

    let mut report = Report::new(args.json);
    report.url = Some(url.clone());
    report.remote = Some(remote);
    report.dry_run = args.dry_run;
    let health = net::probe_health(&url).await?;
    let health_dict = health.as_ref().and_then(J::as_dict).cloned();
    let Some(health_dict) = health_dict.filter(|_| health_ok(&health)) else {
        let status = match health.as_ref().and_then(J::as_dict) {
            Some(dict) => format!(
                " (status: {})",
                pyjson::get(dict, "status").map_or(Ok("None".to_owned()), J::py_str)?
            ),
            None => String::new(),
        };
        return Ok(report.fail(
            EXIT_REFUSED,
            format!(
                "the daemon at {url} did not answer /health with status ok{status}; nothing was changed"
            ),
        ));
    };
    let auth = pyjson::get(&health_dict, "auth").cloned();
    if remote && auth == Some(J::Bool(false)) {
        return Ok(report.fail(
            EXIT_REFUSED,
            format!(
                "the daemon at {url} runs without a bearer token (auth: false). An unauthenticated bank must never be reached over a network: set PSEUDOLIFE_MCP_TOKEN for the daemon on its host, restart it, and re-run. Nothing was changed"
            ),
        ));
    }
    let version = pyjson::get(&health_dict, "version").cloned();
    let version_shown = match &version {
        Some(value) if value.truthy() => value.py_str()?,
        _ => "unknown".to_owned(),
    };

    // ---- from here on the run prints, and never defers ----
    report.daemon = Some((version.unwrap_or(J::Null), auth.clone().unwrap_or(J::Null)));
    if remote
        && url.starts_with("http://")
        && !report.warn(format!(
            "WARNING: {url} is plain HTTP: the link itself is unencrypted, so it must be a private network such as a tailnet, or a TLS reverse proxy must front the daemon."
        ))
    {
        return Ok(EXIT_STDERR_REFUSED);
    }
    report.say(&format!(
        "connect: {url} ({}; daemon {version_shown}, auth {})",
        if remote {
            "another machine"
        } else {
            "this machine"
        },
        if auth.as_ref().is_some_and(J::truthy) {
            "on"
        } else {
            "off"
        }
    ));
    report.plan(&rows);
    let manual = rows.iter().filter(|row| row.state == "manual").count();
    let client_row =
        |row: &Row, states: &[&str]| CLIENTS.contains(&row.client) && states.contains(&row.state);
    if !rows
        .iter()
        .any(|row| client_row(row, &["current", "change"]))
    {
        let names = selected.join(", ");
        if rows.iter().any(|row| client_row(row, &["manual"])) {
            return Ok(report.fail(
                EXIT_NOTHING,
                format!(
                    "no registration connect can write was found for {names}: {} manual action (listed above).",
                    places(manual)
                ),
            ));
        }
        return Ok(report.fail(
            EXIT_NOTHING,
            format!(
                "no registration of the shim was found for {names}; register a client first (the commands are above), or run the installer."
            ),
        ));
    }
    let changes = rows.iter().filter(|row| row.state == "change").count();
    if args.dry_run {
        report.note(
            "dry run: nothing was written and no token was sent; the credentials are verified only on a real run"
                .to_owned(),
        );
        return Ok(report.finish(EXIT_OK));
    }
    if changes == 0 {
        report.note(if manual > 0 {
            format!(
                "{} manual action (listed above); nothing else to write",
                places(manual)
            )
        } else {
            format!("every place already names {url}; nothing to write")
        });
        return Ok(report.finish(EXIT_OK));
    }
    if !args.yes {
        return Ok(report.fail(
            EXIT_USAGE,
            "this run is not interactive: re-run with --yes to apply the plan above (nothing was changed)"
                .to_owned(),
        ));
    }
    let mut created: Vec<PathBuf> = Vec::new();
    if args.read_token
        && let Some(token_file) = &token_file
    {
        // Read only now, just before the write, as the oracle reads it.
        let data = stdin_token();
        match files::write_token_file(std::path::Path::new(token_file), &data) {
            Ok(()) => created.push(PathBuf::from(token_file)),
            Err(files::TokenFail::Helper(message)) => {
                return Ok(report.fail(EXIT_USAGE, format!("{message}; nothing was changed")));
            }
            Err(files::TokenFail::Name(name)) => {
                return Ok(report.fail(
                    EXIT_USAGE,
                    format!(
                        "{name} while writing {token_file}; check its directory's permissions. Nothing was changed"
                    ),
                ));
            }
        }
    }

    // ---- verify every credential against the target ----
    let auth_required = auth != Some(J::Bool(false));
    let (results, warnings) = match verify(&ctx, &rows, auth_required).await {
        Ok(found) => found,
        Err(failure) => {
            for path in &created {
                let _ = std::fs::remove_file(path);
            }
            return Ok(report.fail(EXIT_REFUSED, format!("{failure}. Nothing was written.")));
        }
    };
    report.verification = results.clone();
    for line in warnings {
        if !report.warn(line) {
            // The oracle stops here: nothing is written, and a token file
            // this run created stays.
            return Ok(EXIT_STDERR_REFUSED);
        }
    }

    // ---- apply, all or nothing ----
    let mut steps = Vec::new();
    for row in rows.iter_mut() {
        if row.state != "change" || row.json.is_none() {
            continue;
        }
        if let Err(failure) = apply_json(row, &mut steps) {
            let outcome = rollback(&steps, &created);
            for item in &outcome {
                let item = item.as_dict().expect("an outcome");
                let field = |key: &str| {
                    pyjson::get(item, key)
                        .and_then(J::as_str)
                        .map(str::to_owned)
                };
                let state = field("state").unwrap_or_default();
                let mut line = format!("  {state:<8} {}", field("file").unwrap_or_default());
                if let Some(detail) = field("detail").filter(|d| !d.is_empty()) {
                    line.push_str(&format!(": {detail}"));
                }
                if let Some(backup) = field("backup").filter(|b| !b.is_empty())
                    && state != "restored"
                {
                    line.push_str(&format!(" (backup {backup})"));
                }
                report.say(&line);
            }
            report.rollback = outcome;
            return Ok(report.fail(
                EXIT_FAILED,
                format!(
                    "writing {} failed ({}); every file this run wrote was rolled back as listed above.",
                    show(&failure.path),
                    failure.reason
                ),
            ));
        }
    }

    // ---- report and check ----
    report.rows = rows.iter().map(Row::public).collect();
    for row in &rows {
        if let Some(backup) = &row.backup {
            report.say(&format!("  backup: {backup}"));
        }
    }
    for result in &results {
        let result = result.as_dict().expect("a result");
        let field = |key: &str| match pyjson::get(result, key) {
            Some(J::Str(text)) => text.clone(),
            Some(J::Int(text)) => text.clone(),
            _ => String::new(),
        };
        report.say(&format!(
            "  verified: {}: {} tools; board {}",
            field("credential"),
            field("tools"),
            field("board")
        ));
    }
    let restarted = restart(&ctx, &rows);
    let detail = restarted
        .as_dict()
        .and_then(|r| pyjson::get(r, "detail"))
        .and_then(J::as_str)
        .unwrap_or_default()
        .to_owned();
    let desktop = restarted
        .as_dict()
        .and_then(|r| pyjson::get(r, "claude_desktop"))
        .is_some_and(J::truthy);
    report.restart = Some(restarted);
    report.note(format!("restart: {detail}"));
    if desktop {
        report.note(
            "restart Claude Desktop: fully quit it (tray/menu-bar icon, not just the window) and relaunch it"
                .to_owned(),
        );
    }
    report.note(format!(
        "the agent board keys each session's address by the daemon URL, so sessions get new addresses on {url}; the old state stays in place, and peers see this machine's sessions under the new ones"
    ));
    let pending: Vec<String> = match discover::discover(&ctx) {
        Ok(again) => again
            .iter()
            .filter(|row| CLIENTS.contains(&row.client) && row.state == "change")
            .map(|row| {
                format!(
                    "{} {} ({})",
                    row.client,
                    row.place,
                    row.file.as_deref().unwrap_or("None")
                )
            })
            .collect(),
        Err(Defer) => vec!["the post-apply re-read".to_owned()],
    };
    let after = net::probe_health(&url).await.unwrap_or(None);
    if !pending.is_empty() || !health_ok(&after) {
        let what = if pending.is_empty() {
            format!("the daemon at {url} stopped answering /health")
        } else {
            pending.join(", ")
        };
        return Ok(report.fail(
            EXIT_UNVERIFIED,
            format!(
                "applied, but the post-apply check failed: {what}. The backups are listed above; restore them or re-run connect"
            ),
        ));
    }
    report.note(
        "post-apply check: every place names the daemon, and it answers /health; run `pseudolife-mcp doctor` from a client's environment for the full check"
            .to_owned(),
    );
    Ok(report.finish(EXIT_OK))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str, text: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("connect-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        std::fs::write(&path, text).unwrap();
        path
    }

    #[test]
    fn a_read_back_that_is_not_json_takes_the_helper_error_branch() {
        // C1: _load_json raises HelperError, caught as a write failure whose
        // except branch refreshes `written` before the rollback compares it.
        let path = scratch("settings.json", "{\"env\": ");
        let data = pyjson::loads("{\"env\": {}}").unwrap().unwrap();
        let data = data.as_dict().unwrap();
        match read_back(&path, data) {
            Err(files::Fail::Helper(message)) => {
                assert_eq!(
                    message,
                    "settings.json is not readable JSON; fix it, then re-run"
                )
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_read_back_compares_as_python_dicts_do() {
        // C3: key order and 1 versus 1.0 do not make Python dicts unequal.
        let path = scratch("x.json", "{\"b\": 1.0, \"a\": {\"c\": true}}");
        let data = pyjson::loads("{\"a\": {\"c\": 1}, \"b\": 1}")
            .unwrap()
            .unwrap();
        assert!(matches!(
            read_back(&path, data.as_dict().unwrap()),
            Ok(None)
        ));
        let path = scratch("y.json", "{\"a\": 2}");
        let data = pyjson::loads("{\"a\": 1}").unwrap().unwrap();
        assert!(matches!(
            read_back(&path, data.as_dict().unwrap()),
            Ok(Some(_))
        ));
    }

    #[test]
    fn client_lists_follow_the_oracle() {
        assert_eq!(clients("all").unwrap(), CLIENTS.to_vec());
        assert_eq!(clients(" , ").unwrap(), CLIENTS.to_vec());
        assert_eq!(
            clients("gemini, claude-code").unwrap(),
            vec!["claude-code", "gemini"]
        );
        assert!(clients("all,codex").is_none());
        assert!(clients("cursor").is_none());
        assert_eq!(places(1), "1 place needs");
        assert_eq!(places(2), "2 places need");
    }
}
