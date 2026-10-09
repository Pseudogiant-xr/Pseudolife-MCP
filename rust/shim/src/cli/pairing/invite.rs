//! `pseudolife-mcp invite` (invite_cli.py) over an explicit
//! `PSEUDOLIFE_MCP_DATABASE_URL`:
//!
//! - `invite <name> [--tier T] [--board|--no-board] [--expires D] [--replace]
//!   [--url U] [--port P] [--bank FP] [--json]`
//! - `invite --list [--bank FP] [--json]`
//! - `invite --revoke <name> [--yes] [--bank FP] [--json]`
//!
//! Each option at most once, a value as the next argument; the name
//! anywhere among them. Deferred before any effect: argparse's own errors
//! and help, no DSN (the lite bank, the daemon container, psql in the
//! Postgres container), a DSN this crate's client does not take or cannot
//! reach, a revocation's question on a terminal, environment token or tier
//! maps that would log a warning, a `config.yaml` where `_listed_note` reads
//! one, and (without `--url`) any `tailscale` CLI `expose status` could run.
use super::invite_db::{self, DbError};
use super::pyjson::{self, J};
use super::{Defer, net};
use std::path::{Path, PathBuf};

const EXIT_OK: u8 = 0;
const EXIT_FAILED: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_REFUSED: u8 = 4;
const DEFAULT_PORT: u32 = 8765;
const MIN_SCHEMA: i64 = 53;
const TIERS: [&str; 3] = ["minimal", "core", "full"];
const URL_PLACEHOLDER: &str = "<daemon-url>";
const VERSION_LINE: &str = "pseudolife-mcp invite 1";

#[derive(Default)]
struct Args {
    name: Option<String>,
    tier: Option<String>,
    board: Option<bool>,
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
/// one minute and 24 hours; anything else is argparse's error, deferred.
fn expires(value: &str) -> Result<u32, Defer> {
    let lower = value.to_ascii_lowercase();
    let (digits, unit) = match lower.as_bytes().last() {
        Some(b's') => (&lower[..lower.len() - 1], 1),
        Some(b'm') => (&lower[..lower.len() - 1], 60),
        Some(b'h') => (&lower[..lower.len() - 1], 3600),
        Some(b'd') => (&lower[..lower.len() - 1], 86400),
        _ => (lower.as_str(), 60),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Defer);
    }
    let digits = digits.trim_start_matches('0');
    if digits.len() > 9 {
        return Err(Defer);
    }
    let seconds = digits.parse::<u64>().unwrap_or(0) * unit;
    if !(60..=86400).contains(&seconds) {
        return Err(Defer);
    }
    u32::try_from(seconds).map_err(|_| Defer)
}

/// A port spelled as ASCII digits in 1..=65535 (`expose_cli._port`, and the
/// `PSEUDOLIFE_MCP_PORT` default); any other spelling defers.
fn port(value: &str) -> Result<u32, Defer> {
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Defer);
    }
    let digits = value.trim_start_matches('0');
    if digits.len() > 5 {
        return Err(Defer);
    }
    let port = digits.parse::<u32>().unwrap_or(0);
    if (1..=65535).contains(&port) {
        Ok(port)
    } else {
        Err(Defer)
    }
}

fn parse(arguments: &[String]) -> Result<Args, Defer> {
    let mut args = Args::default();
    let mut items = arguments.iter();
    fn once<T>(slot: &mut Option<T>, value: T) -> Result<(), Defer> {
        if slot.is_some() {
            return Err(Defer);
        }
        *slot = Some(value);
        Ok(())
    }
    fn flag(slot: &mut bool) -> Result<(), Defer> {
        if *slot {
            return Err(Defer);
        }
        *slot = true;
        Ok(())
    }
    while let Some(item) = items.next() {
        let mut value = || {
            items
                .next()
                .filter(|value| !value.starts_with('-'))
                .cloned()
                .ok_or(Defer)
        };
        match item.as_str() {
            "--tier" => {
                let tier = value()?;
                if !TIERS.contains(&tier.as_str()) {
                    return Err(Defer);
                }
                once(&mut args.tier, tier)?;
            }
            "--board" => once(&mut args.board, true)?,
            "--no-board" => once(&mut args.board, false)?,
            "--expires" => once(&mut args.expires, expires(&value()?)?)?,
            "--replace" => flag(&mut args.replace)?,
            "--url" => once(&mut args.url, value()?)?,
            "--port" => once(&mut args.port, port(&value()?)?)?,
            "--bank" => once(&mut args.bank, value()?)?,
            "--list" => flag(&mut args.list)?,
            "--revoke" => once(&mut args.revoke, value()?)?,
            "--yes" => flag(&mut args.yes)?,
            "--json" => flag(&mut args.json)?,
            "--version-check" => flag(&mut args.version_check)?,
            positional if !positional.starts_with('-') => once(&mut args.name, item.clone())?,
            _ => return Err(Defer),
        }
    }
    Ok(args)
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
        if let Some((_, J::List(notes))) = self.data.iter_mut().find(|(key, _)| key == "notes") {
            notes.push(J::Str(line.to_owned()));
        }
        self.say(line);
    }

    fn fail(&mut self, code: u8, line: &str) -> u8 {
        self.set("error", J::Str(line.to_owned()));
        if !self.json {
            super::eprint(&format!("invite: {line}"));
        }
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
fn when(epoch: Option<f64>) -> Result<String, Defer> {
    let Some(epoch) = epoch.filter(|epoch| *epoch != 0.0) else {
        return Ok("-".to_owned());
    };
    if !epoch.is_finite() {
        return Err(Defer);
    }
    let moment = chrono::DateTime::from_timestamp(epoch.floor() as i64, 0).ok_or(Defer)?;
    Ok(moment.format("%Y-%m-%d %H:%M UTC").to_string())
}

/// `parse_token_map` principals and `parse_tier_map` writers, or a deferral
/// when either would log a warning (or holds non-ASCII text).
fn environment_names() -> Result<(Vec<String>, Vec<String>), Defer> {
    let read = |name: &str| -> Result<String, Defer> {
        match std::env::var(name) {
            Ok(value) if value.is_ascii() => Ok(value),
            Ok(_) => Err(Defer),
            Err(std::env::VarError::NotPresent) => Ok(String::new()),
            Err(_) => Err(Defer),
        }
    };
    let mut tokens: Vec<(String, String)> = Vec::new();
    for part in read("PSEUDOLIFE_MCP_TOKENS")?.split(',') {
        let part = pyjson::py_strip(part);
        if part.is_empty() {
            continue;
        }
        let (token, principal) = part.rsplit_once(':').ok_or(Defer)?;
        let token = pyjson::py_strip(token);
        let principal = pyjson::py_strip(principal).to_ascii_lowercase();
        if token.is_empty()
            || principal.is_empty()
            || principal == "default"
            || principal == "maintainer"
            || tokens.iter().any(|(seen, _)| seen == token)
        {
            return Err(Defer);
        }
        tokens.push((token.to_owned(), principal));
    }
    let mut writers = Vec::new();
    for part in read("PSEUDOLIFE_MCP_TIER_MAP")?.split(',') {
        let part = pyjson::py_strip(part);
        if part.is_empty() {
            continue;
        }
        let (writer, tier) = part.split_once(':').ok_or(Defer)?;
        let writer = pyjson::py_strip(writer).to_ascii_lowercase();
        let tier = pyjson::py_strip(tier).to_ascii_lowercase();
        if writer.is_empty() || !TIERS.contains(&tier.as_str()) {
            return Err(Defer);
        }
        writers.push(writer);
    }
    Ok((tokens.into_iter().map(|(_, p)| p).collect(), writers))
}

/// `_name_refusal`.
fn name_refusal(name: &str) -> Result<Option<String>, Defer> {
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

/// Whether a path is there, conservatively: only "missing" answers false.
fn maybe_present(path: &Path) -> bool {
    match std::fs::symlink_metadata(path) {
        Ok(_) => true,
        Err(error) => !matches!(
            error.kind(),
            std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
        ),
    }
}

fn env_nonempty(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

/// `Path.home()` as CPython 3.11 resolves it from the environment; a home
/// it would take from elsewhere (the password database) defers.
fn python_home() -> Result<PathBuf, Defer> {
    if cfg!(windows) {
        if let Some(home) = env_nonempty("USERPROFILE") {
            return Ok(home);
        }
        let path = std::env::var_os("HOMEPATH").ok_or(Defer)?;
        let drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
        Ok(PathBuf::from(drive).join(path))
    } else {
        std::env::var_os("HOME").map(PathBuf::from).ok_or(Defer)
    }
}

/// Whether `_listed_note` could read a `config.yaml`: the data dir's
/// (`PSEUDOLIFE_MCP_DATA_DIR`, else the lite default or `./data`, both
/// checked). Its reading (the whole AppConfig, any failure a silent "no
/// note") is not ported; such an invite defers.
fn config_maybe_present() -> Result<bool, Defer> {
    let mut candidates = Vec::new();
    if let Some(dir) = env_nonempty("PSEUDOLIFE_MCP_DATA_DIR") {
        candidates.push(dir);
    } else {
        let lite = if cfg!(windows) {
            env_nonempty("LOCALAPPDATA").map_or_else(
                || python_home().map(|home| home.join("AppData").join("Local")),
                Ok,
            )?
        } else if cfg!(target_os = "macos") {
            python_home()?.join("Library").join("Application Support")
        } else {
            env_nonempty("XDG_DATA_HOME").map_or_else(
                || python_home().map(|home| home.join(".local").join("share")),
                Ok,
            )?
        };
        candidates.push(lite.join("pseudolife-mcp"));
        candidates.push(std::env::current_dir().map_err(|_| Defer)?.join("data"));
    }
    Ok(candidates
        .iter()
        .any(|dir| maybe_present(&dir.join("config.yaml"))))
}

/// Whether `expose_cli.find_tailscale` could find a CLI (a superset of
/// `shutil.which("tailscale")` and the installers' default locations). Only
/// with none does `exposed_url` answer `None` without running anything.
fn tailscale_maybe_present() -> Result<bool, Defer> {
    let path = std::env::var_os("PATH").ok_or(Defer)?;
    let mut directories: Vec<PathBuf> = std::env::split_paths(&path).collect();
    let mut names = vec!["tailscale".to_owned()];
    if cfg!(windows) {
        directories.insert(0, PathBuf::from("."));
        let pathext = std::env::var("PATHEXT")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WSF;.WSH;.MSC".to_owned());
        names.extend(
            pathext
                .split(';')
                .filter(|ext| !ext.is_empty())
                .map(|ext| format!("tailscale{ext}")),
        );
        let base = env_nonempty("ProgramW6432")
            .or_else(|| env_nonempty("ProgramFiles"))
            .unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
        if maybe_present(&base.join("Tailscale").join("tailscale.exe")) {
            return Ok(true);
        }
    } else if cfg!(target_os = "macos")
        && maybe_present(Path::new(
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        ))
    {
        return Ok(true);
    }
    for directory in directories {
        let directory = if directory.as_os_str().is_empty() {
            PathBuf::from(".")
        } else {
            directory
        };
        if names
            .iter()
            .any(|name| maybe_present(&directory.join(name)))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn python_bool(value: bool) -> J {
    J::Bool(value)
}

pub(super) async fn run(arguments: &[String]) -> Result<u8, Defer> {
    let args = parse(arguments)?;
    // `int(os.environ.get("PSEUDOLIFE_MCP_PORT") or 8765)` runs while the
    // parser is built; a value int() would refuse is a traceback.
    let default_port = match std::env::var("PSEUDOLIFE_MCP_PORT") {
        Ok(value) if !value.is_empty() => port(&value)?,
        Ok(_) | Err(std::env::VarError::NotPresent) => DEFAULT_PORT,
        Err(_) => return Err(Defer),
    };
    if args.version_check {
        super::print(VERSION_LINE);
        return Ok(EXIT_OK);
    }
    let mut report = Report::new(args.json);
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
    if let Some(target) = name.as_ref().or(revoke.as_ref()) {
        if !super::valid_principal_name(target) {
            return Ok(report.fail(
                EXIT_USAGE,
                &format!(
                    "{} is not a principal name: lowercase letters, digits, '.', '_' and '-', starting with a letter or digit, at most 64 characters",
                    super::super::mode_repr(target)
                ),
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
            return Err(Defer);
        }
        return Ok(report.fail(EXIT_USAGE, "not interactive: re-run with --yes to revoke"));
    }
    let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or(Defer)?;
    let dsn = crate::pg::Dsn::parse(&dsn).map_err(|_| Defer)?;
    let url = args.url.clone().filter(|url| !url.is_empty());
    let port = args.port.unwrap_or(default_port);
    let mut health = None;
    if name.is_some() {
        if url.is_none() && tailscale_maybe_present()? {
            return Err(Defer);
        }
        if config_maybe_present()? {
            return Err(Defer);
        }
        match daemon_check(port, &mut report).await? {
            Ok(found) => health = Some(found),
            Err(refusal) => return Ok(report.fail(EXIT_REFUSED, &refusal)),
        }
    }
    let mut session = crate::pg::Session::open(&dsn).await.map_err(|_| Defer)?;
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

/// `_daemon_check(port, report, need_auth=True)`: the health, or the
/// refusal; the report's `daemon` member is set as Python sets it.
async fn daemon_check(
    port: u32,
    report: &mut Report,
) -> Result<Result<pyjson::Dict, String>, Defer> {
    let local = format!("http://127.0.0.1:{port}");
    let health = net::invite_health(&local).await?;
    let Some(health) =
        health.filter(|h| pyjson::get(h, "status").and_then(J::as_str) == Some("ok"))
    else {
        return Ok(Err(format!(
            "no healthy daemon answers at {local}/health: start it (or pass --port), then re-run"
        )));
    };
    let member = |key: &str| pyjson::get(&health, key).cloned().unwrap_or(J::Null);
    report.set(
        "daemon",
        J::Dict(vec![
            ("auth".into(), member("auth")),
            ("schema".into(), member("schema")),
            ("bank".into(), member("bank")),
        ]),
    );
    if pyjson::get(&health, "auth") != Some(&J::Bool(true)) {
        return Ok(Err(format!(
            "the daemon at {local} does not report \"auth\": true. Stored principals do not turn authentication on: give the daemon a token first (docs/guide/remote-bank.md)"
        )));
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
        return Ok(Err(format!(
            "the daemon runs schema {}, older than invite (v{MIN_SCHEMA}): update the daemon first",
            schema.py_str()
        )));
    }
    if !member("bank").truthy() {
        return Ok(Err("the daemon has not reported which bank it serves yet (/health bank is null until its storage has started and the agent board has been used once): start a session against it, then re-run".to_owned()));
    }
    Ok(Ok(health))
}

/// `_bank_refusal`: `None` when the bank is the expected one.
fn bank_refusal(bank: Option<&str>, expected: &J, what: &str) -> Option<String> {
    if matches!((bank, expected), (Some(bank), J::Str(expected)) if bank == expected)
        || (bank.is_none() && *expected == J::Null)
    {
        return None;
    }
    Some(format!(
        "this database holds bank {}, but {what} is bank {}: it is another database. Nothing was changed",
        bank.unwrap_or("(none)"),
        expected.py_str()
    ))
}

const NO_TABLE: &str = "this bank has no principals table yet (schema v53): update the daemon and let it start, then re-run";

/// `_local` after the daemon check: the database errors as Python reports
/// them; an error whose psycopg class is not pinned defers (its statement
/// or transaction changed nothing).
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
) -> Result<u8, Defer> {
    let result = async {
        invite_db::setup(session.client()).await?;
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
            return Ok(Ok(Outcome::List(invite_db::list(session.client()).await?)));
        }
        if let Some(revoke) = &revoke {
            let found = invite_db::revoke(session.client_mut(), revoke).await?;
            return Ok(Ok(Outcome::Revoked(found)));
        }
        let name = name.clone().ok_or(DbError::Defer)?;
        let created = invite_db::create_invite(
            session.client_mut(),
            &name,
            args.tier.as_deref(),
            args.board,
            f64::from(args.expires.unwrap_or(900)),
            args.replace,
        )
        .await?;
        Ok(Ok(Outcome::Created(created)))
    }
    .await;
    match result {
        Ok(Ok(Outcome::List(rows))) => list_report(report, &rows),
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
        Ok(Ok(Outcome::Created(created))) => Ok(invite_report(
            report,
            &name.unwrap_or_default(),
            &created,
            url,
            args.expires.unwrap_or(900),
        )),
        Ok(Err(refusal)) => Ok(report.fail(EXIT_REFUSED, &refusal)),
        Err(DbError::UndefinedTable) => Ok(report.fail(EXIT_REFUSED, NO_TABLE)),
        Err(DbError::Refused(message)) => {
            Ok(report.fail(EXIT_REFUSED, &format!("{message}. Nothing was changed")))
        }
        Err(DbError::Failed(class)) => Ok(report.fail(
            EXIT_FAILED,
            &format!("the database refused the change ({class})"),
        )),
        Err(DbError::Defer) => Err(Defer),
    }
}

enum Outcome {
    List(Vec<invite_db::Listed>),
    Revoked(bool),
    Created(invite_db::Created),
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

/// `_invite`'s report, after the row was written.
fn invite_report(
    report: &mut Report,
    name: &str,
    created: &invite_db::Created,
    url: Option<String>,
    seconds: u32,
) -> u8 {
    let code = format!(
        "{}-{}-{}",
        &created.code[..4],
        &created.code[4..8],
        &created.code[8..]
    );
    let url = url.unwrap_or_else(|| URL_PLACEHOLDER.to_owned());
    let command = format!("pseudolife-mcp pair {url} {code}");
    report.set("principal", J::Str(name.to_owned()));
    report.set("state", J::Str(created.state.into()));
    report.set("code", J::Str(code.clone()));
    report.set("expires_at", J::Float(created.expires_at));
    report.set("tier", created.tier.clone().map_or(J::Null, J::Str));
    report.set("board", python_bool(created.board));
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
        "{name}: {what}; tier {tier}, agent board {}",
        if created.board { "on" } else { "off" }
    ));
    // The expiry is the database clock plus at most a day: always finite.
    let expiry = when(Some(created.expires_at)).unwrap_or_default();
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
    report.note("an invited machine can read and write the whole bank and use the agent board, but not change the daemon's configuration");
    report.finish(EXIT_OK)
}

fn float(value: Option<f64>) -> J {
    value.map_or(J::Null, J::Float)
}

/// `_list` (read only: an unrepresentable time defers before any output).
fn list_report(report: &mut Report, rows: &[invite_db::Listed]) -> Result<u8, Defer> {
    let mut lines = Vec::new();
    let mut members = Vec::new();
    for row in rows {
        for time in [
            row.code_expires_at,
            Some(row.created_at),
            row.paired_at,
            row.revoked_at,
        ]
        .into_iter()
        .flatten()
        {
            if !time.is_finite() {
                return Err(Defer);
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
        lines.push(format!(
            "  {:<24} {:<9} {:<8} {:<6} {:<21} {:<21} {}",
            row.principal,
            state,
            tier,
            if row.board { "on" } else { "off" },
            when(Some(row.created_at))?,
            when(row.paired_at)?,
            when(row.revoked_at)?
        ));
        members.push(J::Dict(vec![
            ("principal".into(), J::Str(row.principal.clone())),
            ("state".into(), J::Str(row.state.into())),
            ("paired".into(), J::Bool(row.paired)),
            ("tier".into(), row.tier.clone().map_or(J::Null, J::Str)),
            ("board".into(), J::Bool(row.board)),
            (
                "code_expires_at".into(),
                float(if row.pending {
                    row.code_expires_at
                } else {
                    None
                }),
            ),
            ("created_at".into(), J::Float(row.created_at)),
            ("paired_at".into(), float(row.paired_at)),
            ("revoked_at".into(), float(row.revoked_at)),
        ]));
    }
    report.set("principals", J::List(members));
    if rows.is_empty() {
        report.say("no stored principals");
        return Ok(report.finish(EXIT_OK));
    }
    report.say(&format!(
        "  {:<24} {:<9} {:<8} {:<6} {:<21} {:<21} revoked",
        "name", "state", "tier", "board", "created", "paired"
    ));
    for line in lines {
        report.say(&line);
    }
    Ok(report.finish(EXIT_OK))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn canonical_argv_only() {
        for good in [
            &["laptop"][..],
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
            &["--version-check"],
            &[""],
        ] {
            assert!(parse(&strings(good)).is_ok(), "{good:?}");
        }
        for deferred in [
            &["a", "b"][..],
            &["--board", "--no-board", "x"],
            &["--json", "--json"],
            &["--tier", "writer", "x"],
            &["--tier"],
            &["--expires", "30s", "x"],
            &["--expires", "25h", "x"],
            &["--expires", "1 h", "x"],
            &["--port", "0", "x"],
            &["--port", "+1", "x"],
            &["--url", "-x", "x"],
            &["--url=u", "x"],
            &["--rev", "x"],
            &["--help"],
            &["-h"],
            &["--"],
        ] {
            assert!(parse(&strings(deferred)).is_err(), "{deferred:?}");
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
    }

    #[test]
    fn bank_refusals_name_both_banks() {
        assert_eq!(
            bank_refusal(Some("ab"), &J::Str("ab".into()), "--bank"),
            None
        );
        assert_eq!(
            bank_refusal(None, &J::Str("ab".into()), "--bank").unwrap(),
            "this database holds bank (none), but --bank is bank ab: it is another database. Nothing was changed"
        );
        assert!(
            bank_refusal(Some("ab"), &J::Int("5".into()), "x")
                .unwrap()
                .contains("bank 5:")
        );
    }
}
