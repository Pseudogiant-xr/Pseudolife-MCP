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

mod store;
mod syntax;

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
    let root = store::store_root(options.profile_dir.as_deref()).ok()?;
    let result = match options.command {
        Command::Status => status(&options, &root),
        Command::Update => update(&root),
        Command::Verify => verify(&options, &root),
        Command::Setup => setup(&options, &root),
    };
    let (stdout, stderr, code) = match result {
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
    let key = store::key_present(root, &profile.name)?;
    // A process record means live-process identity (psutil): deferred.
    if occupied(&root.join(format!("{}.process.json", profile.name)))? {
        return Err(Fail::Defer);
    }
    // Without a launch record the cloud identity is unavailable, so
    // verification_status is constant: nothing verified, no current challenge.
    shallow_challenge(root, &profile.name)?;
    let refresh = refresh_status(root, &profile.name)?;
    Ok((report(&profile, key, &refresh, options.json), 0))
}

fn report(profile: &Profile, key: bool, refresh: &Refresh, as_json: bool) -> String {
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
    if as_json {
        return format!(
            "{{\"profile\": {name_q}, \"state\": {}, \"catalog\": {}, \"private_key_present\": {}, \"key_expiry\": {expiry}, \"runtime\": {{\"running\": false, \"profile\": {name_q}, \"ready\": false, \"key_expiry\": {}}}, \"cloud\": {{\"verified\": false, \"successful_calls\": 0, \"challenge_current\": false}}, \"update\": {}}}\n",
            quoted(&profile.state),
            quoted(&profile.catalog),
            bool_text(key),
            quoted(runtime_expiry),
            refresh.json(),
            name_q = quoted(name),
        );
    }
    let mut text = format!(
        "Profile {name}: {}; runtime running=False, ready=False; cloud verified=False (0/2 calls).\n",
        profile.state
    );
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

fn update(root: &Path) -> Output {
    let names = if exists(root)? {
        store::list(root)?
    } else {
        Vec::new()
    };
    if names.is_empty() {
        return Ok((format!("{NO_PROFILES}\n"), 3));
    }
    let mut profiles = Vec::new();
    for name in &names {
        let profile = store::load(root, name)?;
        // A ready profile with its key would run update_profile, whose
        // launcher discovery depends on the installed interpreter: deferred.
        if profile.state == "ready" && store::key_present(root, name)? {
            return Err(Fail::Defer);
        }
        profiles.push(format!(
            "{{\"profile\": {}, \"state\": \"pending\"}}",
            quoted(name)
        ));
    }
    Ok((
        format!(
            "{{\"state\": \"current\", \"needs_attention\": false, \"detail\": \"{} saved tunnel profiles checked; local client registrations preserved\", \"profiles\": [{}]}}\n",
            names.len(),
            profiles.join(", ")
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
