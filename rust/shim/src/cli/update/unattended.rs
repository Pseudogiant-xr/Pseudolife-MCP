//! Opt-in unattended update policy and per-user schedules. No interpreter is used.
use super::{Result, Update, UpdateError, seams};
use crate::credentials::CredentialProvider;
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};

#[path = "unattended/schedule.rs"]
mod schedule;
pub(crate) use schedule::{schedule, unschedule};

pub(crate) const LOG_NAME: &str = "unattended-update.log";
const BOARD_LABEL: &str = "unattended update";

thread_local! { static LOG: RefCell<Option<File>> = const { RefCell::new(None) }; }
/// Called by the core's step/warn sinks, including while a transaction runs.
pub(crate) fn log_step(text: &str) {
    LOG.with(|log| {
        if let Some(file) = log.borrow_mut().as_mut() {
            let _ = writeln!(
                file,
                "[{}] {text}",
                chrono::Local::now().format("%Y-%m-%d %H:%M:%S")
            );
            let _ = file.flush();
        }
    });
}
struct LogGuard;
impl Drop for LogGuard {
    fn drop(&mut self) {
        LOG.with(|log| {
            log.replace(None);
        });
    }
}

fn version_is_newer(candidate: &str, own: &str) -> bool {
    matches!((super::version_key(candidate),super::version_key(own)),(Some(candidate),Some(own)) if candidate>own)
}
pub(crate) fn on_shim_runtime() -> bool {
    crate::runtime::default_layout()
        .ok()
        .and_then(|layout| crate::runtime::running_runtime(&layout))
        .is_some()
}

#[derive(Debug, Default)]
struct Activity {
    active: Vec<Value>,
    idle: u64,
}

fn bearer() -> Option<String> {
    CredentialProvider::from_environment()
        .ok()?
        .snapshot()
        .ok()?
        .token()
        .map(str::to_owned)
}

/// Refuse redirects on every credential-bearing request, including instance keys.
fn post_json(
    url: &str,
    token: Option<&str>,
    body: Value,
    instance: Option<(&str, &str)>,
) -> std::result::Result<Value, String> {
    seams::block_on(async {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|_| "the HTTP client could not be initialized")?;
        let mut request = client.post(url).json(&body);
        if let Some(token) = token {
            request = request.bearer_auth(token);
        }
        if let Some((agent, credential)) = instance {
            request = request
                .header("X-PL-Agent", agent)
                .header("X-PL-Agent-Key", credential);
        }
        let response = request
            .send()
            .await
            .map_err(|_| "the daemon did not answer")?;
        let status = response.status();
        if response.content_length().is_some_and(|n| n > 1024 * 1024) {
            return Err("the daemon's reply was too large".into());
        }
        use futures::StreamExt;
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| "the daemon's reply was interrupted")?;
            if bytes.len() + chunk.len() > 1024 * 1024 {
                return Err("the daemon's reply was too large".into());
            }
            bytes.extend_from_slice(&chunk);
        }
        let value = serde_json::from_slice::<Value>(&bytes)
            .ok()
            .filter(Value::is_object);
        if !status.is_success() {
            // Never echo arbitrary error bodies, which may contain credentials.
            let error = value
                .as_ref()
                .and_then(|v| v["error"].as_str())
                .unwrap_or("");
            let reason = if error.starts_with("principal_not_allowed") {
                "principal_not_allowed"
            } else if matches!(status.as_u16(), 401 | 403) {
                "bearer refused"
            } else {
                "request refused"
            };
            return Ok(json!({"error":reason,"http_status":status.as_u16()}));
        }
        value.ok_or_else(|| "the daemon's reply was not understood".into())
    })
}

fn board_activity(url: &str) -> std::result::Result<Activity, String> {
    let token = bearer().ok_or(
        "no bearer token in this environment (PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE)",
    )?;
    board_activity_with(url, &token)
}

fn board_activity_with(url: &str, token: &str) -> std::result::Result<Activity, String> {
    let endpoint = format!("{}/api/coordination", url.trim_end_matches('/'));
    let reply = post_json(
        &format!("{endpoint}/register"),
        Some(token),
        json!({"task":"update check","project":"pseudolife-mcp","label":BOARD_LABEL,
            "status":"checking whether a release can be applied", "capabilities":{"resumable":false},"wake_enabled":false}),
        None,
    )?;
    if reply["enabled"] == false || reply.get("error").is_some() {
        return Err("the daemon refused board registration".into());
    }
    let agent = reply["agent_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("the registration reply carried no address")?;
    let key = reply["credential"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("the registration reply carried no credential")?;
    // The throwaway board address expires naturally, as in lease_cli._Board.
    decode_activity(post_json(
        &format!("{endpoint}/agents"),
        Some(token),
        json!({"limit":50}),
        Some((agent, key)),
    )?)
}

fn decode_activity(reply: Value) -> std::result::Result<Activity, String> {
    if reply["enabled"] == false || reply.get("error").is_some() || reply["truncated"] == true {
        return Err("the daemon's agents reply was not understood".into());
    }
    let listed = reply["agents"]
        .as_array()
        .ok_or("the daemon's agents reply was not understood")?;
    if listed
        .iter()
        .any(|agent| !agent.is_object() || !agent["agent_id"].is_string())
    {
        return Err("the daemon's agents reply carried an unreadable session".into());
    }
    let active = listed
        .iter()
        .filter(|a| a.is_object() && a["agent_id"].is_string() && a["label"] != BOARD_LABEL)
        .cloned()
        .collect();
    Ok(Activity {
        active,
        idle: reply["idle_omitted"].as_u64().unwrap_or(0),
    })
}

fn notice(update: &mut Update, text: &str) {
    let url = format!(
        "{}/api/daemon-notice",
        update.o.daemon_url.trim_end_matches('/')
    );
    let reply = post_json(
        &url,
        bearer().as_deref(),
        json!({"text":text.chars().take(4000).collect::<String>()}),
        None,
    )
    .unwrap_or(Value::Null);
    if let Some(n) = reply["recipients"].as_u64() {
        update.step(format!(
            "board notice posted from the daemon principal ({n} recipient(s))"
        ));
    } else if reply["error"]
        .as_str()
        .is_some_and(|s| s.starts_with("principal_not_allowed"))
    {
        update.step("board notice refused: this run's bearer principal is not listed in coordination.daemon_notice_principals (empty by default; see the unattended-update section of docs/guide/configuration.md); the log carries the same text");
    } else if reply["reason"] == "board_unavailable" {
        update.step("board notice not posted: the board is off or has no Postgres; the log carries the same text");
    } else {
        update.step("board notice not posted (the daemon did not answer or refused it); the log carries the same text");
    }
}

fn describe(peers: &[Value]) -> String {
    let mut names = peers
        .iter()
        .take(5)
        .map(|peer| {
            let label = ["label", "principal", "agent_id"]
                .into_iter()
                .find_map(|k| peer[k].as_str().filter(|s| !s.is_empty()))
                .unwrap_or("");
            let label: String = label.chars().take(40).filter(|c| !c.is_control()).collect();
            let task: String = peer["task"]
                .as_str()
                .unwrap_or("")
                .chars()
                .take(60)
                .filter(|c| !c.is_control())
                .collect();
            if task.is_empty() {
                label
            } else {
                format!("{label} ({task})")
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    if peers.len() > 5 {
        names.push_str(&format!(" and {} more", peers.len() - 5));
    }
    names
}

fn health_version(health: &Value) -> Result<String> {
    let version = health["version"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            UpdateError::usage("the daemon's /health carries no version; nothing was changed")
        })?;
    if !super::plain_release(version) {
        return Err(UpdateError::usage(
            "the daemon's /health reports a non-release version; install a published release before using unattended updates; nothing was changed",
        ));
    }
    Ok(version.to_owned())
}

/// The caller owns the final result file; this layer owns only its append log.
pub(crate) fn run_unattended(update: &mut Update) -> Result<u8> {
    let log = seams::data_dir().join(LOG_NAME);
    fs::create_dir_all(log.parent().unwrap())?;
    let mut handle = OpenOptions::new().append(true).create(true).open(&log)?;
    let started = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
    writeln!(
        handle,
        "[{started}] unattended run: checking the daemon, the newest release and the board"
    )?;
    // Core uses the same handle for transaction steps while the unattended run
    // is active, so headless failures retain backup and rollback diagnostics.
    let copy = handle.try_clone()?;
    LOG.with(|log| log.replace(Some(copy)));
    let _guard = LogGuard;
    let result = run_inner(update, board_activity);
    if let Err(error) = &result {
        let _ = writeln!(
            handle,
            "[{}] stopped: {error}",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S")
        );
    }
    result
}

fn run_inner(
    update: &mut Update,
    mut activity: impl FnMut(&str) -> std::result::Result<Activity, String>,
) -> Result<u8> {
    update.step("unattended run: checking the daemon, the newest release and the board");
    let tier = update.detect_tier()?;
    update.report.tier = tier.clone();
    if tier != "docker" {
        if on_shim_runtime() {
            return run_clients(update);
        }
        return Err(UpdateError::usage(
            "unattended updates apply to the Docker tier (a daemon container) or, on a client machine, its shim runtime; this install is neither. Run pseudolife-mcp update yourself. Nothing was changed",
        ));
    }
    let health = update.daemon_health().ok_or_else(|| {
        UpdateError::usage("the daemon did not answer /health; nothing was changed")
    })?;
    update.report.current = health["version"].as_str().map(str::to_owned);
    let latest = update
        .o
        .tag
        .clone()
        .or_else(|| update.latest_release())
        .ok_or_else(|| {
            UpdateError::usage("could not read the newest release; nothing was changed")
        })?;
    update.report.target = Some(latest.clone());
    let current = health_version(&health)?;
    if super::version_key(&latest).is_none() {
        return Err(UpdateError::usage(
            "the newest release number could not be understood; nothing was changed",
        ));
    }
    if !version_is_newer(&latest, &current) {
        update.step(format!(
            "current: {current} is the newest release; nothing to do"
        ));
        return Ok(3);
    }
    if health["updates"]["unattended_daemon"] != true {
        update.step(format!("held off: release {latest} is available (the daemon runs {current}) but updates.unattended_daemon is off in the daemon's config.yaml. Run pseudolife-mcp update yourself, or set the knob for the next scheduled run"));
        return Ok(4);
    }
    let url = update.o.daemon_url.clone();
    let board = match activity(&url) {
        Ok(board) if board.active.is_empty() => board,
        result => {
            let why = match result {
                Ok(board) => format!(
                    "{} session(s) are active on the board: {}{}",
                    board.active.len(),
                    describe(&board.active),
                    if board.idle > 0 {
                        format!(" ({} idle, not counted)", board.idle)
                    } else {
                        String::new()
                    }
                ),
                Err(reason) => format!(
                    "the board could not be read ({reason}), so it cannot tell whether a session is working"
                ),
            };
            let text = format!(
                "Pseudolife-MCP: release {latest} is available (the daemon runs {current}); the unattended update held off because {why}. It tries again at the next scheduled run; run pseudolife-mcp update yourself to take it now."
            );
            update.step(&text);
            notice(update, &text);
            return Ok(4);
        }
    };
    let _lock = match super::UpdateLock::acquire() {
        Ok(lock) => lock,
        Err(error) if busy(&error) => {
            let text = format!(
                "Pseudolife-MCP: release {latest} is available (the daemon runs {current}); the unattended update held off because {error}. It tries again at the next scheduled run."
            );
            update.step(&text);
            notice(update, &text);
            return Ok(4);
        }
        Err(error) => return Err(error),
    };
    let health=update.daemon_health().ok_or_else(||UpdateError::usage("the daemon did not answer /health after the update lock was taken; nothing was changed"))?;
    let current = health_version(&health)?;
    update.report.current = Some(current.clone());
    if !version_is_newer(&latest, &current) {
        update.step(format!(
            "current: the daemon already runs {current} (another update moved it); nothing to do"
        ));
        return Ok(3);
    }
    // A policy revoked while this run waited must take effect before movement.
    if health["updates"]["unattended_daemon"] != true {
        update.step("held off: updates.unattended_daemon is off after taking the update lock; nothing was changed");
        return Ok(4);
    }
    update.step(format!(
        "applying release {latest} unattended: no session is active on the board{}",
        if board.idle > 0 {
            format!(" ({} idle)", board.idle)
        } else {
            String::new()
        }
    ));
    let mut before_recreate = || recheck(activity(&url), &latest);
    update.schema_before = health.get("schema").and_then(Value::as_i64);
    let requested_tag = update.o.tag.replace(latest.clone());
    let deployed = update.deploy_release(
        Some(current.clone()),
        Some(&mut before_recreate),
        health
            .get("hooks_digest")
            .and_then(Value::as_str)
            .map(str::to_owned),
    );
    update.o.tag = requested_tag;
    if let Err(error) = deployed {
        let text = if error.exit_code == 4 {
            error.to_string()
        } else {
            format!(
                "Pseudolife-MCP: the unattended update to {latest} FAILED: {error}. The log is {}.{}",
                seams::data_dir().join(LOG_NAME).display(),
                if update.rollback_lines.is_empty()
                    || error.message.to_ascii_lowercase().contains("to roll back")
                {
                    String::new()
                } else {
                    format!("\nTo roll back:\n{}", update.rollback_lines.join("\n"))
                }
            )
        };
        update.step(&text);
        notice(update, &text);
        return Err(error);
    }
    let clients = update
        .report
        .clients
        .as_ref()
        .map(|report| {
            [("shim", "shim"), ("plugin", "plugin cache")]
                .into_iter()
                .filter_map(|(key, label)| {
                    report[key]["state"]
                        .as_str()
                        .map(|state| format!("{label} {state}"))
                })
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "clients not moved".into());
    let text = format!(
        "Pseudolife-MCP: the daemon was updated unattended from {current} to {latest} (backup taken; rollback tag {}; {clients}). Start a new session to run on the new shim runtime; the log is {}.{}",
        update
            .report
            .rollback
            .get("tag")
            .and_then(Value::as_str)
            .unwrap_or("none"),
        seams::data_dir().join(LOG_NAME).display(),
        if update.report.codex_reapproval.is_empty() {
            String::new()
        } else {
            format!("\n{}", update.report.codex_reapproval)
        }
    );
    update.step(&text);
    notice(update, &text);
    Ok(update.report.exit_code)
}

fn recheck(board: std::result::Result<Activity, String>, latest: &str) -> Result<()> {
    let why = match board {
        Ok(board) if board.active.is_empty() => return Ok(()),
        Ok(board) => format!(
            "{} session(s) became active during the backup: {}",
            board.active.len(),
            describe(&board.active)
        ),
        Err(reason) => format!("the board could not be read ({reason})"),
    };
    Err(UpdateError {
        message: format!(
            "Pseudolife-MCP: the unattended update to {latest} held off after the backup because {why}; the daemon was not recreated. It tries again at the next scheduled run."
        ),
        exit_code: 4,
    })
}

fn run_clients(update: &mut Update) -> Result<u8> {
    update.report.tier = "client".into();
    update.report.mode = "unattended-clients".into();
    if update.o.tag.is_some() {
        return Err(UpdateError::usage(
            "--tag cannot be combined with --unattended on a client machine: the unattended client run follows the daemon's release. Run pseudolife-mcp update --clients-only --tag <version> yourself for another one. Nothing was changed",
        ));
    }
    let health=update.daemon_health().ok_or_else(||UpdateError::usage("the daemon did not answer /health, and the unattended client run follows its release rather than guess one; nothing was changed. Check --daemon-url and try again"))?;
    let daemon=health["version"].as_str().filter(|s|crate::lifecycle::plain_client_release(s)).ok_or_else(||UpdateError::usage("the daemon at --daemon-url reports no plain release number (N.N.N) on /health; nothing was changed"))?.to_owned();
    let own = client_version();
    update.report.current = Some(own.clone());
    update.report.target = Some(daemon.clone());
    let incomplete = seams::data_dir().join(format!("client-update.{daemon}.incomplete"));
    let current = |own: &str| -> Result<bool> {
        let retry = match fs::symlink_metadata(&incomplete) {
            Ok(_) => true,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
            Err(_) => {
                return Err(UpdateError::usage(
                    "the previous client-update result cannot be inspected; restore access to the data directory and retry; nothing was changed",
                ));
            }
        };
        Ok(version_is_newer(own, &daemon)
            || (!version_is_newer(&daemon, own) && super::version_key(own).is_some() && !retry))
    };
    if current(&own)? {
        update.step(format!(
            "current: this machine's shim runs {own} and the daemon {daemon}; nothing to do"
        ));
        return Ok(3);
    }
    let _lock = match super::UpdateLock::acquire() {
        Ok(lock) => lock,
        Err(error) if busy(&error) => {
            update.step(format!(
                "held off: {error}; it tries again at the next scheduled run"
            ));
            return Ok(4);
        }
        Err(error) => return Err(error),
    };
    let own = client_version();
    update.report.current = Some(own.clone());
    if current(&own)? {
        update.step(format!("current: this machine's shim already runs {own} (another update moved it); nothing to do"));
        return Ok(3);
    }
    refuse_checkout_shadow(&daemon)?;
    update.step(format!("the daemon runs {daemon} and this machine's shim {own}: updating this machine's clients to {daemon} unattended"));
    // Persist intent before the first step: a crash after installing the shim
    // must still retry plugin and hook work on the next run.
    seams::write_atomic(&incomplete, format!("{own}\n").as_bytes())?;
    update.clients(None, &format!("pseudolife-mcp=={daemon}"), Some(&health));
    if update.report.ok {
        fs::remove_file(&incomplete)?;
        update.step(format!("updated this machine's clients unattended from {own} to {daemon}; running sessions keep their runtime and new sessions start on the new one. The log is {}",seams::data_dir().join(LOG_NAME).display()));
    }
    Ok(update.report.exit_code)
}

fn client_version() -> String {
    crate::runtime::default_layout()
        .ok()
        .and_then(|layout| crate::runtime::current_runtime(&layout))
        .map(|runtime| runtime.version)
        .filter(|s| super::version_key(s).is_some())
        .unwrap_or_else(|| crate::lifecycle::PACKAGE_VERSION.into())
}

fn refuse_checkout_shadow(target: &str) -> Result<()> {
    if let Some(runtime) = crate::runtime::default_layout()
        .ok()
        .and_then(|layout| crate::runtime::current_runtime(&layout))
        && runtime.version == target
        && (runtime.source_commit.is_some() || Path::new(&runtime.source).is_absolute())
    {
        return Err(UpdateError::usage(
            "the current shim runtime was built from a checkout at this version; a release would replace it for new sessions. Update from that checkout instead; nothing was changed",
        ));
    }
    Ok(())
}

fn busy(error: &UpdateError) -> bool {
    error.exit_code == 4
        || error
            .message
            .starts_with("another pseudolife-mcp update is running (pid ")
}

#[cfg(test)]
#[path = "unattended/tests.rs"]
mod tests;
