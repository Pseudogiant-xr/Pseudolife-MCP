//! Native invite creation and administration over PostgreSQL or the daemon
//! container, with stopped-daemon list/revoke through psql.
//! v59: `--someone-else` / `--mine` on an invite, and an owner column in
//! `--list` (`coordination.not_owner_principals` read natively).
use super::invite_db::{self, DbError};
use super::pyjson::{self, J};
use super::{Refusal, net};
use std::path::PathBuf;
#[path = "invite_transport.rs"]
mod transport;

const EXIT_OK: u8 = 0;
const EXIT_FAILED: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_REFUSED: u8 = 4;
const DEFAULT_PORT: u32 = 8765;
const MIN_SCHEMA: i64 = 53;
const TIERS: [&str; 3] = ["minimal", "core", "full"];
const URL_PLACEHOLDER: &str = "<daemon-url>";
const EXPIRY_ERROR: Refusal = Refusal::usage(
    "--expires must be ASCII digits with an optional s, m, h or d suffix, from 60 seconds to 24 hours; use --expires 15m",
);
const PORT_ERROR: Refusal =
    Refusal::usage("--port must be ASCII digits from 1 to 65535; use --port 8765");
const TIME_ERROR: Refusal = Refusal::new(
    "an invite list timestamp is non-finite or outside 1970 through 9999; repair that timestamp in the bank before listing again",
);
/// `VERSION_LINES`: what `invite --version-check` prints, the highest
/// version listed being this image's; 2 (v59) takes `--someone-else` and
/// `--mine`. Line 1 stays first because every host CLI before v59, Python
/// and shim alike, accepts the image only when its output starts with
/// `pseudolife-mcp invite 1`: without it, a host CLI one release behind
/// refuses a plain invite as an image that predates `invite`.
const VERSION_LINES: [&str; 2] = ["pseudolife-mcp invite 1", "pseudolife-mcp invite 2"];
/// `OWNERSHIP_NOTE`: the `--someone-else` note, one text however invite
/// was run, since a host CLI before v59 forwards an invite into the image
/// with nothing that tells it apart from a direct run, and its own parser
/// refuses `--someone-else`.
const OWNERSHIP_NOTE: &str = "for a machine that is not yours, add --someone-else (an older pseudolife-mcp refuses it: update it first)";

#[derive(Default)]
struct Args {
    name: Option<String>,
    tier: Option<String>,
    board: Option<bool>,
    /// v59: `--someone-else` (false) or `--mine` (true); both is
    /// a mutually exclusive argument error.
    owned: Option<bool>,
    expires: Option<u32>,
    replace: bool,
    url: Option<String>,
    port: Option<u32>,
    bank: Option<String>,
    list: bool,
    revoke: Option<String>,
    yes: bool,
    json: bool,
    version_check: bool,
}

/// `_expires` for its ASCII spellings `<digits>[smhd]` (any case), within
/// one minute and 24 hours; other values receive a usage error.
fn expires(value: &str) -> Result<u32, Refusal> {
    let lower = value.to_ascii_lowercase();
    let (digits, unit) = match lower.as_bytes().last() {
        Some(b's') => (&lower[..lower.len() - 1], 1),
        Some(b'm') => (&lower[..lower.len() - 1], 60),
        Some(b'h') => (&lower[..lower.len() - 1], 3600),
        Some(b'd') => (&lower[..lower.len() - 1], 86400),
        _ => (lower.as_str(), 60),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(EXPIRY_ERROR);
    }
    let digits = digits.trim_start_matches('0');
    if digits.len() > 9 {
        return Err(EXPIRY_ERROR);
    }
    let seconds = digits.parse::<u64>().unwrap_or(0) * unit;
    if !(60..=86400).contains(&seconds) {
        return Err(EXPIRY_ERROR);
    }
    u32::try_from(seconds).map_err(|_| EXPIRY_ERROR)
}

/// A port spelled as ASCII digits in 1..=65535 (`expose_cli._port`, and the
/// `PSEUDOLIFE_MCP_PORT` default).
fn port(value: &str) -> Result<u32, Refusal> {
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(PORT_ERROR);
    }
    let digits = value.trim_start_matches('0');
    if digits.len() > 5 {
        return Err(PORT_ERROR);
    }
    let port = digits.parse::<u32>().unwrap_or(0);
    if (1..=65535).contains(&port) {
        Ok(port)
    } else {
        Err(PORT_ERROR)
    }
}

fn parse(arguments: &[String]) -> Result<Args, Refusal> {
    let mut args = Args::default();
    let mut items = arguments.iter();
    let mut positional_only = false;
    while let Some(item) = items.next() {
        if item == "--" && !positional_only {
            positional_only = true;
            continue;
        }
        if positional_only || !item.starts_with('-') {
            if args.name.replace(item.clone()).is_some() {
                return Err(Refusal::usage(
                    "invite takes one principal name; remove the extra positional argument",
                ));
            }
            continue;
        }
        let (option, inline) = item
            .split_once('=')
            .map_or((item.as_str(), None), |(k, v)| (k, Some(v)));
        let mut value = |missing| {
            inline
                .map(str::to_owned)
                .or_else(|| items.next().filter(|v| !v.starts_with('-')).cloned())
                .ok_or(Refusal::usage(missing))
        };
        match option {
            "--tier" => {
                let tier = value("--tier requires a value; use minimal, core or full")?;
                if !TIERS.contains(&tier.as_str()) {
                    return Err(Refusal::usage(
                        "--tier is not minimal, core or full; choose one of those tiers",
                    ));
                }
                args.tier = Some(tier);
            }
            "--board" | "--no-board" if inline.is_none() => {
                let board = option == "--board";
                if args.board.is_some_and(|old| old != board) {
                    return Err(Refusal::usage(
                        "--board and --no-board conflict; choose just one",
                    ));
                }
                args.board = Some(board);
            }
            "--mine" | "--someone-else" if inline.is_none() => {
                let owned = option == "--mine";
                if args.owned.is_some_and(|old| old != owned) {
                    return Err(Refusal::usage(
                        "--mine and --someone-else conflict; choose just one",
                    ));
                }
                args.owned = Some(owned);
            }
            "--expires" => {
                args.expires = Some(expires(&value(
                    "--expires requires a duration; use --expires 15m",
                )?)?)
            }
            "--port" => {
                args.port = Some(port(&value("--port requires a number; use --port 8765")?)?)
            }
            "--url" => {
                args.url = Some(value(
                    "--url requires the daemon address; supply an http(s) origin",
                )?)
            }
            "--bank" => {
                args.bank = Some(value(
                    "--bank requires a bank fingerprint; supply the daemon's bank fingerprint",
                )?)
            }
            "--revoke" => {
                args.revoke = Some(value(
                    "--revoke requires a principal name; supply the machine to revoke",
                )?)
            }
            "--replace" if inline.is_none() => args.replace = true,
            "--list" if inline.is_none() => args.list = true,
            "--yes" if inline.is_none() => args.yes = true,
            "--json" if inline.is_none() => args.json = true,
            "--version-check" if inline.is_none() => args.version_check = true,
            _ => {
                return Err(Refusal::usage(
                    "unrecognized invite option or a value on a flag; use invite --help for the supported options",
                ));
            }
        }
    }
    Ok(args)
}

pub(super) fn refusal(error: Refusal, json: bool) -> u8 {
    Report::new(json).fail(error.exit, error.message)
}

/// `_Report`: the JSON report's members in their insertion order.
struct Report {
    json: bool,
    data: pyjson::Dict,
}

impl Report {
    fn new(json: bool) -> Self {
        Self {
            json,
            data: vec![
                ("error".into(), J::Null),
                ("notes".into(), J::List(Vec::new())),
                ("exit".into(), J::Null),
            ],
        }
    }

    fn set(&mut self, key: &str, value: J) {
        pyjson::set(&mut self.data, key, value);
    }

    fn say(&self, line: &str) {
        if !self.json {
            super::print(line);
        }
    }

    fn note(&mut self, line: &str) {
        if let Some((_, J::List(notes))) = self
            .data
            .iter_mut()
            .find(|(key, _)| key.as_str() == Some("notes"))
        {
            notes.push(J::Str(line.to_owned()));
        }
        self.say(line);
    }

    fn fail(&mut self, code: u8, line: &str) -> u8 {
        self.fail_units(code, units(line))
    }

    /// `fail` for a line that may hold a lone surrogate: Python's stderr
    /// writes it backslash-escaped, the JSON report as a `\u` escape.
    fn fail_units(&mut self, code: u8, line: Vec<u16>) -> u8 {
        if !self.json {
            let mut text = units("invite: ");
            text.extend(&line);
            super::eprint(&pyjson::backslashreplace(&text));
        }
        self.set("error", pyjson::text(line));
        self.finish(code)
    }

    fn finish(&mut self, code: u8) -> u8 {
        self.set("exit", J::Int(code.to_string()));
        if self.json {
            super::print(&pyjson::dumps(&J::Dict(self.data.clone()), true));
        }
        code
    }
}

/// `time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else "-"`.
fn when(epoch: Option<f64>) -> Result<String, Refusal> {
    when_or(epoch, "-")
}

#[cfg(test)]
mod native_refusal_tests {
    use super::*;

    #[test]
    fn native_refusal_timestamp_has_a_repair_step() {
        for epoch in [-1.0, f64::NAN, f64::INFINITY, 253_402_300_800.0] {
            let error = when(Some(epoch)).unwrap_err();
            assert!(format!("{error:?}").contains("timestamp"));
        }
        assert!(when(Some(1.0)).is_ok());
        assert!(when(Some(253_402_300_799.0)).is_ok());
    }
}

/// `_when(epoch, missing)`.
fn when_or(epoch: Option<f64>, missing: &str) -> Result<String, Refusal> {
    let Some(epoch) = epoch.filter(|epoch| *epoch != 0.0) else {
        return Ok(missing.to_owned());
    };
    // 1970-01-01 to 9999-12-31: glibc prints a year before 1000 unpadded
    // where this formatting pads it, and Windows' gmtime refuses negative
    // times; outside the range the oracle's own answer depends on the host.
    if !(0.0..253_402_300_800.0).contains(&epoch) {
        return Err(TIME_ERROR);
    }
    let moment = chrono::DateTime::from_timestamp(epoch.floor() as i64, 0).ok_or(TIME_ERROR)?;
    Ok(moment.format("%Y-%m-%d %H:%M UTC").to_string())
}

/// Validate both authority maps without echoing their credential-bearing entries.
fn environment_names() -> Result<(Vec<String>, Vec<String>), Refusal> {
    const TOKENS: Refusal = Refusal::new(
        "PSEUDOLIFE_MCP_TOKENS cannot be validated: entries must be ASCII token:principal pairs with nonempty values, unique tokens and no default or maintainer principal; correct that setting before inviting",
    );
    const TIERS_ERROR: Refusal = Refusal::new(
        "PSEUDOLIFE_MCP_TIER_MAP cannot be validated: entries must be ASCII writer:tier pairs with a nonempty writer and a minimal, core or full tier; correct that setting before inviting",
    );
    let read = |name: &str, error| -> Result<String, Refusal> {
        match std::env::var(name) {
            Ok(value) if value.is_ascii() => Ok(value),
            Ok(_) => Err(error),
            Err(std::env::VarError::NotPresent) => Ok(String::new()),
            Err(_) => Err(error),
        }
    };
    let mut tokens: Vec<(String, String)> = Vec::new();
    for part in read("PSEUDOLIFE_MCP_TOKENS", TOKENS)?.split(',') {
        let part = pyjson::py_strip(part);
        if part.is_empty() {
            continue;
        }
        let (token, principal) = part.rsplit_once(':').ok_or(TOKENS)?;
        let token = pyjson::py_strip(token);
        let principal = pyjson::py_strip(principal).to_ascii_lowercase();
        if token.is_empty()
            || principal.is_empty()
            || principal == "default"
            || principal == "maintainer"
            || tokens.iter().any(|(seen, _)| seen == token)
        {
            return Err(TOKENS);
        }
        tokens.push((token.to_owned(), principal));
    }
    let mut writers = Vec::new();
    for part in read("PSEUDOLIFE_MCP_TIER_MAP", TIERS_ERROR)?.split(',') {
        let part = pyjson::py_strip(part);
        if part.is_empty() {
            continue;
        }
        let (writer, tier) = part.split_once(':').ok_or(TIERS_ERROR)?;
        let writer = pyjson::py_strip(writer).to_ascii_lowercase();
        let tier = pyjson::py_strip(tier).to_ascii_lowercase();
        if writer.is_empty() || !TIERS.contains(&tier.as_str()) {
            return Err(TIERS_ERROR);
        }
        writers.push(writer);
    }
    Ok((tokens.into_iter().map(|(_, p)| p).collect(), writers))
}

/// `_environment_names`: the token map's principals, and `default` when
/// `PSEUDOLIFE_MCP_TOKEN` is set (v58's `invite --list`).
fn list_environment() -> Result<Vec<String>, Refusal> {
    let (mut names, _writers) = environment_names()?;
    if std::env::var_os("PSEUDOLIFE_MCP_TOKEN").is_some_and(|value| !value.is_empty()) {
        names.push("default".to_owned());
    }
    names.sort();
    names.dedup();
    Ok(names)
}

/// `_name_refusal`.
fn name_refusal(name: &str) -> Result<Option<String>, Refusal> {
    if super::RESERVED.contains(&name) {
        return Ok(Some(format!("'{name}' is reserved; choose another name")));
    }
    let (tokens, writers) = environment_names()?;
    if tokens.iter().any(|principal| principal == name) {
        return Ok(Some(format!(
            "{name} already has a token in PSEUDOLIFE_MCP_TOKENS; an environment principal keeps working as it is, and a stored one of the same name would be shadowed by it"
        )));
    }
    if writers.iter().any(|writer| writer == name) {
        return Ok(Some(format!(
            "{name} is named in PSEUDOLIFE_MCP_TIER_MAP; remove it there first, or choose another name"
        )));
    }
    Ok(None)
}

fn env_nonempty(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

/// Resolve the optional configuration home from explicit environment settings.
fn python_home() -> Result<PathBuf, Refusal> {
    if cfg!(windows) {
        if let Some(home) = env_nonempty("USERPROFILE") {
            return Ok(home);
        }
        let path = std::env::var_os("HOMEPATH").ok_or(Refusal::new(
            "the home directory is unset; set USERPROFILE to your absolute home directory",
        ))?;
        let drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
        Ok(PathBuf::from(drive).join(path))
    } else {
        std::env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or(Refusal::new(
                "HOME is unset; set HOME to your absolute home directory",
            ))
    }
}

/// The daemon's `config.yaml` as `invite_cli._coordination_config` finds it:
/// `PSEUDOLIFE_MCP_CONFIG`, else the data dir's (`PSEUDOLIFE_MCP_DATA_DIR`,
/// the lite default when it exists, else `./data`). `None` when there is
/// none or it does not parse: a courtesy read, never a failure.
fn config_yaml() -> Option<yaml_rust2::Yaml> {
    let path = match env_nonempty("PSEUDOLIFE_MCP_CONFIG") {
        Some(path) => path,
        None => {
            let directory = env_nonempty("PSEUDOLIFE_MCP_DATA_DIR").or_else(|| {
                let home = python_home().ok()?;
                let base = if cfg!(windows) {
                    env_nonempty("LOCALAPPDATA").unwrap_or_else(|| home.join("AppData/Local"))
                } else if cfg!(target_os = "macos") {
                    home.join("Library/Application Support")
                } else {
                    env_nonempty("XDG_DATA_HOME").unwrap_or_else(|| home.join(".local/share"))
                };
                let lite = base.join("pseudolife-mcp");
                if lite.exists() {
                    Some(lite)
                } else {
                    std::env::current_dir().ok().map(|p| p.join("data"))
                }
            })?;
            directory.join("config.yaml")
        }
    };
    let text = std::fs::read_to_string(path).ok()?;
    yaml_rust2::YamlLoader::load_from_str(&text)
        .ok()?
        .into_iter()
        .next()
}

/// A `coordination` list of names as `CoordinationConfig.__post_init__`
/// keeps it: each stripped and lower-cased, duplicates dropped in order.
/// `None` when any entry is not a string or is blank (the oracle's
/// validation then refuses the whole file).
fn coordination_names(config: &yaml_rust2::Yaml, key: &str) -> Option<Vec<String>> {
    let items = match &config["coordination"][key] {
        yaml_rust2::Yaml::BadValue | yaml_rust2::Yaml::Null => return Some(Vec::new()),
        yaml_rust2::Yaml::Array(items) => items,
        _ => return None,
    };
    let mut names: Vec<String> = Vec::new();
    for value in items {
        let name = value.as_str()?.trim().to_lowercase();
        if name.is_empty() {
            return None;
        }
        if !names.contains(&name) {
            names.push(name);
        }
    }
    Some(names)
}

/// The config is the oracle's only when both lists it reads are valid: a
/// malformed one makes `load_config` refuse the whole file, and then
/// neither the listed note nor the not-owner names apply.
fn coordination_lists(config: &yaml_rust2::Yaml) -> Option<(Vec<String>, Vec<String>)> {
    Some((
        coordination_names(config, "allowed_principals")?,
        coordination_names(config, "not_owner_principals")?,
    ))
}

/// The optional courtesy note never blocks an invite on malformed configuration.
fn listed_in_config(name: &str) -> bool {
    config_yaml()
        .and_then(|config| coordination_lists(&config))
        .is_some_and(|(allowed, _)| allowed.iter().any(|value| value == name))
}

/// `_not_owner_names` (v59): `coordination.not_owner_principals`, the
/// environment principals that are someone else's; empty when the config
/// cannot be read.
fn not_owner_in_config() -> Vec<String> {
    config_yaml()
        .and_then(|config| coordination_lists(&config))
        .map(|(_, not_owner)| not_owner)
        .unwrap_or_default()
}

fn python_bool(value: bool) -> J {
    J::Bool(value)
}

pub(super) async fn run(arguments: &[String]) -> Result<u8, Refusal> {
    let mut args = parse(arguments)?;
    if args.version_check {
        for line in VERSION_LINES {
            super::print(line);
        }
        return Ok(EXIT_OK);
    }
    let default_port = if let Some(port) = args.port {
        port
    } else {
        match std::env::var("PSEUDOLIFE_MCP_PORT") {
        Ok(value) if !value.is_empty() => port(&value).map_err(|_| Refusal::usage("PSEUDOLIFE_MCP_PORT must be ASCII digits from 1 to 65535; set PSEUDOLIFE_MCP_PORT=8765"))?,
        Ok(_) | Err(std::env::VarError::NotPresent) => DEFAULT_PORT,
        Err(_) => return Err(Refusal::usage("PSEUDOLIFE_MCP_PORT is not valid Unicode; set PSEUDOLIFE_MCP_PORT=8765")),
    }
    };
    if let Some(url) = args
        .url
        .as_deref()
        .filter(|url| !url.is_empty() && *url != URL_PLACEHOLDER)
        && !matches!(super::url::validated(url), Ok(Ok(_)))
    {
        return Err(Refusal::usage(
            "--url is not a supported daemon origin; supply an ASCII http(s) URL with no credentials, path, query or fragment",
        ));
    }
    let mut report = Report::new(args.json);
    // Python truthiness: an empty value counts as no mode.
    let name = args.name.clone().filter(|name| !name.is_empty());
    let revoke = args.revoke.clone().filter(|name| !name.is_empty());
    let modes =
        usize::from(name.is_some()) + usize::from(args.list) + usize::from(revoke.is_some());
    if modes != 1 {
        return Ok(report.fail(
            EXIT_USAGE,
            "give one of: a name to invite, --list, or --revoke NAME",
        ));
    }
    // `target = args.name or args.revoke`: an empty --revoke is a target.
    let target = if name.is_some() {
        name.clone()
    } else {
        args.revoke.clone()
    };
    if let Some(target) = target.as_ref() {
        if !super::valid_principal_name(target) {
            return Ok(report.fail(
                EXIT_USAGE,
                "the principal name is invalid: use lowercase letters, digits, '.', '_' and '-', starting with a letter or digit, at most 64 characters",
            ));
        }
        if let Some(name) = &name
            && let Some(refusal) = name_refusal(name)?
        {
            return Ok(report.fail(EXIT_REFUSED, &refusal));
        }
    }
    if revoke.is_some() && !args.yes {
        if super::stdin_is_terminal() {
            use std::io::Write;
            let name = revoke.as_deref().unwrap_or("");
            let _ = write!(
                std::io::stderr(),
                "revoke {name}? Its token stops working within 10 seconds. [y/N] "
            );
            let _ = std::io::stderr().flush();
            let mut answer = String::new();
            let _ = std::io::stdin().read_line(&mut answer);
            if !matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
                return Ok(report.fail(EXIT_USAGE, "not confirmed; nothing was changed"));
            }
            args.yes = true;
        } else {
            return Ok(report.fail(EXIT_USAGE, "not interactive: re-run with --yes to revoke"));
        }
    }
    let port = args.port.unwrap_or(default_port);
    let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
        .ok()
        .filter(|value| !value.is_empty());
    let Some(dsn) = dsn else {
        // With docker, the transport tries the daemon container first and
        // refuses a lite bank only when that container is not running: a
        // completed Docker migration can leave a stopped lite cluster behind
        // (ops/preflight_lite.py).
        if transport::docker_available() {
            return transport::run(&args, &mut report, port).await;
        }
        if let Some(bank) = crate::cli::retired::find_lite_bank(None) {
            return Ok(report.fail(EXIT_REFUSED, &crate::cli::retired::lite_bank_refusal(&bank)));
        }
        return Ok(report.fail(EXIT_REFUSED, "no database found: set PSEUDOLIFE_MCP_DATABASE_URL, or run this on the daemon's host (a Docker install runs it in the daemon container)"));
    };
    let dsn = crate::pg::Dsn::parse(&dsn).map_err(database_error)?;
    let mut url = args.url.clone().filter(|url| !url.is_empty());
    if name.is_some() && url.is_none() {
        url = transport::exposed_url(port).await?;
    }
    // Check the connection, session settings, schema and clock before an
    // invite can write a code. All decoding inside a transaction precedes COMMIT.
    let mut session = crate::pg::Session::open(&dsn)
        .await
        .map_err(database_error)?;
    let ready = async {
        invite_db::setup(session.client()).await?;
        if name.is_some() {
            invite_db::prepare(session.client()).await?;
        }
        Ok::<(), DbError>(())
    }
    .await;
    if let Err(error) = ready {
        let _ = session.close().await;
        return Ok(database_failure(&mut report, error));
    }
    let mut health = None;
    if name.is_some() {
        let checked = daemon_check(port, &mut report).await;
        match checked {
            Ok(Ok(found)) => health = Some(found),
            Ok(Err(refusal)) => {
                let _ = session.close().await;
                return Ok(report.fail_units(EXIT_REFUSED, refusal));
            }
            Err(error) => {
                let _ = session.close().await;
                return Err(error);
            }
        }
    }
    let outcome = local(
        &args,
        &mut report,
        &mut session,
        name,
        revoke,
        url,
        port,
        health,
    )
    .await;
    let _ = session.close().await;
    outcome
}

/// UTF-16 code units of a message (a refusal may quote a daemon value that
/// holds a lone surrogate).
fn units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

/// `f"{prefix}{value}{suffix}"` with `str(value)`.
fn quoting(prefix: &str, value: &J, suffix: &str) -> Vec<u16> {
    let mut out = units(prefix);
    out.extend(value.py_str_units());
    out.extend(units(suffix));
    out
}

/// Preserve health validation while keeping arbitrary response values out of diagnostics.
fn diagnostic_member(key: &str, value: J) -> J {
    let safe = match (key, &value) {
        (_, J::Null) => true,
        ("auth", J::Bool(_)) => true,
        ("schema", J::Bool(_)) => true,
        ("schema", J::Int(digits)) => digits.parse::<u32>().is_ok(),
        ("bank", J::Str(bank)) => bank.len() == 16 && bank.bytes().all(|b| b.is_ascii_hexdigit()),
        _ => false,
    };
    if safe {
        value
    } else {
        J::Str(format!("<invalid {key} value>"))
    }
}

/// Check the daemon before inviting. Diagnostics contain only known field shapes.
async fn daemon_check(
    port: u32,
    report: &mut Report,
) -> Result<Result<pyjson::Dict, Vec<u16>>, Refusal> {
    let local = format!("http://127.0.0.1:{port}");
    let health = net::invite_health(&local).await.map_err(|_| Refusal::new("the daemon /health response exceeds the JSON nesting limit; update the daemon before inviting"))?;
    let Some(health) =
        health.filter(|h| pyjson::get(h, "status").and_then(J::as_str) == Some("ok"))
    else {
        return Ok(Err(units(&format!(
            "no healthy daemon answers at {local}/health: start it (or pass --port), then re-run"
        ))));
    };
    let member = |key: &str| pyjson::get(&health, key).cloned().unwrap_or(J::Null);
    report.set(
        "daemon",
        J::Dict(vec![
            (
                J::Str("auth".into()),
                diagnostic_member("auth", member("auth")),
            ),
            (
                J::Str("schema".into()),
                diagnostic_member("schema", member("schema")),
            ),
            (
                J::Str("bank".into()),
                diagnostic_member("bank", member("bank")),
            ),
        ]),
    );
    if pyjson::get(&health, "auth") != Some(&J::Bool(true)) {
        return Ok(Err(units(&format!(
            "the daemon at {local} does not report \"auth\": true. Stored principals do not turn authentication on: give the daemon a token first (docs/guide/remote-bank.md)"
        ))));
    }
    let schema = member("schema");
    let old = match &schema {
        J::Bool(_) => true,
        J::Int(digits) => {
            digits.starts_with('-') || digits.parse::<i64>().is_ok_and(|v| v < MIN_SCHEMA)
        }
        _ => true,
    };
    if old {
        let shown = diagnostic_member("schema", schema.clone());
        if shown != schema {
            return Ok(Err(units(
                "the daemon /health schema is not a supported version number; update the daemon before inviting",
            )));
        }
        return Ok(Err(quoting(
            "the daemon runs schema ",
            &shown,
            &format!(", older than invite (v{MIN_SCHEMA}): update the daemon first"),
        )));
    }
    if !member("bank").truthy() {
        return Ok(Err(units(
            "the daemon has not reported which bank it serves yet (/health bank is null until its storage has started and the agent board has been used once): start a session against it, then re-run",
        )));
    }
    Ok(Ok(health))
}

/// `_bank_refusal`: `None` when the bank is the expected one.
fn bank_refusal(bank: Option<&str>, expected: &J, what: &str) -> Option<Vec<u16>> {
    if matches!((bank, expected), (Some(bank), J::Str(expected)) if bank == expected)
        || (bank.is_none() && *expected == J::Null)
    {
        return None;
    }
    let shown = diagnostic_member("bank", expected.clone());
    if shown != *expected {
        return Some(units(if what == "--bank" {
            "--bank is not a valid bank fingerprint; supply the daemon's 16 hexadecimal digit fingerprint. Nothing was changed"
        } else {
            "the daemon /health bank field is not a valid bank fingerprint; update the daemon before inviting. Nothing was changed"
        }));
    }
    Some(quoting(
        &format!(
            "this database holds bank {}, but {what} is bank ",
            bank.unwrap_or("(none)")
        ),
        &shown,
        ": it is another database. Nothing was changed",
    ))
}

const NO_TABLE: &str = "this bank has no principals table yet (schema v53): update the daemon and let it start, then re-run";

fn database_error(error: crate::pg::Error) -> Refusal {
    use crate::pg::Error;
    Refusal::new(match error {
        Error::InvalidDsn => {
            "PSEUDOLIFE_MCP_DATABASE_URL is not a valid PostgreSQL connection string; supply a PostgreSQL URL or libpq keyword connection string"
        }
        Error::UnsupportedOption(option) if option == "sslmode" => {
            "PSEUDOLIFE_MCP_DATABASE_URL has an unsupported sslmode; choose disable, prefer, require, verify-ca or verify-full without weakening the required verification"
        }
        Error::UnsupportedOption(option) if option == "sslcert" => {
            "PSEUDOLIFE_MCP_DATABASE_URL requires unsupported sslcert client-certificate authentication; keep the required policy and ask the maintainer to add support before retrying"
        }
        Error::UnsupportedOption(option) if option == "sslkey" => {
            "PSEUDOLIFE_MCP_DATABASE_URL requires unsupported sslkey client-certificate authentication; keep the required policy and ask the maintainer to add support before retrying"
        }
        Error::UnsupportedOption(option) if option == "sslcrl" => {
            "PSEUDOLIFE_MCP_DATABASE_URL requires an unsupported sslcrl revocation policy; keep the required policy and ask the maintainer to add support before retrying"
        }
        Error::UnsupportedOption(_) => {
            "PSEUDOLIFE_MCP_DATABASE_URL contains an unsupported connection option; keep the required setting and ask the maintainer to add support before retrying"
        }
        Error::InvalidOption("host") => {
            "PSEUDOLIFE_MCP_DATABASE_URL has an invalid host; supply one TCP hostname or IP address"
        }
        Error::InvalidOption("port") => {
            "PSEUDOLIFE_MCP_DATABASE_URL has an invalid port; supply an integer from 1 through 65535"
        }
        Error::InvalidOption("sslmode") => {
            "PSEUDOLIFE_MCP_DATABASE_URL has an invalid sslmode; choose disable, prefer, require, verify-ca or verify-full without weakening the required verification"
        }
        Error::InvalidOption(_) => {
            "PSEUDOLIFE_MCP_DATABASE_URL has an invalid connection option; correct the connection settings before retrying"
        }
        Error::AmbientControl(control) => ambient_error(&control),
        Error::RootCertificate => {
            "the PostgreSQL sslrootcert setting cannot be read as a trusted certificate; provide the correct readable CA certificate"
        }
        Error::HostnameRules => {
            "the PostgreSQL certificate does not validate for the host in PSEUDOLIFE_MCP_DATABASE_URL; use a certificate whose SAN matches that host"
        }
        Error::Connection => {
            "the PostgreSQL connection selected by PSEUDOLIFE_MCP_DATABASE_URL failed; verify that connection and its credentials before retrying"
        }
        Error::SessionSetup => {
            "the PostgreSQL session settings for PSEUDOLIFE_MCP_DATABASE_URL could not be applied; allow the login to set the required session parameters before retrying"
        }
        Error::Shutdown => {
            "the PostgreSQL connection could not close cleanly; run invite --list to verify the outcome before retrying"
        }
    })
}

// Only known setting names enter diagnostics: a driver-supplied name can
// itself contain a secret. Unknown PGSSL/PGTLS keys get the safe prefix.
fn ambient_error(control: &str) -> &'static str {
    macro_rules! settings {
        ($($name:literal),* $(,)?) => {
            match control.to_ascii_uppercase().as_str() {
                $($name => concat!($name, " is unsupported by this invite client; keep the required policy and ask the maintainer to add support before retrying"),)*
                _ => "a PGSSL or PGTLS environment setting is unsupported by this invite client; keep the required TLS policy and ask the maintainer to add support before retrying",
            }
        };
    }
    match control.to_ascii_uppercase().as_str() {
        "PGHOST" => {
            "PGHOST is unsupported as an ambient setting for invite; move its required value to host in PSEUDOLIFE_MCP_DATABASE_URL before unsetting it"
        }
        "PGPORT" => {
            "PGPORT is unsupported as an ambient setting for invite; move its required value to port in PSEUDOLIFE_MCP_DATABASE_URL before unsetting it"
        }
        "PGDATABASE" => {
            "PGDATABASE is unsupported as an ambient setting for invite; move its required value to dbname in PSEUDOLIFE_MCP_DATABASE_URL before unsetting it"
        }
        "PGUSER" => {
            "PGUSER is unsupported as an ambient setting for invite; move its required value to user in PSEUDOLIFE_MCP_DATABASE_URL before unsetting it"
        }
        "PGSSLMODE" => {
            "PGSSLMODE is unsupported as an ambient setting for invite; move its required value to sslmode in PSEUDOLIFE_MCP_DATABASE_URL before unsetting it"
        }
        "PGSSLROOTCERT" => {
            "PGSSLROOTCERT is unsupported as an ambient setting for invite; move its required value to sslrootcert in PSEUDOLIFE_MCP_DATABASE_URL before unsetting it"
        }
        "POSTGRESQL.CRT" => {
            "the implicit PostgreSQL postgresql.crt client-certificate policy is unsupported by this invite client; keep the required policy and ask the maintainer to add support before retrying"
        }
        "POSTGRESQL.KEY" => {
            "the implicit PostgreSQL postgresql.key client-certificate policy is unsupported by this invite client; keep the required policy and ask the maintainer to add support before retrying"
        }
        "ROOT.CRL" => {
            "the implicit PostgreSQL root.crl revocation policy is unsupported by this invite client; keep the required policy and ask the maintainer to add support before retrying"
        }
        _ => settings!(
            "PGHOSTADDR",
            "PGPASSFILE",
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGSYSCONFDIR",
            "PGOPTIONS",
            "PGAPPNAME",
            "PGCONNECT_TIMEOUT",
            "PGCLIENTENCODING",
            "PGCHANNELBINDING",
            "PGTARGETSESSIONATTRS",
            "PGLOADBALANCEHOSTS",
            "PGREQUIREAUTH",
            "PGGSSENCMODE",
            "PGKRBSRVNAME",
            "PGGSSLIB",
            "PGGSSDELEGATION",
            "PGREQUIREPEER",
            "PGMINPROTOCOLVERSION",
            "PGMAXPROTOCOLVERSION",
            "PGREQUIRESSL",
            "PGREALM",
            "PGDATESTYLE",
            "PGTZ",
            "PGGEQO",
            "PGSSLCERT",
            "PGSSLKEY",
            "PGSSLCRL",
            "PGSSLCRLDIR",
            "PGSSLCOMPRESSION",
            "PGSSLSNI",
            "PGSSLNEGOTIATION",
            "PGSSLMINPROTOCOLVERSION",
            "PGSSLMAXPROTOCOLVERSION",
        ),
    }
}

fn database_failure(report: &mut Report, error: DbError) -> u8 {
    match error {
        DbError::UndefinedTable => report.fail(EXIT_REFUSED, NO_TABLE),
        DbError::Refused(message) => report.fail(EXIT_REFUSED, &format!("{message}. Nothing was changed")),
        DbError::Failed(class) => report.fail(EXIT_FAILED, &format!("the database refused or lost the invite operation ({class}); check PSEUDOLIFE_MCP_DATABASE_URL and run invite --list before retrying")),
        DbError::Invalid(error) => report.fail(error.exit, error.message),
    }
}

/// `_local` after the daemon check: the database errors as Python reports
/// them (`classify`); `Refusal` remains only for a value this client cannot
/// decode, which `prepare` rules out before an invite's request.
#[allow(clippy::too_many_arguments)]
async fn local(
    args: &Args,
    report: &mut Report,
    session: &mut crate::pg::Session,
    name: Option<String>,
    revoke: Option<String>,
    url: Option<String>,
    port: u32,
    health: Option<pyjson::Dict>,
) -> Result<u8, Refusal> {
    let result = async {
        let bank_given = args.bank.clone().filter(|bank| !bank.is_empty());
        if name.is_some() || bank_given.is_some() {
            let bank = invite_db::bank_of(session.client()).await?;
            let refusal = bank_given
                .as_ref()
                .and_then(|given| bank_refusal(bank.as_deref(), &J::Str(given.clone()), "--bank"))
                .or_else(|| {
                    health.as_ref().and_then(|health| {
                        bank_refusal(
                            bank.as_deref(),
                            pyjson::get(health, "bank").unwrap_or(&J::Null),
                            &format!("the daemon on port {port} serves"),
                        )
                    })
                });
            if let Some(refusal) = refusal {
                return Ok(Err(refusal));
            }
        }
        if args.list {
            let environment = list_environment()?;
            let rows =
                invite_db::list(session.client(), &environment, &not_owner_in_config()).await?;
            let rejected = invite_db::list_rejected(session.client()).await?;
            return Ok(Ok(Outcome::List(rows, rejected)));
        }
        if let Some(revoke) = &revoke {
            let found = invite_db::revoke(session.client_mut(), revoke).await?;
            return Ok(Ok(Outcome::Revoked(found)));
        }
        let name = name.clone().ok_or(DbError::Invalid(Refusal::usage(
            "no invite name was supplied; give a principal name",
        )))?;
        let ownership = invite_db::has_owned_column(session.client()).await?;
        let created = invite_db::create_invite(
            session.client_mut(),
            &name,
            args.tier.as_deref(),
            args.board,
            args.owned,
            f64::from(args.expires.unwrap_or(900)),
            args.replace,
        )
        .await?;
        Ok(Ok(Outcome::Created(created, ownership)))
    }
    .await;
    match result {
        Ok(Ok(Outcome::List(rows, rejected))) => list_report(report, &rows, &rejected),
        Ok(Ok(Outcome::Revoked(false))) => Ok(report.fail(
            EXIT_REFUSED,
            &format!(
                "no stored principal is named {}",
                revoke.unwrap_or_default()
            ),
        )),
        Ok(Ok(Outcome::Revoked(true))) => {
            let revoke = revoke.unwrap_or_default();
            report.set("principal", J::Str(revoke.clone()));
            report.set("state", J::Str("revoked".into()));
            report.say(&format!(
                "{revoke}: revoked; the daemon stops accepting its token within 10 seconds"
            ));
            Ok(report.finish(EXIT_OK))
        }
        Ok(Ok(Outcome::Created(created, ownership))) => Ok(invite_report(
            report,
            &name.unwrap_or_default(),
            &created,
            url,
            args.expires.unwrap_or(900),
            // The note is offered only where the flag can work: a bank with
            // the column, and neither flag given.
            ownership && args.owned.is_none(),
        )),
        Ok(Err(refusal)) => Ok(report.fail_units(EXIT_REFUSED, refusal)),
        Err(error) => Ok(database_failure(report, error)),
    }
}

enum Outcome {
    List(Vec<invite_db::Listed>, Vec<invite_db::Rejected>),
    Revoked(bool),
    /// The row, and whether the bank records ownership (v59).
    Created(invite_db::Created, bool),
}

/// `round(seconds / 60)`: half to even.
fn minutes(seconds: u32) -> u32 {
    let (whole, rest) = (seconds / 60, seconds % 60);
    match (rest * 2).cmp(&60) {
        std::cmp::Ordering::Less => whole,
        std::cmp::Ordering::Greater => whole + 1,
        std::cmp::Ordering::Equal => whole + whole % 2,
    }
}

/// `_invite`'s report, after the row was written. `ownership_note`:
/// whether to print `OWNERSHIP_NOTE` when the row is the owner's.
fn invite_report(
    report: &mut Report,
    name: &str,
    created: &invite_db::Created,
    url: Option<String>,
    seconds: u32,
    ownership_note: bool,
) -> u8 {
    let (Some(raw), Some(expires_at)) = (created.code.as_deref(), created.expires_at) else {
        // A paired machine's ownership alone: no code, nothing to re-pair.
        report.set("principal", J::Str(name.to_owned()));
        report.set("state", J::Str(created.state.into()));
        report.set("tier", created.tier.clone().map_or(J::Null, J::Str));
        report.set("board", python_bool(created.board));
        report.set("owned", python_bool(created.owned));
        let whose = if created.owned {
            "the owner's"
        } else {
            "someone else's"
        };
        report.say(&format!(
            "{name}: paired; now {whose} machine. Its token and pairing are unchanged: no new code, nothing to re-pair"
        ));
        return report.finish(EXIT_OK);
    };
    let code = format!("{}-{}-{}", &raw[..4], &raw[4..8], &raw[8..]);
    let url = url.unwrap_or_else(|| URL_PLACEHOLDER.to_owned());
    let command = format!("pseudolife-mcp pair {url} {code}");
    report.set("principal", J::Str(name.to_owned()));
    report.set("state", J::Str(created.state.into()));
    report.set("code", J::Str(code.clone()));
    report.set("expires_at", J::Float(expires_at));
    report.set("tier", created.tier.clone().map_or(J::Null, J::Str));
    report.set("board", python_bool(created.board));
    report.set("owned", python_bool(created.owned));
    report.set("url", J::Str(url.clone()));
    report.set("pair_command", J::Str(command.clone()));
    let what = match created.state {
        "new" => "invited",
        "pending" => "re-invited (the earlier code no longer works)",
        "revoked" => "re-invited after its revocation",
        _ => "given a replacement code (its current token works until the code is redeemed)",
    };
    let tier = created
        .tier
        .clone()
        .filter(|tier| !tier.is_empty())
        .unwrap_or_else(|| "the daemon default".to_owned());
    report.say(&format!(
        "{name}: {what}; tier {tier}, agent board {}{}",
        if created.board { "on" } else { "off" },
        if created.owned {
            ""
        } else {
            "; someone else's machine"
        }
    ));
    // The expiry is the database clock plus at most a day: always finite.
    let expiry = when(Some(expires_at)).unwrap_or_default();
    report.say(&format!(
        "  code     {code}   single use, expires in {} min ({expiry})",
        minutes(seconds)
    ));
    report.say(&format!("  on the new machine:  {command}"));
    report.say(
        "  or run the installer there and answer 2, giving this code where it asks for the token",
    );
    if url == URL_PLACEHOLDER {
        report.note("the daemon is not exposed on the tailnet here; put its URL in place of <daemon-url> (`pseudolife-mcp expose tailscale` exposes it), or pass --url");
    }
    if listed_in_config(name) {
        report.note(&format!("note: {name} is already listed in coordination.allowed_principals; the invite gives it a token"));
    }
    if ownership_note && created.owned {
        report.note(OWNERSHIP_NOTE);
    }
    report.note("an invited machine can read and write the whole bank and use the agent board, but not change the daemon's configuration");
    report.finish(EXIT_OK)
}

fn float(value: Option<f64>) -> J {
    value.map_or(J::Null, J::Float)
}

/// Build the complete list before emitting output; invalid times refuse it.
fn list_report(
    report: &mut Report,
    rows: &[invite_db::Listed],
    rejected: &[invite_db::Rejected],
) -> Result<u8, Refusal> {
    let mut lines = Vec::new();
    let mut members = Vec::new();
    for row in rows {
        for time in [
            row.code_expires_at,
            row.created_at,
            row.paired_at,
            row.revoked_at,
            row.last_seen_at,
        ]
        .into_iter()
        .flatten()
        {
            if !time.is_finite() {
                return Err(TIME_ERROR);
            }
        }
        let state = if row.state == "pending" {
            format!("pending until {}", when(row.code_expires_at)?)
        } else {
            row.state.to_owned()
        };
        let tier = row
            .tier
            .clone()
            .filter(|tier| !tier.is_empty())
            .unwrap_or_else(|| "default".to_owned());
        let board = match row.board {
            None => "-",
            Some(true) => "on",
            Some(false) => "off",
        };
        lines.push(format!(
            "  {:<24} {:<11} {:<21} {:<8} {:<6} {:<6} {:<21} {:<21} {}",
            row.principal,
            state,
            when_or(row.last_seen_at, "never")?,
            tier,
            board,
            match row.owner {
                None => "-",
                Some(true) => "yes",
                Some(false) => "no",
            },
            when(row.created_at)?,
            when(row.paired_at)?,
            when(row.revoked_at)?
        ));
        members.push(J::Dict(vec![
            ("principal".into(), J::Str(row.principal.clone())),
            ("state".into(), J::Str(row.state.into())),
            ("paired".into(), J::Bool(row.paired)),
            ("tier".into(), row.tier.clone().map_or(J::Null, J::Str)),
            ("board".into(), row.board.map_or(J::Null, J::Bool)),
            (
                "code_expires_at".into(),
                float(if row.pending {
                    row.code_expires_at
                } else {
                    None
                }),
            ),
            ("created_at".into(), float(row.created_at)),
            ("paired_at".into(), float(row.paired_at)),
            ("revoked_at".into(), float(row.revoked_at)),
            ("last_seen_at".into(), float(row.last_seen_at)),
            ("owner".into(), row.owner.map_or(J::Null, J::Bool)),
        ]));
    }
    let mut refused_lines = Vec::new();
    let mut refused = Vec::new();
    for row in rejected {
        if !row.first_seen_at.is_finite() || !row.last_seen_at.is_finite() {
            return Err(TIME_ERROR);
        }
        refused_lines.push(format!(
            "  {:<8} {:>8}  {:<21} {}",
            row.hash_prefix,
            row.rejections,
            when(Some(row.first_seen_at))?,
            when(Some(row.last_seen_at))?
        ));
        refused.push(J::Dict(vec![
            ("hash_prefix".into(), J::Str(row.hash_prefix.clone())),
            ("rejections".into(), J::Int(row.rejections.to_string())),
            ("first_seen_at".into(), J::Float(row.first_seen_at)),
            ("last_seen_at".into(), J::Float(row.last_seen_at)),
        ]));
    }
    report.set("principals", J::List(members));
    report.set("rejected", J::List(refused));
    if rows.is_empty() {
        report.say("no stored principals");
    } else {
        report.say(&format!(
            "  {:<24} {:<11} {:<21} {:<8} {:<6} {:<6} {:<21} {:<21} revoked",
            "name", "state", "last seen", "tier", "board", "owner", "created", "paired"
        ));
    }
    for line in lines {
        report.say(&line);
    }
    if !rows.is_empty() {
        report.say("  (environment: a token in the daemon's PSEUDOLIFE_MCP_TOKENS or PSEUDOLIFE_MCP_TOKEN; last seen: its last accepted request, written once a minute;");
        report.say("   owner: the bank owner's machine, whose board mail the owner's Console shows; `invite --someone-else` and coordination.not_owner_principals say otherwise;");
        report.say("   owner -: unknown, an environment token listed with the daemon stopped)");
    }
    report.say("");
    if rejected.is_empty() {
        report.say("no refused tokens recorded");
        return Ok(report.finish(EXIT_OK));
    }
    report.say("refused tokens, by the first 8 characters of their SHA-256 (`pseudolife-mcp doctor` on a client prints its own token's as token_sha256_prefix):");
    report.say(&format!(
        "  {:<8} {:>8}  {:<21} last",
        "prefix", "refused", "first"
    ));
    for line in refused_lines {
        report.say(&line);
    }
    Ok(report.finish(EXIT_OK))
}

#[cfg(test)]
mod tests {
    #[test]
    fn config_names_are_normalised_as_the_oracle_keeps_them() {
        let doc = |text: &str| {
            yaml_rust2::YamlLoader::load_from_str(text)
                .unwrap()
                .remove(0)
        };
        let config = doc("coordination:\n  not_owner_principals: [' Friend ', friend, CODEX]\n");
        assert_eq!(
            coordination_names(&config, "not_owner_principals"),
            Some(vec!["friend".to_owned(), "codex".to_owned()])
        );
        assert_eq!(
            coordination_names(&config, "allowed_principals"),
            Some(vec![])
        );
        for bad in ["[' ']", "[1]", "[friend, '']", "friend"] {
            let config = doc(&format!("coordination:\n  not_owner_principals: {bad}\n"));
            assert_eq!(
                coordination_names(&config, "not_owner_principals"),
                None,
                "{bad}"
            );
        }
        let other_bad =
            doc("coordination:\n  allowed_principals: ['']\n  not_owner_principals: [friend]\n");
        assert!(coordination_lists(&other_bad).is_none());
    }

    use super::*;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn native_argument_shapes() {
        for good in [
            &["laptop"][..],
            &["--json", "--json"],
            &["x", "--someone-else", "--someone-else"],
            &["--url=u", "x"],
            &["--"],
            &["--tier", "core", "laptop", "--no-board", "--expires", "2h"],
            &[
                "laptop",
                "--replace",
                "--url",
                "http://h:1",
                "--port",
                "8765",
                "--json",
            ],
            &["--list", "--bank", "abc", "--json"],
            &["--revoke", "laptop", "--yes"],
            &["laptop", "--someone-else"],
            &["--mine", "laptop"],
            &["--version-check"],
            &[""],
        ] {
            assert!(parse(&strings(good)).is_ok(), "{good:?}");
        }
        for invalid in [
            &["a", "b"][..],
            &["--board", "--no-board", "x"],
            &["x", "--mine", "--someone-else"],
            &["--tier", "writer", "x"],
            &["--tier"],
            &["--expires", "30s", "x"],
            &["--expires", "25h", "x"],
            &["--expires", "1 h", "x"],
            &["--port", "0", "x"],
            &["--port", "+1", "x"],
            &["--url", "-x", "x"],
            &["--rev", "x"],
            &["--help"],
            &["-h"],
        ] {
            assert!(parse(&strings(invalid)).is_err(), "{invalid:?}");
        }
    }

    #[test]
    fn durations_follow_the_oracle() {
        assert_eq!(expires("15m").unwrap(), 900);
        assert_eq!(expires("2H").unwrap(), 7200);
        assert_eq!(expires("1d").unwrap(), 86400);
        assert_eq!(expires("90").unwrap(), 5400);
        assert_eq!(expires("60s").unwrap(), 60);
        assert_eq!(expires("0015m").unwrap(), 900);
        assert!(expires("59s").is_err());
        assert!(expires("2d").is_err());
        assert!(expires("m").is_err());
    }

    #[test]
    fn minutes_round_half_to_even() {
        assert_eq!(minutes(900), 15);
        assert_eq!(minutes(90), 2);
        assert_eq!(minutes(150), 2);
        assert_eq!(minutes(89), 1);
        assert_eq!(minutes(91), 2);
        assert_eq!(minutes(86400), 1440);
    }

    #[test]
    fn times_floor_to_the_minute_in_utc() {
        assert_eq!(when(None).unwrap(), "-");
        assert_eq!(when(Some(0.0)).unwrap(), "-");
        assert_eq!(when(Some(1_700_000_059.9)).unwrap(), "2023-11-14 22:14 UTC");
        assert!(when(Some(f64::NAN)).is_err());
        // Outside 1970..=9999 the platforms disagree (glibc prints year 1
        // unpadded, Windows' gmtime refuses negative times): refused.
        assert!(when(Some(-62_135_596_800.0)).is_err());
        assert!(when(Some(-1.0)).is_err());
        assert!(when(Some(253_402_300_800.0)).is_err());
        assert_eq!(
            when(Some(253_402_300_799.0)).unwrap(),
            "9999-12-31 23:59 UTC"
        );
    }

    #[test]
    fn bank_refusals_name_only_valid_fingerprints() {
        let bank = "0123456789abcdef";
        assert_eq!(
            bank_refusal(Some(bank), &J::Str(bank.into()), "--bank"),
            None
        );
        let text = |units: Vec<u16>| String::from_utf16(&units).unwrap();
        assert!(text(bank_refusal(None, &J::Str(bank.into()), "--bank").unwrap()).contains(bank));
        for invalid in [
            J::Int("5".into()),
            J::Wtf(vec![0xd800]),
            J::Str("fixture-private-secret".into()),
        ] {
            let message = text(bank_refusal(Some(bank), &invalid, "--bank").unwrap());
            assert!(message.contains("valid bank fingerprint"));
            assert!(!message.contains("fixture-private-secret"));
        }
    }
}
