//! `pseudolife-mcp doctor`: read-only runtime diagnostics, and the
//! canonical `--disposable-proof` refusal. Every input outside the
//! reproduced domain defers before the first request to the daemon, or,
//! for an answer only the daemon can give, before the handshake starts.
mod clients;
mod handshake;
mod probes;
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
    agent_state: bool,
}

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
        agent_state: false,
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
                1 => args.timeout = positive_decimal(value)?,
                2 => args.host = HOSTS.iter().find(|host| **host == value)?,
                _ if value.is_empty() || value.starts_with('-') => return None,
                _ => {}
            }
        }
    }
    args.proof = seen[0];
    args.agent_state = seen[3];
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
    let args = parse(&values)?;
    if args.proof {
        // A nonempty fixture database runs the Python daemon's ASGI app in
        // process; saved-state proofs are refused there too. Both defer.
        if args.agent_state
            || std::env::var_os("PSEUDOLIFE_TEST_DATABASE_URL").is_some_and(|dsn| !dsn.is_empty())
        {
            return None;
        }
        return Some(emit(REFUSAL, 2));
    }
    // A saved-instance registration check (--agent-state) stays deferred.
    if args.agent_state {
        return None;
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    let (report, ok) = runtime.block_on(diagnose(&args)).ok()?;
    Some(emit(
        &(pyjson::dumps(&report) + "\n"),
        if ok { 0 } else { 1 },
    ))
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

/// urllib reads `*_proxy` variables (and, on Windows without them, the
/// registry); the native probes connect directly, so a proxy defers.
fn no_proxy_environment() -> Res<()> {
    for (name, value) in std::env::vars_os() {
        let name = name.to_string_lossy().to_ascii_lowercase();
        if name.ends_with("_proxy") && !value.is_empty() {
            return Err(Defer);
        }
    }
    Ok(())
}

/// The bearer as http.client sends it: Latin-1, no control characters.
fn sendable(token: &str) -> bool {
    token
        .chars()
        .all(|c| (c as u32) <= 0xff && (c as u32) >= 0x20 && c != '\u{7f}')
}

/// This runtime's identity, in the fields the Python report fills from its
/// interpreter (declared substitution `doctor-runtime-identity`).
fn identity() -> Res<[(&'static str, String); 4]> {
    let executable = std::env::current_exe().map_err(|_| Defer)?;
    let interpreter = executable.to_str().ok_or(Defer)?.to_owned();
    let resolved = std::fs::canonicalize(&executable).map_err(|_| Defer)?;
    let source = resolved.parent().ok_or(Defer)?.to_str().ok_or(Defer)?;
    let source = source.strip_prefix("\\\\?\\").unwrap_or(source).to_owned();
    Ok([
        ("interpreter", interpreter),
        ("source", source),
        ("pseudolife-mcp", env!("CARGO_PKG_VERSION").to_owned()),
        ("mcp", "not installed".to_owned()),
    ])
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

fn coordination_snapshot(
    health: Option<&Map<String, Value>>,
    board_state: &str,
    wake: &Map<String, Value>,
    host: &str,
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
    let selected = wake.get(if host == "codex" {
        "codex"
    } else {
        "claude_code"
    });
    let configured = selected.and_then(|selected| {
        selected.get(if host == "codex" {
            "doorbell"
        } else {
            "stop_hook"
        })
    });
    let wake_state = if host == "generic" || host == "claude-desktop" {
        "unsupported"
    } else if configured == Some(&json!("on")) {
        "configured"
    } else {
        "unavailable"
    };
    let health_ok = health.and_then(|h| h.get("status")) == Some(&json!("ok"));
    let mut next = Vec::new();
    if !reachable {
        next.push("Start the intended daemon and retry; HTTP health does not prove mail delivery.");
    } else if auth != "admitted" {
        next.push("Check the board reason and credential source; repair authentication or daemon coordination settings.");
    } else if tools_present == Some(false) {
        next.push("Update the daemon and shim together; this diagnostic requires coordination tools and read-only context support.");
    } else if host != "claude-desktop" {
        next.push("Start or reconnect the client, make a memory call, then use --agent-state with its private saved instance file to check registration.");
    }
    if host == "claude-desktop" {
        next.push("Claude Desktop shares its MCP process across conversations; use a supported session host for a mailbox.");
    } else if wake_state == "unsupported" {
        next.push("This host has no verified idle wake path; pull mail on the next turn.");
    } else {
        next.push("Configured wake is not delivery evidence; verify enqueue, hint/ring, receive and explicit ack in a disposable bank.");
    }
    json!({
        "daemon": if reachable { "reachable" } else { "offline" },
        "health": if health_ok { "ok" } else { "not_verified" },
        "authentication": auth,
        "registration": if host == "claude-desktop" { "unsupported_host" } else { "not_checked" },
        "transport": {
            "coordination_tools": match tools_present {
                Some(true) => "advertised",
                Some(false) => "unsupported_capability",
                None => "not_checked",
            },
            "mailbox_pull": "not_verified",
        },
        "host": host,
        "wake": {"state": wake_state, "evidence": "configuration_only"},
        "delivery": "unverified",
        "next": next,
    })
}

async fn diagnose(args: &Args) -> Res<(Value, bool)> {
    // Everything before the first request: environment and client files.
    no_proxy_environment()?;
    let mut env = Env::default();
    let (overrides, credential_source) = clients::registration_credentials(&env)?;
    for (key, value) in overrides {
        env.set(&key, value);
    }
    let url_value = env
        .var("PSEUDOLIFE_MCP_DAEMON_URL")?
        .unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned());
    let url = crate::daemon_url::validate(&url_value).map_err(|_| Defer)?;
    let token_file = env.var("PSEUDOLIFE_MCP_TOKEN_FILE")?;
    let token_value = env.var("PSEUDOLIFE_MCP_TOKEN")?;
    let provider = crate::credentials::CredentialProvider::from_lookup(|key| match key {
        "PSEUDOLIFE_MCP_TOKEN_FILE" => token_file.clone().map(OsString::from),
        "PSEUDOLIFE_MCP_TOKEN" => token_value.clone().map(OsString::from),
        _ => None,
    })
    .map_err(|_| Defer)?;
    let token = provider
        .snapshot()
        .map_err(|_| Defer)?
        .token()
        .map(str::to_owned);
    if token.as_deref().is_some_and(|token| !sendable(token)) {
        return Err(Defer);
    }
    let git_bash = if pyenv::WINDOWS {
        Some(clients::git_bash_report(&env)?)
    } else {
        None
    };
    let claude_wake = clients::claude_code_wake(&env)?;
    let codex_wake = clients::codex_wake(&env)?;
    let hooks = clients::codex_hooks(&env)?;
    let resolution = clients::path_resolution(&env)?;
    if clients::tunnels_present(&env)? {
        return Err(Defer);
    }
    let identity = identity()?;

    let short = Duration::from_secs_f64(args.timeout.min(2.0));
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
    let board = probes::board(&url, token.as_deref(), short).await?;
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
    let maintainer = match (&token, board_state == "on") {
        (Some(token), true) => probes::maintainer(&url, token, short).await?,
        _ => {
            let mut off = Map::new();
            off.insert("state".into(), json!("not_checked"));
            off.insert(
                "line".into(),
                json!("not checked - the board is off for this token"),
            );
            off
        }
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
    let health = match probes::health(&url, short).await? {
        None => None,
        Some(Value::Object(map)) => Some(map),
        Some(_) => return Err(Defer),
    };
    let present = health.as_ref().filter(|map| !map.is_empty());
    let status = present.map(|map| map.get("status").cloned().unwrap_or(Value::Null));
    match &status {
        None => put(&mut report, "daemon_status", json!("unreachable")),
        Some(value @ (Value::Null | Value::String(_))) => {
            put(&mut report, "daemon_status", value.clone())
        }
        Some(_) => return Err(Defer),
    }
    let mut tools_present = None;
    if status != Some(json!("ok")) {
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
        put(&mut report, "recovery", json!(BEARER_MISSING));
    } else if let Some(health) = present {
        let daemon_version = match health.get("version") {
            Some(Value::String(text)) if !text.is_empty() => text.clone(),
            Some(value) if truthy(value) => return Err(Defer),
            _ => "unknown".into(),
        };
        let hooks_line = match hooks {
            clients::CodexHooks::Line(line) => line,
            clients::CodexHooks::Plugin => match health.get("hooks_digest") {
                None | Some(Value::Null) => "unknown".into(),
                Some(_) => return Err(Defer),
            },
        };
        put(&mut report, "daemon_version", json!(daemon_version));
        put(&mut report, "codex_hooks", json!(hooks_line));
        // Nothing defers past this point: the handshake starts a shim.
        let budget = Duration::from_secs_f64(args.timeout);
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
                        json!(
                            "Check shim stderr and daemon MCP access, then compare daemon and shim versions; update the component missing instructions or annotations and reconnect."
                        ),
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
                    json!(
                        "Check daemon health and MCP access; if startup is slow, retry doctor with a larger --timeout budget."
                    ),
                );
            }
            Err(handshake::Failure::Group) => {
                put(&mut report, "error", json!("ExceptionGroup"));
                put(
                    &mut report,
                    "recovery",
                    json!(
                        "Check daemon health and the exact registered interpreter. Run that interpreter with -m pip check and -m pip show pseudolife-mcp mcp; reinstall there if stale, then retry."
                    ),
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
    let snapshot = coordination_snapshot(
        health.as_ref(),
        &board_state,
        &wake,
        args.host,
        tools_present,
    );
    put(&mut report, "wake", Value::Object(wake));
    put(&mut report, "coordination", snapshot);
    // No Git Bash fails the report; an earlier error keeps its name.
    if report.get("git_bash") == Some(&Value::Null) {
        put(&mut report, "ok", json!(false));
        if !report.contains_key("error") {
            put(&mut report, "error", json!("GitBashMissing"));
            let recovery = report
                .get("git_bash_recovery")
                .cloned()
                .unwrap_or(Value::Null);
            put(&mut report, "recovery", recovery);
        }
    }
    if maintainer_invalid {
        put(&mut report, "ok", json!(false));
        if !report.contains_key("error") {
            put(&mut report, "error", json!("MaintainerPasskeysInvalid"));
            put(
                &mut report,
                "recovery",
                maintainer_recovery.unwrap_or(Value::Null),
            );
        }
    }
    put(&mut report, "path_resolution", resolution);
    let ok = report.get("ok") == Some(&json!(true));
    Ok((Value::Object(report), ok))
}
