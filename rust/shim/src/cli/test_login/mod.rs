//! `test-login create`: the test suite's own PostgreSQL login, which creates
//! and drops its own databases and cannot connect to the bank.
//!
//! Oracle: `pseudolife_memory/test_login_cli.py`. Canonical argv only:
//! `create` followed by `--rotate`, `--json`, `--role R`, `--file P`,
//! `--admin-url URL`, `--container NAME` (each at most once, the value a
//! separate argument) and `--bank DB` (repeatable). Every other spelling,
//! an unprintable home or file path, a daemon DSN or admin URL outside the
//! shared client's grammar, and an ambient libpq control that client refuses
//! defer before any connection or file change.
mod exec;
mod file;
mod scram;
mod sql;

use exec::Executor;
use serde_json::Value;
use std::{
    collections::HashMap,
    ffi::OsString,
    fmt::Write as _,
    io::{self, Write},
    path::{Path, PathBuf},
    process::ExitCode,
    sync::LazyLock,
};

const EXIT_OK: u8 = 0;
const EXIT_FAILED: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_REFUSED: u8 = 4;

const POSTGRES_CONTAINER: &str = "pseudolife-mcp-postgres";
const DEFAULT_ROLE: &str = "pseudolife_test";
const FILE_ENV: &str = "PSEUDOLIFE_TEST_PG_LOGIN_FILE";
const USER_KEY: &str = "PSEUDOLIFE_TEST_PG_USER";
const PASSWORD_KEY: &str = "PSEUDOLIFE_TEST_PG_PASSWORD";
const DAEMON_DSN_ENV: &str = "PSEUDOLIFE_MCP_DATABASE_URL";
const DEFAULT_BANKS: &[&str] = &["pseudolife_memory"];
const NOT_BANKS: &[&str] = &["postgres", "template0", "template1"];
const ROLE_PATTERN: &str = "[a-z_][a-z0-9_]{0,62}";
const FILE_HEADER: &str = "\
# Pseudolife-MCP test login, written by `pseudolife-mcp test-login create`.
# The PostgreSQL role the test suite logs in as: it creates and drops its own
# databases and cannot connect to the bank. Not the bank owner's password.
# Rotate with `pseudolife-mcp test-login create --rotate`.
";

/// `re`'s `\s` for `str` patterns (`str.isspace()`).
const SPACE: &str = r"\t\n\x0B\x0C\r\x1C-\x1F \x{85}\x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}";

static URL_PASSWORD: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(&format!(r"(://[^:/?#@{SPACE}]*:)[^@{SPACE}]*@")).expect("pattern")
});
static KEYWORD_PASSWORD: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(&format!(
        r"(?i)(password[{SPACE}]*=[{SPACE}]*)('(?:[^'\\]|\\.)*'|[^{SPACE}]+)"
    ))
    .expect("pattern")
});
static QUOTED_TOKEN: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(&format!(r#"(?i)(percent-encoded token:[{SPACE}]*)"[^"]*""#))
        .expect("pattern")
});
static LEFTOVER: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(&format!("^(?:{})$", sql::LEFTOVER_PATTERN)).expect("pattern")
});

/// `_userinfo_tokens`: the password an admin URL carries, raw and decoded.
fn userinfo_tokens(url: Option<&str>) -> Vec<String> {
    let Some(url) = url.filter(|url| !url.is_empty()) else {
        return Vec::new();
    };
    let mut found = Vec::new();
    let authority =
        regex::Regex::new(&format!(r"(?i)^[{SPACE}]*[a-z]+://([^/?#]*)@")).expect("pattern");
    if let Some(userinfo) = authority.captures(url).map(|c| c[1].to_owned())
        && let Some((_, password)) = userinfo.split_once(':')
    {
        found.push(password.to_owned());
    }
    let keyword = regex::Regex::new(&format!(
        r"(?i)password[{SPACE}]*=[{SPACE}]*('(?:[^'\\]|\\.)*'|[^{SPACE}]+)"
    ))
    .expect("pattern");
    if let Some(value) = keyword.captures(url).map(|c| c[1].to_owned()) {
        found.push(value.trim_matches('\'').to_owned());
    }
    let decoded: Vec<String> = found
        .iter()
        .map(|value| {
            percent_encoding::percent_decode_str(value)
                .decode_utf8_lossy()
                .into_owned()
        })
        .collect();
    found.extend(decoded);
    let mut unique: Vec<String> = Vec::new();
    for value in found {
        if !value.is_empty() && !unique.contains(&value) {
            unique.push(value);
        }
    }
    unique
}

/// `redacted`: the admin URL's password, and anything shaped like a
/// password, replaced by `***`.
fn redacted(text: &str, url: Option<&str>) -> String {
    let mut tokens = userinfo_tokens(url);
    tokens.sort_by_key(|token| std::cmp::Reverse(token.chars().count()));
    let mut text = text.to_owned();
    for token in tokens {
        text = text.replace(&token, "***");
    }
    let text = URL_PASSWORD.replace_all(&text, "${1}***@");
    let text = KEYWORD_PASSWORD.replace_all(&text, "${1}***");
    QUOTED_TOKEN.replace_all(&text, "${1}\"***\"").into_owned()
}

/// `json.dumps` with its defaults (`ensure_ascii`, `", "` and `": "`).
fn py_json(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => out.push_str(&number.to_string()),
        Value::String(text) => {
            out.push('"');
            for c in text.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    '\u{8}' => out.push_str("\\b"),
                    '\u{c}' => out.push_str("\\f"),
                    ' '..='~' => out.push(c),
                    _ => {
                        let mut units = [0u16; 2];
                        for unit in c.encode_utf16(&mut units) {
                            let _ = write!(out, "\\u{unit:04x}");
                        }
                    }
                }
            }
            out.push('"');
        }
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                py_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                py_json(&Value::String(key.clone()), out);
                out.push_str(": ");
                py_json(item, out);
            }
            out.push('}');
        }
    }
}

/// Python truthiness of a decoded JSON value.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|n| n != 0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// `str(value)` of a decoded JSON value.
fn py_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(flag)) => if *flag { "True" } else { "False" }.into(),
        Some(other) => other.to_string(),
    }
}

/// `repr(value)` of a decoded JSON value.
fn py_repr(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => super::mode_repr(text),
        other => py_str(other),
    }
}

fn strings(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items.iter().map(|item| py_str(Some(item))).collect(),
        _ => Vec::new(),
    }
}

struct Report {
    json: bool,
    lines: Vec<String>,
    data: Vec<(&'static str, Value)>,
}

impl Report {
    fn print(text: &str) {
        let mut out = io::stdout().lock();
        let _ = out.write_all(&super::text_bytes(&format!("{text}\n")));
        let _ = out.flush();
    }

    fn say(&mut self, line: String) {
        if !self.json {
            Self::print(&line);
        }
        self.lines.push(line);
    }

    fn finish(&self, code: u8, error: Option<&str>) -> u8 {
        let error = error.map(|error| redacted(error, None));
        if let Some(error) = &error
            && !self.json
        {
            Self::print(&format!("test-login: {error}"));
        }
        if self.json {
            let mut map = serde_json::Map::new();
            map.insert("exit".into(), Value::from(code));
            map.insert("error".into(), error.map_or(Value::Null, Value::String));
            map.insert(
                "changes".into(),
                Value::Array(self.lines.iter().cloned().map(Value::String).collect()),
            );
            for (key, value) in &self.data {
                map.insert((*key).into(), value.clone());
            }
            let mut text = String::new();
            py_json(&Value::Object(map), &mut text);
            Self::print(&text);
        }
        code
    }
}

struct Args {
    role: String,
    file: Option<String>,
    rotate: bool,
    banks: Vec<String>,
    admin_url: Option<String>,
    container: Option<String>,
    json: bool,
}

fn parse(values: &[OsString]) -> Option<Args> {
    let mut values = values.iter();
    if values.next()?.to_str()? != "create" {
        return None;
    }
    let mut args = Args {
        role: DEFAULT_ROLE.into(),
        file: None,
        rotate: false,
        banks: Vec::new(),
        admin_url: None,
        container: None,
        json: false,
    };
    let mut role_seen = false;
    while let Some(option) = values.next() {
        let option = option.to_str()?;
        match option {
            "--rotate" if !args.rotate => {
                args.rotate = true;
                continue;
            }
            "--json" if !args.json => {
                args.json = true;
                continue;
            }
            "--role" | "--file" | "--admin-url" | "--container" | "--bank" => {}
            _ => return None,
        }
        let value = values.next()?.to_str()?;
        // argparse reads a leading '-' as an option; an empty value is a
        // shape no producer writes (except the role, whose check answers it).
        if value.starts_with('-') || (value.is_empty() && option != "--role") {
            return None;
        }
        match option {
            "--role" if !role_seen => {
                role_seen = true;
                args.role = value.into();
            }
            "--file" if args.file.is_none() && file::canonical(value) => {
                args.file = Some(value.into());
            }
            "--admin-url" if args.admin_url.is_none() => args.admin_url = Some(value.into()),
            "--container" if args.container.is_none() => args.container = Some(value.into()),
            "--bank" => args.banks.push(value.into()),
            _ => return None,
        }
    }
    Some(args)
}

fn role_name_ok(role: &str) -> bool {
    let mut chars = role.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_lowercase() || c == '_')
        && role.chars().count() <= 63
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// `_dsn_parts_stdlib`, for comparison with the libpq reading.
fn stdlib_dsn_parts(dsn: &str) -> HashMap<&'static str, String> {
    let decode = |text: &str| {
        percent_encoding::percent_decode_str(text)
            .decode_utf8_lossy()
            .into_owned()
    };
    let mut parts = HashMap::new();
    let scheme =
        regex::Regex::new(&format!(r"(?i)^[{SPACE}]*postgres(?:ql)?://")).expect("pattern");
    if scheme.is_match(dsn) {
        let url = file::py_strip(dsn);
        let rest = url.split_once("//").map_or("", |(_, rest)| rest);
        let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        let (netloc, tail) = rest.split_at(end);
        let path = tail.split(['?', '#']).next().unwrap_or("");
        let user = netloc
            .rsplit_once('@')
            .map_or("", |(userinfo, _)| userinfo.split(':').next().unwrap_or(""));
        parts.insert("user", decode(user));
        parts.insert("dbname", decode(path.trim_start_matches('/')));
        return parts;
    }
    let keyword = regex::Regex::new(&format!(
        r"(user|dbname)[{SPACE}]*=[{SPACE}]*('(?:[^'\\]|\\.)*'|[^{SPACE}]+)"
    ))
    .expect("pattern");
    for capture in keyword.captures_iter(dsn) {
        let key = if &capture[1] == "user" {
            "user"
        } else {
            "dbname"
        };
        parts.insert(key, capture[2].trim_matches('\'').to_owned());
    }
    parts
}

/// `_dsn_parts`: the daemon DSN's `user` and `dbname`. `None` defers: a DSN
/// the shared client cannot read, or one that psycopg's and the standard
/// library's readings (the oracle uses whichever is installed) would answer
/// differently.
fn dsn_parts(dsn: Option<&str>) -> Option<HashMap<&'static str, String>> {
    let Some(dsn) = dsn.filter(|dsn| !dsn.is_empty()) else {
        return Some(HashMap::new());
    };
    let parsed = crate::pg::Dsn::parse(dsn).ok()?;
    let config = parsed.config();
    let libpq = [
        ("user", config.get_user().unwrap_or("").to_owned()),
        ("dbname", config.get_dbname().unwrap_or("").to_owned()),
    ];
    let stdlib = stdlib_dsn_parts(dsn);
    let mut parts = HashMap::new();
    for (key, value) in libpq {
        if stdlib.get(key).map_or("", String::as_str) != value {
            return None;
        }
        if !value.is_empty() {
            parts.insert(key, value);
        }
    }
    Some(parts)
}

/// `shutil.which("docker")` as Python 3.11 answers it. `None` defers.
fn docker_on_path() -> Option<bool> {
    let path = std::env::var_os("PATH")?.into_string().ok()?;
    if path.is_empty() {
        return Some(false);
    }
    let separator = if cfg!(windows) { ';' } else { ':' };
    let mut dirs: Vec<String> = path.split(separator).map(str::to_owned).collect();
    let mut names = vec!["docker".to_owned()];
    if cfg!(windows) {
        if !dirs.iter().any(|dir| dir == ".") {
            dirs.insert(0, ".".into());
        }
        let pathext = std::env::var("PATHEXT")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC".into());
        names = pathext
            .split(';')
            .filter(|ext| !ext.is_empty())
            .map(|ext| format!("docker{ext}"))
            .collect();
    }
    let mut seen = Vec::new();
    for dir in dirs {
        let normal = if cfg!(windows) {
            dir.replace('/', "\\").to_lowercase()
        } else {
            dir.clone()
        };
        if seen.contains(&normal) {
            continue;
        }
        seen.push(normal);
        for name in &names {
            let candidate = if dir.is_empty() {
                PathBuf::from(name)
            } else {
                Path::new(&dir).join(name)
            };
            let Ok(meta) = std::fs::metadata(&candidate) else {
                continue;
            };
            if meta.is_dir() {
                continue;
            }
            #[cfg(unix)]
            if rustix::fs::access(&candidate, rustix::fs::Access::EXEC_OK).is_err() {
                continue;
            }
            return Some(true);
        }
    }
    Some(false)
}

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    // The oracle resolves the home at import, before parsing.
    let home = file::home()?;
    let args = parse(&values)?;
    let mut report = Report {
        json: args.json,
        lines: Vec::new(),
        data: Vec::new(),
    };
    if !role_name_ok(&args.role) {
        return Some(ExitCode::from(report.finish(
            EXIT_USAGE,
            Some(&format!(
                "role name {} must match {ROLE_PATTERN}",
                super::mode_repr(&args.role)
            )),
        )));
    }
    if let Some(named) = args
        .banks
        .iter()
        .find(|bank| NOT_BANKS.contains(&bank.as_str()))
    {
        return Some(ExitCode::from(report.finish(
            EXIT_USAGE,
            Some(&format!("{named} is not a bank and stays open")),
        )));
    }
    let path = match &args.file {
        Some(value) => PathBuf::from(value),
        None => match std::env::var_os(FILE_ENV).filter(|value| !value.is_empty()) {
            Some(value) => {
                let value = value.into_string().ok()?;
                if !file::canonical(&value) {
                    return None;
                }
                PathBuf::from(value)
            }
            None => home.join(".pseudolife-mcp").join("test-pg.env"),
        },
    };
    if !file::writable_shape(&path) {
        return None;
    }
    let shown_path = file::shown(&path, &home)?;
    let path_text = path.to_str()?.to_owned();
    let mut banks: Vec<String> = Vec::new();
    for bank in DEFAULT_BANKS
        .iter()
        .map(|bank| (*bank).to_owned())
        .chain(args.banks.iter().cloned())
    {
        if !banks.contains(&bank) {
            banks.push(bank);
        }
    }
    let daemon_env = match std::env::var_os(DAEMON_DSN_ENV) {
        Some(value) => Some(value.into_string().ok()?),
        None => None,
    };
    let daemon = dsn_parts(daemon_env.as_deref())?;
    let container = args
        .container
        .clone()
        .unwrap_or_else(|| POSTGRES_CONTAINER.into());
    let executor = if let Some(url) = &args.admin_url {
        if !exec::admin_url_admitted(url) {
            return None;
        }
        if let Some(dbname) = daemon.get("dbname") {
            banks.push(dbname.clone());
        }
        Executor::Admin { url: url.clone() }
    } else if docker_on_path()? {
        Executor::Container { name: container }
    } else {
        return Some(ExitCode::from(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "no superuser connection: run this on the Docker host, where the \
                 {container} container runs, or give --admin-url with a superuser URL"
            )),
        )));
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    let context = Context {
        args: &args,
        executor: &executor,
        path: &path,
        path_text,
        shown_path,
        home: &home,
        daemon: &daemon,
        daemon_set: daemon_env.as_deref().is_some_and(|dsn| !dsn.is_empty()),
    };
    let code = runtime.block_on(async {
        match create(&context, banks, &mut report).await {
            Ok(code) => code,
            Err(Failure::Database(text)) => {
                report.finish(EXIT_FAILED, Some(&format!("the database refused: {text}")))
            }
            Err(Failure::Write(text)) => report.finish(
                EXIT_FAILED,
                Some(&format!("could not write {}: {text}", context.shown_path)),
            ),
            Err(Failure::Answer) => report.finish(
                EXIT_FAILED,
                Some(&format!(
                    "{} gave an answer this command does not understand",
                    executor.description()
                )),
            ),
        }
    });
    Some(ExitCode::from(code))
}

struct Context<'a> {
    args: &'a Args,
    executor: &'a Executor,
    path: &'a Path,
    path_text: String,
    shown_path: String,
    home: &'a Path,
    daemon: &'a HashMap<&'static str, String>,
    daemon_set: bool,
}

enum Failure {
    Database(String),
    Write(String),
    Answer,
}

async fn query_json(
    executor: &Executor,
    database: Option<&str>,
    statement: String,
) -> Result<Value, Failure> {
    let answer = executor
        .query(database, &[statement])
        .await
        .map_err(Failure::Database)?;
    serde_json::from_str(&answer).map_err(|_| Failure::Answer)
}

async fn create(
    context: &Context<'_>,
    banks: Vec<String>,
    report: &mut Report,
) -> Result<u8, Failure> {
    let Context {
        args,
        executor,
        path,
        daemon,
        ..
    } = context;
    let role_name = args.role.as_str();
    let who = query_json(executor, None, sql::whoami_statement()).await?;
    if !truthy(who.get("super")) {
        return Ok(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "{} logs in as {}, which is not a superuser. Installing vector into template1 \
                 needs one (it is not a trusted extension); on the Docker tier that is the bank \
                 owner, POSTGRES_USER. Nothing was changed.",
                executor.description(),
                py_repr(who.get("who"))
            )),
        ));
    }
    if who.get("who").and_then(Value::as_str) == Some(role_name) {
        return Ok(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "{role_name} is the role this runs as; the test login must be a different one"
            )),
        ));
    }
    let mut banks = banks;
    if let Some(db) = who.get("db").and_then(Value::as_str)
        && !db.is_empty()
        && !NOT_BANKS.contains(&db)
        && executor.is_container()
    {
        banks.push(db.to_owned());
    }
    let mut unique: Vec<String> = Vec::new();
    for bank in banks {
        if !unique.contains(&bank) && !NOT_BANKS.contains(&bank.as_str()) {
            unique.push(bank);
        }
    }
    let banks = unique;
    let daemon_user = daemon.get("user").map(String::as_str);
    let before = query_json(
        executor,
        Some("postgres"),
        sql::state_statement(role_name, &banks, daemon_user),
    )
    .await?;
    let role = before.get("role").filter(|role| truthy(Some(*role)));
    if role.is_some_and(|role| truthy(role.get("super"))) {
        return Ok(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "role {role_name} exists and is a superuser: not a test login, and this will \
                 not demote it"
            )),
        ));
    }
    let empty = serde_json::Map::new();
    let before_banks = before
        .get("banks")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let owned: Vec<String> = strings(before.get("owns"))
        .into_iter()
        .filter(|bank| banks.contains(bank))
        .collect();
    let owners: Vec<String> = before_banks
        .iter()
        .filter(|(_, info)| info.get("owner").and_then(Value::as_str) == Some(role_name))
        .map(|(bank, _)| bank.clone())
        .collect();
    if let Some(first) = owned.first().or(owners.first()) {
        return Ok(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "role {role_name} owns the bank {first}: not a test login"
            )),
        ));
    }
    let mut present: Vec<String> = before_banks.keys().cloned().collect();
    present.sort();
    if daemon_user == Some(role_name) {
        return Ok(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "{role_name} is the daemon's database user ({DAEMON_DSN_ENV}): this would close \
                 the bank to the daemon. The test login must be a different role. Nothing was \
                 changed."
            )),
        ));
    }
    let connected: Vec<&Value> = match before.get("connected") {
        Some(Value::Array(entries)) => entries
            .iter()
            .filter(|entry| {
                entry
                    .get("db")
                    .and_then(Value::as_str)
                    .is_some_and(|db| present.iter().any(|bank| bank == db))
                    && entry.get("user").and_then(Value::as_str) != Some(role_name)
            })
            .collect(),
        _ => Vec::new(),
    };
    for entry in &connected {
        if !truthy(entry.get("keeps")) {
            let (user, db) = (py_str(entry.get("user")), py_str(entry.get("db")));
            return Ok(report.finish(
                EXIT_REFUSED,
                Some(&format!(
                    "role {user} is connected to {db} now and can connect to it only through \
                     PUBLIC's CONNECT, which this revokes: it would be locked out at its next \
                     connection (a daemon whose database URL is not in this shell, perhaps). \
                     Grant it first, as a superuser: GRANT CONNECT ON DATABASE {} TO {}; then \
                     run this again. Nothing was changed.",
                    sql::ident(&db),
                    sql::ident(&user)
                )),
            ));
        }
    }
    let mut daemon_line = None;
    match daemon_user {
        None => {
            let why = if context.daemon_set {
                "names no user"
            } else {
                "is not set in this shell"
            };
            daemon_line = Some(format!(
                "  daemon user not checked: {DAEMON_DSN_ENV} {why}; only the roles connected \
                 to the bank now were"
            ));
        }
        Some(daemon_user) => {
            let state = before.get("daemon");
            if !truthy(state.and_then(|state| state.get("exists"))) {
                daemon_line = Some(format!(
                    "  {DAEMON_DSN_ENV}'s user {daemon_user} is not a role on this server: not \
                     checked"
                ));
            } else {
                let dbname = daemon.get("dbname");
                let checked: Vec<&String> = present
                    .iter()
                    .filter(|bank| dbname.is_none_or(|dbname| *bank == dbname))
                    .collect();
                let keeps = state.and_then(|state| state.get("keeps"));
                let locked = checked
                    .iter()
                    .find(|bank| !truthy(keeps.and_then(|keeps| keeps.get(bank.as_str()))));
                if let Some(locked) = locked {
                    return Ok(report.finish(
                        EXIT_REFUSED,
                        Some(&format!(
                            "the daemon's database user {daemon_user} ({DAEMON_DSN_ENV}) can \
                             connect to {locked} only through PUBLIC's CONNECT, which this \
                             revokes: the daemon would be locked out of the bank. Grant it \
                             first, as a superuser: GRANT CONNECT ON DATABASE {} TO {}; then \
                             run this again. Nothing was changed.",
                            sql::ident(locked),
                            sql::ident(daemon_user)
                        )),
                    ));
                }
                if !checked.is_empty() {
                    let names: Vec<&str> = checked.iter().map(|bank| bank.as_str()).collect();
                    daemon_line = Some(format!(
                        "  daemon user {daemon_user} ({DAEMON_DSN_ENV}): keeps CONNECT on {} as \
                         owner, superuser or by grant",
                        names.join(", ")
                    ));
                }
            }
        }
    }
    let old = file::read_file(path);
    let reusable = old.get(USER_KEY).map_or(DEFAULT_ROLE, String::as_str) == role_name
        && old.get(PASSWORD_KEY).is_some_and(|value| !value.is_empty());
    if role.is_some() && !reusable && !args.rotate {
        return Ok(report.finish(
            EXIT_REFUSED,
            Some(&format!(
                "role {role_name} already exists and {} holds no password for it, so this \
                 would draw a new one, and every other copy of the login file (another \
                 account's, another machine's) would stop working. Copy the current file here, \
                 or pass --rotate to draw a new password and copy the file again everywhere. \
                 Nothing was changed.",
                context.shown_path
            )),
        ));
    }
    let reuse = reusable && !args.rotate;
    let leftovers: Vec<String> = strings(before.get("leftovers"))
        .into_iter()
        .filter(|name| {
            LEFTOVER.is_match(name) && !banks.contains(name) && !NOT_BANKS.contains(&name.as_str())
        })
        .collect();
    let password = if reuse {
        old[PASSWORD_KEY].clone()
    } else {
        scram::new_password().ok_or_else(|| Failure::Write("no random source".into()))?
    };
    report.say(format!(
        "test-login: on {}, as {} (superuser)",
        executor.description(),
        py_str(who.get("who"))
    ));

    let staged = file::write_private(
        path,
        &format!("{FILE_HEADER}{USER_KEY}={role_name}\n{PASSWORD_KEY}={password}\n"),
    )
    .map_err(Failure::Write)?;
    let salt = scram::random(16).ok_or_else(|| Failure::Write("no random source".into()));
    let changed = match salt {
        Ok(salt) => {
            executor
                .query(
                    Some("postgres"),
                    &sql::role_statements(
                        role_name,
                        &scram::verifier(&password, &salt, 4096),
                        &present,
                        &leftovers,
                    ),
                )
                .await
        }
        Err(_) => Err("no random source".into()),
    };
    if let Err(error) = changed {
        let _ = std::fs::remove_file(&staged);
        return Err(Failure::Database(error));
    }
    if let Err(error) = std::fs::rename(&staged, path) {
        let staged_shown = file::shown(&staged, context.home).unwrap_or_default();
        return Ok(report.finish(
            EXIT_FAILED,
            Some(&format!(
                "role {role_name} now has the password in {staged_shown}, but {} could not be \
                 replaced ({error}). Move that file to {}; nothing else holds this password.",
                context.shown_path, context.shown_path
            )),
        ));
    }
    let how = if reuse {
        "password re-applied from the file"
    } else if role.is_some() {
        "password rotated"
    } else {
        "password set"
    };
    report.say(format!(
        "  role {role_name}: {}: LOGIN CREATEDB, not superuser, no CREATEROLE, REPLICATION or \
         BYPASSRLS; {how}",
        if role.is_some() { "reset" } else { "created" }
    ));
    for granted in strings(before.get("member_of")) {
        report.say(format!(
            "  role {role_name}: membership in {granted} revoked"
        ));
    }
    for name in &leftovers {
        report.say(format!(
            "  database {name}: a leftover test database, handed to {role_name} so the suite's \
             prune can drop it"
        ));
    }
    for bank in &present {
        let info = &before_banks[bank];
        let state = if truthy(info.get("public_connect")) {
            "CONNECT revoked from PUBLIC"
        } else {
            "already closed to PUBLIC"
        };
        report.say(format!(
            "  database {bank}: {state}; its owner {} keeps CONNECT",
            py_str(info.get("owner"))
        ));
    }
    for bank in &banks {
        if !present.contains(bank) {
            report.say(format!("  database {bank}: not on this server"));
        }
    }
    if let Some(line) = daemon_line {
        report.say(line);
    }
    for entry in &connected {
        report.say(format!(
            "  role {}, connected to {} now, keeps CONNECT as owner, superuser or by grant",
            py_str(entry.get("user")),
            py_str(entry.get("db"))
        ));
    }
    report.say(format!(
        "  template1: {}; CREATE DATABASE still copies it",
        if truthy(before.get("template1_public_connect")) {
            "CONNECT revoked from PUBLIC"
        } else {
            "already closed to PUBLIC"
        }
    ));

    let answer = executor
        .query(Some("template1"), &sql::extension_statements())
        .await
        .map_err(Failure::Database)?;
    let (old_ext, new_ext) = answer.split_once('|').unwrap_or((answer.as_str(), ""));
    if old_ext.is_empty() {
        report.say(format!(
            "  template1: installed {new_ext}, so every database the test login creates has it"
        ));
    } else if old_ext != new_ext {
        report.say(format!("  template1: updated {old_ext} to {new_ext}"));
    } else {
        report.say(format!("  template1: already has {new_ext}"));
    }

    let after = query_json(
        executor,
        Some("postgres"),
        sql::state_statement(role_name, &banks, None),
    )
    .await?;
    let problems = verify(&after, role_name, &present);
    report.say(format!(
        "  wrote {} (owner-only): {USER_KEY}, {PASSWORD_KEY}",
        context.shown_path
    ));
    let others = match after.get("others") {
        Some(value) if truthy(Some(value)) => value.clone(),
        _ => Value::Array(Vec::new()),
    };
    let other_names = strings(Some(&others));
    if !other_names.is_empty() {
        report.say(format!(
            "  other databases PUBLIC may connect to (the test login holds no table privileges \
             there; close them with --bank): {}",
            other_names.join(", ")
        ));
    }
    report.data = vec![
        ("role", Value::String(role_name.to_owned())),
        ("file", Value::String(context.path_text.clone())),
        (
            "banks",
            Value::Array(present.iter().cloned().map(Value::String).collect()),
        ),
        ("template1", Value::String(new_ext.to_owned())),
        ("others", others),
        ("password_reused", Value::Bool(reuse)),
    ];
    if !problems.is_empty() {
        return Ok(report.finish(
            EXIT_FAILED,
            Some(&format!("after the change: {}", problems.join("; "))),
        ));
    }
    let closed = if present.is_empty() {
        "no production database (none on this server)".to_owned()
    } else {
        present.join(", ")
    };
    report.say(format!(
        "done: the test suite on this account logs in as {role_name}, which cannot connect to \
         {closed}. Copy the file to another account's ~/.pseudolife-mcp/ to give its sessions \
         the same login."
    ));
    Ok(report.finish(EXIT_OK, None))
}

/// `_verify`: what the state after the change still gets wrong.
fn verify(state: &Value, role: &str, banks: &[String]) -> Vec<String> {
    let mut problems = Vec::new();
    let attributes = state.get("role").filter(|value| truthy(Some(*value)));
    let expected = [
        ("super", false),
        ("login", true),
        ("createdb", true),
        ("createrole", false),
        ("replication", false),
        ("bypassrls", false),
    ];
    match attributes {
        None => problems.push(format!("role {role} does not exist")),
        Some(attributes) => {
            let wrong: Vec<&str> = expected
                .iter()
                .filter(|(key, value)| attributes.get(*key) != Some(&Value::Bool(*value)))
                .map(|(key, _)| *key)
                .collect();
            if !wrong.is_empty() {
                problems.push(format!("role {role} has the wrong {}", wrong.join(", ")));
            }
        }
    }
    if truthy(state.get("member_of")) {
        problems.push(format!(
            "role {role} is still a member of {}",
            strings(state.get("member_of")).join(", ")
        ));
    }
    if truthy(state.get("template1_public_connect")) {
        problems.push("PUBLIC can still connect to template1".into());
    }
    let empty = Value::Object(serde_json::Map::new());
    for bank in banks {
        let info = state
            .get("banks")
            .and_then(|banks| banks.get(bank.as_str()))
            .filter(|info| truthy(Some(*info)))
            .unwrap_or(&empty);
        if truthy(info.get("public_connect")) {
            problems.push(format!("PUBLIC can still connect to {bank}"));
        }
        if truthy(info.get("role_connect")) {
            problems.push(format!("{role} can still connect to {bank}"));
        }
        if !truthy(info.get("owner_connect")) {
            problems.push(format!("the owner of {bank} lost CONNECT"));
        }
    }
    problems
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Option<Args> {
        parse(&values.iter().map(OsString::from).collect::<Vec<_>>())
    }

    #[test]
    fn only_canonical_argv_is_admitted() {
        assert!(args(&["create"]).is_some());
        assert!(args(&["create", "--rotate", "--json", "--bank", "a", "--bank", "b"]).is_some());
        assert!(args(&["create", "--role", ""]).is_some());
        for refused in [
            &[][..],
            &["--json", "create"],
            &["create", "--rotate", "--rotate"],
            &["create", "--role=x"],
            &["create", "--rol", "x"],
            &["create", "--role", "a", "--role", "b"],
            &["create", "--role"],
            &["create", "--bank", "-x"],
            &["create", "--admin-url", ""],
            &["create", "--file", "a/../b"],
            &["create", "extra"],
            &["create", "--help"],
        ] {
            assert!(args(refused).is_none(), "{refused:?}");
        }
    }

    #[test]
    fn role_names_follow_the_oracle_pattern() {
        assert!(role_name_ok("pseudolife_test"));
        assert!(role_name_ok("_x9"));
        assert!(role_name_ok(&"a".repeat(63)));
        assert!(!role_name_ok(&"a".repeat(64)));
        for bad in ["", "9x", "Abc", "a-b", "é"] {
            assert!(!role_name_ok(bad), "{bad}");
        }
    }

    #[test]
    fn redaction_masks_password_shapes() {
        assert_eq!(
            redacted("postgresql://u:secret@h/db failed", None),
            "postgresql://u:***@h/db failed"
        );
        assert_eq!(redacted("x PASSWORD = 'a b' y", None), "x PASSWORD = *** y");
        assert_eq!(
            redacted(r#"invalid percent-encoded token: "p%zz""#, None),
            r#"invalid percent-encoded token: "***""#
        );
        assert_eq!(
            redacted(
                "hunter%32 and hunter2",
                Some("postgresql://u:hunter%32@h/db")
            ),
            "*** and ***"
        );
    }

    #[test]
    fn json_is_dumped_like_python() {
        let mut out = String::new();
        py_json(
            &serde_json::json!({"a": [1, "é😀\n"], "b": null, "c": true}),
            &mut out,
        );
        let expected = concat!(
            r#"{"a": [1, ""#,
            r"\u00e9\ud83d\ude00\n",
            r#""], "b": null, "c": true}"#
        );
        assert_eq!(out, expected);
    }

    #[test]
    fn daemon_dsns_defer_where_the_two_readings_differ() {
        let parts = dsn_parts(Some(
            "postgresql://pseudolife:pw@postgres:5432/pseudolife_memory",
        ))
        .expect("admitted");
        assert_eq!(parts.get("user").map(String::as_str), Some("pseudolife"));
        assert_eq!(
            parts.get("dbname").map(String::as_str),
            Some("pseudolife_memory")
        );
        let parts = dsn_parts(Some("postgresql://127.0.0.1:5/x")).expect("admitted");
        assert!(!parts.contains_key("user"));
        assert!(dsn_parts(Some("postgresql://u@h/db?dbname=other")).is_none());
        assert!(dsn_parts(Some("postgresql:///db")).is_none());
        assert!(dsn_parts(Some("host=h application_name=x")).is_none());
        assert_eq!(dsn_parts(Some("")).map(|parts| parts.len()), Some(0));
    }
}
