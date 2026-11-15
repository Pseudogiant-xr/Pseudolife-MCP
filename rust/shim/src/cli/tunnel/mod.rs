//! `pseudolife-mcp tunnel`: the read-only subset (`tunnel_cli.py`). Saved
//! profiles are read, never written; every path that would write, start a
//! process, open a socket or decrypt a Windows key defers before any effect.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::{Path, PathBuf},
    process::ExitCode,
};

use chrono::Utc;
use serde_json::{Map, Value};

use super::hook_json::quoted;

mod record;
mod store;
mod syntax;

use record::Liveness;
use store::{Profile, exists, occupied};
use syntax::{Admit, json_loads, refresh_id};

/// Why a ported command stops: a sanitized `TunnelError` (exit 2), or a
/// shape this leaf does not decide (the dispatcher's deferral, exit 1).
#[derive(Debug, PartialEq)]
pub(super) enum Fail {
    Tunnel(&'static str),
    Defer,
}

const NO_PROFILES: &str = "No saved tunnel profiles; update does not start new setup.";
const AMBIGUOUS: &str = "daemon connection is missing or ambiguous; pass --daemon-url and --token-file explicitly (an owner-only credential file)";
const UNFINISHED: &str = "finish tunnel setup before requesting cloud verification";
const DEFAULT_URL: &str = "http://127.0.0.1:8765";

#[derive(Clone, Copy, PartialEq)]
enum Command {
    Status,
    Update,
    Verify,
    Setup,
}

#[derive(Default)]
struct Setup {
    daemon_url: Option<String>,
    token_file: Option<String>,
    tunnel_id: Option<String>,
    organization_id: Option<String>,
    catalog: Option<String>,
    key_expires_at: Option<String>,
}

struct Options {
    command: Command,
    profile: String,
    profile_dir: Option<String>,
    json: bool,
    setup: Setup,
}

/// Canonical argv only: the subcommand, then its own options in any order,
/// each at most once, values as the next argument and never `-`-led.
fn parse(values: &[OsString]) -> Option<Options> {
    let values: Vec<&str> = values.iter().map(|v| v.to_str()).collect::<Option<_>>()?;
    let (command, rest) = values.split_first()?;
    let command = match *command {
        "status" => Command::Status,
        "update" => Command::Update,
        "verify" => Command::Verify,
        "setup" => Command::Setup,
        _ => return None,
    };
    let mut options = Options {
        command,
        profile: "dot".into(),
        profile_dir: None,
        json: false,
        setup: Setup::default(),
    };
    let mut seen: Vec<&str> = Vec::new();
    let mut rest = rest.iter();
    while let Some(option) = rest.next() {
        if seen.contains(option) {
            return None;
        }
        seen.push(option);
        match (*option, command) {
            ("--json", Command::Status) => options.json = true,
            ("--accept-access", Command::Setup) => {}
            _ => {
                let value = rest.next().filter(|value| !value.starts_with('-'))?;
                let value = Some((*value).to_owned());
                let s = &mut options.setup;
                match (*option, command) {
                    ("--profile", _) => options.profile = value?,
                    ("--profile-dir", _) => options.profile_dir = value,
                    ("--daemon-url", Command::Setup) => s.daemon_url = value,
                    ("--token-file", Command::Setup) => s.token_file = value,
                    ("--tunnel-id", Command::Setup) => s.tunnel_id = value,
                    ("--organization-id", Command::Setup) => s.organization_id = value,
                    ("--key-expires-at", Command::Setup) => s.key_expires_at = value,
                    ("--catalog", Command::Setup)
                        if matches!(value.as_deref(), Some("current" | "full")) =>
                    {
                        s.catalog = value
                    }
                    _ => return None,
                }
            }
        }
    }
    Some(options)
}

pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    let options = parse(&values)?;
    let (stdout, stderr, code) = match outcome(&options)? {
        Ok((text, code)) => (text, String::new(), code),
        Err(Fail::Tunnel(message)) => (String::new(), format!("{message}\n"), 2),
        Err(Fail::Defer) => return None,
    };
    // The oracle's stdout is block-buffered: a refused stdout fails only its
    // interpreter-shutdown flush, which turns any exit into 120. An empty
    // stdout is never written, so it cannot be refused.
    let mut out = io::stdout().lock();
    let stdout_refused = out
        .write_all(&super::text_bytes(&stdout))
        .and_then(|()| out.flush())
        .is_err();
    drop(out);
    let written = io::stderr().lock().write_all(&super::text_bytes(&stderr));
    Some(if stdout_refused {
        ExitCode::from(STDOUT_REFUSED)
    } else if written.is_ok() {
        ExitCode::from(code)
    } else {
        ExitCode::FAILURE
    })
}

/// The command's answer; `None` when the store root cannot be opened and
/// the command defers.
fn outcome(options: &Options) -> Option<Output> {
    let root = store::store_root(options.profile_dir.as_deref());
    // `update` runs unattended inside `pseudolife-mcp update`, which shows
    // its refusal: a root it cannot open is named, never deferred.
    let root = match root {
        Err(_) if options.command == Command::Update => {
            return Some(Err(Fail::Tunnel(store::UNSAFE_ROOT)));
        }
        root => root.ok()?,
    };
    Some(match options.command {
        Command::Status => status(options, &root),
        Command::Update => update(&root),
        Command::Verify => verify(options, &root),
        Command::Setup => setup(options, &root),
    })
}

/// CPython's exit when its shutdown flush of a refused stdout fails.
const STDOUT_REFUSED: u8 = 120;

type Output = Result<(String, u8), Fail>;

fn bool_text(value: bool) -> &'static str {
    if value { "true" } else { "false" }
}

/// `_refresh_status`: the allowlisted last-refresh outcome.
enum Refresh {
    None,
    Pending,
    Unavailable,
    Done { state: String, rolled_back: bool },
}

impl Refresh {
    fn state(&self) -> &str {
        match self {
            Refresh::None => "none",
            Refresh::Pending => "pending",
            Refresh::Unavailable => "unavailable",
            Refresh::Done { state, .. } => state,
        }
    }
    fn needs_attention(&self) -> bool {
        !matches!(self, Refresh::None) && self.state() != "refreshed"
    }
    fn json(&self) -> String {
        let head = format!(
            "{{\"state\": {}, \"needs_attention\": {}",
            quoted(self.state()),
            bool_text(self.needs_attention())
        );
        match self {
            Refresh::Done { rolled_back, .. } => {
                format!("{head}, \"rolled_back\": {}}}", bool_text(*rolled_back))
            }
            _ => head + "}",
        }
    }
}

fn private_object(path: &Path) -> Result<Option<Map<String, Value>>, Fail> {
    match store::private_read(path, 65536) {
        Err(Fail::Defer) => Err(Fail::Defer),
        Err(Fail::Tunnel(_)) => Ok(None),
        Ok(data) => match json_loads(&data) {
            Admit::Yes(Value::Object(map)) => Ok(Some(map)),
            Admit::Yes(_) | Admit::No => Ok(None),
            Admit::Defer => Err(Fail::Defer),
        },
    }
}

fn refresh_status(root: &Path, name: &str) -> Result<Refresh, Fail> {
    let pending = root.join(format!("{name}.reload.json"));
    if exists(&pending)? {
        return Ok(match private_object(&pending)? {
            Some(request) if refresh_id(request.get("id")) => Refresh::Pending,
            _ => Refresh::Unavailable,
        });
    }
    let path = root.join(format!("{name}.reload.result.json"));
    if !exists(&path)? {
        return Ok(Refresh::None);
    }
    Ok(match private_object(&path)? {
        Some(result) if refresh_id(result.get("id")) => match result.get("state") {
            Some(Value::String(state))
                if ["refreshed", "rolled-back", "failed", "rollback-incomplete"]
                    .contains(&state.as_str()) =>
            {
                Refresh::Done {
                    state: state.clone(),
                    rolled_back: result.get("rolled_back") == Some(&Value::Bool(true)),
                }
            }
            _ => Refresh::Unavailable,
        },
        _ => Refresh::Unavailable,
    })
}

/// A challenge record nested near CPython's recursion limit would raise out
/// of `verification_status`; this bound leaves only ordinary records.
fn shallow_challenge(root: &Path, name: &str) -> Result<(), Fail> {
    let path = root
        .join(format!("{name}-verification"))
        .join("challenge.json");
    // `_challenge` reads through private_read: a redirect, an unsafe or
    // missing file, or one too large is no challenge at all (`{}`).
    match store::private_read(&path, 65536) {
        Ok(data) if data.iter().filter(|b| matches!(b, b'[' | b'{')).count() > 100 => {
            Err(Fail::Defer)
        }
        Ok(_) | Err(Fail::Tunnel(_)) => Ok(()),
        Err(Fail::Defer) => Err(Fail::Defer),
    }
}

fn status(options: &Options, root: &Path) -> Output {
    let profile = store::load(root, &options.profile)?;
    let key = store::key_state(root, &profile.name)?;
    let path = record::path(root, &profile.name);
    let table = if occupied(&path).unwrap_or(true) {
        record::table()
    } else {
        None
    };
    let record = record::read(path, table.as_deref());
    // Without a running launch the cloud identity is unavailable, so
    // verification_status is constant: nothing verified, no current challenge.
    shallow_challenge(root, &profile.name)?;
    let refresh = refresh_status(root, &profile.name)?;
    Ok((
        report(&profile, key, record.as_ref(), &refresh, options.json),
        0,
    ))
}

/// What a process record says about the tunnel, in one sentence for a
/// person, with the file it names.
fn record_sentence(name: &str, record: &record::Record) -> String {
    let file = record.path.display();
    match (record.liveness, record.readable) {
        (Liveness::Running, _) => format!(
            "tunnel {name} keeps running on its current runtime (process record {file}); this client cannot refresh or stop it yet, so it was left as it is"
        ),
        (Liveness::Gone, _) => format!(
            "tunnel {name} is not running; its process record {file} is left from a tunnel that stopped and can be deleted"
        ),
        (Liveness::Unknown, false) => format!(
            "tunnel {name}'s process record {file} is not an owner-only record this client can read, so whether the tunnel runs is unknown; it was left as it is"
        ),
        (Liveness::Unknown, true) => format!(
            "this machine's process table cannot be read, so whether tunnel {name} runs (process record {file}) is unknown; it was left as it is"
        ),
    }
}

fn report(
    profile: &Profile,
    key: Option<bool>,
    record: Option<&record::Record>,
    refresh: &Refresh,
    as_json: bool,
) -> String {
    let running = record.map(|record| record.liveness);
    let ready = running == Some(Liveness::Running) && record.is_some_and(|record| record.ready);
    let running_json = match running {
        Some(Liveness::Running) => "true",
        Some(Liveness::Unknown) => "\"unknown\"",
        Some(Liveness::Gone) | None => "false",
    };
    let key_json = match key {
        Some(present) => bool_text(present),
        None => "\"not_verified\"",
    };
    let now = Utc::now();
    let (expiry, runtime_expiry, expired) = match &profile.expires_at {
        None => (
            "{\"known\": false, \"expired\": null}".to_owned(),
            "unknown",
            false,
        ),
        Some((text, instant)) => {
            let expired = *instant <= now;
            let remaining = *instant - now;
            let class = if remaining <= chrono::Duration::zero() {
                "expired"
            } else if remaining < chrono::Duration::days(7) {
                "near-expiry"
            } else {
                "valid"
            };
            (
                format!(
                    "{{\"known\": true, \"expires_at\": {}, \"expired\": {}}}",
                    quoted(text),
                    bool_text(expired)
                ),
                class,
                expired,
            )
        }
    };
    let name = &profile.name;
    // A running tunnel's cloud verification is bound to its launch, which
    // this client does not read: not checked, rather than "not verified".
    let cloud_checked = running != Some(Liveness::Running);
    if as_json {
        let record_json = record.map_or(String::new(), |record| {
            format!(
                ", \"process_record\": {{\"path\": {}, \"detail\": {}}}",
                quoted(&record.path.display().to_string()),
                quoted(&record_sentence(name, record)),
            )
        });
        let cloud = if cloud_checked {
            "{\"verified\": false, \"successful_calls\": 0, \"challenge_current\": false}"
        } else {
            "\"not_checked\""
        };
        return format!(
            "{{\"profile\": {name_q}, \"state\": {}, \"catalog\": {}, \"private_key_present\": {key_json}, \"key_expiry\": {expiry}, \"runtime\": {{\"running\": {running_json}, \"profile\": {name_q}, \"ready\": {}, \"key_expiry\": {}{record_json}}}, \"cloud\": {cloud}, \"update\": {}}}\n",
            quoted(&profile.state),
            quoted(&profile.catalog),
            bool_text(ready),
            quoted(runtime_expiry),
            refresh.json(),
            name_q = quoted(name),
        );
    }
    let shown_running = match running {
        Some(Liveness::Running) => "True",
        Some(Liveness::Unknown) => "unknown",
        Some(Liveness::Gone) | None => "False",
    };
    let shown_ready = if ready { "True" } else { "False" };
    let cloud = if cloud_checked {
        "cloud verified=False (0/2 calls)"
    } else {
        "cloud verification not checked by this client"
    };
    let mut text = format!(
        "Profile {name}: {}; runtime running={shown_running}, ready={shown_ready}; {cloud}.\n",
        profile.state
    );
    if let Some(record) = record {
        let sentence = record_sentence(name, record);
        let mut chars = sentence.chars();
        let first = chars.next().map(|c| c.to_uppercase().collect::<String>());
        text.push_str(&format!(
            "{}{}.\n",
            first.unwrap_or_default(),
            chars.as_str()
        ));
    }
    let shown = profile
        .expires_at
        .as_ref()
        .map_or("unknown; check Platform key settings", |(text, _)| text);
    text.push_str(&format!("API key expiry: {shown}.\n"));
    if refresh.needs_attention() {
        text.push_str(&format!(
            "Last tunnel refresh: {}; needs attention. Inspect tunnel doctor before retrying the update.\n",
            refresh.state()
        ));
    }
    if expired {
        text.push_str(&format!(
            "Renew privately: tunnel setup --profile {name} --read-key --key-expires-at <ISO timestamp>.\n"
        ));
    }
    text
}

/// `tunnel update`: report each saved profile's tunnel as it is. This client
/// cannot refresh a running tunnel yet, so it starts, stops and signals
/// nothing: a running tunnel is reported `stale` and left running, a record
/// whose process is gone is named as a file to delete, and neither a record
/// nor a key ever stops the command. Refused only for a store or profile it
/// cannot trust (exit 2), and exit 3 when nothing is saved at all.
fn update(root: &Path) -> Output {
    // A store this client cannot list is a refusal with its cause, never a
    // deferral: the update step shows the cause to the user.
    let unreadable = |fail: Fail| match fail {
        Fail::Defer => Fail::Tunnel(store::UNREADABLE_STORE),
        refused => refused,
    };
    let present = exists(root).map_err(unreadable)?;
    let names = if present {
        store::list(root).map_err(unreadable)?
    } else {
        Vec::new()
    };
    let orphans = if present {
        record::orphans(root, &names)?
    } else {
        Vec::new()
    };
    if names.is_empty() && orphans.is_empty() {
        return Ok((format!("{NO_PROFILES}\n"), 3));
    }
    // The process table is read once, and only when some record exists.
    let any_record = !orphans.is_empty()
        || names
            .iter()
            .any(|name| occupied(&record::path(root, name)).unwrap_or(true));
    let rows = if any_record { record::table() } else { None };
    let mut entries = Vec::new();
    let mut notes = Vec::new();
    let mut stale = false;
    for name in &names {
        // A profile in a form this client does not read is named with the
        // reason and still reported by its record; only a refused one stops
        // the update.
        let unread = match store::load(root, name) {
            Ok(profile) => Err(profile.state == "ready"),
            Err(Fail::Defer) => Ok(format!(
                "this client could not read profile {}: {}",
                store::profile_file(root, name).display(),
                store::unread_reason(root, name)
            )),
            Err(refused) => return Err(refused),
        };
        let record = record::read(record::path(root, name), rows.as_deref());
        let name_q = quoted(name);
        let (state, running, sentence) = match (&record, unread) {
            (None, Err(true)) => {
                entries.push(format!(
                    "{{\"profile\": {name_q}, \"state\": \"not-running\", \"running\": false}}"
                ));
                continue;
            }
            (None, Err(false)) => {
                entries.push(format!("{{\"profile\": {name_q}, \"state\": \"pending\"}}"));
                continue;
            }
            (None, Ok(unread)) => (
                "stale",
                "false",
                format!("{unread}; it has no tunnel process record, so no tunnel of it runs"),
            ),
            (Some(record), unread) => {
                // A stopped tunnel of a readable profile needs nothing; an
                // unread profile always needs the user's eye.
                let state = match (&unread, record.liveness) {
                    (Err(_), Liveness::Gone) => "not-running",
                    _ => "stale",
                };
                let sentence = record_sentence(name, record);
                let sentence = match unread {
                    Ok(unread) => format!("{unread}; {sentence}"),
                    Err(_) => sentence,
                };
                let running = match record.liveness {
                    Liveness::Running => "true",
                    Liveness::Unknown => "\"unknown\"",
                    Liveness::Gone => "false",
                };
                (state, running, sentence)
            }
        };
        stale |= state == "stale";
        entries.push(format!(
            "{{\"profile\": {name_q}, \"state\": \"{state}\", \"running\": {running}, \"detail\": {}}}",
            quoted(&sentence)
        ));
        notes.push(sentence);
    }
    for file_name in &orphans {
        let Some(record) = record::read(root.join(file_name), rows.as_deref()) else {
            continue;
        };
        let file = record.path.display();
        let (running, sentence) = match record.liveness {
            Liveness::Running => (
                "true",
                format!(
                    "{file} has no saved profile beside it but names a running process; this client cannot stop it, so it was left as it is"
                ),
            ),
            Liveness::Gone => (
                "false",
                format!(
                    "{file} has no saved profile beside it and names no running process; it can be deleted"
                ),
            ),
            Liveness::Unknown => (
                "\"unknown\"",
                format!(
                    "{file} has no saved profile beside it, and whether it names a running process cannot be checked; it was left as it is"
                ),
            ),
        };
        stale = true;
        entries.push(format!(
            "{{\"record\": {}, \"state\": \"stale\", \"running\": {running}, \"detail\": {}}}",
            quoted(file_name),
            quoted(&sentence)
        ));
        notes.push(sentence);
    }
    let mut detail = format!(
        "{} saved tunnel profiles checked; local client registrations preserved",
        names.len()
    );
    for note in &notes {
        detail.push_str("; ");
        detail.push_str(note);
    }
    Ok((
        format!(
            "{{\"state\": \"{}\", \"needs_attention\": false, \"detail\": {}, \"profiles\": [{}]}}\n",
            if stale { "stale" } else { "current" },
            quoted(&detail),
            entries.join(", ")
        ),
        0,
    ))
}

fn verify(options: &Options, root: &Path) -> Output {
    let profile = store::load(root, &options.profile)?;
    if profile.state != "ready" || !store::key_present(root, &profile.name)? {
        return Err(Fail::Tunnel(UNFINISHED));
    }
    // begin_challenge computes the launch identity before writing anything;
    // without a process record its private read fails first. With a record
    // the challenge would be written: deferred.
    if occupied(&root.join(format!("{}.process.json", profile.name)))? {
        return Err(Fail::Defer);
    }
    Err(Fail::Tunnel(store::UNAVAILABLE))
}

fn environment(key: &str) -> Result<Option<String>, Fail> {
    match std::env::var_os(key) {
        None => Ok(None),
        Some(value) => value
            .into_string()
            .map(|value| Some(value).filter(|value| !value.is_empty()))
            .map_err(|_| Fail::Defer),
    }
}

fn truthy(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|value| !value.is_empty())
}

/// `Path(token).expanduser().absolute()`, as text, for the shapes decided here.
fn absolute_token(token: &str) -> Result<String, Fail> {
    if token.starts_with('~') || token.contains('\0') {
        return Err(Fail::Defer);
    }
    if cfg!(windows) {
        let b = token.as_bytes();
        let drive = b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':';
        let rooted = b.first().is_some_and(|c| matches!(c, b'\\' | b'/'));
        let absolute = drive && b.get(2).is_some_and(|c| matches!(c, b'\\' | b'/'));
        if rooted || (drive && !absolute) {
            return Err(Fail::Defer);
        }
    }
    crate::credentials::absolute_expanded(Path::new(token))
        .ok()
        .and_then(|path| path.to_str().map(str::to_owned))
        .ok_or(Fail::Defer)
}

fn home() -> Result<PathBuf, Fail> {
    crate::credentials::expand_user(Path::new("~"))
        .ok()
        .filter(|home| !home.as_os_str().is_empty())
        .ok_or(Fail::Defer)
}

/// `_discover`: the daemon connection setup would save.
fn discover(setup: &Setup) -> Result<(String, String), Fail> {
    let (url, token) = (truthy(&setup.daemon_url), truthy(&setup.token_file));
    if let (Some(url), Some(token)) = (url, token) {
        // The returned tuple validates the URL before resolving the path.
        syntax::validate_url(url)?;
        return Ok((url.to_owned(), absolute_token(token)?));
    }
    let env_file = environment("PSEUDOLIFE_MCP_TOKEN_FILE")?;
    let env_token = environment("PSEUDOLIFE_MCP_TOKEN")?;
    if env_file.is_none() && env_token.is_none() {
        // Registration env blocks: only their absence is decided here.
        let claude = match environment("CLAUDE_CONFIG_DIR")? {
            Some(directory) => PathBuf::from(directory).join(".claude.json"),
            None => home()?.join(".claude.json"),
        };
        let codex = match environment("CODEX_HOME")? {
            Some(directory) => PathBuf::from(directory),
            None => home()?.join(".codex"),
        }
        .join("config.toml");
        if occupied(&claude)? || occupied(&codex)? {
            return Err(Fail::Defer);
        }
        return Err(Fail::Tunnel(AMBIGUOUS));
    }
    let Some(file) = token.map(str::to_owned).or(env_file) else {
        // Only a literal credential: setup requires a file.
        return Err(Fail::Tunnel(AMBIGUOUS));
    };
    let token = absolute_token(&file)?;
    let selected = match url {
        Some(url) => url.to_owned(),
        None => environment("PSEUDOLIFE_MCP_DAEMON_URL")?.unwrap_or_else(|| DEFAULT_URL.into()),
    };
    syntax::validate_url(&selected)?;
    Ok((selected, token))
}

/// `_setup` up to `profile.validate()`: its refusals. A profile that
/// validates would run the daemon handshake next, so it defers.
fn setup(options: &Options, root: &Path) -> Output {
    let path = store::profile_path(root, &options.profile)?;
    if occupied(&path)? {
        return Err(Fail::Defer);
    }
    let (url, token) = discover(&options.setup)?;
    let mut payload = Map::new();
    payload.insert("name".into(), Value::String(options.profile.clone()));
    payload.insert("daemon_url".into(), Value::String(url));
    payload.insert("token_file".into(), Value::String(token));
    let s = &options.setup;
    for (key, value) in [
        ("tunnel_id", &s.tunnel_id),
        ("organization_id", &s.organization_id),
        ("minimum_catalog", &s.catalog),
        ("runtime_key_expires_at", &s.key_expires_at),
    ] {
        if let Some(value) = value {
            payload.insert(key.into(), Value::String(value.clone()));
        }
    }
    store::validate(&payload)?;
    Err(Fail::Defer)
}

/// `doctor`'s saved-tunnel section, read-only: `None` when `root` holds no
/// `*.profile.json` (absent, empty or unrelated files say nothing), else one
/// entry per profile, where a profile or a store this client cannot read is
/// a named `unavailable` entry, never a reason to stop. Key presence only:
/// no key, token or credential path is reported.
pub(super) fn doctor_section(root: &Path) -> Option<Vec<Value>> {
    use serde_json::json;
    let unavailable = |profile: Option<&str>, reason: &str, recovery: String| {
        let mut entry = Map::new();
        if let Some(profile) = profile {
            entry.insert("profile".into(), json!(profile));
        }
        entry.insert("state".into(), json!("unavailable"));
        entry.insert("reason".into(), json!(reason));
        entry.insert("recovery".into(), json!(recovery));
        Value::Object(entry)
    };
    let store_recovery = format!(
        "Make {} a directory only your account can access, holding only tunnel profile files, then retry.",
        root.display()
    );
    match std::fs::symlink_metadata(root) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return None,
        Err(_) => {
            return Some(vec![unavailable(
                None,
                "the tunnel directory cannot be inspected",
                store_recovery,
            )]);
        }
        Ok(_) => {}
    }
    // Profiles first, then the store's own checks: an empty or unrelated
    // directory is not a store anyone set up.
    let any_profile = std::fs::read_dir(root).ok().map(|entries| {
        entries.flatten().any(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .to_ascii_lowercase()
                .ends_with(".profile.json")
        })
    });
    if any_profile == Some(false) {
        return None;
    }
    let names = match store::checked_path(root).and_then(|root| store::list(&root)) {
        Ok(names) if names.is_empty() => return None,
        Ok(names) => names,
        Err(Fail::Tunnel(reason)) => return Some(vec![unavailable(None, reason, store_recovery)]),
        Err(Fail::Defer) => {
            return Some(vec![unavailable(
                None,
                "the tunnel directory holds entries this client does not read",
                store_recovery,
            )]);
        }
    };
    let entries = names
        .iter()
        .map(|name| {
            let recovery = format!(
                "Check that {} is an owner-only file holding a tunnel profile; if it is left from an abandoned setup, move it aside. Setting the profile up again needs pseudolife-mcp tunnel setup, which the native client does not run yet (it hands it to the Python client).",
                root.join(format!("{name}.profile.json")).display()
            );
            let profile = match store::load(root, name) {
                Ok(profile) => profile,
                Err(Fail::Tunnel(reason)) => return unavailable(Some(name), reason, recovery),
                Err(Fail::Defer) => {
                    return unavailable(
                        Some(name),
                        "the profile is in a form this client does not read",
                        recovery,
                    );
                }
            };
            let now = Utc::now();
            let (key_expiry, expired) = match &profile.expires_at {
                None => (json!({"known": false, "expired": null}), false),
                Some((text, instant)) => (
                    json!({"known": true, "expires_at": text, "expired": *instant <= now}),
                    *instant <= now,
                ),
            };
            let key_checked = store::key_present(root, name);
            let key = match key_checked {
                Ok(present) => json!(present),
                // A Windows DPAPI key needs CryptUnprotectData to prove.
                Err(_) => json!("not_verified"),
            };
            // `None` when the path cannot be inspected: not a claim either way.
            let process_record = occupied(&root.join(format!("{name}.process.json"))).ok();
            let mut next = Vec::new();
            let update = match refresh_status(root, name) {
                Ok(refresh) => {
                    if refresh.needs_attention() {
                        // Native `tunnel status` proves the key first, then
                        // hands the run to Python on a process record or one
                        // it cannot inspect.
                        let python = |why: &str| {
                            format!(
                                "pseudolife-mcp tunnel status --profile {name} shows the outcome, but needs the Python client {why}"
                            )
                        };
                        let inspect = match (key_checked.is_ok(), process_record) {
                            (false, _) => python("because the key cannot be checked natively"),
                            (true, Some(true)) => python("while a tunnel process record exists"),
                            (true, None) => python("while the process record cannot be checked"),
                            (true, Some(false)) => format!(
                                "pseudolife-mcp tunnel status --profile {name} shows the outcome and the key's state"
                            ),
                        };
                        next.push(format!(
                            "Last tunnel refresh: {}; needs attention: {inspect}.",
                            refresh.state()
                        ));
                    }
                    let mut update = json!({"state": refresh.state(),
                        "needs_attention": refresh.needs_attention()});
                    if let Refresh::Done { rolled_back, .. } = refresh {
                        update["rolled_back"] = json!(rolled_back);
                    }
                    update
                }
                Err(_) => json!("not_checked"),
            };
            if expired {
                next.push(format!(
                    "The tunnel key has expired. Renew it privately with pseudolife-mcp tunnel setup --profile {name} --read-key --key-expires-at <ISO timestamp>; the native client does not run tunnel setup yet, so this needs the Python client."
                ));
            }
            let mut entry = json!({
                "profile": profile.name,
                "state": profile.state,
                "catalog": profile.catalog,
                "private_key_present": key,
                "key_expiry": key_expiry,
                "update": update,
                "runtime": {"checked": false, "process_record": process_record},
                "cloud": "not_checked",
            });
            if !next.is_empty() {
                entry["next"] = json!(next);
            }
            entry
        })
        .collect();
    Some(entries)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    /// A disposable owner-only store, written as the native client writes
    /// private files (so the store's own owner-only checks accept it).
    struct Store(PathBuf);
    impl Store {
        fn new() -> Self {
            let base = std::env::temp_dir().join(format!("tunnel-unit-{}", uuid::Uuid::new_v4()));
            let root = base.join("tunnel");
            crate::board::state::private_dir(&root).unwrap();
            Self(root)
        }
        fn put(&self, rel: &str, data: &[u8]) {
            crate::board::state::atomic_create(&self.0.join(rel), data).unwrap();
        }
        fn profile(&self, name: &str, ready: bool) {
            let token = serde_json::to_string(&self.0.join("token")).unwrap();
            let ready = if ready {
                ", \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true"
            } else {
                ""
            };
            self.put(
                &format!("{name}.profile.json"),
                format!("{{\"name\": \"{name}\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {token}{ready}}}").as_bytes(),
            );
        }
        /// The private key as this platform's saved setup holds it.
        fn key(&self, name: &str) {
            let data: &[u8] = if cfg!(windows) {
                b"DPAPI\0sealed-fixture"
            } else {
                b"PLAIN\0fixture-tunnel-key"
            };
            self.put(&format!("{name}.key"), data);
        }
        fn record(&self, name: &str, pid: u64, exe: &Path) {
            let record = serde_json::json!({"pid": pid, "created": 0.0, "exe": exe, "argv": [exe]});
            self.put(
                &format!("{name}.process.json"),
                record.to_string().as_bytes(),
            );
        }
    }
    impl Drop for Store {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(self.0.parent().unwrap());
        }
    }

    /// A pid no process table holds: above Linux's pid_max ceiling and not
    /// a multiple of four (Windows process ids are).
    const DEAD_PID: u64 = 4_294_967_001;

    #[test]
    fn update_reports_a_dead_record_beside_a_ready_keyed_profile_as_not_running() {
        let store = Store::new();
        store.profile("dot", true);
        store.key("dot");
        store.record("dot", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        let (text, code) = update(&store.0).expect("update answers");
        assert_eq!(code, 0, "{text}");
        let report: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(report["state"], "current", "{text}");
        assert_eq!(report["profiles"][0]["profile"], "dot");
        assert_eq!(report["profiles"][0]["state"], "not-running", "{text}");
        assert!(
            report["detail"]
                .as_str()
                .unwrap()
                .contains("dot.process.json"),
            "{text}"
        );
    }

    fn own_exe() -> PathBuf {
        std::env::current_exe().unwrap()
    }

    fn report_of(output: Output) -> Value {
        let (text, code) = output.expect("answers");
        assert_eq!(code, 0, "{text}");
        serde_json::from_str(&text).unwrap()
    }

    #[test]
    fn update_leaves_a_running_tunnel_running_and_reports_it_stale() {
        let store = Store::new();
        store.profile("dot", true);
        store.key("dot");
        // This test process stands in for a live tunnel: nothing is spawned.
        store.record("dot", u64::from(std::process::id()), &own_exe());
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "stale", "{report}");
        assert_eq!(report["needs_attention"], false);
        let entry = &report["profiles"][0];
        assert_eq!(entry["state"], "stale");
        assert_eq!(entry["running"], true);
        let detail = report["detail"].as_str().unwrap();
        assert!(
            detail.contains("tunnel dot keeps running on its current runtime")
                && detail
                    .contains("this client cannot refresh or stop it yet, so it was left as it is"),
            "{detail}"
        );
        // Still running afterwards: the record and this process are untouched.
        assert!(store.0.join("dot.process.json").is_file());
    }

    #[test]
    fn update_names_an_orphan_record_as_stale() {
        let store = Store::new();
        store.record("gone", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        // No profile at all: the record alone is reported, not exit 3.
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "stale", "{report}");
        let entry = &report["profiles"][0];
        assert_eq!(entry["record"], "gone.process.json");
        assert_eq!(entry["state"], "stale");
        assert_eq!(entry["running"], false);
        let detail = report["detail"].as_str().unwrap();
        assert!(
            detail.contains("gone.process.json has no saved profile beside it and names no running process; it can be deleted"),
            "{detail}"
        );
        // Beside another profile, too.
        store.profile("dot", false);
        let report = report_of(update(&store.0));
        assert_eq!(report["profiles"][0]["state"], "pending");
        assert_eq!(report["profiles"][1]["record"], "gone.process.json");
    }

    #[test]
    fn update_reports_an_unreadable_record_without_failing() {
        let store = Store::new();
        store.profile("dot", true);
        store.put("dot.process.json", b"[not a record");
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "stale");
        assert_eq!(report["profiles"][0]["running"], "unknown");
        // An empty record names no process: not running.
        std::fs::remove_file(store.0.join("dot.process.json")).unwrap();
        store.put("dot.process.json", b"{}");
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "current");
        assert_eq!(report["profiles"][0]["state"], "not-running");
    }

    #[test]
    fn update_still_refuses_an_invalid_profile_and_says_when_nothing_is_saved() {
        let store = Store::new();
        assert_eq!(update(&store.0).unwrap(), (format!("{NO_PROFILES}\n"), 3));
        store.profile("dot", true);
        store.record("dot", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        store.put("bad.profile.json", b"{");
        assert_eq!(
            update(&store.0).unwrap_err(),
            Fail::Tunnel("tunnel profile is invalid")
        );
    }

    /// Each profile shape `load` does not read is named, with its reason,
    /// beside what its record says; the update still answers (exit 0).
    #[test]
    fn update_names_each_profile_it_cannot_read_and_still_answers() {
        let token = |store: &Store| serde_json::to_string(&store.0.join("token")).unwrap();
        type Shape = (&'static str, Box<dyn Fn(&Store) -> Vec<u8>>, &'static str);
        let shapes: Vec<Shape> = vec![
            (
                "seven-digit fraction",
                Box::new(|s| {
                    format!("{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {}, \"runtime_key_expires_at\": \"2099-12-31T00:00:00.1234567Z\"}}", token(s)).into_bytes()
                }),
                "its runtime_key_expires_at uses a timestamp spelling this client does not read",
            ),
            (
                "one-digit fraction",
                Box::new(|s| {
                    format!("{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {}, \"runtime_key_expires_at\": \"2099-12-31T00:00:00.5Z\"}}", token(s)).into_bytes()
                }),
                "its runtime_key_expires_at uses a timestamp spelling",
            ),
            (
                "hour 24",
                Box::new(|s| {
                    format!("{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {}, \"runtime_key_expires_at\": \"2099-12-31T24:00:00Z\"}}", token(s)).into_bytes()
                }),
                "its runtime_key_expires_at uses a timestamp spelling",
            ),
            (
                "non-ASCII runtime version",
                Box::new(|s| {
                    format!("{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {}, \"runtime_version\": \"\u{661}.2.3\"}}", token(s)).into_bytes()
                }),
                "its runtime_version is not plain ASCII digits",
            ),
            (
                "byte-order mark",
                Box::new(|s| {
                    let mut data = b"\xef\xbb\xbf".to_vec();
                    data.extend(format!("{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {}}}", token(s)).into_bytes());
                    data
                }),
                "the profile file uses JSON this client does not read",
            ),
        ];
        let mut shapes = shapes;
        if cfg!(windows) {
            shapes.push((
                "UNC token file",
                Box::new(|_| br#"{"name": "dot", "daemon_url": "http://127.0.0.1:9", "token_file": "\\\\server\\share\\token"}"#.to_vec()),
                "its token_file is a path form this client does not read",
            ));
        }
        for (shape, profile, reason) in &shapes {
            let store = Store::new();
            store.put("dot.profile.json", &profile(&store));
            assert_eq!(
                store::load(&store.0, "dot").unwrap_err(),
                Fail::Defer,
                "{shape}: the shape must be one load defers on"
            );
            let report = report_of(update(&store.0));
            assert_eq!(report["state"], "stale", "{shape}: {report}");
            let entry = &report["profiles"][0];
            assert_eq!(entry["profile"], "dot", "{shape}");
            assert_eq!(entry["running"], false, "{shape}");
            let detail = entry["detail"].as_str().unwrap();
            assert!(
                detail.starts_with("this client could not read profile ")
                    && detail.contains("dot.profile.json")
                    && detail.contains(reason)
                    && detail.ends_with("it has no tunnel process record, so no tunnel of it runs"),
                "{shape}: {detail}"
            );
            assert!(
                report["detail"].as_str().unwrap().contains(reason),
                "{shape}"
            );
            // Beside a record, the record's own sentence follows.
            store.record("dot", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
            let report = report_of(update(&store.0));
            let detail = report["profiles"][0]["detail"].as_str().unwrap();
            assert!(
                detail.contains(reason) && detail.contains("is left from a tunnel that stopped"),
                "{shape}: {detail}"
            );
            assert_eq!(report["profiles"][0]["state"], "stale", "{shape}");
        }
    }

    #[test]
    fn update_refuses_a_store_root_it_cannot_open_with_the_cause() {
        // An empty --profile-dir: update names it, status still defers.
        let answer =
            |command: &str| outcome(&parse(&argv(&[command, "--profile-dir", ""])).unwrap());
        assert_eq!(
            answer("update"),
            Some(Err(Fail::Tunnel(store::UNSAFE_ROOT)))
        );
        assert_eq!(answer("status"), None);
        // A store root reached through a link.
        let store = Store::new();
        let link = store.0.parent().unwrap().join("link");
        #[cfg(unix)]
        std::os::unix::fs::symlink(&store.0, &link).unwrap();
        #[cfg(windows)]
        if std::os::windows::fs::symlink_dir(&store.0, &link).is_err() {
            return; // no symlink privilege on this host: the empty case stands
        }
        let link = link.to_str().unwrap();
        assert_eq!(
            outcome(&parse(&argv(&["update", "--profile-dir", link])).unwrap()),
            Some(Err(Fail::Tunnel(store::UNSAFE_ROOT)))
        );
    }

    #[test]
    fn status_reads_the_process_record() {
        let store = Store::new();
        store.profile("dot", true);
        let options = parse(&argv(&["status", "--json"])).unwrap();
        store.record("dot", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        let report = report_of(status(&options, &store.0));
        assert_eq!(report["runtime"]["running"], false, "{report}");
        assert!(
            report["runtime"]["process_record"]["detail"]
                .as_str()
                .unwrap()
                .contains("is left from a tunnel that stopped and can be deleted"),
            "{report}"
        );
        assert_eq!(report["cloud"]["verified"], false);
        std::fs::remove_file(store.0.join("dot.process.json")).unwrap();
        store.record("dot", u64::from(std::process::id()), &own_exe());
        let report = report_of(status(&options, &store.0));
        assert_eq!(report["runtime"]["running"], true, "{report}");
        assert_eq!(report["runtime"]["ready"], false);
        assert_eq!(report["cloud"], "not_checked");
        let text = parse(&argv(&["status"])).unwrap();
        let (text, _) = status(&text, &store.0).unwrap();
        assert!(
            text.starts_with("Profile dot: ready; runtime running=True, ready=False; cloud verification not checked by this client.\nTunnel dot keeps running"),
            "{text}"
        );
    }

    #[test]
    fn status_answers_beside_this_platforms_saved_key() {
        let store = Store::new();
        store.profile("dot", true);
        store.key("dot");
        let options = parse(&argv(&["status", "--json"])).unwrap();
        let report = report_of(status(&options, &store.0));
        // A DPAPI key needs CryptUnprotectData, which this client does not
        // call: present on disk, not verified.
        let expected = if cfg!(windows) {
            Value::from("not_verified")
        } else {
            Value::Bool(true)
        };
        assert_eq!(report["private_key_present"], expected, "{report}");
    }

    #[test]
    fn parse_admits_canonical_shapes_only() {
        let ok = |values: &[&str]| parse(&argv(values)).is_some();
        assert!(ok(&["status"]));
        assert!(ok(&[
            "status",
            "--json",
            "--profile",
            "a",
            "--profile-dir",
            "d"
        ]));
        assert!(ok(&["update", "--profile-dir", "d"]));
        assert!(ok(&["verify", "--profile", ""]));
        assert!(ok(&[
            "setup",
            "--accept-access",
            "--catalog",
            "full",
            "--tunnel-id",
            "x"
        ]));
        for shape in [
            &[][..],
            &["doctor"],
            &["status", "--json", "--json"],
            &["status", "--profile"],
            &["status", "--profile", "-x"],
            &["status", "--profile=dot"],
            &["status", "--prof", "dot"],
            &["status", "extra"],
            &["update", "--json"],
            &["verify", "--accept-access"],
            &["setup", "--catalog", "all"],
            &["setup", "--read-key"],
            &["setup", "--key-file", "k"],
            &["setup", "--start"],
            &["setup", "--open-browser", "keys"],
            &["status", "-h"],
        ] {
            assert!(!ok(shape), "{shape:?}");
        }
    }
}
