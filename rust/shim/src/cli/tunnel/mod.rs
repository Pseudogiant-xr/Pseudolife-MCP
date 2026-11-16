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

mod bridge;
mod doctor_section;
#[cfg(windows)]
mod dpapi;
mod record;
mod store;
mod syntax;

pub(super) use doctor_section::doctor_section;
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

/// A command's answer: its stdout and exit, or a refusal for stderr
/// (exit 2), which may name the file it is about.
type Answer = Result<(String, u8), String>;

fn answer(output: Output) -> Option<Answer> {
    match output {
        Ok(printed) => Some(Ok(printed)),
        Err(Fail::Tunnel(message)) => Some(Err(message.to_owned())),
        Err(Fail::Defer) => None,
    }
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
    // The hidden bridge the vendor's tunnel-client starts (`tunnel shim`).
    if values.first().is_some_and(|command| command == "shim") {
        return Some(bridge::run(&values[1..]));
    }
    let options = parse(&values)?;
    let (stdout, stderr, code) = match outcome(&options)? {
        Ok((text, code)) => (text, String::new(), code),
        Err(message) => (String::new(), format!("{message}\n"), 2),
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
fn outcome(options: &Options) -> Option<Answer> {
    let root = store::store_root(options.profile_dir.as_deref());
    // `update` runs unattended inside `pseudolife-mcp update`, which shows
    // its refusal: a root it cannot open is named, never deferred.
    let root = match root {
        Err(_) if options.command == Command::Update => {
            return Some(Err(store::UNSAFE_ROOT.to_owned()));
        }
        root => root.ok()?,
    };
    answer(match options.command {
        Command::Status => status(options, &root),
        Command::Update => return Some(update(&root)),
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

/// Why `status` cannot read a profile's verification challenge.
#[derive(Clone, Copy, Debug, PartialEq)]
enum ChallengeIssue {
    /// A form `private_read` does not open (a pipe, a link or a junction).
    Form,
    /// Nested near CPython's recursion limit, which would raise out of
    /// `verification_status`; this bound leaves only ordinary records.
    Deep,
}

fn challenge_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}-verification"))
        .join("challenge.json")
}

/// What stops `status` reading the challenge record, if anything. Shared by
/// `status` and `doctor`, so doctor never names `status` where it defers.
fn challenge_issue(root: &Path, name: &str) -> Option<ChallengeIssue> {
    // `_challenge` reads through private_read: a redirect, an unsafe or
    // missing file, or one too large is no challenge at all (`{}`).
    match store::private_read(&challenge_path(root, name), 65536) {
        Ok(data) if data.iter().filter(|b| matches!(b, b'[' | b'{')).count() > 100 => {
            Some(ChallengeIssue::Deep)
        }
        Ok(_) | Err(Fail::Tunnel(_)) => None,
        Err(Fail::Defer) => Some(ChallengeIssue::Form),
    }
}

fn shallow_challenge(root: &Path, name: &str) -> Result<(), Fail> {
    challenge_issue(root, name).map_or(Ok(()), |_| Err(Fail::Defer))
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
fn record_sentence(name: &str, record: &record::Record, updating: bool) -> String {
    let file = record.path.display();
    let left = if updating {
        "; it was left as it is"
    } else {
        ""
    };
    match record.liveness {
        Liveness::Running if updating => format!(
            "tunnel {name} keeps running on its current runtime (process record {file}); this client cannot refresh or stop it yet, so it was left as it is"
        ),
        Liveness::Running => format!(
            "tunnel {name} is running (process record {file}); this client cannot refresh or stop it yet"
        ),
        Liveness::Gone => format!(
            "tunnel {name} is not running; its process record {file} is left from a tunnel that stopped and can be deleted"
        ),
        Liveness::Unknown => format!(
            "whether tunnel {name} runs is unknown: its process record {file} {}{left}",
            unknown_cause(record.why)
        ),
    }
}

/// The one cause the code knows for an unknown verdict, and what to do.
fn unknown_cause(why: Option<record::Why>) -> &'static str {
    use record::Why;
    match why {
        Some(Why::Unreadable) | None => {
            "is not an owner-only JSON record this client can read; check that file"
        }
        Some(Why::NoProcessId) => {
            "names no process id; look at that file, and keep it while a tunnel may still run"
        }
        Some(Why::NoTable) => {
            "cannot be checked, because this machine's process table cannot be read; run the command again once it can"
        }
        Some(Why::Uninspectable) => {
            "names a process that exists but this account cannot inspect; check which account runs it"
        }
        Some(Why::OutOfRange) => {
            "names a process id out of range (0, or larger than any process id); look at that file, and keep it while a tunnel may still run"
        }
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
    let cloud_checked = matches!(running, None | Some(Liveness::Gone));
    if as_json {
        let record_json = record.map_or(String::new(), |record| {
            format!(
                ", \"process_record\": {{\"path\": {}, \"detail\": {}}}",
                quoted(&record.path.display().to_string()),
                quoted(&record_sentence(name, record, false)),
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
        let sentence = record_sentence(name, record, false);
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
fn update(root: &Path) -> Answer {
    // A store this client cannot list is a refusal with its cause, never a
    // deferral: the update step shows the cause to the user.
    let unreadable = |fail: Fail| match fail {
        Fail::Tunnel(store::INVALID_NAME) => match store::invalid_profile_entry(root) {
            Some(path) => format!(
                "{} is not a valid tunnel profile name (up to 64 letters, digits, '-' or '_', starting with a letter or digit, and not a reserved device name); rename it or move it out of the tunnel directory, then retry",
                path.display()
            ),
            None => format!(
                "{} in the tunnel directory {}; rename or move out the profile file whose name is not valid, then retry",
                store::INVALID_NAME,
                root.display()
            ),
        },
        Fail::Tunnel(store::NOT_OWNER_ONLY) => format!(
            "the tunnel directory {} must be owner-only; make it a directory only your account can access, then retry",
            root.display()
        ),
        Fail::Tunnel(message) => format!(
            "{message} (tunnel directory {}); fix that, then retry",
            root.display()
        ),
        Fail::Defer => store::list_refusal(root),
    };
    let present = exists(root).map_err(unreadable)?;
    let names = if present {
        store::list(root).map_err(unreadable)?
    } else {
        Vec::new()
    };
    let orphans = if present {
        record::orphans(root, &names).map_err(unreadable)?
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
            Err(Fail::Tunnel(reason)) => {
                return Err(format!(
                    "{reason}: {}; fix that file or move it out of the tunnel directory, then retry",
                    store::profile_file(root, name).display()
                ));
            }
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
                "unread",
                "false",
                format!("{unread}; it has no tunnel process record, so no tunnel of it runs"),
            ),
            (Some(record), unread) => {
                // A stopped tunnel needs nothing and leaves the update
                // `current`; an unread profile is named so the user can fix it.
                // `stale`: a tunnel that runs or may run; `unread`: a profile
                // this client cannot read, whose tunnel does not run.
                let state = match (&unread, record.liveness) {
                    (Err(_), Liveness::Gone) => "not-running",
                    (Ok(_), Liveness::Gone) => "unread",
                    _ => "stale",
                };
                let sentence = record_sentence(name, record, true);
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
        // `leftover`: a file a stopped tunnel left; `stale`: one that runs or
        // may run, left as it is.
        let (state, running, sentence) = match record.liveness {
            Liveness::Running => (
                "stale",
                "true",
                format!(
                    "{file} has no saved profile beside it but names a running process; this client cannot stop it, so it was left as it is"
                ),
            ),
            Liveness::Gone => (
                "leftover",
                "false",
                format!(
                    "{file} has no saved profile beside it and names no running process; it can be deleted"
                ),
            ),
            Liveness::Unknown => (
                "stale",
                "\"unknown\"",
                format!(
                    "{file} has no saved profile beside it, and whether a tunnel of it runs is unknown: it {}; it was left as it is",
                    unknown_cause(record.why)
                ),
            ),
        };
        stale |= state == "stale";
        entries.push(format!(
            "{{\"record\": {}, \"state\": \"{state}\", \"running\": {running}, \"detail\": {}}}",
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
        /// A key file in this platform's saved format. On Windows the bytes
        /// are a stand-in no account can unseal (CryptUnprotectData reads it
        /// as damaged), so tests that only need a key file also run in a
        /// key-based SSH logon, which cannot seal one.
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

    fn report_of<E: std::fmt::Debug>(output: Result<(String, u8), E>) -> Value {
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
    fn update_names_an_orphan_record_by_file() {
        let store = Store::new();
        store.record("gone", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        // No profile at all: the record alone is reported, not exit 3. A
        // dead one is a `leftover` (nothing runs), never `stale`.
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "current", "{report}");
        let entry = &report["profiles"][0];
        assert_eq!(entry["record"], "gone.process.json");
        assert_eq!(entry["state"], "leftover");
        assert_eq!(entry["running"], false);
        let detail = report["detail"].as_str().unwrap();
        assert!(
            detail.contains("gone.process.json has no saved profile beside it and names no running process; it can be deleted"),
            "{detail}"
        );
        // A running orphan is `stale`, left as it is.
        store.record("live", u64::from(std::process::id()), &own_exe());
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "stale", "{report}");
        assert_eq!(report["profiles"][1]["record"], "live.process.json");
        assert_eq!(report["profiles"][1]["state"], "stale");
        std::fs::remove_file(store.0.join("live.process.json")).unwrap();
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
        // A record naming no usable process id proves nothing either way:
        // unknown, with that cause, and never called deletable.
        for shape in [
            &b"{}"[..],
            br#"{"pid": "12"}"#,
            br#"{"pid": -3}"#,
            br#"{"pid": 1.5}"#,
        ] {
            std::fs::remove_file(store.0.join("dot.process.json")).unwrap();
            store.put("dot.process.json", shape);
            let report = report_of(update(&store.0));
            let shape = String::from_utf8_lossy(shape);
            assert_eq!(report["state"], "stale", "{shape}: {report}");
            assert_eq!(report["profiles"][0]["running"], "unknown", "{shape}");
            let detail = report["profiles"][0]["detail"].as_str().unwrap();
            assert!(
                detail.contains("names no process id") && !detail.contains("can be deleted"),
                "{shape}: {detail}"
            );
        }
    }

    /// The listing's other refusals name the directory or the file too.
    #[test]
    fn update_names_the_file_or_directory_a_listing_refusal_is_about() {
        let store = Store::new();
        store.put("bad.name.profile.json", b"{}");
        let refused = update(&store.0).unwrap_err();
        assert!(
            refused.contains("bad.name.profile.json is not a valid tunnel profile name")
                && refused.ends_with("then retry"),
            "{refused}"
        );
        // Windows folds the suffix but `list` takes the stem case-sensitively:
        // `dot.PROFILE.JSON` is refused as a whole name, and named.
        #[cfg(windows)]
        {
            let store = Store::new();
            store.put("dot.PROFILE.JSON", b"{}");
            let refused = update(&store.0).unwrap_err();
            assert!(
                refused.contains("dot.PROFILE.JSON is not a valid tunnel profile name"),
                "{refused}"
            );
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let store = Store::new();
            store.profile("dot", true);
            std::fs::set_permissions(&store.0, std::fs::Permissions::from_mode(0o755)).unwrap();
            let refused = update(&store.0).unwrap_err();
            assert_eq!(
                refused,
                format!(
                    "the tunnel directory {} must be owner-only; make it a directory only your account can access, then retry",
                    store.0.display()
                )
            );
        }
    }

    /// An out-of-range process id reaches the update's own words.
    #[test]
    fn update_reports_an_out_of_range_process_id_as_unknown() {
        let store = Store::new();
        store.profile("dot", true);
        store.put("dot.process.json", br#"{"pid": 0}"#);
        let report = report_of(update(&store.0));
        assert_eq!(report["state"], "stale", "{report}");
        let detail = report["profiles"][0]["detail"].as_str().unwrap();
        assert!(
            detail.contains("names a process id out of range")
                && !detail.contains("can be deleted"),
            "{detail}"
        );
    }

    /// `list_refusal`'s other causes: the root is a file, the root cannot be
    /// listed (Unix, as a non-root account), and a non-UTF-8 name (Linux;
    /// APFS refuses such names).
    #[test]
    fn update_names_each_listing_cause_it_observed() {
        let store = Store::new();
        let file = store.0.parent().unwrap().join("not-a-directory");
        std::fs::write(&file, b"").unwrap();
        let refused = update(&file).unwrap_err();
        assert!(
            refused.contains("not-a-directory is not a directory; move it aside, then retry"),
            "{refused}"
        );
        #[cfg(unix)]
        if !rustix::process::geteuid().is_root() {
            use std::os::unix::fs::PermissionsExt;
            let store = Store::new();
            store.profile("dot", true);
            // Owner-only, but not readable: the listing itself fails.
            std::fs::set_permissions(&store.0, std::fs::Permissions::from_mode(0o300)).unwrap();
            let refused = update(&store.0);
            std::fs::set_permissions(&store.0, std::fs::Permissions::from_mode(0o700)).unwrap();
            let refused = refused.unwrap_err();
            assert!(
                refused.contains("cannot be listed; make it a directory your account can read"),
                "{refused}"
            );
            // A parent the account cannot search: the root cannot even be
            // inspected.
            let parent = store.0.parent().unwrap().to_owned();
            std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o000)).unwrap();
            let refused = update(&store.0);
            std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o700)).unwrap();
            let refused = refused.unwrap_err();
            assert!(
                refused.contains("cannot be inspected; make it a directory your account can read"),
                "{refused}"
            );
        }
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::ffi::OsStrExt;
            let store = Store::new();
            let name = std::ffi::OsStr::from_bytes(b"bad\xffname");
            std::fs::write(store.0.join(name), b"").unwrap();
            let refused = update(&store.0).unwrap_err();
            assert!(
                refused.contains("holds an entry whose name is not UTF-8 (bad\u{fffd}name)"),
                "{refused}"
            );
        }
    }

    /// A store `list` cannot read is refused (exit 2) naming the entry it
    /// stopped on, never deferred.
    #[test]
    fn update_refuses_a_store_entry_it_does_not_read_and_names_it() {
        let store = Store::new();
        store.profile("dot", true);
        std::fs::create_dir(store.0.join("x.profile.json")).unwrap();
        let refused = update(&store.0).unwrap_err();
        assert!(
            refused.contains("x.profile.json") && refused.contains("is not a regular file"),
            "{refused}"
        );
        assert_eq!(
            outcome(
                &parse(&argv(&[
                    "update",
                    "--profile-dir",
                    store.0.to_str().unwrap()
                ]))
                .unwrap()
            ),
            Some(Err(refused))
        );
        #[cfg(windows)]
        {
            let store = Store::new();
            store.profile("dot", true);
            store.put("na\u{ef}ve.txt", b"");
            let refused = update(&store.0).unwrap_err();
            assert!(
                refused.contains("na\u{ef}ve.txt") && refused.contains("not plain ASCII"),
                "{refused}"
            );
        }
    }

    #[test]
    fn update_still_refuses_an_invalid_profile_and_says_when_nothing_is_saved() {
        let store = Store::new();
        assert_eq!(update(&store.0).unwrap(), (format!("{NO_PROFILES}\n"), 3));
        store.profile("dot", true);
        store.record("dot", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        store.put("bad.profile.json", b"{");
        let refused = update(&store.0).unwrap_err();
        assert!(
            refused.starts_with("tunnel profile is invalid: ")
                && refused.contains("bad.profile.json")
                && refused.ends_with(
                    "; fix that file or move it out of the tunnel directory, then retry"
                ),
            "{refused}"
        );
    }

    /// Each profile shape `load` does not read is named, with its reason,
    /// beside what its record says; the update still answers (exit 0).
    #[test]
    fn update_names_each_profile_it_cannot_read_and_still_answers() {
        fn token(store: &Store) -> String {
            serde_json::to_string(&store.0.join("token")).unwrap()
        }
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
            // Named `unread`; nothing runs, so the update is `current`.
            let report = report_of(update(&store.0));
            assert_eq!(report["state"], "current", "{shape}: {report}");
            let entry = &report["profiles"][0];
            assert_eq!(entry["profile"], "dot", "{shape}");
            assert_eq!(entry["state"], "unread", "{shape}");
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
            assert_eq!(report["profiles"][0]["state"], "unread", "{shape}");
            // A running tunnel of an unread profile is `stale`, left as it is.
            std::fs::remove_file(store.0.join("dot.process.json")).unwrap();
            store.record("dot", u64::from(std::process::id()), &own_exe());
            let report = report_of(update(&store.0));
            assert_eq!(report["state"], "stale", "{shape}: {report}");
            assert_eq!(report["profiles"][0]["state"], "stale", "{shape}");
        }
    }

    #[test]
    fn update_refuses_a_store_root_it_cannot_open_with_the_cause() {
        // An empty --profile-dir: update names it, status still defers.
        let answer =
            |command: &str| outcome(&parse(&argv(&[command, "--profile-dir", ""])).unwrap());
        assert_eq!(answer("update"), Some(Err(store::UNSAFE_ROOT.to_owned())));
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
            Some(Err(store::UNSAFE_ROOT.to_owned()))
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
            text.starts_with("Profile dot: ready; runtime running=True, ready=False; cloud verification not checked by this client.\nTunnel dot is running (process record "),
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
        // The Windows stand-in is no DPAPI blob: no usable key.
        let expected = if cfg!(windows) {
            Value::Bool(false)
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

    /// The doctor entry for `dot`, as one JSON text and its `next` lines.
    fn doctor_entry(store: &Store) -> (Value, Vec<String>) {
        let entries = doctor_section(&store.0).expect("a tunnel section");
        let entry = entries
            .into_iter()
            .find(|entry| entry["profile"] == "dot")
            .expect("an entry for dot");
        let next = entry["next"]
            .as_array()
            .map(|lines| {
                lines
                    .iter()
                    .map(|l| l.as_str().unwrap().to_owned())
                    .collect()
            })
            .unwrap_or_default();
        assert!(!entry.to_string().contains("Python"), "{entry}");
        (entry, next)
    }

    const FAILED_REFRESH: &[u8] =
        b"{\"id\": \"0123456789abcdef0123456789abcdef\", \"state\": \"failed\"}";

    #[test]
    fn doctor_names_tunnel_status_where_it_answers_beside_a_process_record() {
        let store = Store::new();
        store.profile("dot", true);
        store.key("dot");
        store.put("dot.reload.result.json", FAILED_REFRESH);
        store.record("dot", DEAD_PID, Path::new("/nonexistent/tunnel-client"));
        let (entry, next) = doctor_entry(&store);
        assert_eq!(entry["runtime"]["process_record"], true, "{entry}");
        assert_eq!(
            next,
            [
                "Last tunnel refresh: failed; needs attention: pseudolife-mcp tunnel status --profile dot --json shows the outcome and the key's state."
            ]
        );
        // The advice is true: native `tunnel status --json` answers this
        // store with both.
        let options = parse(&argv(&["status", "--json"])).unwrap();
        let report = report_of(status(&options, &store.0));
        assert_eq!(report["update"]["state"], "failed", "{report}");
        assert!(report.get("private_key_present").is_some(), "{report}");
    }

    #[test]
    fn doctor_names_the_challenge_file_status_cannot_read_and_why() {
        let store = Store::new();
        store.profile("dot", true);
        store.key("dot");
        store.put("dot.reload.result.json", FAILED_REFRESH);
        crate::board::state::private_dir(&store.0.join("dot-verification")).unwrap();
        store.put("dot-verification/challenge.json", &[b'['; 150]);
        let (_, next) = doctor_entry(&store);
        assert_eq!(
            next,
            [format!(
                "Last tunnel refresh: failed; needs attention: pseudolife-mcp tunnel status --profile dot does not read the verification challenge {}, which is nested too deeply to be one this client wrote; move that file aside.",
                challenge_path(&store.0, "dot").display()
            )]
        );
        // Doctor names that file because `status` really does not answer.
        let options = parse(&argv(&["status", "--json"])).unwrap();
        assert_eq!(status(&options, &store.0).unwrap_err(), Fail::Defer);
        // Moved aside, `status` answers and doctor names it instead.
        std::fs::remove_file(challenge_path(&store.0, "dot")).unwrap();
        assert!(status(&options, &store.0).is_ok());
        let (_, next) = doctor_entry(&store);
        assert!(
            next[0].ends_with(
                "tunnel status --profile dot --json shows the outcome and the key's state."
            ),
            "{next:?}"
        );
    }

    #[test]
    fn doctor_names_the_file_a_profile_could_not_be_read_from_without_naming_python() {
        let store = Store::new();
        store.put("bad.profile.json", b"{not json");
        store.profile("dot", false);
        let entries = doctor_section(&store.0).expect("a tunnel section");
        let bad = &entries[0];
        assert_eq!(bad["profile"], "bad");
        let recovery = bad["recovery"].as_str().unwrap();
        assert!(!recovery.contains("Python"), "{recovery}");
        assert_eq!(
            recovery,
            format!(
                "Check that {} is an owner-only file holding a tunnel profile; if it is left from an abandoned setup, move it aside; this client cannot set a tunnel up yet.",
                store.0.join("bad.profile.json").display()
            )
        );
    }

    #[test]
    fn doctor_names_the_expiry_date_and_where_to_create_a_new_key() {
        let store = Store::new();
        store.put(
            "dot.profile.json",
            format!(
                "{{\"name\": \"dot\", \"daemon_url\": \"http://127.0.0.1:9\", \"token_file\": {}, \"tunnel_id\": \"tunnel_0123\", \"state\": \"ready\", \"consent\": true, \"runtime_key_expires_at\": \"2020-01-01T00:00:00+00:00\"}}",
                serde_json::to_string(&store.0.join("token")).unwrap()
            )
            .as_bytes(),
        );
        let (_, next) = doctor_entry(&store);
        assert_eq!(
            next,
            [
                "The tunnel key expired on 2020-01-01T00:00:00+00:00; create a new restricted key at https://platform.openai.com/settings/organization/api-keys; this client cannot save it yet."
            ]
        );
    }

    /// A key file as the Python client's `set_key` writes it on Windows, or
    /// `None` in a key-based SSH logon, which cannot seal (said on stderr).
    #[cfg(windows)]
    fn sealed(key: &[u8]) -> Option<Vec<u8>> {
        Some([&b"DPAPI\0"[..], &dpapi::seal(key)?].concat())
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_protected_key_is_unsealed_and_checked() {
        let Some(key) = sealed(b"fixture-tunnel-key") else {
            return;
        };
        let store = Store::new();
        store.profile("dot", true);
        store.put("dot.key", &key);
        assert_eq!(store::key_present(&store.0, "dot"), Ok(true));
        let options = parse(&argv(&["status", "--json"])).unwrap();
        let report = report_of(status(&options, &store.0));
        assert_eq!(report["private_key_present"], true, "{report}");
        let (entry, next) = doctor_entry(&store);
        assert_eq!(entry["private_key_present"], true, "{entry}");
        assert!(next.is_empty(), "{next:?}");
        // Without a process record, verify refuses on the identity read
        // instead of deferring.
        let verify_options = parse(&argv(&["verify"])).unwrap();
        assert_eq!(
            verify(&verify_options, &store.0).unwrap_err(),
            Fail::Tunnel(store::UNAVAILABLE)
        );
        let replace = |data: &[u8]| {
            std::fs::remove_file(store.0.join("dot.key")).unwrap();
            store.put("dot.key", data);
        };
        // It unseals, but not to a key.
        replace(&sealed(b"two words").unwrap());
        assert_eq!(store::key_state(&store.0, "dot"), Ok(Some(false)));
        // Damaged ciphertext, a truncated blob, and bytes that are no blob:
        // no usable key, never "unlock it elsewhere".
        let mut flipped = key.clone();
        let at = flipped.len() - 5;
        flipped[at] ^= 1;
        replace(&flipped);
        assert_eq!(store::key_state(&store.0, "dot"), Ok(Some(false)));
        replace(&key[..key.len() - 20]);
        assert_eq!(store::key_state(&store.0, "dot"), Ok(Some(false)));
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_key_file_holding_no_blob_is_no_key() {
        // Needs no sealing, so it also runs in a key-based SSH logon.
        let store = Store::new();
        store.profile("dot", true);
        // No DPAPI blob, an empty blob after the prefix, a prefix without
        // its separator, and another host's protection.
        for data in [
            &b"DPAPI\0sealed-fixture"[..],
            b"DPAPI\0",
            b"DPAPI",
            b"PLAIN\0fixture-tunnel-key",
        ] {
            let _ = std::fs::remove_file(store.0.join("dot.key"));
            store.put("dot.key", data);
            assert_eq!(
                store::key_state(&store.0, "dot"),
                Ok(Some(false)),
                "{data:?}"
            );
            let (entry, next) = doctor_entry(&store);
            assert_eq!(entry["private_key_present"], false, "{data:?}");
            // No key is no unlock problem: doctor adds no line for it.
            assert!(next.is_empty(), "{data:?}: {next:?}");
        }
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_key_this_session_cannot_unseal_is_named_not_guessed() {
        let Some(mut key) = sealed(b"fixture-tunnel-key") else {
            return;
        };
        // A blob naming a master key this account does not hold, as another
        // account's key does (CryptUnprotectData: NTE_BAD_KEY_STATE). The
        // GUID sits after the 6-byte prefix and the blob's version and
        // provider fields.
        for byte in &mut key[6 + 24..6 + 40] {
            *byte ^= 0x5a;
        }
        let store = Store::new();
        store.profile("dot", true);
        store.put("dot.key", &key);
        assert_eq!(store::key_state(&store.0, "dot"), Ok(None));
        let options = parse(&argv(&["status", "--json"])).unwrap();
        let report = report_of(status(&options, &store.0));
        assert_eq!(report["private_key_present"], "not_verified", "{report}");
        // verify cannot tell whether the key is finished: it defers,
        // never "finish tunnel setup".
        let verify_options = parse(&argv(&["verify"])).unwrap();
        assert_eq!(verify(&verify_options, &store.0).unwrap_err(), Fail::Defer);
        let (entry, next) = doctor_entry(&store);
        assert_eq!(entry["private_key_present"], "not_verified", "{entry}");
        assert_eq!(
            next,
            [format!(
                "The saved tunnel key {} cannot be unlocked in this session: a Windows-protected key opens only for the account that saved it, in a logon signed in with its password (not a key-based SSH logon), so run this check from such a logon.",
                store.0.join("dot.key").display()
            )]
        );
    }
}
