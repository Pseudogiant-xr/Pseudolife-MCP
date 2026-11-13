//! `pseudolife-mcp installer credentials ...`: the installers' board-token
//! plumbing (formerly `ops/client_credentials.py`), with the same
//! subcommands, arguments and JSON reports:
//!
//!   mint --env-file F                 give ops/.env a random PSEUDOLIFE_MCP_TOKEN when it has none
//!   token-file --principal P --path F write one client's owner-only token file
//!   registration-env --config C --set K=V...  add missing keys to a Claude Code registration
//!   claude-settings-env --settings S --token-file T --daemon-url U [--replace]
//!   check-token-file --path F          the shim's own check of a token file
//!   write-token-file --path F          create an owner-only token file from stdin
//!   board --daemon-url U [--token-file T]  one line: whether the board is on
//!
//! Token values arrive through PSEUDOLIFE_INSTALLER_TOKEN /
//! PSEUDOLIFE_INSTALLER_TOKENS or stdin, never the command line, and are
//! never printed. Reports are JSON on stdout (`board` prints its line);
//! exit 1 means failed.
use crate::cli::connect::files::{self, Fail, Read as JsonRead, TokenFail};
use crate::cli::connect::pyjson::{self, Dict, J};
use serde_json::{Value, json};
use std::{
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

const SERVER: &str = "pseudolife-memory";
const TOKEN_KEY: &str = "PSEUDOLIFE_MCP_TOKEN";
const TOKENS_KEY: &str = "PSEUDOLIFE_MCP_TOKENS";
const TOKEN_FILE_KEY: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
const DAEMON_URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";
const SOURCE_ENV: &str = "PSEUDOLIFE_INSTALLER_TOKEN";
const MAP_SOURCE_ENV: &str = "PSEUDOLIFE_INSTALLER_TOKENS";
const MINTED_COMMENT: &str = "# Bearer token minted by the installer: the agent board needs one. See 'Turning the board on' in docs/guide/configuration.md.";
pub(super) const USAGE: &str = "usage: pseudolife-mcp installer credentials mint --env-file F | token-file --principal P --path F | registration-env --config C [--set K=V]... | claude-settings-env --settings S --token-file T --daemon-url U [--replace] | check-token-file --path F | write-token-file --path F | board --daemon-url U [--token-file T]";

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

/// Parsed `--name value` options (repeatable `--set`, flag `--replace`).
#[derive(Default, Debug)]
struct Options {
    values: Vec<(String, String)>,
    sets: Vec<String>,
    replace: bool,
}
impl Options {
    fn get(&self, name: &str) -> Option<&str> {
        self.values
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }
    fn need(&self, name: &str) -> Result<&str, String> {
        self.get(name)
            .ok_or_else(|| format!("--{name} is required"))
    }
}

fn parse(args: &[String], allowed: &[&str]) -> Result<Options, String> {
    let mut options = Options::default();
    let mut items = args.iter();
    while let Some(item) = items.next() {
        let Some(name) = item.strip_prefix("--") else {
            return Err(format!("unexpected argument {item:?}"));
        };
        let (name, inline) = match name.split_once('=') {
            Some((n, v)) => (n, Some(v.to_owned())),
            None => (name, None),
        };
        if !allowed.contains(&name) {
            return Err(format!("unknown option --{name}"));
        }
        if name == "replace" {
            options.replace = true;
            continue;
        }
        let value = match inline {
            Some(v) => v,
            None => items
                .next()
                .cloned()
                .ok_or_else(|| format!("--{name} needs a value"))?,
        };
        if name == "set" {
            options.sets.push(value);
        } else if options.get(name).is_some() {
            return Err(format!("--{name} given twice"));
        } else {
            options.values.push((name.to_owned(), value));
        }
    }
    Ok(options)
}

fn emit(report: &Value) -> u8 {
    let failed = report["status"] == "failed";
    let mut stdout = io::stdout().lock();
    // json.dumps(report): the installers read `"key": "value"` spacing.
    if writeln!(stdout, "{}", super::state::py_dumps(report)).is_err() {
        return 1;
    }
    u8::from(failed)
}

fn failed(recovery: impl Into<String>) -> Value {
    json!({"status": "failed", "recovery": recovery.into()})
}

fn fail_value(error: Fail) -> Value {
    match error {
        Fail::Helper(message) => failed(message),
        Fail::Name(name) => failed(format!(
            "{name} while writing; check the target's permissions, then re-run"
        )),
    }
}

/// Arguments follow `installer credentials`.
pub(super) fn run(args: &[String]) -> u8 {
    let Some((command, rest)) = args.split_first() else {
        return usage("expected a credentials step");
    };
    let allowed: &[&str] = match command.as_str() {
        "mint" => &["env-file"],
        "token-file" => &["principal", "path"],
        "registration-env" => &["config", "set"],
        "claude-settings-env" => &["settings", "token-file", "daemon-url", "replace"],
        "check-token-file" | "write-token-file" => &["path"],
        "board" => &["daemon-url", "token-file"],
        "--help" | "-h" => {
            let _ = writeln!(io::stdout().lock(), "{USAGE}");
            return 0;
        }
        _ => return usage(&format!("unknown credentials step {command:?}")),
    };
    let options = match parse(rest, allowed) {
        Ok(options) => options,
        Err(message) => return usage(&message),
    };
    let report = (|| -> Result<Value, String> {
        Ok(match command.as_str() {
            "mint" => mint(Path::new(options.need("env-file")?)),
            "token-file" => {
                token_file(options.need("principal")?, Path::new(options.need("path")?))
            }
            "registration-env" => {
                registration_env(Path::new(options.need("config")?), &options.sets)?
            }
            "claude-settings-env" => claude_settings_env(
                Path::new(options.need("settings")?),
                options.need("token-file")?,
                options.need("daemon-url")?,
                options.replace,
            ),
            "check-token-file" => check_token_file(Path::new(options.need("path")?)),
            "write-token-file" => write_token_file(Path::new(options.need("path")?)),
            _ => {
                let line = board(options.need("daemon-url")?, options.get("token-file"));
                let _ = writeln!(io::stdout().lock(), "{line}");
                return Ok(Value::Null);
            }
        })
    })();
    match report {
        Ok(Value::Null) => 0,
        Ok(report) => emit(&report),
        Err(message) => usage(&message),
    }
}

fn usage(message: &str) -> u8 {
    let _ = writeln!(
        io::stderr().lock(),
        "installer credentials: {message}\n{USAGE}"
    );
    2
}

// -- mint ---------------------------------------------------------------------

/// The last `KEY=value` line's value, stripped (`_active_value`).
fn active_value(text: &str, key: &str) -> String {
    let prefix = format!("{key}=");
    text.split('\n')
        .filter_map(|line| line.strip_prefix(prefix.as_str()))
        .map(|value| value.strip_suffix('\r').unwrap_or(value).trim().to_owned())
        .next_back()
        .unwrap_or_default()
}

fn new_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "no secure random source is available".to_owned())?;
    Ok(base64url(&bytes))
}

fn base64url(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for i in 0..=chunk.len() {
            out.push(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

fn mint(env_file: &Path) -> Value {
    // Compose takes a shell value over ops/.env, so a token in the
    // installer's environment already is the daemon's token.
    if env(TOKEN_KEY).is_some() || env(TOKENS_KEY).is_some() {
        return json!({"status": "present"});
    }
    let text = match std::fs::read(env_file) {
        Ok(bytes) => match String::from_utf8(bytes) {
            Ok(text) => text,
            Err(_) => return failed("ops/.env is not UTF-8 text; save it as UTF-8, then re-run"),
        },
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => return fail_value(Fail::Name(files::os_error_name(&error))),
    };
    if !active_value(&text, TOKEN_KEY).is_empty() || !active_value(&text, TOKENS_KEY).is_empty() {
        return json!({"status": "present"});
    }
    let token = match new_token() {
        Ok(token) => token,
        Err(message) => return failed(message),
    };
    // An empty `PSEUDOLIFE_MCP_TOKEN=` line takes the token in place.
    let mut replaced = false;
    let mut lines: Vec<String> = Vec::new();
    for line in text.split('\n') {
        let body = line.strip_suffix('\r');
        let cr = if body.is_some() { "\r" } else { "" };
        let body = body.unwrap_or(line);
        if !replaced
            && body
                .strip_prefix(&format!("{TOKEN_KEY}="))
                .is_some_and(|rest| rest.chars().all(|c| c == ' ' || c == '\t'))
        {
            lines.push(format!("{TOKEN_KEY}={token}{cr}"));
            replaced = true;
        } else {
            lines.push(line.to_owned());
        }
    }
    let mut text = if replaced { lines.join("\n") } else { text };
    if !replaced {
        let eol = if text.contains("\r\n") { "\r\n" } else { "\n" };
        if !text.is_empty() && !text.ends_with('\n') {
            text.push_str(eol);
        }
        text.push_str(&format!("{MINTED_COMMENT}{eol}{TOKEN_KEY}={token}{eol}"));
    }
    match files::write_private(env_file, text.as_bytes()) {
        Ok(()) => json!({"status": "minted"}),
        Err(error) => fail_value(error),
    }
}

// -- token-file ---------------------------------------------------------------

/// `PSEUDOLIFE_MCP_TOKENS` (`token:principal,...`) as Python's
/// `parse_token_map` reads it: ordered `(token, principal)` pairs, the
/// principal lowercased, malformed entries and the reserved `default` and
/// `maintainer` principals skipped, the first of a duplicated token kept.
pub(in crate::cli) fn parse_token_map(raw: &str) -> Vec<(String, String)> {
    let mut pairs: Vec<(String, String)> = Vec::new();
    for part in raw.split(',') {
        // rpartition: principals cannot contain ":", tokens may.
        let Some((token, name)) = part.trim().rsplit_once(':') else {
            continue;
        };
        let (token, name) = (token.trim(), name.trim().to_lowercase());
        if token.is_empty() || name.is_empty() || name == "default" || name == "maintainer" {
            continue;
        }
        if !pairs.iter().any(|(t, _)| t == token) {
            pairs.push((token.to_owned(), name));
        }
    }
    pairs
}

/// This client's own entry in the token map, else the singular token.
fn principal_token(principal: &str) -> Result<Option<String>, String> {
    if let Some(raw) = env(MAP_SOURCE_ENV).filter(|r| !r.trim().is_empty()) {
        let wanted = principal.to_lowercase();
        let mut matches: Vec<String> = parse_token_map(&raw)
            .into_iter()
            .filter(|(_, name)| *name == wanted)
            .map(|(token, _)| token)
            .collect();
        if matches.len() > 1 {
            return Err(format!(
                "the daemon token map holds {} tokens for principal '{principal}'; keep exactly one",
                matches.len()
            ));
        }
        if let Some(token) = matches.pop() {
            return Ok(Some(token));
        }
    }
    Ok(env(SOURCE_ENV))
}

fn read_token(path: &Path) -> Result<Option<String>, &'static str> {
    crate::credentials::CredentialProvider::new(None, Some(path.to_path_buf()))
        .and_then(|provider| provider.snapshot())
        .map(|snapshot| snapshot.token().map(str::to_owned))
        .map_err(|error| error.0)
}

fn token_file(principal: &str, path: &Path) -> Value {
    let source = match principal_token(principal) {
        Ok(source) => source,
        Err(message) => return failed(message),
    };
    let exists = std::fs::symlink_metadata(path).is_ok();
    let Some(source) = source else {
        if !exists {
            return json!({"status": "tokenless"});
        }
        return match read_token(path) {
            Ok(_) => json!({"status": "ready", "path": path}),
            Err(reason) => failed(format!(
                "the existing token file {} cannot be used ({reason}); fix it, or re-run with the token in the environment so it is rewritten",
                path.display()
            )),
        };
    };
    if exists && read_token(path).ok().flatten().as_deref() == Some(source.as_str()) {
        return json!({"status": "ready", "path": path});
    }
    // As credentials._write_token_file: the token is validated before
    // anything is written, and the private edit refuses a link or a
    // redirected ancestor instead of writing through it.
    let written = crate::credentials::CredentialProvider::new(Some(source.clone()), None)
        .map_err(|error| error.to_string())
        .and_then(|_| {
            crate::setup::files::PrivateEdit::prepare(path, source.as_bytes().to_vec())
                .and_then(|mut edit| edit.commit())
                .map_err(|error| error.to_string())
        });
    if let Err(cause) = written {
        return failed(format!(
            "could not write the token file {}: {cause}",
            path.display()
        ));
    }
    match read_token(path) {
        Ok(Some(token)) if token == source => json!({"status": "ready", "path": path}),
        _ => failed(format!(
            "could not write the token file {}: credential file validation failed",
            path.display()
        )),
    }
}

// -- registration-env / claude-settings-env -----------------------------------

fn load(path: &Path) -> Result<Dict, Value> {
    match files::load_json(path) {
        Ok(JsonRead::Ok(data)) => Ok(data),
        Ok(JsonRead::Missing) => Ok(Vec::new()),
        Ok(JsonRead::Error(message)) => Err(failed(message)),
        Err(_) => Err(failed(format!(
            "{} cannot be read; fix it, then re-run",
            path.display()
        ))),
    }
}

fn pairs(values: &[String]) -> Result<Vec<(String, String)>, String> {
    values
        .iter()
        .map(|item| match item.split_once('=') {
            Some((key, value)) if !key.is_empty() => Ok((key.to_owned(), value.to_owned())),
            _ => Err("--set takes KEY=VALUE".to_owned()),
        })
        .collect()
}

fn registration_env(config: &Path, sets: &[String]) -> Result<Value, String> {
    let wanted = pairs(sets)?;
    let mut data = match load(config) {
        Ok(data) => data,
        Err(report) => return Ok(report),
    };
    let Some(J::Dict(servers)) = pyjson::get_mut(&mut data, "mcpServers") else {
        return Ok(json!({"status": "absent", "backup": null}));
    };
    let Some(J::Dict(entry)) = pyjson::get_mut(servers, SERVER) else {
        return Ok(json!({"status": "absent", "backup": null}));
    };
    // entry.get("type", "stdio") == "stdio": a key that is there but null
    // is not stdio.
    let stdio = !pyjson::has(entry, "type")
        || pyjson::get(entry, "type").is_some_and(|t| t.as_str() == Some("stdio"));
    if !stdio || !pyjson::get(entry, "command").is_some_and(J::truthy) {
        return Ok(json!({"status": "unsupported", "backup": null}));
    }
    let mut env_block: Dict = match pyjson::get(entry, "env") {
        None => Vec::new(),
        Some(J::Dict(env)) => env.clone(),
        Some(other) if !other.truthy() => Vec::new(),
        Some(_) => return Ok(json!({"status": "unsupported", "backup": null})),
    };
    let has_credential = pyjson::get(&env_block, TOKEN_KEY).is_some_and(J::truthy)
        || pyjson::get(&env_block, TOKEN_FILE_KEY).is_some_and(J::truthy);
    let additions: Vec<(String, String)> = wanted
        .into_iter()
        .filter(|(key, _)| {
            !(pyjson::has(&env_block, key) || (key == TOKEN_FILE_KEY && has_credential))
        })
        .collect();
    if additions.is_empty() {
        return Ok(json!({"status": "unchanged", "backup": null}));
    }
    for (key, value) in additions {
        env_block.push((key, J::Str(value)));
    }
    pyjson::set(entry, "env", J::Dict(env_block));
    let backup = match files::backup(config) {
        Ok(backup) => backup,
        Err(error) => return Ok(fail_value(error)),
    };
    if let Err(error) = files::write_json(config, &data) {
        return Ok(fail_value(error));
    }
    Ok(json!({"status": "updated", "backup": backup}))
}

fn claude_settings_env(
    settings: &Path,
    token_path: &str,
    daemon_url: &str,
    replace: bool,
) -> Value {
    if !replace && (env(TOKEN_KEY).is_some() || env(TOKEN_FILE_KEY).is_some()) {
        return json!({"status": "kept", "backup": null});
    }
    let mut data = match load(settings) {
        Ok(data) => data,
        Err(report) => return report,
    };
    let mut env_block: Dict = match pyjson::get(&data, "env") {
        None => Vec::new(),
        Some(J::Dict(env)) => env.clone(),
        Some(other) if !other.truthy() => Vec::new(),
        Some(_) => return failed("settings.json 'env' is not an object; fix it, then re-run"),
    };
    let existed = std::fs::symlink_metadata(settings).is_ok();
    let backup = |path: &Path| -> Result<Option<String>, Value> {
        if existed {
            files::backup(path).map(Some).map_err(fail_value)
        } else {
            Ok(None)
        }
    };
    if replace {
        let wanted = [(TOKEN_FILE_KEY, token_path), (DAEMON_URL_KEY, daemon_url)];
        let mut changed: Vec<&str> = wanted
            .iter()
            .filter(|(key, value)| pyjson::get(&env_block, key).and_then(J::as_str) != Some(value))
            .map(|(key, _)| *key)
            .collect();
        changed.sort_unstable();
        if changed.is_empty() {
            return json!({"status": "unchanged", "backup": null, "changed": []});
        }
        let saved = match backup(settings) {
            Ok(saved) => saved,
            Err(report) => return report,
        };
        for (key, value) in wanted {
            pyjson::set(&mut env_block, key, J::Str(value.to_owned()));
        }
        pyjson::set(&mut data, "env", J::Dict(env_block));
        if let Err(error) = files::write_json(settings, &data) {
            return fail_value(error);
        }
        return json!({"status": "updated", "backup": saved, "changed": changed});
    }
    if pyjson::get(&env_block, TOKEN_KEY).is_some_and(J::truthy)
        || pyjson::get(&env_block, TOKEN_FILE_KEY).is_some_and(J::truthy)
    {
        return json!({"status": "kept", "backup": null});
    }
    let saved = match backup(settings) {
        Ok(saved) => saved,
        Err(report) => return report,
    };
    let url_set = pyjson::get(&env_block, DAEMON_URL_KEY).is_some_and(J::truthy);
    pyjson::set(
        &mut env_block,
        TOKEN_FILE_KEY,
        J::Str(token_path.to_owned()),
    );
    if !url_set {
        pyjson::set(
            &mut env_block,
            DAEMON_URL_KEY,
            J::Str(daemon_url.to_owned()),
        );
    }
    pyjson::set(&mut data, "env", J::Dict(env_block));
    if let Err(error) = files::write_json(settings, &data) {
        return fail_value(error);
    }
    json!({"status": "updated", "backup": saved})
}

// -- check-token-file / write-token-file --------------------------------------

fn check_token_file(path: &Path) -> Value {
    match read_token(path) {
        Ok(Some(token)) if token.starts_with('\u{feff}') => failed(
            "the file starts with a UTF-8 byte-order mark, which the shim would send as part of the token; write it without one",
        ),
        Ok(Some(_)) => json!({"status": "ready"}),
        Ok(None) => failed("the token file holds no token"),
        Err(reason) => failed(reason),
    }
}

fn write_token_file(path: &Path) -> Value {
    let target: PathBuf =
        crate::credentials::absolute_expanded(path).unwrap_or_else(|_| path.to_path_buf());
    let mut data = Vec::new();
    let _ = io::stdin().lock().take(64 * 1024).read_to_end(&mut data);
    match files::write_token_file(&target, &data) {
        Ok(()) => json!({"status": "written", "path": target}),
        Err(TokenFail::Helper(message)) => failed(message),
        Err(TokenFail::Name(name)) => failed(format!(
            "{name} while writing; check the target's permissions, then re-run"
        )),
    }
}

// -- board --------------------------------------------------------------------

fn board(daemon_url: &str, token_path: Option<&str>) -> String {
    let token = match token_path {
        Some(path) => match read_token(Path::new(path)) {
            Ok(token) => token,
            Err(_) => {
                return "off - the client token file is missing, unsafe or malformed".to_owned();
            }
        },
        None => env(SOURCE_ENV),
    };
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        return "off - daemon unreachable".to_owned();
    };
    runtime.block_on(crate::cli::connect::net::board_line(
        daemon_url,
        token.as_deref(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_token_map_reads_as_python_parse_token_map() {
        let pairs = super::parse_token_map(
            " a:b:Alice , bad, :x, t:, d:default, m:Maintainer, a:b:bob, c:Carol",
        );
        assert_eq!(
            pairs,
            vec![
                ("a:b".to_owned(), "alice".to_owned()),
                ("c".to_owned(), "carol".to_owned())
            ]
        );
    }

    #[test]
    fn base64url_matches_python_token_urlsafe_alphabet_and_length() {
        assert_eq!(base64url(&[0xfb, 0xff, 0xbf]), "-_-_");
        assert_eq!(base64url(&[0u8; 32]).len(), 43);
        assert_eq!(base64url(b"f"), "Zg");
    }

    #[test]
    fn active_value_takes_the_last_assignment_and_strips_cr() {
        let text = "PSEUDOLIFE_MCP_TOKEN=one\r\nOTHER=x\r\nPSEUDOLIFE_MCP_TOKEN= two \r\n";
        assert_eq!(active_value(text, TOKEN_KEY), "two");
        assert_eq!(active_value("# PSEUDOLIFE_MCP_TOKEN=x\n", TOKEN_KEY), "");
    }

    #[test]
    fn options_refuse_repeats_and_unknown_names() {
        let args = |items: &[&str]| items.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        assert!(parse(&args(&["--path", "a", "--path", "b"]), &["path"]).is_err());
        assert!(parse(&args(&["--bogus", "a"]), &["path"]).is_err());
        let o = parse(
            &args(&["--set", "A=1", "--set=B=2", "--config", "c"]),
            &["config", "set"],
        )
        .unwrap();
        assert_eq!(o.sets, vec!["A=1", "B=2"]);
        assert_eq!(o.get("config"), Some("c"));
    }
}
