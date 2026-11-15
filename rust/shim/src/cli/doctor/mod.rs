//! `pseudolife-mcp doctor`: read-only runtime diagnostics and an explicit
//! disposable proof through the native daemon. A check whose input the
//! native reader does not parse (a client file, a daemon answer, a saved
//! tunnel, a proxy or an unusable credential) is named in the report while
//! every unrelated check still runs; only argument shapes and an
//! unresolvable --agent-state path still defer the whole command.
mod agent;
mod clients;
mod handshake;
mod probes;
mod proof;
mod pyenv;
mod pyjson;

use pyenv::{Defer, Env, Res};
use serde_json::{Map, Value, json};
use std::{
    ffi::OsString,
    io::{self, Write},
    process::ExitCode,
    time::Duration,
};

const HOSTS: [&str; 4] = ["codex", "claude-code", "claude-desktop", "generic"];
const REFUSAL: &str = "{\"ok\": false, \"error\": \"ExplicitDisposableDatabaseRequired\", \"recovery\": \"Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used.\"}\n";
const BEARER_MISSING: &str = "The daemon requires bearer authentication (/health reports auth=true) and doctor found no credential: neither PSEUDOLIFE_MCP_TOKEN_FILE nor PSEUDOLIFE_MCP_TOKEN is set in this shell, and no Claude Code (~/.claude.json) or Codex (config.toml) registration of pseudolife-memory carries one in its env block. Set PSEUDOLIFE_MCP_TOKEN_FILE=<path to a private file holding the token> (or PSEUDOLIFE_MCP_TOKEN=<the token>) and retry; to fix a client, re-run ops/install.* --client <client> with PSEUDOLIFE_MCP_TOKEN set.";

struct Args {
    proof: bool,
    timeout: f64,
    host: &'static str,
    agent_state: Option<String>,
}

/// The longest `--timeout` answered, in seconds. Python takes any positive
/// float; a budget past this (about 11.6 days) defers, so no deadline or
/// Duration can overflow.
const MAX_TIMEOUT: f64 = 1_000_000.0;

fn positive_decimal(raw: &str) -> Option<f64> {
    (raw.bytes().any(|byte| byte.is_ascii_digit())
        && raw
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'.')
        && raw.bytes().filter(|byte| *byte == b'.').count() <= 1)
        .then(|| raw.parse::<f64>().ok())
        .flatten()
        .filter(|value| value.is_finite() && *value > 0.0)
}

fn parse(values: &[OsString]) -> Option<Args> {
    let mut seen = [false; 4];
    let mut args = Args {
        proof: false,
        timeout: 20.0,
        host: "generic",
        agent_state: None,
    };
    let mut values = values.iter();
    while let Some(raw) = values.next() {
        let selected = match raw.to_str()? {
            "--disposable-proof" => 0,
            "--timeout" => 1,
            "--host" => 2,
            "--agent-state" => 3,
            _ => return None,
        };
        if seen[selected] {
            return None;
        }
        seen[selected] = true;
        if selected != 0 {
            let value = values.next()?.to_str()?;
            match selected {
                1 => args.timeout = positive_decimal(value).filter(|t| *t <= MAX_TIMEOUT)?,
                2 => args.host = HOSTS.iter().find(|host| **host == value)?,
                _ if value.is_empty() || value.starts_with('-') => return None,
                _ => args.agent_state = Some(value.to_owned()),
            }
        }
    }
    args.proof = seen[0];
    Some(args)
}

fn emit(text: &str, code: u8) -> ExitCode {
    if io::stdout()
        .lock()
        .write_all(&super::text_bytes(text))
        .is_ok()
    {
        ExitCode::from(code)
    } else {
        ExitCode::FAILURE
    }
}

pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    let arguments = values
        .iter()
        .map(|value| value.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    let environment = match super::host_connection::Environment::discover("doctor", &arguments) {
        Ok(environment) => environment,
        Err(error) => {
            crate::stderrln!("pseudolife-mcp: {error}");
            return Some(ExitCode::from(2));
        }
    };
    let args = parse(&values)?;
    if args.proof {
        if args.agent_state.is_some() {
            let message = "usage: cli.py [-h] [--timeout TIMEOUT]\n              [--host {codex,claude-code,claude-desktop,generic}]\n              [--agent-state AGENT_STATE] [--disposable-proof]\ncli.py: error: --disposable-proof cannot use a saved --agent-state\n";
            let _ = io::stderr().lock().write_all(&super::text_bytes(message));
            return Some(ExitCode::from(2));
        }
        if matches!(
            std::env::var("PSEUDOLIFE_TEST_DATABASE_URL"),
            Err(std::env::VarError::NotUnicode(_))
        ) {
            return None;
        }
        let Some(dsn) = std::env::var("PSEUDOLIFE_TEST_DATABASE_URL")
            .ok()
            .filter(|v| !v.is_empty())
        else {
            return Some(emit(REFUSAL, 2));
        };
        let Some(daemon) = proof::daemon() else {
            return Some(emit(&(pyjson::dumps(&proof::missing_daemon()) + "\n"), 2));
        };
        let Some(models) = proof::models() else {
            return Some(emit(&(pyjson::dumps(&proof::missing_models()) + "\n"), 2));
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .ok()?;
        let report = runtime.block_on(proof::run(&dsn, &daemon, &models, args.timeout));
        let code = if report["ok"] == true { 0 } else { 1 };
        let text = if code == 0 {
            pyjson::dumps(&report)
        } else {
            // Python prints proof exceptions without indent. Error reports
            // contain only scalar values, preserving insertion order and ASCII.
            format!(
                "{{{}}}",
                report
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(key, value)| {
                        format!("{}: {}", pyjson::dumps(&json!(key)), pyjson::dumps(value))
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        };
        return Some(emit(&(text + "\n"), code));
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    let (report, ok) = runtime.block_on(diagnose(&args, &environment)).ok()?;
    Some(emit(
        &(pyjson::dumps(&report) + "\n"),
        if ok { 0 } else { 1 },
    ))
}

/// The environment overrides a registration supplies, and its source label.
pub(super) type Registration = (Vec<(String, String)>, Option<String>);

/// `doctor_cli.registration_credentials(os.environ)` for another command
/// (`console`): the overrides to apply and the source label, or `None`
/// where doctor's reproduced domain ends (that command then defers).
pub(super) fn registration_credentials() -> Option<Registration> {
    clients::registration_credentials(&Env::default()).ok()
}

/// Python truthiness of a JSON value.
pub(super) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

/// The proxy variables this environment sets, by name only (a proxy URL
/// may carry credentials). Doctor's own probes connect to the daemon
/// directly; the shim it starts, like every client's shim, uses reqwest's
/// proxy, which is hyper-util's `Matcher::from_system()` (this build has no
/// system-proxy feature, so it is the environment alone: `HTTP_PROXY`,
/// `HTTPS_PROXY`, `ALL_PROXY` by scheme, `NO_PROXY` exceptions).
fn proxy_variables() -> Vec<String> {
    let mut names: Vec<String> = std::env::vars_os()
        .filter(|(name, value)| {
            !value.is_empty()
                && matches!(
                    name.to_string_lossy().to_ascii_lowercase().as_str(),
                    "http_proxy" | "https_proxy" | "all_proxy" | "no_proxy"
                )
        })
        .map(|(name, _)| name.to_string_lossy().into_owned())
        .collect();
    names.sort();
    names.dedup();
    names
}

/// Whether the shim's own proxy rules send a request for `url` through a
/// proxy: the same matcher reqwest builds, asked about this daemon URL.
fn shim_proxies(url: &str) -> bool {
    url.parse::<http::Uri>().is_ok_and(|uri| {
        hyper_util::client::proxy::matcher::Matcher::from_system()
            .intercept(&uri)
            .is_some()
    })
}

/// The report line for set proxy variables (names only).
fn proxy_note(names: &[String], routes: bool) -> String {
    let names = names.join(", ");
    if routes {
        format!(
            "{names} set: doctor's own /health, board and maintainer probes connect to the daemon directly, while the shim this doctor starts, like every client's shim, connects to this daemon through the proxy; NO_PROXY naming the daemon's host would make it connect directly."
        )
    } else {
        format!(
            "{names} set: not used for this daemon (its scheme has no proxy set, or NO_PROXY names its host), so doctor's probes and the shim both connect directly."
        )
    }
}

/// Appended to a handshake recovery while the shim's rules proxy the
/// daemon URL.
const PROXY_RECOVERY: &str = " The shim connects to this daemon through the configured proxy: add the daemon's host to NO_PROXY (or unset the proxy for this client), then retry.";

/// A section whose input the native reader does not parse; the rest of
/// the report is unaffected.
fn unread(input: &str) -> String {
    format!("unknown (not read by the native client: {input})")
}

const REGISTRATIONS_UNREAD: &str = "not read by the native client: the search of the Claude Code (~/.claude.json) and Codex (config.toml) registrations stopped on an input it does not decide, such as Python-only JSON (NaN), invalid TOML, a home directory that cannot be resolved, or a CLAUDE_CONFIG_DIR, CODEX_HOME or PSEUDOLIFE_MCP_DAEMON_URL that is not plain text or starts with `~`. Fix that input, or set PSEUDOLIFE_MCP_TOKEN_FILE in this shell to name the credential directly.";

/// Where the credential came from, which decides what the user can fix.
#[derive(Clone, Copy)]
enum Source {
    /// `PSEUDOLIFE_MCP_TOKEN_FILE` (from the shell or a registration).
    File,
    /// `PSEUDOLIFE_MCP_TOKEN` (from the shell or a registration).
    Inline,
    /// A variable that is not valid text.
    NotText,
}

/// Why the configured credential cannot be used. Its text never includes
/// the token or the token file's contents.
enum BearerProblem {
    /// The source could not be read or is configured wrongly.
    Unavailable(Source, &'static str),
    /// The token is outside printable ASCII.
    Unsendable,
}

impl BearerProblem {
    fn reason(&self) -> &'static str {
        match self {
            // decode_token's refusal names a file even for an inline token.
            Self::Unavailable(Source::Inline, _) => {
                "the PSEUDOLIFE_MCP_TOKEN value is empty, longer than 4096 bytes, or contains whitespace or control characters (a space or tab from a paste counts)"
            }
            Self::Unavailable(Source::NotText, _) => {
                "PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE is not valid text"
            }
            Self::Unavailable(Source::File, reason) => reason,
            Self::Unsendable => "the bearer token contains characters other than printable ASCII",
        }
    }
    fn board_line(&self) -> String {
        format!(
            "not checked - the credential is unusable ({})",
            self.reason()
        )
    }
    /// `source` is the report's `credential_source`: `environment` or a
    /// client registration's label.
    fn error(&self, source: &str) -> (&'static str, String) {
        let reason = self.reason();
        let registration = source != "environment";
        let fix = match (self, registration) {
            (Self::Unavailable(Source::File, _), false) => {
                "Point PSEUDOLIFE_MCP_TOKEN_FILE at an owner-only file holding only the token, or unset PSEUDOLIFE_MCP_TOKEN_FILE and set PSEUDOLIFE_MCP_TOKEN instead (the file wins while it is set), then retry"
            }
            (Self::Unavailable(Source::Inline, _), false) => {
                "Set PSEUDOLIFE_MCP_TOKEN again in this shell without spaces, tabs or other invisible characters, then retry"
            }
            (Self::Unsendable, false) => {
                "Set PSEUDOLIFE_MCP_TOKEN (or the file PSEUDOLIFE_MCP_TOKEN_FILE names) again from where the token was issued (pseudolife-mcp invite on the daemon host, or the daemon's ops/.env), without extra characters, then retry"
            }
            (Self::Unavailable(Source::NotText, _), false) => {
                "Set the variable again as plain text, then retry"
            }
            // Shell variables would only make doctor pass: the client reads
            // its own registration.
            (_, true) => {
                "Fix the credential in that registration's env block (re-run ops/install.* --client <client> with PSEUDOLIFE_MCP_TOKEN set, or point its PSEUDOLIFE_MCP_TOKEN_FILE at an owner-only file holding only the token), then retry; setting shell variables would only make doctor pass while that client stays broken"
            }
        };
        let (error, cause) = match self {
            Self::Unsendable => (
                "BearerUnsendable",
                format!(
                    "The configured bearer token (source: {source}) contains characters other than printable ASCII. The native shim sends such a token as UTF-8 while the daemon reads the Authorization header as Latin-1, so MCP calls can never authenticate with it (DEL and other control characters cannot be sent at all)."
                ),
            ),
            Self::Unavailable(..) => (
                "CredentialUnavailable",
                format!(
                    "doctor could not use the configured credential ({reason}; source: {source})."
                ),
            ),
        };
        (
            error,
            format!("{cause} {fix}; the checks that need no credential ran."),
        )
    }
}

/// `principal_activity.token_hash_prefix`: the first 8 hex of the SHA-256
/// of the token's UTF-8.
fn token_hash_prefix(token: &str) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(token.as_bytes())
        .iter()
        .take(4)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A bearer the native shim authenticates with: printable ASCII.
/// `HeaderValue::from_str` (`lifecycle.rs` builds the shim's Authorization
/// header that way) refuses only DEL and C0 controls, so a non-ASCII token
/// is sent, as UTF-8, and the daemon decodes the header as Latin-1: it can
/// never match. Whitespace is already refused by `decode_token`.
fn sendable(token: &str) -> bool {
    token.bytes().all(|byte| (0x20..0x7f).contains(&byte))
}

/// This runtime's identity, in the fields the Python report fills from its
/// interpreter (declared substitution `doctor-runtime-identity`). Python's
/// `mcp` (its MCP SDK version) has no native counterpart and is omitted:
/// after cutover a placeholder would read as a broken install (maintainer
/// decision 2026-10-09).
/// A path this runtime cannot name (not UTF-8, or unresolvable) is
/// `unknown`; the version is always known.
fn identity() -> Vec<(&'static str, String)> {
    let executable = std::env::current_exe().ok();
    let resolved = executable
        .as_deref()
        .and_then(|path| std::fs::canonicalize(path).ok());
    let interpreter = executable
        .as_deref()
        .and_then(std::path::Path::to_str)
        .map_or_else(|| "unknown".to_owned(), str::to_owned);
    let source = resolved
        .and_then(|resolved| {
            let parent = resolved.parent()?.to_str()?;
            Some(parent.strip_prefix("\\\\?\\").unwrap_or(parent).to_owned())
        })
        .unwrap_or_else(|| "unknown".to_owned());
    vec![
        ("interpreter", interpreter),
        ("source", source),
        ("pseudolife-mcp", env!("CARGO_PKG_VERSION").to_owned()),
    ]
}

fn put(report: &mut Map<String, Value>, key: &str, value: Value) {
    report.insert(key.to_owned(), value);
}

fn caps(health: Option<&Map<String, Value>>) -> Value {
    let Some(health) = health else {
        return json!("unknown (daemon unreachable)");
    };
    let wake = health
        .get("coordination")
        .and_then(Value::as_object)
        .and_then(|coordination| coordination.get("wake"))
        .and_then(Value::as_object);
    let Some(wake) = wake else {
        return json!("unknown (the daemon does not report them; update it)");
    };
    const KNOWN: [&str; 6] = [
        "per_recipient_per_hour",
        "urgent_per_sender_per_hour",
        "nightly_total",
        "fan_out_stagger_seconds",
        "active_seconds",
        "authority_per_sender_per_hour",
    ];
    let mut out = Map::new();
    for (key, value) in wake {
        if let Value::Number(number) = value
            && KNOWN.contains(&key.as_str())
        {
            let text = number.to_string();
            if pyjson::is_int_token(&text)
                && (!text.starts_with('-') || pyjson::python_int(&text) == "0")
            {
                out.insert(key.clone(), value.clone());
            }
        }
    }
    Value::Object(out)
}

/// `dream_health_line(health, daemon_url)`: `unknown` with no `/health`
/// answer, `no stall reported` for any answer without a stall object (an
/// empty `{}` included, as Python's `isinstance(health, dict)` admits it).
/// A stall object (only a Python daemon sends one) is named by its kind;
/// the session-start hook's full stall line is not rendered here.
fn dreams(health: Option<&Map<String, Value>>) -> Value {
    match health {
        None => json!("unknown"),
        Some(health) => match health.get("stall") {
            Some(Value::Object(stall)) => {
                let kind = stall
                    .get("kind")
                    .and_then(Value::as_str)
                    .filter(|kind| !kind.is_empty())
                    .unwrap_or("unknown");
                json!(format!(
                    "stalled ({kind}): the daemon reports a dream stall; this client does not render its detail, so read the session-start briefing or the daemon's log for the fix"
                ))
            }
            _ => json!("no stall reported"),
        },
    }
}

/// `daemon_version` and `codex_hooks` from a `status: ok` health answer.
async fn health_details(
    health: &Map<String, Value>,
    hooks: clients::CodexHooks,
) -> (String, String) {
    let daemon_version = match health.get("version") {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        // A version that is not text is not one doctor can compare.
        _ => "unknown".into(),
    };
    let hooks_line = match hooks {
        clients::CodexHooks::Line(line) => line,
        clients::CodexHooks::Plugin => match health.get("hooks_digest") {
            None | Some(Value::Null) => "unknown".into(),
            Some(digest) => {
                let checked =
                    super::update::clients::check_codex_hooks(None, digest.as_str(), false);
                let state = checked["state"].as_str().unwrap_or("unknown");
                if state == "current" || state == "needs-approval" {
                    let trust = super::update::clients::runtime_hook_status().await;
                    super::update::clients::runtime_hook_line(trust, state)
                } else {
                    // The file check's own detail names the next step.
                    match checked["detail"]
                        .as_str()
                        .filter(|detail| !detail.is_empty())
                    {
                        Some(detail) => format!("{state}: {detail}"),
                        None => state.to_owned(),
                    }
                }
            }
        },
    };
    (daemon_version, hooks_line)
}

fn coordination_snapshot(
    health: Option<&Map<String, Value>>,
    board_state: &str,
    wake: &Map<String, Value>,
    host: &str,
    registration: Option<&str>,
    tools_present: Option<bool>,
) -> Value {
    const REACHABLE: [&str; 9] = [
        "on",
        "unauthorized",
        "disabled",
        "authentication_required",
        "principal_not_allowed",
        "coordination_requires_postgres",
        "unsupported_capability",
        "transport_refused",
        "refused",
    ];
    let reachable = health.is_some() || REACHABLE.contains(&board_state);
    let auth = if !reachable {
        "offline"
    } else if board_state == "on" {
        "admitted"
    } else {
        board_state
    };
    let mut paths = Vec::new();
    if matches!(host, "generic" | "claude-code")
        && wake
            .get("claude_code")
            .and_then(|client| client.get("stop_hook"))
            .and_then(Value::as_str)
            == Some("on")
    {
        paths.push("Claude Code wakes via the plugin Stop hook; wake_enabled controls only the optional push path.");
    }
    if matches!(host, "generic" | "codex")
        && wake
            .get("codex")
            .and_then(|client| client.get("doorbell"))
            .and_then(Value::as_str)
            == Some("on")
    {
        paths.push("Codex wakes via the doorbell.");
    }
    let wake_state = if host == "claude-desktop" {
        "unsupported"
    } else if !paths.is_empty() {
        "configured"
    } else if host == "generic" {
        "unsupported"
    } else {
        "unavailable"
    };
    let health_ok = health.and_then(|h| h.get("status")) == Some(&json!("ok"));
    let mut next = Vec::new();
    if !reachable {
        next.push("Start the intended daemon and retry; HTTP health does not prove mail delivery.");
    } else if auth != "admitted" {
        next.push("Check the board reason and credential source; repair authentication or daemon coordination settings.");
    } else if registration == Some("principal_changed") {
        next.push("The daemon now maps this login to a different principal than the saved address was registered under (same bank); this file needs no repair: the shim registers a new address on its next start (mail sent to the old address is not carried over), or restart the session to do it now.");
    } else if matches!(
        registration,
        Some("invalid_state" | "invalid_credential" | "bank_identity_mismatch")
    ) {
        next.push("Preserve the saved instance file; verify its bank and principal and the client's credentials before explicit recovery.");
    } else if registration == Some("unsupported_capability") || tools_present == Some(false) {
        next.push("Update the daemon and shim together; this diagnostic requires coordination tools and read-only context support.");
    } else if registration != Some("authenticated") && host != "claude-desktop" {
        next.push("Start or reconnect the client, make a memory call, then use --agent-state with its private saved instance file to check registration.");
    }
    if host == "claude-desktop" {
        next.push("Claude Desktop shares its MCP process across conversations; use a supported session host for a mailbox.");
    } else if host == "generic" && paths.is_empty() {
        next.push("No host selected and no configured wake path detected; use --host claude-code or --host codex to diagnose that client's wake setup.");
    } else if wake_state == "unavailable" {
        next.push("This host has no configured idle wake path; check its stop_hook or doorbell reason above and enable that client's plugin or wake setting, or pull mail on the next turn.");
    } else {
        next.extend(paths);
        next.push("Configured wake is not delivery evidence; verify enqueue, hint/ring, receive and explicit ack in a disposable bank.");
    }
    json!({
        "daemon": if reachable { "reachable" } else { "offline" },
        "health": if health_ok { "ok" } else { "not_verified" },
        "authentication": auth,
        "registration": if host == "claude-desktop" {
            "unsupported_host"
        } else {
            registration.unwrap_or("not_checked")
        },
        "transport": {
            "coordination_tools": match tools_present {
                Some(true) => "advertised",
                Some(false) => "unsupported_capability",
                None => "not_checked",
            },
            "mailbox_pull": if registration == Some("authenticated") {
                "authenticated"
            } else {
                "not_verified"
            },
        },
        "host": host,
        "wake": {"state": wake_state, "evidence": "configuration_only"},
        "delivery": "unverified",
        "next": next,
    })
}

/// doctor_cli.py:660-674: no Git Bash, then passkeys the daemon refuses,
/// fail the report; an earlier error keeps its name and recovery.
fn finalize(
    report: &mut Map<String, Value>,
    maintainer_invalid: bool,
    maintainer_recovery: Option<Value>,
) {
    if report.get("git_bash") == Some(&Value::Null) {
        put(report, "ok", json!(false));
        if !report.contains_key("error") {
            put(report, "error", json!("GitBashMissing"));
            let recovery = report
                .get("git_bash_recovery")
                .cloned()
                .unwrap_or(Value::Null);
            put(report, "recovery", recovery);
        }
    }
    if maintainer_invalid {
        put(report, "ok", json!(false));
        if !report.contains_key("error") {
            put(report, "error", json!("MaintainerPasskeysInvalid"));
            put(
                report,
                "recovery",
                maintainer_recovery.unwrap_or(Value::Null),
            );
        }
    }
}

async fn diagnose(
    args: &Args,
    environment: &super::host_connection::Environment,
) -> Res<(Value, bool)> {
    // Everything before the first request: environment and client files.
    // A section whose input the native reader does not parse is named in
    // the report; it never ends the run.
    let proxy_names = proxy_variables();
    let mut env = Env::default();
    for (key, value) in environment.additions() {
        env.set(key, value.clone());
    }
    let (overrides, credential_source, registrations_unread) =
        match clients::registration_credentials(&env) {
            Ok((overrides, source)) => (overrides, source, false),
            // An unreadable token variable defers here too; that is
            // named as CredentialUnavailable below, not as a client file.
            Err(Defer) => (
                Vec::new(),
                None,
                env.var("PSEUDOLIFE_MCP_TOKEN").is_ok()
                    && env.var("PSEUDOLIFE_MCP_TOKEN_FILE").is_ok(),
            ),
        };
    for (key, value) in overrides {
        env.set(&key, value);
    }
    // An invalid URL is a named error; nothing is requested.
    let url = env
        .var("PSEUDOLIFE_MCP_DAEMON_URL")
        .ok()
        .map(|value| value.unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned()))
        .and_then(|value| crate::daemon_url::validate(&value).ok());
    let mut problem = None;
    let provider = match (
        env.var("PSEUDOLIFE_MCP_TOKEN_FILE"),
        env.var("PSEUDOLIFE_MCP_TOKEN"),
    ) {
        (Ok(token_file), Ok(token_value)) => {
            // The file wins while it is set (`from_lookup`).
            let source = if token_file.is_some() {
                Source::File
            } else {
                Source::Inline
            };
            match crate::credentials::CredentialProvider::from_lookup(|key| match key {
                "PSEUDOLIFE_MCP_TOKEN_FILE" => token_file.clone().map(OsString::from),
                "PSEUDOLIFE_MCP_TOKEN" => token_value.clone().map(OsString::from),
                _ => None,
            }) {
                Ok(provider) => Some(provider),
                Err(error) => {
                    problem = Some(BearerProblem::Unavailable(source, error.0));
                    None
                }
            }
        }
        _ => {
            problem = Some(BearerProblem::Unavailable(Source::NotText, ""));
            None
        }
    };
    let token = match provider.as_ref().map(|provider| provider.snapshot()) {
        Some(Ok(snapshot)) => snapshot.token().map(str::to_owned),
        // Only a file is read at snapshot time.
        Some(Err(error)) => {
            problem = Some(BearerProblem::Unavailable(Source::File, error.0));
            None
        }
        None => None,
    };
    if token.as_deref().is_some_and(|token| !sendable(token)) {
        problem = Some(BearerProblem::Unsendable);
    }
    let git_bash = if pyenv::WINDOWS {
        Some(clients::git_bash_report(&env).unwrap_or_else(|_| {
            vec![(
                "git_bash",
                json!(unread(
                    "the Claude Code settings or PATH that locate Git Bash"
                )),
            )]
        }))
    } else {
        None
    };
    let claude_wake = clients::claude_code_wake(&env).unwrap_or_else(|_| {
        clients::ClaudeWake::Fixed(
            json!({"registered": unread("~/.claude.json or the Claude Code plugin list")}),
        )
    });
    let codex_wake = clients::codex_wake(&env).unwrap_or_else(|_| {
        clients::CodexWake::Fixed(json!({"registered": unread("the Codex config.toml")}))
    });
    let hooks = clients::codex_hooks(&env)
        .unwrap_or_else(|_| clients::CodexHooks::Line(unread("the Codex home or its config.toml")));
    let resolution = clients::path_resolution(&env)
        .unwrap_or_else(|_| json!({"on_path": unread("PATH or the launcher location")}));
    let tunnel_root = clients::tunnel_root(&env).ok();
    let identity = identity();
    // A saved instance is read now; its context request runs after the
    // handshake, as in Python. Claude Desktop is never checked.
    let saved = match (&args.agent_state, &url) {
        (Some(path), Some(url)) if args.host != "claude-desktop" => Some(agent::read(path, url)?),
        _ => None,
    };

    let short = Duration::try_from_secs_f64(args.timeout.min(2.0)).map_err(|_| Defer)?;
    // _maintainer_probe and the --agent-state check build a new provider
    // and snapshot it again (the token file is read again).
    let fresh = || -> Result<Option<String>, ()> {
        let provider = provider.as_ref().ok_or(())?;
        provider
            .snapshot()
            .map(|snapshot| snapshot.token().map(str::to_owned))
            .map_err(|_| ())
    };
    let mut report = Map::new();
    put(&mut report, "ok", json!(false));
    for (key, value) in identity {
        put(&mut report, key, json!(value));
    }
    put(
        &mut report,
        "credential_source",
        json!(credential_source.as_deref().unwrap_or("none")),
    );
    if registrations_unread {
        put(&mut report, "registrations", json!(REGISTRATIONS_UNREAD));
    }
    // `_token_prefix` (v58): never the token, the 8 hex of its SHA-256 that
    // the daemon's `invite --list` shows when it refuses it.
    put(
        &mut report,
        "token_sha256_prefix",
        token
            .as_deref()
            .filter(|token| !token.is_empty())
            .map_or(Value::Null, |token| json!(token_hash_prefix(token))),
    );
    // Only a URL that validated can be proxied; nothing is sent otherwise.
    let proxy_routes = url.as_deref().is_some_and(shim_proxies);
    if url.is_some()
        && proxy_names
            .iter()
            .any(|name| !name.eq_ignore_ascii_case("no_proxy"))
    {
        put(
            &mut report,
            "proxy",
            json!(proxy_note(&proxy_names, proxy_routes)),
        );
    }
    let not_checked = |line: &str| {
        let mut off = Map::new();
        off.insert("state".into(), json!("not_checked"));
        off.insert("line".into(), json!(line));
        off
    };
    // Board and maintainer probes send the bearer: an unusable one is
    // never sent, and the probes are named as not checked.
    let board = match (&url, &problem) {
        (None, _) => not_checked("not checked - PSEUDOLIFE_MCP_DAEMON_URL is invalid"),
        (Some(_), Some(problem)) => not_checked(&problem.board_line()),
        (Some(url), None) => probes::board(url, token.as_deref(), short)
            .await
            .unwrap_or_else(|_| {
                not_checked(
                    "not checked - the daemon's board answer is in a form this client does not read",
                )
            }),
    };
    let board_state = board
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    put(
        &mut report,
        "board",
        board.get("line").cloned().unwrap_or(Value::Null),
    );
    let maintainer = match (&url, board_state.as_str()) {
        (Some(url), "on") => match fresh() {
            Err(()) => not_checked("not checked - no usable credential or URL"),
            Ok(Some(token)) if !token.is_empty() => probes::maintainer(url, &token, short)
                .await
                .unwrap_or_else(|_| {
                    not_checked(
                        "not checked - the daemon's maintainer answer was incomplete or in a form this client does not read",
                    )
                }),
            Ok(_) => {
                not_checked("not checked - no bearer token (a tokenless daemon refuses passkeys)")
            }
        },
        (None, _) => not_checked("not checked - PSEUDOLIFE_MCP_DAEMON_URL is invalid"),
        (Some(_), _) if problem.is_some() => not_checked("not checked - no usable credential"),
        (Some(_), "not_checked") => not_checked("not checked - the board answer could not be read"),
        _ => not_checked("not checked - the board is off for this token"),
    };
    let maintainer_invalid = maintainer.get("state") == Some(&json!("invalid"));
    let maintainer_recovery = maintainer.get("recovery").cloned();
    put(
        &mut report,
        "maintainer_passkeys",
        Value::Object(maintainer),
    );
    if let Some(git_bash) = &git_bash {
        for (key, value) in git_bash {
            put(&mut report, key, value.clone());
        }
    }
    // `None` for no answer; `Err` for an answer that is no daemon's health
    // report (not an object, a status that is not text, a redirect off HTTP).
    let answer = match &url {
        None => Ok(None),
        Some(url) => match probes::health(url, short).await {
            Ok(None) => Ok(None),
            Ok(Some(Value::Object(map))) => match map.get("status") {
                None | Some(Value::Null | Value::String(_)) => Ok(Some(map)),
                Some(_) => Err(()),
            },
            Ok(Some(_)) | Err(Defer) => Err(()),
        },
    };
    let unrecognized = answer.is_err();
    let health = answer.unwrap_or(None);
    let present = health.as_ref().filter(|map| !map.is_empty());
    let status = present.map(|map| map.get("status").cloned().unwrap_or(Value::Null));
    put(
        &mut report,
        "daemon_status",
        if url.is_none() {
            json!("not_checked")
        } else if unrecognized {
            json!("unrecognized")
        } else {
            status.clone().unwrap_or_else(|| json!("unreachable"))
        },
    );
    put(&mut report, "dreams", dreams(health.as_ref()));
    let mut tools_present = None;
    let healthy = present.filter(|_| status == Some(json!("ok")));
    if url.is_none() {
        put(&mut report, "error", json!("DaemonUrlInvalid"));
        put(
            &mut report,
            "recovery",
            json!(format!(
                "PSEUDOLIFE_MCP_DAEMON_URL is not a URL the client accepts: use an http(s) origin without credentials, a path, query, or fragment, such as {}. Correct it in this shell or in the client registration that sets it, then retry; doctor requested nothing.",
                crate::daemon_url::DEFAULT_URL
            )),
        );
    } else if let Some(problem) = &problem {
        let (error, recovery) =
            problem.error(credential_source.as_deref().unwrap_or("environment"));
        put(&mut report, "error", json!(error));
        put(&mut report, "recovery", json!(recovery));
        // The unauthenticated details still help; the handshake would only
        // fail on the same credential.
        if let Some(health) = healthy {
            let (daemon_version, hooks_line) = health_details(health, hooks).await;
            put(&mut report, "daemon_version", json!(daemon_version));
            put(&mut report, "codex_hooks", json!(hooks_line));
        }
    } else if unrecognized {
        put(&mut report, "error", json!("DaemonUnrecognized"));
        put(
            &mut report,
            "recovery",
            json!(
                "The daemon URL answered /health with something that is not a Pseudolife daemon's health report. Check that PSEUDOLIFE_MCP_DAEMON_URL names the Pseudolife daemon (not a proxy or another service), then retry."
            ),
        );
    } else if status != Some(json!("ok")) {
        if board_state == "unauthorized" {
            put(&mut report, "error", json!("BearerRejected"));
            put(
                &mut report,
                "recovery",
                json!(
                    "The endpoint is reachable but rejects this bearer; verify the credential source and daemon authentication configuration, then reconnect."
                ),
            );
        } else {
            put(&mut report, "error", json!("DaemonUnavailable"));
            put(
                &mut report,
                "recovery",
                json!("Start the intended daemon, then retry; doctor never starts one."),
            );
        }
    } else if let Some(health) = present
        && health.get("auth").is_some_and(truthy)
        && credential_source.is_none()
    {
        put(&mut report, "error", json!("BearerMissing"));
        let recovery = if registrations_unread {
            format!("{BEARER_MISSING} A client registration was {REGISTRATIONS_UNREAD}")
        } else {
            BEARER_MISSING.to_owned()
        };
        put(&mut report, "recovery", json!(recovery));
    } else if let Some(health) = present {
        let (daemon_version, hooks_line) = health_details(health, hooks).await;
        put(&mut report, "daemon_version", json!(daemon_version));
        put(&mut report, "codex_hooks", json!(hooks_line));
        let proxy_hint = |text: &str| {
            if proxy_routes {
                format!("{text}{PROXY_RECOVERY}")
            } else {
                text.to_owned()
            }
        };
        // Nothing defers past this point: the handshake starts a shim.
        let budget = Duration::try_from_secs_f64(args.timeout).map_err(|_| Defer)?;
        match handshake::run(env.overlay(), budget).await {
            Ok(result) => {
                let ok = result.instructions_present
                    && result.tool_count > 0
                    && result.missing_annotations.is_empty();
                put(
                    &mut report,
                    "instructions_present",
                    json!(result.instructions_present),
                );
                put(&mut report, "tool_count", json!(result.tool_count));
                put(
                    &mut report,
                    "tools_missing_annotations",
                    json!(result.missing_annotations),
                );
                put(
                    &mut report,
                    "coordination_tools_present",
                    json!(result.coordination_tools),
                );
                tools_present = Some(result.coordination_tools);
                put(&mut report, "ok", json!(ok));
                if !ok {
                    put(
                        &mut report,
                        "recovery",
                        json!(proxy_hint(
                            "Check shim stderr and daemon MCP access, then compare daemon and shim versions; update the component missing instructions or annotations and reconnect."
                        )),
                    );
                }
                let installed = env!("CARGO_PKG_VERSION");
                if daemon_version != "unknown" && daemon_version != installed {
                    put(&mut report, "ok", json!(false));
                    put(&mut report, "version_mismatch", json!(true));
                    put(
                        &mut report,
                        "recovery",
                        json!(format!(
                            "The shim is pseudolife-mcp {installed} but the daemon is {daemon_version}. Run pseudolife-mcp update --clients-only --tag {daemon_version} (the daemon's release as a new shim runtime beside the running one; from a checkout: python ops/update_clients.py --only shim; no session has to close), or update the daemon with pseudolife-mcp update; then start a new session and retry."
                        )),
                    );
                }
            }
            Err(handshake::Failure::Timeout) => {
                put(&mut report, "error", json!("TimeoutError"));
                put(
                    &mut report,
                    "recovery",
                    json!(proxy_hint(
                        "Check daemon health and MCP access; if startup is slow, retry doctor with a larger --timeout budget."
                    )),
                );
            }
            Err(handshake::Failure::Group) => {
                put(&mut report, "error", json!("ExceptionGroup"));
                put(
                    &mut report,
                    "recovery",
                    json!(proxy_hint(
                        "Check daemon health and the exact registered interpreter. Run that interpreter with -m pip check and -m pip show pseudolife-mcp mcp; reinstall there if stale, then retry."
                    )),
                );
            }
        }
    }
    let enabled = health
        .as_ref()
        .and_then(|h| h.get("coordination"))
        .and_then(Value::as_object)
        .and_then(|c| c.get("enabled"))
        .and_then(Value::as_bool);
    let mut wake = Map::new();
    wake.insert("claude_code".into(), claude_wake.report(enabled));
    wake.insert("codex".into(), codex_wake.report(enabled));
    wake.insert("caps".into(), caps(health.as_ref()));
    // The snapshot comes first: its failure is `unavailable` whatever the
    // saved file holds (doctor_cli.py:650-655).
    let registration = match &saved {
        Some(_) if board_state != "on" => None,
        None => None,
        Some(saved) => Some(match (fresh(), saved) {
            (Err(()), _) => "unavailable",
            (_, agent::Saved::Missing) => "missing_registration",
            (_, agent::Saved::Invalid) => "invalid_state",
            (token, agent::Saved::State(state)) => match &url {
                Some(url) => agent::probe(url, token, state, short).await,
                None => "unavailable",
            },
        }),
    };
    let mut snapshot = coordination_snapshot(
        health.as_ref(),
        &board_state,
        &wake,
        args.host,
        registration,
        tools_present,
    );
    // With an invalid URL no daemon was contacted: not "offline".
    if url.is_none() {
        snapshot["daemon"] = json!("not_checked");
        snapshot["authentication"] = json!("not_checked");
        if let Some(first) = snapshot["next"]
            .as_array_mut()
            .and_then(|next| next.first_mut())
        {
            *first = json!(
                "Correct PSEUDOLIFE_MCP_DAEMON_URL first (see recovery); doctor contacted no daemon."
            );
        }
    }
    let registered = snapshot.get("registration") == Some(&json!("authenticated"));
    put(&mut report, "wake", Value::Object(wake));
    put(&mut report, "coordination", snapshot);
    if args.agent_state.is_some() && !registered {
        put(&mut report, "ok", json!(false));
    }
    finalize(&mut report, maintainer_invalid, maintainer_recovery);
    put(&mut report, "path_resolution", resolution);
    // Saved tunnel profiles, read-only and informational; no profile
    // files (an absent or empty directory) adds nothing.
    if let Some(tunnels) = tunnel_root
        .as_deref()
        .and_then(|root| super::tunnel::doctor_section(std::path::Path::new(root)))
    {
        put(&mut report, "tunnels", json!(tunnels));
    }
    let ok = report.get("ok") == Some(&json!(true));
    Ok((Value::Object(report), ok))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(pairs: Value) -> Map<String, Value> {
        pairs.as_object().unwrap().clone()
    }

    #[test]
    fn coordination_reports_configured_wake_paths_without_claiming_delivery() {
        let health = report(json!({"status": "ok", "coordination": {"wake_enabled": false}}));
        let wake = report(json!({"claude_code": {"stop_hook": "on"}}));
        for host in ["generic", "claude-code"] {
            let snapshot = coordination_snapshot(Some(&health), "on", &wake, host, None, None);
            assert_eq!(snapshot["wake"]["state"], "configured");
            assert_eq!(snapshot["delivery"], "unverified");
            assert!(snapshot["next"].as_array().unwrap().contains(&json!(
                "Claude Code wakes via the plugin Stop hook; wake_enabled controls only the optional push path."
            )));
        }
        let codex = report(json!({"codex": {"doorbell": "on"}}));
        let snapshot = coordination_snapshot(Some(&health), "on", &codex, "generic", None, None);
        assert_eq!(snapshot["wake"]["state"], "configured");
        assert!(
            snapshot["next"]
                .as_array()
                .unwrap()
                .contains(&json!("Codex wakes via the doorbell."))
        );
        let disabled = report(json!({"claude_code": {"stop_hook": "off (plugin disabled)"}}));
        let snapshot =
            coordination_snapshot(Some(&health), "on", &disabled, "claude-code", None, None);
        assert_eq!(snapshot["wake"]["state"], "unavailable");
        assert!(
            snapshot["next"]
                .as_array()
                .unwrap()
                .last()
                .unwrap()
                .as_str()
                .unwrap()
                .contains("no configured idle wake path")
        );
        let snapshot =
            coordination_snapshot(Some(&health), "on", &wake, "claude-desktop", None, None);
        assert_eq!(snapshot["wake"]["state"], "unsupported");
    }

    #[test]
    fn missing_git_bash_fails_and_names_itself_only_without_an_earlier_error() {
        // Unreachable through the harness on a host with Git for Windows in
        // its default directory: doctor_cli.py hard-codes those paths.
        let mut fresh = report(json!({"ok": true, "git_bash": null,
            "git_bash_recovery": "install Git", "wake": {}}));
        finalize(&mut fresh, false, None);
        assert_eq!(
            Value::Object(fresh),
            json!({"ok": false, "git_bash": null, "git_bash_recovery": "install Git",
                   "wake": {}, "error": "GitBashMissing", "recovery": "install Git"})
        );
        let mut earlier = report(json!({"ok": false, "error": "DaemonUnavailable",
            "recovery": "start it", "git_bash": null, "git_bash_recovery": "install Git"}));
        finalize(&mut earlier, true, Some(json!("fix passkeys")));
        assert_eq!(earlier["error"], json!("DaemonUnavailable"));
        assert_eq!(earlier["recovery"], json!("start it"));
        let mut passkeys = report(json!({"ok": true, "git_bash": "bash.exe"}));
        finalize(&mut passkeys, true, Some(json!("fix passkeys")));
        assert_eq!(
            Value::Object(passkeys),
            json!({"ok": false, "git_bash": "bash.exe", "error": "MaintainerPasskeysInvalid",
                   "recovery": "fix passkeys"})
        );
    }

    #[test]
    fn the_dreams_line_follows_dream_health_line_and_names_a_stall() {
        let line = |health: Option<Value>| {
            let map = health.map(report);
            dreams(map.as_ref())
        };
        assert_eq!(line(None), json!("unknown"));
        assert_eq!(line(Some(json!({}))), json!("no stall reported"));
        assert_eq!(
            line(Some(json!({"status": "ok"}))),
            json!("no stall reported")
        );
        for stall in [json!(null), json!("stalled"), json!([]), json!(1)] {
            assert_eq!(
                line(Some(json!({"status": "ok", "stall": stall}))),
                json!("no stall reported")
            );
        }
        // A stall object is named by its kind, never a deferral.
        let stalled = |kind: &str| {
            json!(format!(
                "stalled ({kind}): the daemon reports a dream stall; this client does not render its detail, so read the session-start briefing or the daemon's log for the fix"
            ))
        };
        assert_eq!(
            line(Some(json!({"status": "ok", "stall": {}}))),
            stalled("unknown")
        );
        assert_eq!(
            line(Some(
                json!({"status": "ok", "stall": {"kind": "signed_out"}})
            )),
            stalled("signed_out")
        );
    }

    #[test]
    fn timeouts_past_the_cap_defer_in_parse() {
        let args = |value: &str| {
            parse(&[OsString::from("--timeout"), OsString::from(value)]).map(|args| args.timeout)
        };
        assert_eq!(args("1000000"), Some(1_000_000.0));
        assert_eq!(args("1000000.5"), None);
        assert_eq!(args("100000000000000000000"), None);
    }
}
