//! `pseudolife-mcp tunnel`: profile setup, status, and read-only checks of
//! the saved tunnel runtime, plus the vendor client's stdio bridge.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::{Path, PathBuf},
    process::ExitCode,
};

use chrono::Utc;
use serde_json::{Map, Value};

use super::hook_json::quoted;

mod argv;
mod bridge;
mod doctor_section;
#[cfg(windows)]
mod dpapi;
mod record;
mod refusal;
mod runtime;
mod setup;
mod store;
mod syntax;
mod vendor;

pub(super) use doctor_section::doctor_section;
use record::Liveness;
use store::{Profile, exists, occupied};
use syntax::{Admit, json_loads, refresh_id};

/// Why a command stops: a refusal (exit 2), or a file in a form this client
/// does not read, which `outcome` turns into a refusal naming it.
#[derive(Debug, PartialEq)]
pub(super) enum Fail {
    Tunnel(&'static str),
    Detail(String),
    Defer,
}

/// A command's answer: its stdout and exit, or a refusal for stderr
/// (exit 2), which may name the file it is about.
type Answer = Result<(String, u8), String>;

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
    Run,
    Stop,
}

#[derive(Default)]
struct Setup {
    accept_access: bool,
    read_key: bool,
    restore_key: bool,
    discard_previous_key: bool,
    key_file: Option<String>,
    daemon_url: Option<String>,
    token_file: Option<String>,
    tunnel_id: Option<String>,
    organization_id: Option<String>,
    catalog: Option<String>,
    key_expires_at: Option<String>,
}

impl Setup {
    fn profile_fields(&self) -> [(&'static str, &Option<String>); 4] {
        [
            ("tunnel_id", &self.tunnel_id),
            ("organization_id", &self.organization_id),
            ("minimum_catalog", &self.catalog),
            ("runtime_key_expires_at", &self.key_expires_at),
        ]
    }

    fn edit(&self, payload: &mut Map<String, Value>) {
        for (field, value) in self.profile_fields() {
            if let Some(value) = value {
                payload.insert(field.into(), Value::String(value.clone()));
            }
        }
    }

    fn has_profile_settings(&self) -> bool {
        self.key_expires_at.is_some() || self.has_non_expiry_settings()
    }

    fn has_non_expiry_settings(&self) -> bool {
        self.daemon_url.is_some()
            || self.token_file.is_some()
            || self.tunnel_id.is_some()
            || self.organization_id.is_some()
            || self.catalog.is_some()
    }

    fn has_key_input(&self) -> bool {
        self.read_key || self.key_file.is_some()
    }
}

struct Options {
    command: Command,
    profile: String,
    profile_dir: Option<String>,
    json: bool,
    setup: Setup,
}

/// The options of an argv `argv::read` admits, for tests.
#[cfg(test)]
fn parse(values: &[OsString]) -> Option<Options> {
    match argv::read(values) {
        Ok(argv::Parsed::Run(options)) => Some(*options),
        _ => None,
    }
}

pub(super) fn run(values: Vec<OsString>) -> ExitCode {
    // The hidden bridge the vendor's tunnel-client starts (`tunnel shim`).
    if values.first().is_some_and(|command| command == "shim") {
        return bridge::run(&values[1..]);
    }
    let (stdout, stderr, code) = match argv::read(&values) {
        Ok(argv::Parsed::Help(text)) => (text, String::new(), 0),
        Ok(argv::Parsed::Run(options)) => match outcome(&options) {
            Ok((text, code)) => (text, String::new(), code),
            Err(message) => (String::new(), format!("{message}\n"), 2),
        },
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
    if stdout_refused {
        ExitCode::from(STDOUT_REFUSED)
    } else if written.is_ok() {
        ExitCode::from(code)
    } else {
        ExitCode::FAILURE
    }
}

/// The command's answer. A store root that cannot be opened, or a file in
/// a form this client does not read, is a refusal naming it.
fn outcome(options: &Options) -> Answer {
    let Ok(root) = store::store_root(options.profile_dir.as_deref()) else {
        return Err(store::UNSAFE_ROOT.to_owned());
    };
    if matches!(options.command, Command::Status | Command::Setup) {
        setup::warn_for_stages(&root);
    }
    let output = match options.command {
        Command::Status => status(options, &root),
        Command::Update => return update(&root),
        Command::Verify => verify(options, &root),
        Command::Setup => setup(options, &root),
        Command::Run => runtime::run(&root, &options.profile),
        Command::Stop => runtime::stop(&root, &options.profile),
    };
    match output {
        Ok(printed) => Ok(printed),
        Err(Fail::Tunnel(message)) => Err(message.to_owned()),
        Err(Fail::Detail(message)) => Err(message),
        Err(Fail::Defer) => Err(refusal::unread(options, &root)),
    }
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
        Err(Fail::Tunnel(_) | Fail::Detail(_)) => Ok(None),
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
        Ok(_) | Err(Fail::Tunnel(_) | Fail::Detail(_)) => None,
        Err(Fail::Defer) => Some(ChallengeIssue::Form),
    }
}

fn shallow_challenge(root: &Path, name: &str) -> Result<(), Fail> {
    challenge_issue(root, name).map_or(Ok(()), |_| Err(Fail::Defer))
}

fn status(options: &Options, root: &Path) -> Output {
    let profile = store::load(root, &options.profile)?;
    let key = store::key_state(root, &profile.name)?;
    setup::check_pending(root, &profile.name)?;
    let key = KeyStatus {
        readable: key,
        saved: occupied(&store::key_path(root, &profile.name))?,
        retained: setup::validate_previous_key(root, &profile.name)?,
    };
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
    let native_refresh = runtime::restart::status(root, &profile.name);
    let cloud = match &record {
        Some(record) if record.native => Some(bridge::cloud_status(root, &profile.name)),
        Some(record) if record.liveness != Liveness::Gone => None,
        _ => Some(bridge::Cloud::default()),
    };
    Ok((
        report(
            &profile,
            key,
            record.as_ref(),
            &refresh,
            native_refresh
                .as_ref()
                .map(|(state, detail)| (*state, detail.as_str())),
            cloud,
            options,
        ),
        0,
    ))
}

struct KeyStatus {
    readable: Option<bool>,
    saved: bool,
    retained: bool,
}

fn setup_command(name: &str, directory: Option<&str>) -> String {
    tunnel_command("setup", name, directory)
}

fn tunnel_command(action: &str, name: &str, directory: Option<&str>) -> String {
    let mut command = format!("pseudolife-mcp tunnel {action} --profile {name}");
    if let Some(directory) = directory {
        command.push_str(&format!(" --profile-dir {}", quote_tunnel_arg(directory)));
    }
    command
}

fn quote_tunnel_arg(text: &str) -> String {
    let quoted = crate::cli::update::seams::quote(text);
    if cfg!(windows) {
        // PowerShell treats each of these as a single quote delimiter.
        quoted
            .replace('‘', "‘‘")
            .replace('’', "’’")
            .replace('‚', "‚‚")
            .replace('‛', "‛‛")
    } else {
        quoted
    }
}

fn legacy_handoff(name: &str, record: &record::Record) -> String {
    let directory = record.path.parent().and_then(Path::to_str);
    if let Some((console, directory)) = record
        .legacy_console
        .as_deref()
        .and_then(Path::to_str)
        .zip(directory)
    {
        let invoke = if cfg!(windows) { "& " } else { "" };
        return format!(
            "Stop it with `{invoke}{} tunnel stop --profile {name} --profile-dir {}`",
            quote_tunnel_arg(console),
            quote_tunnel_arg(directory)
        );
    }
    let run = tunnel_command("run", name, directory);
    format!(
        "It keeps serving until its process ends, for example when this machine restarts. Once you confirm it has stopped, move its leftover process record at {} aside if it still exists. Start it natively with `{run}` in a terminal you keep open",
        record.path.display()
    )
}

fn legacy_profile_recovery(path: &Path) -> String {
    format!(
        "Restore the saved tunnel profile at {} from a readable, owner-only backup before stopping it, or let its process end",
        path.display()
    )
}

fn previous_key_hint(command: &str) -> String {
    format!(
        "Previous key retained until the replacement completes a control-plane poll. Stop the tunnel before restoring or discarding. Restore with `{command} --restore-key` if the replacement fails. Use `{command} --discard-previous-key` only after the replacement has worked."
    )
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
        Liveness::Running if record.native && updating => format!(
            "tunnel {name} keeps running (process record {file}). Check tunnel status before restarting it"
        ),
        Liveness::Running if record.native => format!(
            "tunnel {name} is running (process record {file}); use tunnel stop to stop its native supervisor"
        ),
        Liveness::Running if updating => format!(
            "the Python client started tunnel {name} (process record {file}). The native client cannot verify or restart that launch and left it running. {}",
            legacy_handoff(name, record)
        ),
        Liveness::Running => format!(
            "tunnel {name} is running (process record {file}). {}",
            legacy_handoff(name, record)
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
    key: KeyStatus,
    record: Option<&record::Record>,
    refresh: &Refresh,
    native_refresh: Option<(&str, &str)>,
    cloud: Option<bridge::Cloud>,
    options: &Options,
) -> String {
    let running = record.map(|record| record.liveness);
    let ready = running == Some(Liveness::Running) && record.is_some_and(|record| record.ready);
    let running_json = match running {
        Some(Liveness::Running) => "true",
        Some(Liveness::Unknown) => "\"unknown\"",
        Some(Liveness::Gone) | None => "false",
    };
    let key_json = match key.readable {
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
    let command = setup_command(name, options.profile_dir.as_deref());
    let verification_due = ready
        && native_refresh.is_some_and(|(state, _)| state == "refreshed")
        && !cloud.as_ref().is_some_and(|cloud| cloud.current);
    let previous = key.retained.then(|| previous_key_hint(&command));
    if options.json {
        let record_json = record.map_or(String::new(), |record| {
            format!(
                ", \"process_record\": {{\"path\": {}, \"detail\": {}}}",
                quoted(&record.path.display().to_string()),
                quoted(&record_sentence(name, record, false)),
            )
        });
        let cloud = cloud.as_ref().map_or_else(
            || "\"not_checked\"".into(),
            |cloud| {
                format!(
                    "{{\"verified\": {}, \"successful_calls\": {}, \"challenge_current\": {}}}",
                    cloud.successful == 2,
                    cloud.successful,
                    cloud.current
                )
            },
        );
        let previous = previous.as_ref().map_or_else(String::new, |hint| {
            format!(
                ", \"previous_key_retained\": true, \"previous_key_detail\": {}",
                quoted(hint)
            )
        });
        let native = native_refresh.map_or_else(String::new, |(state, detail)| {
            format!(", \"native_refresh\": {}", serde_json::json!({ "state": state, "detail": detail, "new_verification_required": verification_due }))
        });
        return format!(
            "{{\"profile\": {name_q}, \"state\": {}, \"catalog\": {}, \"private_key_present\": {key_json}, \"key_expiry\": {expiry}, \"runtime\": {{\"running\": {running_json}, \"profile\": {name_q}, \"ready\": {}, \"key_expiry\": {}{record_json}}}, \"cloud\": {cloud}, \"update\": {}{previous}{native}}}\n",
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
    let cloud = cloud.as_ref().map_or_else(
        || "cloud verification not checked by this client".into(),
        |cloud| {
            format!(
                "cloud verified={} ({}/2 calls)",
                if cloud.successful == 2 {
                    "True"
                } else {
                    "False"
                },
                cloud.successful
            )
        },
    );
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
    if refresh.needs_attention() && native_refresh.is_none() {
        let run = tunnel_command("run", name, options.profile_dir.as_deref());
        let next = if profile.state != "ready" {
            let status = tunnel_command("status", name, options.profile_dir.as_deref());
            format!(
                "finish this profile's setup first (`{status}` shows the step it needs), then start it with `{run}`"
            )
        } else if !key.saved {
            format!(
                "import its runtime key with `{command} --read-key` first, then start it with `{run}`"
            )
        } else if running == Some(Liveness::Gone) || running.is_none() {
            format!("start it with `{run}`")
        } else {
            format!("stop that tunnel with the client that started it, then start it with `{run}`")
        };
        text.push_str(&format!(
            "Last tunnel refresh: {}; needs attention. The Python client recorded it; {next} to run it on this client.\n",
            refresh.state()
        ));
    }
    if let Some((_, detail)) = native_refresh {
        text.push_str(&format!("Native refresh: {detail}.\n"));
        if verification_due {
            let verify = tunnel_command("verify", name, options.profile_dir.as_deref());
            text.push_str(&format!(
                "The refreshed launch needs a new challenge. Run `{verify}`.\n"
            ));
        }
    }
    if expired {
        if profile.state == "ready" && key.saved {
            text.push_str(&format!(
                "Renew privately: {command} --read-key --key-expires-at <ISO timestamp>.\n"
            ));
        } else {
            let id = if profile.tunnel_id.is_none() {
                " --tunnel-id <tunnel_id>"
            } else {
                ""
            };
            text.push_str(&format!(
                "Save the first key's settings: {command}{id} --key-expires-at <ISO timestamp>.\nImport the key privately: {command} --read-key.\n"
            ));
        }
    }
    if let Some(previous) = previous {
        text.push_str(&previous);
        text.push('\n');
    }
    text
}

/// `tunnel update`: check the running launch against this runtime and report
/// how to restart a stale tunnel. The release updater consumes this report.
fn update(root: &Path) -> Answer {
    update_with_runtime(root, std::env::current_exe().ok().as_deref())
}

fn update_with_runtime(root: &Path, executable: Option<&Path>) -> Answer {
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
        Fail::Detail(message) => message,
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
    let mut profiles = Vec::with_capacity(names.len());
    // Check every profile for a store refusal before asking any supervisor
    // to restart. A later invalid profile cannot hide an earlier result.
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
            Err(Fail::Detail(message)) => return Err(message),
            Err(Fail::Tunnel(reason)) => {
                return Err(format!(
                    "{reason}: {}; fix that file or move it out of the tunnel directory, then retry",
                    store::profile_file(root, name).display()
                ));
            }
        };
        profiles.push((name, unread));
    }
    let mut entries = Vec::new();
    let mut notes = Vec::new();
    let mut stale = false;
    let mut refreshed = false;
    let mut failed = false;
    for (name, unread) in profiles {
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
            (Some(record), Err(true))
                if record.native && record.ready && record.liveness == Liveness::Running =>
            {
                let (state, running, detail) = match check_running_launch(root, name, executable) {
                    Ok(None) => (
                        "current",
                        "true",
                        "already running on the current runtime".to_owned(),
                    ),
                    Ok(Some(outcome)) => (outcome.state, outcome.running, outcome.detail),
                    Err(cause) => ("stale", "true", cause),
                };
                (
                    state,
                    running,
                    format!(
                        "tunnel {name} (process record {}): {detail}",
                        record.path.display()
                    ),
                )
            }
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
                let sentence = match unread {
                    Ok(unread) if !record.native && record.liveness == Liveness::Running => {
                        let recovery = legacy_profile_recovery(&store::profile_file(root, name));
                        format!("{unread}. The running Python tunnel was left in place. {recovery}")
                    }
                    Ok(unread) => format!("{unread}; {}", record_sentence(name, record, true)),
                    Err(_) => record_sentence(name, record, true),
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
        refreshed |= state == "refreshed";
        failed |= state == "failed";
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
            "{{\"state\": \"{}\", \"needs_attention\": {failed}, \"detail\": {}, \"profiles\": [{}]}}\n",
            if failed {
                "failed"
            } else if stale {
                "stale"
            } else if refreshed {
                "refreshed"
            } else {
                "current"
            },
            quoted(&detail),
            entries.join(", ")
        ),
        if failed { 2 } else { 0 },
    ))
}

fn check_running_launch(
    root: &Path,
    name: &str,
    executable: Option<&Path>,
) -> Result<Option<runtime::restart::Report>, String> {
    let directory = root.to_str();
    let status = || bridge::status_command(name, directory);
    let explain = |failure| match failure {
        Fail::Detail(detail) => detail,
        Fail::Tunnel(message) => format!("{message}. Check with `{}`", status()),
        Fail::Defer => format!(
            "the private key state could not be read. Check with `{}`",
            status()
        ),
    };
    setup::check_pending(root, name).map_err(explain)?;
    if setup::validate_previous_key(root, name).map_err(explain)? {
        return Err(format!(
            "the previous key is still retained. Check with `{}`",
            status()
        ));
    }
    let executable = executable.ok_or_else(|| {
        "the installed client executable could not be identified. Run update through the installed launcher".to_owned()
    })?;
    if runtime::restart::vendor_changed(root, name) {
        let stop = tunnel_command("stop", name, directory);
        let run = tunnel_command("run", name, directory);
        return Err(format!(
            "the installed client pins a different vendor version. Run `{stop}`, then `{run}` to reconnect the tunnel"
        ));
    }
    match runtime::identity::active(root, name, executable, None) {
        Ok(_) => Ok(None),
        Err(runtime::identity::CHANGED) => {
            if let Some(outcome) = runtime::restart::attempt(root, name, executable) {
                return Ok(Some(outcome));
            }
            let stop = tunnel_command("stop", name, directory);
            let run = tunnel_command("run", name, directory);
            if runtime::restart::predates_refresh(root, name) {
                return Err(format!(
                    "the running launch differs from the current runtime or saved settings, and its supervisor predates native refresh. Run `{stop}`, then `{run}` to reconnect the tunnel if it runs in the foreground. If a service or scheduled task started it, restart that registration through its service manager"
                ));
            }
            Err(format!(
                "the running launch differs from the current runtime or saved settings. Run `{stop}`, then `{run}` to reconnect the tunnel"
            ))
        }
        Err(_) => Err(format!(
            "the running launch could not be verified against the current runtime and saved settings. Check with `{}`",
            status()
        )),
    }
}

fn verify(options: &Options, root: &Path) -> Output {
    setup::check_pending(root, &options.profile)?;
    let profile = store::load(root, &options.profile)?;
    if profile.state != "ready" || !store::key_present(root, &profile.name)? {
        return Err(Fail::Tunnel(UNFINISHED));
    }
    // A native challenge needs an active native record. Legacy launch
    // identities are outside this path.
    if occupied(&root.join(format!("{}.process.json", profile.name)))? {
        let path = record::path(root, &profile.name);
        let record = record::read(path.clone(), None);
        if record.as_ref().is_some_and(|record| record.native) {
            return runtime::restart::verify(root, &profile.name);
        }
        if record.is_none_or(|record| {
            matches!(
                record.why,
                Some(record::Why::Unreadable | record::Why::NoProcessId)
            )
        }) {
            return Err(Fail::Detail(format!(
                "cannot tell which launch the process record {} belongs to: it cannot be read or names no process. Check `{}`; once it reports the tunnel stopped, move that record aside, start the tunnel with `{}`, then retry",
                path.display(),
                tunnel_command("status", &profile.name, options.profile_dir.as_deref()),
                tunnel_command("run", &profile.name, options.profile_dir.as_deref())
            )));
        }
        return Err(Fail::Detail(format!(
            "tunnel verify checks only a launch this client started, and {} is not one. Check `{}`: if that tunnel still runs, stop it with the client that started it; once it reports the tunnel stopped, move the record aside, start the tunnel with `{}`, then retry",
            record::path(root, &profile.name).display(),
            tunnel_command("status", &profile.name, options.profile_dir.as_deref()),
            tunnel_command("run", &profile.name, options.profile_dir.as_deref())
        )));
    }
    Err(Fail::Tunnel(store::UNAVAILABLE))
}

fn environment(key: &str) -> Result<Option<String>, Fail> {
    match std::env::var_os(key) {
        None => Ok(None),
        Some(value) => value
            .into_string()
            .map(|value| Some(value).filter(|value| !value.is_empty()))
            .map_err(|_| {
                Fail::Detail(format!(
                    "the environment variable {key} is not valid text; unset it, or pass --daemon-url and --token-file explicitly"
                ))
            }),
    }
}

fn truthy(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|value| !value.is_empty())
}

/// `Path(token).expanduser().absolute()`, as text, for the shapes decided here.
fn absolute_token(token: &str) -> Result<String, Fail> {
    const ABSOLUTE: Fail = Fail::Tunnel(
        "pass the daemon credential file as an absolute path (on Windows with its drive letter, such as C:\\Users\\<user>\\token)",
    );
    if token.starts_with('~') || token.contains('\0') {
        return Err(ABSOLUTE);
    }
    if cfg!(windows) {
        let b = token.as_bytes();
        let drive = b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':';
        let rooted = b.first().is_some_and(|c| matches!(c, b'\\' | b'/'));
        let absolute = drive && b.get(2).is_some_and(|c| matches!(c, b'\\' | b'/'));
        if rooted || (drive && !absolute) {
            return Err(ABSOLUTE);
        }
    }
    crate::credentials::absolute_expanded(Path::new(token))
        .ok()
        .and_then(|path| path.to_str().map(str::to_owned))
        .ok_or(ABSOLUTE)
}

fn home() -> Result<PathBuf, Fail> {
    crate::credentials::expand_user(Path::new("~"))
        .ok()
        .filter(|home| !home.as_os_str().is_empty())
        .ok_or(Fail::Tunnel(
            "cannot find your home directory to look for a registered daemon; pass --daemon-url and --token-file explicitly",
        ))
}

/// `_discover`: the daemon connection setup would save.
fn discover(setup: &Setup) -> Result<(String, String), Fail> {
    let (url, token) = (truthy(&setup.daemon_url), truthy(&setup.token_file));
    if let (Some(url), Some(token)) = (url, token) {
        // The returned tuple validates the URL before resolving the path.
        checked_url(url, "--daemon-url")?;
        return Ok((url.to_owned(), absolute_token(token)?));
    }
    let env_file = environment("PSEUDOLIFE_MCP_TOKEN_FILE")?;
    let env_token = environment("PSEUDOLIFE_MCP_TOKEN")?;
    if env_file.is_none() && env_token.is_none() {
        // Registration env blocks: only their absence is decided here.
        let claude_dir = environment("CLAUDE_CONFIG_DIR")?;
        let codex_dir = environment("CODEX_HOME")?;
        let named = if claude_dir.is_some() || codex_dir.is_some() {
            " (CLAUDE_CONFIG_DIR and CODEX_HOME must name directories)"
        } else {
            ""
        };
        let claude = match claude_dir {
            Some(directory) => PathBuf::from(directory).join(".claude.json"),
            None => home()?.join(".claude.json"),
        };
        let codex = match codex_dir {
            Some(directory) => PathBuf::from(directory),
            None => home()?.join(".codex"),
        }
        .join("config.toml");
        let inspect = |path: &Path| {
            occupied(path).map_err(|_| {
                Fail::Detail(format!(
                    "cannot inspect {} to look for a client registration{named}; fix that path, or pass --daemon-url and --token-file (an owner-only credential file) explicitly, with --accept-access",
                    path.display()
                ))
            })
        };
        if inspect(&claude)? || inspect(&codex)? {
            return Err(Fail::Tunnel(
                "this client does not read the daemon connection from a Claude Code or Codex registration; pass --daemon-url and --token-file (an owner-only credential file) explicitly, with --accept-access",
            ));
        }
        return Err(Fail::Tunnel(AMBIGUOUS));
    }
    let Some(file) = token.map(str::to_owned).or(env_file) else {
        // Only a literal credential: setup requires a file.
        return Err(Fail::Tunnel(AMBIGUOUS));
    };
    let token = absolute_token(&file)?;
    let (selected, source) = match url {
        Some(url) => (url.to_owned(), "--daemon-url"),
        None => (
            environment("PSEUDOLIFE_MCP_DAEMON_URL")?.unwrap_or_else(|| DEFAULT_URL.into()),
            "PSEUDOLIFE_MCP_DAEMON_URL",
        ),
    };
    checked_url(&selected, source)?;
    Ok((selected, token))
}

/// `validate_url`, naming where an http(s) URL in a spelling this client
/// does not accept came from and the spellings it does. The URL itself is
/// not echoed: it may carry credentials.
fn checked_url(url: &str, source: &str) -> Result<(), Fail> {
    match syntax::validate_url(url) {
        Err(Fail::Defer) => Err(Fail::Detail(format!(
            "the URL in {source} is a spelling this client does not accept; use http://127.0.0.1:<port> or http://localhost:<port> in lower case, or an https:// URL with a plain host name"
        ))),
        other => other,
    }
}

const KEY_RENEWAL: &str = "renew the runtime key with tunnel setup --read-key or --key-file and an optional new --key-expires-at";

fn setup(options: &Options, root: &Path) -> Output {
    let path = store::profile_path(root, &options.profile)?;
    let settings = &options.setup;
    if settings.read_key && settings.key_file.is_some() {
        return Err(Fail::Tunnel(
            "use either --read-key or --key-file for private key intake",
        ));
    }
    let saved = occupied(&path)?;
    if let Some(recovered) = setup::recover_pending(options, root, &path)? {
        return Ok(recovered);
    }
    if settings.restore_key || settings.discard_previous_key {
        if settings.restore_key == settings.discard_previous_key
            || settings.has_key_input()
            || settings.has_profile_settings()
            || settings.accept_access
        {
            return Err(Fail::Tunnel(
                "use --restore-key or --discard-previous-key by itself with the saved profile",
            ));
        }
        return setup::finish_previous_key(options, root, &path);
    }
    if saved
        && settings.has_key_input()
        && settings.key_expires_at.is_some()
        && !occupied(&store::key_path(root, &options.profile))?
    {
        return Err(Fail::Tunnel(
            "save the expiry with tunnel setup --key-expires-at first, then import the private key with --read-key or --key-file in a separate invocation",
        ));
    }
    if settings.has_key_input()
        && (settings.has_non_expiry_settings()
            || settings.accept_access
            || (!saved && settings.key_expires_at.is_some()))
    {
        return Err(if saved {
            Fail::Tunnel(
                "import the private key with --read-key or --key-file by itself (with --key-expires-at for a renewal); save other settings in a separate tunnel setup invocation",
            )
        } else {
            Fail::Tunnel(
                "save profile settings first, then use --read-key or --key-file in a separate setup invocation",
            )
        });
    }
    if saved {
        if !settings.has_profile_settings() && !settings.has_key_input() && !settings.accept_access
        {
            return Err(Fail::Detail(format!(
                "profile {name} is saved; guided setup is not part of the native client. `{status}` shows what it still needs; resume it with that option of tunnel setup (--tunnel-id, --organization-id, --catalog, --key-expires-at, --daemon-url with --token-file, or --read-key or --key-file)",
                name = options.profile,
                status = tunnel_command("status", &options.profile, options.profile_dir.as_deref()),
            )));
        }
        return setup::resume(options, root, &path);
    }
    if settings.has_key_input() {
        return Err(Fail::Tunnel(
            "save a tunnel profile with a tunnel ID before importing its private runtime key",
        ));
    }
    let explicit = truthy(&settings.daemon_url).is_some() && truthy(&settings.token_file).is_some();
    let (url, token) = discover(settings)?;
    let mut payload = Map::new();
    payload.insert("name".into(), Value::String(options.profile.clone()));
    payload.insert("daemon_url".into(), Value::String(url));
    payload.insert("token_file".into(), Value::String(token));
    settings.edit(&mut payload);
    if explicit {
        setup::save(options, root, &path, payload)
    } else {
        let profile = store::validate(&payload)?;
        Err(Fail::Detail(format!(
            "guided setup is not part of the native client. To use the daemon this environment names, run tunnel setup with --daemon-url {} --token-file {} --accept-access",
            quote_tunnel_arg(&profile.daemon_url),
            quote_tunnel_arg(&profile.token_file)
        )))
    }
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
            detail.contains("the Python client started tunnel dot")
                && detail.contains("cannot verify or restart that launch and left it running")
                && detail.contains("It keeps serving until its process ends")
                && detail.contains(&tunnel_command("run", "dot", store.0.to_str()))
                && detail.contains("in a terminal you keep open")
                && !detail.contains("Use the Python client that started it"),
            "{detail}"
        );
        assert!(
            matches!(runtime::stop(&store.0, "dot"), Err(Fail::Detail(message))
            if message.contains("It keeps serving until its process ends")
                && message.contains(&tunnel_command("run", "dot", store.0.to_str()))
                && !message.contains("stop it with the client that started it"))
        );
        // Still running afterwards: the record and this process are untouched.
        assert!(store.0.join("dot.process.json").is_file());
    }

    #[test]
    fn legacy_status_and_handoff_render_literal_commands() {
        let mut record = record::Record {
            path: "tunnels/dot.process.json".into(),
            liveness: Liveness::Running,
            native: false,
            legacy_console: None,
            ready: true,
            why: None,
        };
        assert_eq!(
            record_sentence("dot", &record, false),
            "tunnel dot is running (process record tunnels/dot.process.json). It keeps serving until its process ends, for example when this machine restarts. Once you confirm it has stopped, move its leftover process record at tunnels/dot.process.json aside if it still exists. Start it natively with `pseudolife-mcp tunnel run --profile dot --profile-dir 'tunnels'` in a terminal you keep open"
        );
        let (console, command) = if cfg!(windows) {
            (
                r"old‘a’b‚c‛d\Scripts\pseudolife-mcp.exe",
                r"`& 'old‘‘a’’b‚‚c‛‛d\Scripts\pseudolife-mcp.exe' tunnel stop --profile dot --profile-dir 'tunnels'`",
            )
        } else {
            (
                "old‘a’b‚c‛d/bin/pseudolife-mcp",
                "`'old‘a’b‚c‛d/bin/pseudolife-mcp' tunnel stop --profile dot --profile-dir 'tunnels'`",
            )
        };
        record.legacy_console = Some(console.into());
        assert_eq!(
            legacy_handoff("dot", &record),
            format!("Stop it with {command}")
        );
        assert_eq!(
            record_sentence("dot", &record, true),
            format!(
                "the Python client started tunnel dot (process record tunnels/dot.process.json). The native client cannot verify or restart that launch and left it running. Stop it with {command}"
            )
        );
    }

    #[test]
    fn update_and_stop_name_the_retained_python_console() {
        let mut store = Store::new();
        let directory = store.0.with_file_name("tunnel’s data");
        std::fs::rename(&store.0, &directory).unwrap();
        store.0 = directory;
        store.profile("dot", true);
        store.key("dot");
        let venv = store.0.parent().unwrap().join("old runtime’s venv");
        let console = venv.join(if cfg!(windows) {
            "Scripts/pseudolife-mcp.exe"
        } else {
            "bin/pseudolife-mcp"
        });
        std::fs::create_dir_all(console.parent().unwrap()).unwrap();
        std::fs::write(&console, b"fixture console").unwrap();
        std::fs::write(venv.join("pyvenv.cfg"), b"fixture venv").unwrap();
        let exe = own_exe();
        let pid = std::process::id();
        let process = serde_json::json!({
            "pid": pid, "exe": exe,
            "supervisor": {"pid": pid, "exe": exe, "argv": [exe, console, "tunnel", "run"]}
        });
        store.put("dot.process.json", &serde_json::to_vec(&process).unwrap());

        let quote = |text| {
            let quoted = crate::cli::update::seams::quote(text);
            if cfg!(windows) {
                quoted.replace('’', "’’")
            } else {
                quoted
            }
        };
        let invoke = if cfg!(windows) { "& " } else { "" };
        let console = store::checked_path(&console).unwrap();
        let expected = format!(
            "{invoke}{} tunnel stop --profile dot --profile-dir {}",
            quote(console.to_str().unwrap()),
            quote(store.0.to_str().unwrap())
        );
        let report = report_of(update(&store.0));
        assert!(
            report["detail"].as_str().unwrap().contains(&expected),
            "{report}"
        );
        assert!(
            matches!(runtime::stop(&store.0, "dot"), Err(Fail::Detail(message))
            if message.contains(&expected) && !message.contains("stop it with the client that started it"))
        );
        assert!(store.0.join("dot.process.json").is_file());
    }

    #[test]
    fn stop_a_live_python_orphan_names_the_missing_profile() {
        let store = Store::new();
        store.record("dot", u64::from(std::process::id()), &own_exe());
        let outcome = runtime::stop(&store.0, "dot");
        let profile = store.0.join("dot.profile.json");
        assert!(
            matches!(outcome, Err(Fail::Detail(ref message))
                if message.contains("Restore the saved tunnel profile")
                    && message.contains(profile.to_str().unwrap())
                    && !message.contains("tunnel run")),
            "{outcome:?}"
        );
    }

    #[test]
    fn unreadable_profile_gets_repair_guidance_before_stopping_python_tunnel() {
        let store = Store::new();
        // A BOM reaches the unread-report path. Malformed JSON refuses earlier.
        store.put("dot.profile.json", b"\xef\xbb\xbf{}");
        assert!(matches!(store::load(&store.0, "dot"), Err(Fail::Defer)));
        store.record("dot", u64::from(std::process::id()), &own_exe());
        let stop = runtime::stop(&store.0, "dot");
        let update = report_of(update(&store.0));
        let detail = update["detail"].as_str().unwrap();
        let helps = |message: &str| {
            message.contains("Restore the saved tunnel profile")
                && message.contains("dot.profile.json")
                && !message.contains("tunnel status")
                && !message.contains("tunnel run")
        };
        assert!(
            matches!(&stop, Err(Fail::Detail(message)) if helps(message)) && helps(detail),
            "{stop:?}: {detail}"
        );
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
            Err(refused)
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
        // An empty --profile-dir: every command names it.
        let answer =
            |command: &str| outcome(&parse(&argv(&[command, "--profile-dir", ""])).unwrap());
        for command in ["update", "status", "verify", "setup", "run", "stop"] {
            assert_eq!(
                answer(command),
                Err(store::UNSAFE_ROOT.to_owned()),
                "{command}"
            );
        }
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
            Err(store::UNSAFE_ROOT.to_owned())
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
        assert!(
            parse(&argv(&["setup", "--read-key"]))
                .unwrap()
                .setup
                .read_key
        );
        assert_eq!(
            parse(&argv(&["setup", "--key-file", "k"]))
                .unwrap()
                .setup
                .key_file
                .as_deref(),
            Some("k")
        );
        assert!(ok(&[
            "setup",
            "--accept-access",
            "--catalog",
            "full",
            "--tunnel-id",
            "x"
        ]));
        // The joined form is the same option.
        assert_eq!(
            parse(&argv(&["status", "--profile=alpha", "--profile-dir=d"]))
                .map(|o| (o.profile, o.profile_dir)),
            Some(("alpha".to_owned(), Some("d".to_owned())))
        );
    }

    #[test]
    fn a_python_refresh_line_names_this_profiles_commands_and_its_first_step() {
        let result = r#"{"id": "0123456789abcdef0123456789abcdef", "state": "failed"}"#;
        let dir = |store: &Store| store.0.to_str().unwrap().to_owned();
        let text = |store: &Store| {
            let options = parse(&argv(&[
                "status",
                "--profile",
                "work-2",
                "--profile-dir",
                &dir(store),
            ]))
            .unwrap();
            status(&options, &store.0).unwrap().0
        };
        let run = |store: &Store| tunnel_command("run", "work-2", Some(&dir(store)));
        // Pending: setup comes first, with this profile's status command.
        let store = Store::new();
        store.profile("work-2", false);
        store.put("work-2.reload.result.json", result.as_bytes());
        let pending = text(&store);
        let status_command = tunnel_command("status", "work-2", Some(&dir(&store)));
        assert!(
            pending.contains(&format!(
                "finish this profile's setup first (`{status_command}` shows the step it needs), then start it with `{}`",
                run(&store)
            )),
            "{pending}"
        );
        // Ready without a key: import it first.
        let store = Store::new();
        store.profile("work-2", true);
        store.put("work-2.reload.result.json", result.as_bytes());
        let keyless = text(&store);
        assert!(
            keyless.contains("--read-key` first, then start it with `"),
            "{keyless}"
        );
        assert!(keyless.contains(&run(&store)), "{keyless}");
        // Ready with a key and no record: just run it, never "stop".
        store.key("work-2");
        let idle = text(&store);
        assert!(
            idle.contains(&format!(
                "The Python client recorded it; start it with `{}` to run it on this client.",
                run(&store)
            )),
            "{idle}"
        );
        assert!(!idle.contains("stop"), "{idle}");
    }

    #[test]
    fn verify_names_a_launch_another_client_started() {
        let store = Store::new();
        store.profile("dot", true);
        #[cfg(windows)]
        {
            let Some(key) = sealed(b"fixture-tunnel-key") else {
                return;
            };
            store.put("dot.key", &key);
        }
        #[cfg(not(windows))]
        store.key("dot");
        store.record("dot", u64::from(std::process::id()), &own_exe());
        let options = parse(&argv(&["verify"])).unwrap();
        match verify(&options, &store.0) {
            Err(Fail::Detail(message)) => assert!(
                message.starts_with("tunnel verify checks only a launch this client started")
                    && message.contains("pseudolife-mcp tunnel run --profile dot"),
                "{message}"
            ),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn every_argv_this_client_does_not_run_is_refused_with_its_cause() {
        let refused = |values: &[&str]| match argv::read(&argv(values)) {
            Err(message) => message,
            Ok(_) => panic!("{values:?} was admitted"),
        };
        for (shape, says) in [
            (&[][..], "tunnel needs a command: setup, status"),
            (&["sk-fixture"], "unknown tunnel command; the commands are"),
            (
                &["doctor"],
                "tunnel doctor is not part of the native client",
            ),
            (
                &["start"],
                "tunnel start is not part of the native client. Run `pseudolife-mcp tunnel run`",
            ),
            (
                &["service", "install"],
                "tunnel service is not part of the native client",
            ),
            (&["status", "--json", "--json"], "--json is given twice"),
            (&["status", "--profile"], "--profile needs a value"),
            (&["status", "--profile", "-x"], "--profile needs a value"),
            (
                &["status", "--prof", "dot"],
                "tunnel status does not take --prof",
            ),
            (
                &["status", "extra"],
                "tunnel status takes no positional arguments",
            ),
            (
                &["setup", "--read-key", "sk-fixture"],
                "tunnel setup takes no positional arguments",
            ),
            (
                &["setup", "-k=sk-fixture"],
                "tunnel setup does not take -k;",
            ),
            (&["update", "--json"], "tunnel update does not take --json"),
            (
                &["verify", "--accept-access"],
                "tunnel verify does not take --accept-access",
            ),
            (
                &["setup", "--catalog", "all"],
                "--catalog takes current or full",
            ),
            (
                &["setup", "--read-key", "--read-key"],
                "--read-key is given twice",
            ),
            (&["setup", "--read-key=yes"], "--read-key takes no value"),
            (&["setup", "--key-file"], "--key-file needs a value"),
            (
                &["setup", "--start"],
                "tunnel setup --start is not part of the native client",
            ),
            (
                &["setup", "--open-browser", "keys"],
                "tunnel setup --open-browser is not part of the native client",
            ),
        ] {
            let message = refused(shape);
            assert!(message.starts_with(says), "{shape:?}: {message}");
            assert!(!message.contains("candidate"), "{shape:?}: {message}");
            // A word typed in the wrong place may be a secret: never echoed.
            assert!(!message.contains("sk-fixture"), "{shape:?}: {message}");
        }
        for help in [&["--help"][..], &["-h"], &["help"]] {
            assert!(
                matches!(argv::read(&argv(help)), Ok(argv::Parsed::Help(text)) if text == argv::OVERVIEW)
            );
        }
        for command in ["setup", "status", "verify", "run", "stop", "update"] {
            let Ok(argv::Parsed::Help(text)) = argv::read(&argv(&[command, "-h"])) else {
                panic!("{command} -h");
            };
            assert!(
                text.starts_with(&format!("usage: pseudolife-mcp tunnel {command}")),
                "{text}"
            );
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
                "Check that {} is an owner-only file holding a tunnel profile; if it is left from an abandoned setup, move it aside and save it again with tunnel setup --daemon-url and --token-file.",
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
        // verify cannot tell whether the key is finished: it names the
        // session as the cause, never "finish tunnel setup".
        let verify_options = parse(&argv(&["verify"])).unwrap();
        assert_eq!(
            verify(&verify_options, &store.0).unwrap_err(),
            Fail::Tunnel(store::SEALED)
        );
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
