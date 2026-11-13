//! `installer claude-desktop`: register the native client in Claude
//! Desktop's `claude_desktop_config.json` (Desktop has no `mcp add`).
//!
//! What shapes the entry, `mcpServers.pseudolife-desktop`:
//! * Desktop's Code tab runs Claude Code, whose own per-session server is
//!   `pseudolife-memory`; an app-level entry of that name would capture the
//!   session's calls. An entry this step wrote under the old name (its env
//!   sets `PSEUDOLIFE_WRITER_ID=claude-desktop`) moves to the new one with
//!   its other settings; a `pseudolife-memory` entry it did not write stays
//!   and is reported.
//! * Desktop starts servers with a sanitized environment, so the bearer
//!   reaches the client only as `PSEUDOLIFE_MCP_TOKEN_FILE` in the entry's
//!   `env`: a private file this step wrote or validated, never the token.
//!   A literal `PSEUDOLIFE_MCP_TOKEN` already in the entry moves into that
//!   file and leaves the configuration.
//! * The sanitized PATH means `command` must be absolute.
//! * The Store (MSIX) Desktop reads its configuration from
//!   `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`; that copy
//!   wins where it exists, and another existing copy is named.
//!
//! The configuration is replaced atomically through the shared client
//! configuration writer (a private backup of the displaced file); the token
//! file is written owner-only. A rerun that changes nothing says so in one
//! line. Exit 0 registered, 2 refused (nothing written), 3 registered
//! WITHOUT a credential a token-gated daemon needs, 4 refused because the
//! client at `--command` cannot read a token file. `--json` prints one
//! report on stdout (`state`, `exit`, `config`, `backup`, `notes`) and puts
//! the human lines on stderr.
use crate::credentials::CredentialProvider;
use crate::runtime::registrations;
use serde_json::{Map, Value, json};
use std::{
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub(super) const USAGE: &str = "usage: pseudolife-mcp installer claude-desktop --command ABSOLUTE_PATH [--writer-id ID] [--daemon-url URL] [--token-file PATH | --default-token-file PATH] [--token-from-env VAR] [--tokens-from-env VAR] [--config PATH] [--dry-run] [--skip-shim-check] [--json]";

pub(super) const SERVER: &str = "pseudolife-desktop";
/// The entry's name before 2026-09-25, and Claude Code's per-session name.
const LEGACY_SERVER: &str = "pseudolife-memory";
const DEFAULT_WRITER_ID: &str = "claude-desktop";
const DEFAULT_DAEMON_URL: &str = "http://127.0.0.1:8765";
/// The env keys this step owns on the entry; anything else is kept.
const MANAGED_ENV: [&str; 4] = [
    "PSEUDOLIFE_WRITER_ID",
    "PSEUDOLIFE_MCP_NO_SPAWN",
    "PSEUDOLIFE_MCP_DAEMON_URL",
    "PSEUDOLIFE_MCP_TOKEN_FILE",
];
const FILE_KEY: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
const LITERAL_KEY: &str = "PSEUDOLIFE_MCP_TOKEN";
/// What a client able to read a token file names in its `--help`.
pub(super) const TOKEN_FILE_MARKER: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
const PROBE_CREDENTIAL_ENV: [&str; 3] = [
    LITERAL_KEY,
    "PSEUDOLIFE_MCP_TOKENS",
    "PSEUDOLIFE_MCP_TOKEN_FILE",
];
const PROBE_TIMEOUT: Duration = Duration::from_secs(30);

pub(super) const EXIT_OK: u8 = 0;
pub(super) const EXIT_REFUSED: u8 = 2;
pub(super) const EXIT_NO_CREDENTIAL: u8 = 3;
pub(super) const EXIT_OLD_SHIM: u8 = 4;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(super) struct Options {
    pub command: String,
    pub writer_id: String,
    pub daemon_url: String,
    pub token_file: Option<String>,
    pub default_token_file: Option<String>,
    pub token_from_env: Option<String>,
    pub tokens_from_env: Option<String>,
    pub config: Option<PathBuf>,
    pub dry_run: bool,
    pub skip_shim_check: bool,
    pub json: bool,
}

pub(super) fn parse(arguments: &[String]) -> Result<Option<Options>, String> {
    let mut options = Options::default();
    let mut command = None;
    let mut writer = None;
    let mut url = None;
    let mut items = arguments.iter();
    while let Some(item) = items.next() {
        let mut value = |name: &str| -> Result<String, String> {
            items
                .next()
                .cloned()
                .ok_or_else(|| format!("{name} needs a value"))
        };
        let repeated = |set: bool| -> Result<(), String> {
            if set {
                Err(format!("{item} was given twice"))
            } else {
                Ok(())
            }
        };
        match item.as_str() {
            "--help" | "-h" => return Ok(None),
            "--command" => {
                repeated(command.is_some())?;
                command = Some(value("--command")?);
            }
            "--writer-id" => {
                repeated(writer.is_some())?;
                writer = Some(value("--writer-id")?);
            }
            "--daemon-url" => {
                repeated(url.is_some())?;
                url = Some(value("--daemon-url")?);
            }
            "--token-file" => {
                repeated(options.token_file.is_some())?;
                options.token_file = Some(value("--token-file")?);
            }
            "--default-token-file" => {
                repeated(options.default_token_file.is_some())?;
                options.default_token_file = Some(value("--default-token-file")?);
            }
            "--token-from-env" => {
                repeated(options.token_from_env.is_some())?;
                options.token_from_env = Some(value("--token-from-env")?);
            }
            "--tokens-from-env" => {
                repeated(options.tokens_from_env.is_some())?;
                options.tokens_from_env = Some(value("--tokens-from-env")?);
            }
            "--config" => {
                repeated(options.config.is_some())?;
                options.config = Some(PathBuf::from(value("--config")?));
            }
            "--dry-run" => {
                repeated(options.dry_run)?;
                options.dry_run = true;
            }
            "--skip-shim-check" => {
                repeated(options.skip_shim_check)?;
                options.skip_shim_check = true;
            }
            "--json" => {
                repeated(options.json)?;
                options.json = true;
            }
            other => return Err(format!("unknown option {other:?}")),
        }
    }
    if options.token_file.is_some() && options.default_token_file.is_some() {
        return Err("--token-file and --default-token-file are alternatives; pass one".into());
    }
    options.command = command.ok_or("--command is required")?;
    options.writer_id = writer.unwrap_or_else(|| DEFAULT_WRITER_ID.into());
    options.daemon_url = url.unwrap_or_else(|| DEFAULT_DAEMON_URL.into());
    Ok(Some(options))
}

/// Either operating system's absolute form counts (the installers pass the
/// host's); a bare name or a relative path does not.
fn is_absolute(path: &str) -> bool {
    let bytes = path.as_bytes();
    path.starts_with('/')
        || path.starts_with("\\\\")
        || (bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && matches!(bytes[2], b'\\' | b'/'))
}

/// Where Desktop's configuration may live, most authoritative first. On
/// Windows: each MSIX package cache that exists, then `%APPDATA%`.
pub(super) fn config_candidates(
    windows: bool,
    macos: bool,
    lookup: &dyn Fn(&str) -> Option<String>,
    home: &Path,
) -> Vec<PathBuf> {
    let file = "claude_desktop_config.json";
    if windows {
        let mut found = Vec::new();
        if let Some(local) = lookup("LOCALAPPDATA").filter(|v| !v.is_empty()) {
            let packages = PathBuf::from(local).join("Packages");
            let mut families: Vec<PathBuf> = std::fs::read_dir(&packages)
                .into_iter()
                .flatten()
                .filter_map(Result::ok)
                .filter(|e| e.file_name().to_string_lossy().starts_with("Claude_"))
                .map(|e| e.path())
                .collect();
            families.sort();
            for family in families {
                let cache = family.join("LocalCache").join("Roaming").join("Claude");
                if cache.is_dir() {
                    found.push(cache.join(file));
                }
            }
        }
        let appdata = lookup("APPDATA")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Roaming"));
        found.push(appdata.join("Claude").join(file));
        return found;
    }
    if macos {
        return vec![
            home.join("Library")
                .join("Application Support")
                .join("Claude")
                .join(file),
        ];
    }
    let base = lookup("XDG_CONFIG_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    vec![base.join("Claude").join(file)]
}

/// A refusal: nothing was written. The message names the cause and the fix.
#[derive(Debug)]
pub(super) struct Refusal(pub String);

fn refuse<T>(message: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal(message.into()))
}

/// The configuration as JSON, `{}` for a missing or blank file. A UTF-8
/// byte-order mark is accepted on read and never written.
fn load(path: &Path) -> Result<(Option<Vec<u8>>, Map<String, Value>), Refusal> {
    let bytes = registrations::read_optional(path).map_err(|error| {
        Refusal(format!(
            "{} could not be read ({error}); fix or move it first",
            path.display()
        ))
    })?;
    let Some(raw) = bytes.clone() else {
        return Ok((None, Map::new()));
    };
    let text = std::str::from_utf8(raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&raw))
        .map_err(|_| {
            Refusal(format!(
                "{} is not UTF-8 JSON; fix or move it first",
                path.display()
            ))
        })?;
    if text.trim().is_empty() {
        return Ok((bytes, Map::new()));
    }
    let value: Value = serde_json::from_str(text).map_err(|error| {
        Refusal(format!(
            "{} is not valid JSON ({error}); fix or move it first",
            path.display()
        ))
    })?;
    match value {
        Value::Object(map) => Ok((bytes, map)),
        _ => refuse(format!(
            "{} is not a JSON object; refusing to rewrite it",
            path.display()
        )),
    }
}

fn written_by_us(entry: &Value) -> bool {
    entry["env"]["PSEUDOLIFE_WRITER_ID"] == DEFAULT_WRITER_ID
}

/// Move an entry this step wrote as `pseudolife-memory` to [`SERVER`]. The
/// [`SERVER`] entry wins where both set a key; the notes name any dropped
/// env key, never a value. `foreign` reports an entry someone else wrote.
fn migrate_legacy(
    config: &Map<String, Value>,
    dry_run: bool,
) -> Result<(Map<String, Value>, Vec<String>, bool), Refusal> {
    let Some(servers) = config.get("mcpServers").and_then(Value::as_object) else {
        return Ok((config.clone(), vec![], false));
    };
    let Some(legacy) = servers.get(LEGACY_SERVER) else {
        return Ok((config.clone(), vec![], false));
    };
    if !written_by_us(legacy) {
        return Ok((config.clone(), vec![], true));
    }
    let mut merged = config.clone();
    let servers = merged["mcpServers"].as_object_mut().expect("checked above");
    let mut moved = servers.shift_remove(LEGACY_SERVER).expect("checked above");
    let mut conflicts = Vec::new();
    if let Some(target) = servers.get(SERVER) {
        let Some(target) = target.as_object() else {
            return refuse(format!(
                "mcpServers.{SERVER} is not a JSON object; refusing to rewrite it"
            ));
        };
        let target_env = match target.get("env") {
            None | Some(Value::Null) => Map::new(),
            Some(Value::Object(env)) => env.clone(),
            Some(_) => {
                return refuse(format!(
                    "mcpServers.{SERVER}.env is not a JSON object; refusing to rewrite it"
                ));
            }
        };
        let moved_env = moved["env"].as_object().cloned().unwrap_or_default();
        // A managed key other than the token file is rewritten every run,
        // so a conflict there loses nothing worth naming.
        conflicts = moved_env
            .iter()
            .filter(|(key, value)| {
                target_env.get(*key).is_some_and(|other| other != *value)
                    && (!MANAGED_ENV.contains(&key.as_str()) || key.as_str() == FILE_KEY)
            })
            .map(|(key, _)| key.clone())
            .collect();
        conflicts.sort();
        let mut env = moved_env;
        env.extend(target_env);
        let mut combined = moved.as_object().cloned().unwrap_or_default();
        combined.extend(target.clone());
        combined.insert("env".into(), Value::Object(env));
        moved = Value::Object(combined);
    }
    servers.insert(SERVER.into(), moved);
    let (verb, keep) = if dry_run {
        ("would migrate", "would keep")
    } else {
        ("migrated", "kept")
    };
    let mut notes = vec![format!(
        "{verb} mcpServers.{LEGACY_SERVER} (written by this installer) to {SERVER} with its other settings; Chat and Cowork list its tools as mcp__{SERVER}__*, and Code-tab sessions keep mcp__{LEGACY_SERVER}__* from their own server"
    )];
    if !conflicts.is_empty() {
        notes.push(format!(
            "both names were present: {keep} the {SERVER} value of {}",
            conflicts.join(", ")
        ));
    }
    Ok((merged, notes, false))
}

fn foreign_warning(path: &Path) -> String {
    format!(
        "WARNING: mcpServers.{LEGACY_SERVER} in {} was not written by this installer (its env does not set PSEUDOLIFE_WRITER_ID={DEFAULT_WRITER_ID}), so it was left untouched, and this installer's own entry, {SERVER}, goes beside it: Desktop loads both. Claude Code names its per-session server {LEGACY_SERVER} too, and while an app-level entry carries that name, Desktop serves a Code-tab session's mcp__{LEGACY_SERVER}__* calls from the app-level entry instead of the session's own server. Rename or remove it by hand unless that is what you want.",
        path.display()
    )
}

/// `Some(true/false)` when `<command> --help` gave evidence: exit 0 with
/// output that does or does not name the token file, or the "unknown mode"
/// answer of a client from before `--help` existed. `None` when it could not
/// be asked (missing, not executable, timed out, another non-zero exit, or
/// exit 0 with no output). The child gets no stdin and no credential
/// variable, so a wrapper that drops its arguments cannot start a real
/// client holding the bearer.
pub(super) fn shim_reads_token_file(command: &str, scrub: &[&str], timeout: Duration) -> Option<bool> {
    let mut child = Command::new(command);
    child
        .arg("--help")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONIOENCODING", "utf-8");
    for key in PROBE_CREDENTIAL_ENV.iter().chain(scrub) {
        child.env_remove(key);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        child.creation_flags(0x0800_0000);
    }
    let mut child = child.spawn().ok()?;
    let mut stdout = child.stdout.take()?;
    let mut stderr = child.stderr.take()?;
    let out = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stdout.by_ref().take(1 << 20).read_to_end(&mut bytes);
        bytes
    });
    let err = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stderr.by_ref().take(1 << 20).read_to_end(&mut bytes);
        bytes
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(25)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let mut output = out.join().unwrap_or_default();
    output.extend(err.join().unwrap_or_default());
    let output = String::from_utf8_lossy(&output);
    if !status.success() {
        return output.contains("unknown mode").then_some(false);
    }
    if output.trim().is_empty() {
        return None;
    }
    Some(output.contains(TOKEN_FILE_MARKER))
}

fn old_shim_refusal(command: &str) -> String {
    format!(
        "the client at {command} cannot read a token file: its --help does not list {TOKEN_FILE_MARKER} (no PyPI release through 0.15.0 does, and one that answers \"unknown mode\" predates --help itself). Claude Desktop cannot pass the literal PSEUDOLIFE_MCP_TOKEN, so registering it against a token-gated daemon would leave every Desktop session refused (401). Nothing was written. Install the native client (rerun the installer), then rerun; --skip-shim-check bypasses this check for a wrapper the probe cannot see through."
    )
}

/// The sole `PSEUDOLIFE_MCP_TOKENS` token mapped to the Desktop principal.
fn desktop_token_from_map(raw: &str) -> Result<String, Refusal> {
    let matches: Vec<String> = parse_token_map(raw)
        .into_iter()
        .filter(|(_, principal)| principal == DEFAULT_WRITER_ID)
        .map(|(token, _)| token)
        .collect();
    match matches.as_slice() {
        [token] => Ok(token.clone()),
        _ => refuse(format!(
            "the daemon token map must contain exactly one usable credential for principal '{DEFAULT_WRITER_ID}'; found {}",
            matches.len()
        )),
    }
}

/// `PSEUDOLIFE_MCP_TOKENS` (`token:principal,...`) as ordered pairs: the
/// last `:` splits (tokens may hold one), principals are lowercased, the
/// reserved `default` and `maintainer` and malformed entries are skipped,
/// and the first of duplicate tokens wins.
// TODO(W3): replace with `super::credentials::parse_token_map` once the
// installer credentials module lands; this is the same rule.
fn parse_token_map(raw: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for part in raw.split(',') {
        let part = part.trim();
        let Some((token, principal)) = part.rsplit_once(':') else {
            continue;
        };
        let (token, principal) = (token.trim(), principal.trim().to_lowercase());
        if token.is_empty()
            || principal.is_empty()
            || principal == "default"
            || principal == "maintainer"
            || out.iter().any(|(seen, _)| seen == token)
        {
            continue;
        }
        out.push((token.to_owned(), principal));
    }
    out
}

/// The usable token in an existing file, or why it is not usable.
fn file_token(path: &str) -> Result<String, String> {
    CredentialProvider::new(None, Some(PathBuf::from(path)))
        .and_then(|provider| provider.snapshot())
        .map_err(|error| error.to_string())?
        .token()
        .map(str::to_owned)
        .ok_or_else(|| "it holds no token".into())
}

fn exists(path: &str) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

struct Credential {
    token_file: Option<String>,
    drop_literal: bool,
    notes: Vec<String>,
}

/// Decide the entry's credential and, unless `dry_run`, write the token
/// file. A token from the environment wins; failing that, a literal on the
/// entry moves into the file; with neither, an existing valid file is used
/// as it is; with nothing usable the entry gets no credential.
fn plan_credential(
    existing_env: &Map<String, Value>,
    token_file: Option<&str>,
    token: Option<&str>,
    dry_run: bool,
    preserve_existing: bool,
) -> Result<Credential, Refusal> {
    let Some(token_file) = token_file else {
        return Ok(Credential {
            token_file: None,
            drop_literal: false,
            notes: vec![],
        });
    };
    let literal = existing_env
        .get(LITERAL_KEY)
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty());
    let using = if dry_run { "would use" } else { "using" };
    let mut notes = Vec::new();
    if preserve_existing {
        if !exists(token_file) {
            return Ok(Credential {
                token_file: None,
                drop_literal: false,
                notes,
            });
        }
        file_token(token_file).map_err(|cause| {
            Refusal(format!(
                "the existing token file {token_file} cannot be used ({cause}); fix it or pass --token-file with another usable path"
            ))
        })?;
        notes.push(format!("token file: {using} the existing {token_file}"));
        if literal.is_some() {
            notes.push(format!(
                "removed the superseded literal {LITERAL_KEY} from the planned entry"
            ));
        }
        return Ok(Credential {
            token_file: Some(token_file.into()),
            drop_literal: literal.is_some(),
            notes,
        });
    }
    if let Some(source) = token.or(literal) {
        if exists(token_file) && file_token(token_file).ok().as_deref() == Some(source) {
            notes.push(format!("token file: {using} the existing {token_file}"));
            return Ok(Credential {
                token_file: Some(token_file.into()),
                drop_literal: literal.is_some(),
                notes,
            });
        }
        if dry_run {
            notes.push(format!("token file: would write {token_file} (owner-only)"));
        } else {
            write_token_file(token_file, source)?;
            notes.push(format!(
                "token file: written {token_file} (owner-only; the client rereads it per call)"
            ));
        }
        if literal.is_some() && token.is_none() {
            notes.push(format!(
                "migrated the literal {LITERAL_KEY} out of the config into that file"
            ));
        } else if literal.is_some() {
            notes.push(format!(
                "dropped the literal {LITERAL_KEY} from the config (the file supersedes it)"
            ));
        }
        return Ok(Credential {
            token_file: Some(token_file.into()),
            drop_literal: literal.is_some(),
            notes,
        });
    }
    if exists(token_file) {
        file_token(token_file).map_err(|cause| {
            Refusal(format!(
                "the existing token file {token_file} cannot be used ({cause}); fix it, or rerun with the token in the environment so it is rewritten"
            ))
        })?;
        notes.push(format!("token file: {using} the existing {token_file}"));
        return Ok(Credential {
            token_file: Some(token_file.into()),
            drop_literal: false,
            notes,
        });
    }
    Ok(Credential {
        token_file: None,
        drop_literal: false,
        notes,
    })
}

fn write_token_file(path: &str, token: &str) -> Result<(), Refusal> {
    let failed = |cause: String| Refusal(format!("could not write the token file {path}: {cause}"));
    CredentialProvider::new(Some(token.to_owned()), None)
        .map_err(|error| failed(error.to_string()))?;
    let mut edit = crate::setup::files::PrivateEdit::prepare(Path::new(path), token.as_bytes().to_vec())
        .map_err(|error| failed(error.to_string()))?;
    edit.commit().map_err(|error| failed(error.to_string()))?;
    if file_token(path).ok().as_deref() != Some(token) {
        return Err(failed("the written file did not validate".into()));
    }
    Ok(())
}

fn build_entry(command: &str, writer_id: &str, daemon_url: &str, token_file: Option<&str>) -> Value {
    let mut env = Map::new();
    env.insert("PSEUDOLIFE_WRITER_ID".into(), json!(writer_id));
    env.insert("PSEUDOLIFE_MCP_NO_SPAWN".into(), json!("1"));
    env.insert("PSEUDOLIFE_MCP_DAEMON_URL".into(), json!(daemon_url));
    if let Some(file) = token_file {
        env.insert(FILE_KEY.into(), json!(file));
    }
    json!({"command": command, "env": env})
}

/// Replace only the managed keys of our entry; every other key, server and
/// user-added env var is kept. `drop_literal` removes the literal token,
/// only after its value is in the token file.
fn merge_config(
    existing: &Map<String, Value>,
    entry: &Value,
    drop_literal: bool,
) -> Result<Map<String, Value>, Refusal> {
    let mut merged = existing.clone();
    let servers = merged
        .entry("mcpServers")
        .or_insert_with(|| Value::Object(Map::new()));
    if servers.is_null() {
        *servers = Value::Object(Map::new());
    }
    let Some(servers) = servers.as_object_mut() else {
        return refuse("mcpServers is not a JSON object; refusing to rewrite it");
    };
    let mut new = match servers.get(SERVER) {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(current)) => current.clone(),
        Some(_) => {
            return refuse(format!(
                "mcpServers.{SERVER} is not a JSON object; refusing to rewrite it"
            ));
        }
    };
    new.insert("command".into(), entry["command"].clone());
    // The client takes no arguments; a stale list would break launch.
    new.shift_remove("args");
    let mut env = new
        .get("env")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    for key in MANAGED_ENV {
        env.shift_remove(key);
    }
    if drop_literal {
        env.shift_remove(LITERAL_KEY);
    }
    for (key, value) in entry["env"].as_object().expect("built above") {
        env.insert(key.clone(), value.clone());
    }
    new.insert("env".into(), Value::Object(env));
    servers.insert(SERVER.into(), Value::Object(new));
    Ok(merged)
}

/// What one run decided and did, for the human lines and `--json`.
#[derive(Debug, Default)]
pub(super) struct Outcome {
    pub code: u8,
    /// `written`, `unchanged`, `dry-run` or `refused`.
    pub state: &'static str,
    pub config: Option<PathBuf>,
    pub backup: Option<PathBuf>,
    pub entry: Option<Value>,
    /// Information lines (stdout without `--json`).
    pub lines: Vec<String>,
    /// Warnings and the refusal (always stderr).
    pub warnings: Vec<String>,
}

/// The environment and home a run reads, injected so tests never see the
/// real profile.
pub(super) struct Host<'a> {
    pub lookup: &'a dyn Fn(&str) -> Option<String>,
    pub home: PathBuf,
    pub windows: bool,
    pub macos: bool,
    pub probe: &'a dyn Fn(&str, &[&str]) -> Option<bool>,
}

pub(super) fn register(options: &Options, host: &Host) -> Outcome {
    let mut outcome = Outcome {
        state: "refused",
        code: EXIT_REFUSED,
        ..Outcome::default()
    };
    if let Err(Refusal(message)) = register_inner(options, host, &mut outcome) {
        outcome.state = "refused";
        outcome.code = EXIT_REFUSED;
        outcome.warnings.push(format!("error: {message}"));
    }
    outcome
}

fn register_inner(options: &Options, host: &Host, outcome: &mut Outcome) -> Result<(), Refusal> {
    for candidate in [&options.token_file, &options.default_token_file]
        .into_iter()
        .flatten()
    {
        if !is_absolute(candidate) {
            return refuse(format!(
                "token file must be an absolute path (got {candidate:?}): Desktop launches servers from an arbitrary working directory"
            ));
        }
    }
    if !is_absolute(&options.command) {
        return refuse(format!(
            "--command must be an absolute path (got {:?}): Claude Desktop launches servers with a sanitized PATH",
            options.command
        ));
    }
    let token = options
        .token_from_env
        .as_deref()
        .and_then(|name| (host.lookup)(name))
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty());
    let path = match &options.config {
        Some(path) => path.clone(),
        None => {
            let candidates = config_candidates(host.windows, host.macos, host.lookup, &host.home);
            let chosen = candidates[0].clone();
            for other in candidates.iter().skip(1).filter(|p| p.exists()) {
                outcome.lines.push(format!(
                    "note: another Desktop config also exists at {}; the Store/MSIX build reads the package-cache copy, an unpackaged install reads %APPDATA%. Writing to the package cache; pass --config to choose the other.",
                    other.display()
                ));
            }
            chosen
        }
    };
    outcome.config = Some(path.clone());
    outcome.lines.push(format!("config: {}", path.display()));

    let (raw, existing) = load(&path)?;
    let (config, mut notes, foreign) = migrate_legacy(&existing, options.dry_run)?;
    if foreign {
        outcome.warnings.push(foreign_warning(&path));
    }
    let before = config
        .get("mcpServers")
        .and_then(|servers| servers.get(SERVER))
        .cloned()
        .unwrap_or(Value::Null);
    let existing_env = match &before["env"] {
        Value::Null => Map::new(),
        Value::Object(env) => env.clone(),
        _ => {
            return refuse(format!(
                "mcpServers.{SERVER}.env is not a JSON object; refusing to rewrite it"
            ));
        }
    };
    if !before.is_null() && !before.is_object() {
        return refuse(format!(
            "mcpServers.{SERVER} is not a JSON object; refusing to rewrite it"
        ));
    }
    let existing_file = existing_env
        .get(FILE_KEY)
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .map(str::to_owned);
    let requested = options
        .token_file
        .clone()
        .or_else(|| existing_file.clone())
        .or_else(|| options.default_token_file.clone());
    if let Some(file) = &requested
        && !is_absolute(file)
    {
        return refuse(format!(
            "the existing token file path is not absolute ({file:?}); pass --token-file with a usable absolute replacement"
        ));
    }
    // Probe the path selection chose, before any credential or config write.
    if requested.is_some() {
        if options.skip_shim_check {
            outcome.lines.push(format!(
                "shim check: skipped (--skip-shim-check); the entry assumes {} reads {TOKEN_FILE_MARKER}",
                options.command
            ));
        } else {
            let scrub: Vec<&str> = [&options.token_from_env, &options.tokens_from_env]
                .into_iter()
                .flatten()
                .map(String::as_str)
                .collect();
            match (host.probe)(&options.command, &scrub) {
                Some(false) => {
                    outcome.state = "refused";
                    outcome.code = EXIT_OLD_SHIM;
                    outcome
                        .warnings
                        .push(format!("error: {}", old_shim_refusal(&options.command)));
                    return Ok(());
                }
                None => outcome.lines.push(format!(
                    "note: could not verify that {} reads {TOKEN_FILE_MARKER} (its --help did not answer); proceeding. If Desktop sessions fail with a 401, the client predates token-file support: rerun the installer and then this step.",
                    options.command
                )),
                Some(true) => outcome.lines.push(format!(
                    "shim check: {} reads {TOKEN_FILE_MARKER}",
                    options.command
                )),
            }
        }
    }
    let preserve = (existing_file.is_some() && options.token_file.is_none())
        || options.token_file.as_deref().is_some_and(exists);
    let credential = if preserve {
        plan_credential(&existing_env, requested.as_deref(), None, options.dry_run, true)?
    } else {
        let token = match (token, &options.tokens_from_env) {
            (Some(token), _) => Some(token),
            (None, Some(name)) => Some(desktop_token_from_map(
                &(host.lookup)(name).unwrap_or_default(),
            )?),
            (None, None) => None,
        };
        if token.is_some() && requested.is_none() {
            return refuse(
                "a token source was supplied but there is no token-file path; pass --token-file or --default-token-file",
            );
        }
        plan_credential(
            &existing_env,
            requested.as_deref(),
            token.as_deref(),
            options.dry_run,
            false,
        )?
    };
    if credential.token_file.is_none()
        && let Some(file) = &options.token_file
    {
        return refuse(format!(
            "the explicit token file {file} does not exist and no credential was available to write it; existing config was preserved"
        ));
    }
    if credential.token_file.is_none()
        && let Some(file) = &existing_file
    {
        return refuse(format!(
            "the configured token file {file} does not exist and no credential was available to restore it; existing config was preserved"
        ));
    }
    let entry = build_entry(
        &options.command,
        &options.writer_id,
        &options.daemon_url,
        credential.token_file.as_deref(),
    );
    let merged = merge_config(&config, &entry, credential.drop_literal)?;
    notes.extend(credential.notes);
    if before.get("args").is_some() {
        notes.push("dropped the entry's stale 'args' list (the client takes none)".into());
    }
    let no_credential = requested.is_some() && credential.token_file.is_none();
    outcome.entry = Some(json!({SERVER: entry}));
    let finish = |outcome: &mut Outcome| {
        if no_credential {
            outcome.code = EXIT_NO_CREDENTIAL;
            outcome.warnings.push(format!(
                "WARNING: the daemon is token-gated but no token was available to write {}, and no usable file exists there: the entry was written WITHOUT a credential, so Desktop sessions will be refused (401) until one is configured. Rerun the installer with the token in the environment ({LITERAL_KEY}=<token>) so it writes the owner-only file, or set {FILE_KEY} to an existing private file.",
                requested.as_deref().unwrap_or("")
            ));
        } else {
            outcome.code = EXIT_OK;
        }
    };
    if options.dry_run {
        outcome.state = "dry-run";
        outcome
            .lines
            .push("dry run: nothing written. Entry that would be merged:".into());
        outcome.lines.push(
            serde_json::to_string_pretty(&json!({SERVER: entry})).expect("JSON value"),
        );
        outcome.lines.extend(notes);
        finish(outcome);
        return Ok(());
    }
    if merged == existing {
        outcome.state = "unchanged";
        outcome.lines.push(format!(
            "entry: unchanged ({SERVER} already registered this way)"
        ));
        outcome.lines.extend(notes);
        finish(outcome);
        return Ok(());
    }
    let mut text = serde_json::to_string_pretty(&Value::Object(merged)).expect("JSON value");
    text.push('\n');
    let backup = registrations::write_config(&path, raw.as_deref().unwrap_or_default(), text.as_bytes())
        .map_err(|error| {
            Refusal(format!(
                "{} could not be written ({error}); the existing file was left as it was",
                path.display()
            ))
        })?;
    if raw.is_some() {
        let backup = ordinary(backup);
        outcome
            .lines
            .push(format!("backup: {} (private)", backup.display()));
        outcome.backup = Some(backup);
    }
    outcome.state = "written";
    outcome.lines.push(format!(
        "entry: written ({SERVER} -> {})",
        options.command
    ));
    outcome.lines.extend(notes);
    outcome.lines.push(
        "next: fully quit Claude Desktop (tray/menu-bar icon, not just the window) and relaunch it so the new entry loads"
            .into(),
    );
    finish(outcome);
    Ok(())
}

/// A Windows verbatim path (`\\?\C:\...`) in its ordinary spelling, for
/// the lines people read; other paths unchanged.
fn ordinary(path: PathBuf) -> PathBuf {
    crate::setup::codex::resolved_path(&path).unwrap_or(path)
}

pub(super) fn run(arguments: &[String]) -> u8 {
    let options = match parse(arguments) {
        Ok(Some(options)) => options,
        Ok(None) => {
            let _ = writeln!(io::stdout().lock(), "{USAGE}");
            return 0;
        }
        Err(message) => {
            let _ = writeln!(io::stderr().lock(), "installer: {message}\n{USAGE}");
            return EXIT_REFUSED;
        }
    };
    let lookup = |key: &str| std::env::var(key).ok();
    let probe = |command: &str, scrub: &[&str]| shim_reads_token_file(command, scrub, PROBE_TIMEOUT);
    let host = Host {
        lookup: &lookup,
        home: dirs::home_dir().unwrap_or_default(),
        windows: cfg!(windows),
        macos: cfg!(target_os = "macos"),
        probe: &probe,
    };
    let outcome = register(&options, &host);
    let mut err = io::stderr().lock();
    let mut out = io::stdout().lock();
    if options.json {
        for line in &outcome.lines {
            let _ = writeln!(err, "{line}");
        }
    } else {
        for line in &outcome.lines {
            let _ = writeln!(out, "{line}");
        }
    }
    for line in &outcome.warnings {
        let _ = writeln!(err, "{line}");
    }
    if options.json {
        let report = json!({
            "state": outcome.state,
            "exit": outcome.code,
            "config": outcome.config,
            "backup": outcome.backup,
            "entry": outcome.entry,
            "notes": outcome.lines,
            "warnings": outcome.warnings,
        });
        if writeln!(out, "{report}").is_err() {
            return EXIT_REFUSED;
        }
    }
    outcome.code
}

#[cfg(test)]
#[path = "claude_desktop_tests.rs"]
mod tests;
