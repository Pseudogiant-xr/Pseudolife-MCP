//! The client-step hand-off (`client-handoff-v1`).
//!
//! A release-mode client update installs a runtime of another release in its
//! shim step. The steps after it (plugin cache, Codex hooks, tunnels) belong
//! to that release's code, so this process asks the new runtime to run them:
//! `<runtime> update --clients-only --tag <its version> --daemon-url <url>`,
//! with [`HANDOFF_ENV`] naming this process (which holds the update lock the
//! child borrows, and which stops the child handing off again) and
//! [`HANDOFF_REPORT_ENV`] naming the file the child writes its JSON report to.
//!
//! The environment and report contract is shared with the Python updater
//! (`pseudolife_memory/update_cli.py`, `Update._hand_off`): an installed
//! Python updater may start a native child and the reverse, so the names,
//! the pid format and the report's field names must not drift from it.
use super::command::Runner;
use crate::runtime;
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

pub const CLIENT_HANDOFF: &str = "client-handoff-v1";
/// The hand-off protocols this updater accepts as a child.
pub const PROTOCOLS: &[&str] = &[CLIENT_HANDOFF];
pub const HANDOFF_ENV: &str = "PSEUDOLIFE_UPDATE_HANDOFF";
pub const HANDOFF_REPORT_ENV: &str = "PSEUDOLIFE_UPDATE_HANDOFF_REPORT";
/// The handing-off updater's runtime directory. A Python child holds exactly
/// that runtime in its prune; a native child's prune already sees the parent
/// process's image, so it only reads and removes the variable.
pub const HANDOFF_RUNTIME_ENV: &str = "PSEUDOLIFE_UPDATE_HANDOFF_RUNTIME";
/// `update <PROBE>` prints this binary's version, then the protocols it
/// accepts, one line each (the shape of the Python probe's output).
pub const PROBE: &str = "--client-handoff-protocols";
/// Answered by every native updater, including those that predate
/// [`PROBE`]: it proves the runtime starts before its protocols are read.
const START_PROBE: &str = "--native-capability";
const START_PROBE_ANSWER: &str = "pseudolife-mcp native-update-v1";
/// Exit code when a client step failed after the daemon side succeeded.
pub const CLIENT_STEP_FAILED: u8 = 5;
const PROBE_TIMEOUT: Duration = Duration::from_secs(120);
/// No step in the child may run longer than its own timeouts; this only
/// bounds a child that hangs outside them.
const CHILD_TIMEOUT: Duration = Duration::from_secs(4 * 3600);

/// The probe's answer.
pub fn probe_text() -> String {
    format!("{}\n{}\n", env!("CARGO_PKG_VERSION"), PROTOCOLS.join(" "))
}

/// What the updater that started this process handed it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Inherited {
    /// The handing-off updater's pid: the update lock's holder.
    pub from: Option<String>,
    pub report: Option<PathBuf>,
}

/// Honoured only for an all-digit pid, as the Python child does.
pub fn inherited(from: Option<OsString>, report: Option<OsString>) -> Inherited {
    let from = from
        .and_then(|v| v.into_string().ok())
        .filter(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()));
    match from {
        Some(from) => Inherited {
            from: Some(from),
            report: report.filter(|r| !r.is_empty()).map(PathBuf::from),
        },
        None => Inherited::default(),
    }
}

/// Read the hand-off variables once and remove them, so nothing this run
/// starts (a tunnel refresh, `claude`, git) inherits them and no grandchild
/// mistakes itself for a handed-off run.
#[allow(unsafe_code)]
pub fn take_environment() -> Inherited {
    let from = std::env::var_os(HANDOFF_ENV);
    let report = std::env::var_os(HANDOFF_REPORT_ENV);
    for key in [HANDOFF_ENV, HANDOFF_REPORT_ENV, HANDOFF_RUNTIME_ENV] {
        if std::env::var_os(key).is_some() {
            // SAFETY: called once from `update::run`, the update command's
            // entry, before it starts any thread or child; nothing else reads
            // or writes the environment concurrently at that point.
            unsafe { std::env::remove_var(key) };
        }
    }
    inherited(from, report)
}

/// Whether this run hands the steps after the shim to a new runtime: only a
/// release-mode run (a checkout deploy already runs the checkout's own code)
/// that was not itself handed them (the marker stops a second hand-off).
pub fn hands_off(repo: Option<&Path>, inherited: &Inherited) -> bool {
    repo.is_none() && inherited.from.is_none()
}

/// What the parent passes to the child, and how it shows the run.
pub struct Parent<'a> {
    pub own_version: &'a str,
    pub daemon_url: &'a str,
    pub allow_downgrade: bool,
    /// `--json`: the child's output is dropped; its report is the result.
    pub quiet: bool,
    /// The command a failed hand-off names for the retry (this binary, not
    /// the launcher, which would start the runtime that just failed).
    pub retry_program: String,
    pub pid: u32,
    /// The runtime directory this process runs from, when it is one.
    pub runtime_dir: Option<PathBuf>,
}

/// The child's command line after the executable. Never `--reinstall`
/// (the parent's shim step already did that) nor `--result-file` (the
/// parent writes the result).
pub fn child_args(version: &str, daemon_url: &str, allow_downgrade: bool) -> Vec<String> {
    let mut args = [
        "update",
        "--clients-only",
        "--tag",
        version,
        "--daemon-url",
        daemon_url,
    ]
    .map(str::to_owned)
    .to_vec();
    if allow_downgrade {
        args.push("--allow-downgrade".into());
    }
    args
}

/// The variables the child reads, on top of this process's environment.
pub fn child_env(
    pid: u32,
    report: &Path,
    runtime_dir: Option<&Path>,
) -> Vec<(String, OsString)> {
    let mut env = vec![
        (HANDOFF_ENV.to_owned(), OsString::from(pid.to_string())),
        (HANDOFF_REPORT_ENV.to_owned(), report.as_os_str().to_owned()),
    ];
    if let Some(dir) = runtime_dir {
        env.push((HANDOFF_RUNTIME_ENV.to_owned(), dir.as_os_str().to_owned()));
    }
    env
}

/// Runs the handed-off child to completion: `Ok(exit code)`, or `Err(why)`
/// when it could not be started or contained.
pub trait Spawner {
    fn run(
        &self,
        executable: &Path,
        args: &[String],
        env: &[(String, OsString)],
        quiet: bool,
    ) -> Result<i32, String>;
}

/// What the parent shows while the hand-off proceeds, in order.
pub enum Note<'a> {
    Step(String),
    /// The shim row, printed before the child's own ladder.
    Shim(&'a Value),
}

/// The child's part of the run, folded into the parent's result.
#[derive(Debug, Clone, PartialEq)]
pub struct Outcome {
    pub runtime: String,
    pub version: String,
    pub exit_code: u8,
    pub steps: Vec<String>,
    pub codex: String,
}

/// After the shim step: `None` to run the remaining steps here (no runtime,
/// the same version, a target that predates the hand-off), else the ladder
/// for the run and, when the child ran, its outcome. A runtime that cannot
/// run the steps fails the ladder with the cause and the retry; this
/// process's older code never stands in for it.
pub fn hand_off(
    shim: &Value,
    parent: &Parent,
    probe: &impl Runner,
    spawner: &impl Spawner,
    note: &mut dyn FnMut(Note),
) -> Option<(Value, Option<Outcome>)> {
    let state = shim["state"].as_str().unwrap_or("");
    let (kind, state_version) = state.split_once(':').unwrap_or((state, ""));
    // A failed shim step that still installed the runtime (a registration
    // that would not migrate) hands off too: the new code retries it.
    if !matches!(kind, "installed" | "current" | "failed") {
        return None;
    }
    let path = PathBuf::from(shim["runtime"].as_str()?);
    if state_version == parent.own_version {
        return None;
    }
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let retry = |version: &str| {
        format!(
            "{} update --clients-only --tag {version} --reinstall",
            parent.retry_program
        )
    };
    let failed = |name: &str, version: &str, why: &str| -> Value {
        json!({"shim": shim, "handoff": {"state": "failed", "detail": format!(
            "the new shim runtime {name} ({version}) did not start for the steps after the shim ({why}), so the plugin cache, Codex hook and tunnel steps did not run; the runtime itself is installed. Retry, building the runtime again: {}",
            retry(version))}, "ok": false})
    };
    let runtime = match runtime::read_runtime(&path) {
        Ok(runtime) => runtime,
        Err(why) => {
            let version = if state_version.is_empty() {
                "unknown version"
            } else {
                state_version
            };
            return Some((
                failed(&name, version, &format!("its runtime could not be verified: {why}")),
                None,
            ));
        }
    };
    let version = runtime.version.clone();
    if version == parent.own_version {
        return None;
    }
    let executable = runtime.executable();
    let started = probe.run(&executable, &["update", START_PROBE], None, PROBE_TIMEOUT, &[]);
    if started.code != 0 || started.text().trim() != START_PROBE_ANSWER {
        let text = started.text();
        let last = text.trim().lines().last().unwrap_or("").trim();
        let why = if last.is_empty() {
            format!("exit {}", started.code)
        } else {
            format!("exit {}: {}", started.code, truncate(last, 200))
        };
        return Some((failed(&name, &version, &why), None));
    }
    let answer = probe.run(&executable, &["update", PROBE], None, PROBE_TIMEOUT, &[]);
    let text = answer.text();
    let lines: Vec<_> = text.trim().lines().map(str::trim).collect();
    let predates = match answer.code {
        0 => {
            if lines.first() != Some(&version.as_str()) {
                let why = match lines.first() {
                    Some(first) => format!("its probe reported {:?}, not {version}", truncate(first, 64)),
                    None => "its probe printed nothing".into(),
                };
                return Some((failed(&name, &version, &why), None));
            }
            !lines
                .get(1)
                .is_some_and(|l| l.split_whitespace().any(|p| p == CLIENT_HANDOFF))
        }
        // The usage refusal of an updater that has no such probe.
        2 => true,
        code => {
            return Some((
                failed(&name, &version, &format!("its protocol probe ended with exit {code}")),
                None,
            ));
        }
    };
    if predates {
        note(Note::Step(format!(
            "runtime {name} ({version}) predates the client hand-off, so this run's own code ({}) finishes the client steps",
            parent.own_version
        )));
        return None;
    }
    note(Note::Shim(shim));
    note(Note::Step(format!(
        "the client steps after the shim continue in runtime {name} ({version}), the release just installed"
    )));
    let scratch = std::env::temp_dir().join(format!("pseudolife-handoff-{}", uuid::Uuid::new_v4()));
    if crate::board::state::private_dir(&scratch).is_err() {
        return Some((
            failed(&name, &version, "no private scratch directory for its report"),
            None,
        ));
    }
    let report_file = scratch.join("report.json");
    let env = child_env(parent.pid, &report_file, parent.runtime_dir.as_deref());
    let args = child_args(&version, parent.daemon_url, parent.allow_downgrade);
    let ran = spawner.run(&executable, &args, &env, parent.quiet);
    let child: Option<Value> = fs::read(&report_file)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok());
    let _ = fs::remove_dir_all(&scratch);
    let code = match ran {
        Ok(code) => code,
        Err(why) => return Some((failed(&name, &version, &why), None)),
    };
    let Some(child) = child.filter(Value::is_object) else {
        return Some((
            failed(
                &name,
                &version,
                &format!("it ended with exit {code} without writing its report"),
            ),
            None,
        ));
    };
    let exit = u8::try_from(code.clamp(0, 255)).unwrap_or(1);
    let steps: Vec<String> = child["steps"]
        .as_array()
        .map(|s| {
            s.iter()
                .map(|v| v.as_str().map_or_else(|| v.to_string(), str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    let mut outcome = Outcome {
        runtime: name.clone(),
        version: version.clone(),
        exit_code: if exit == 0 { CLIENT_STEP_FAILED } else { exit },
        steps,
        codex: child["codex_reapproval"].as_str().unwrap_or("").to_owned(),
    };
    let Some(clients) = child["clients"].as_object() else {
        // It started, then stopped before its client step (its /health read,
        // its tier check, the update lock): its last warning is the cause.
        let why = outcome
            .steps
            .iter()
            .rev()
            .find_map(|s| s.strip_prefix("WARNING: "))
            .map(|s| truncate(s, 400).to_owned())
            .unwrap_or_else(|| "it gave no reason".into());
        return Some((
            json!({"shim": shim, "handoff": {"state": "failed", "detail": format!(
                "runtime {name} ({version}) started but stopped before the client steps (exit {code}): {why}")}, "ok": false}),
            Some(outcome),
        ));
    };
    outcome.exit_code = exit;
    let mut report = Value::Object(clients.clone());
    let child_shim = report["shim"]["state"].as_str().unwrap_or("");
    if kind == "installed" && child_shim.starts_with("current:") {
        // The install this run did, not the child's "already installed". A
        // child that installed again or failed keeps its own row.
        report["shim"] = shim.clone();
    }
    report["handoff"] = json!({"state": format!("handed-off:{version}"),
        "detail": format!("runtime {name} ({version}) ran the steps after the shim (exit {code})")});
    report["ok"] = json!(code == 0 && child["ok"] == true);
    Some((report, Some(outcome)))
}

fn truncate(text: &str, limit: usize) -> &str {
    match text.char_indices().nth(limit) {
        Some((index, _)) => &text[..index],
        None => text,
    }
}

/// Starts the child with stdin closed and this process's stdout and stderr
/// (dropped under `--json`), contained like every other command the update
/// runs: a Windows job that kills it if this process dies, a Unix process
/// group that receives the forwarded terminal signal.
pub struct NativeSpawner;
impl Spawner for NativeSpawner {
    fn run(
        &self,
        executable: &Path,
        args: &[String],
        env: &[(String, OsString)],
        quiet: bool,
    ) -> Result<i32, String> {
        use std::io::Write;
        #[cfg(unix)]
        let _signals = super::super::signals::Guard::new()
            .map_err(|_| "cannot install command cancellation handlers".to_owned())?;
        #[cfg(unix)]
        if super::super::signals::checkpoint() != 0 {
            return Err("the update was interrupted before the hand-off".into());
        }
        let _ = std::io::stdout().flush();
        let _ = std::io::stderr().flush();
        let executable = executable.to_owned();
        let args = args.to_vec();
        let env = env.to_vec();
        // The synchronous update can itself run under a Tokio runtime: the
        // dedicated thread owns the child and its cancellation cleanup.
        std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|e| format!("cannot start the hand-off runtime: {e}"))?;
            runtime.block_on(async move {
                use process_wrap::tokio::{CommandWrap, KillOnDrop};
                #[cfg(windows)]
                let job = super::super::windows_job::Job::new()
                    .map_err(|_| "cannot contain the new runtime's process".to_owned())?;
                let mut command = CommandWrap::with_new(&executable, |c| {
                    #[cfg(windows)]
                    c.creation_flags(windows_sys::Win32::System::Threading::CREATE_SUSPENDED);
                    c.args(&args).stdin(Stdio::null());
                    if quiet {
                        c.stdout(Stdio::null()).stderr(Stdio::null());
                    } else {
                        c.stdout(Stdio::inherit()).stderr(Stdio::inherit());
                    }
                    for (key, value) in &env {
                        c.env(key, value);
                    }
                });
                command.wrap(KillOnDrop);
                #[cfg(unix)]
                command.wrap(process_wrap::tokio::ProcessGroup::leader());
                let mut child = command.spawn().map_err(|e| {
                    if e.kind() == std::io::ErrorKind::NotFound {
                        format!("{} is missing", executable.display())
                    } else {
                        format!("{} could not be started: {e}", executable.display())
                    }
                })?;
                #[cfg(windows)]
                if child
                    .try_inner_child()
                    .ok_or_else(|| std::io::Error::other("no native child"))
                    .and_then(|inner| job.assign(inner))
                    .is_err()
                {
                    let _ = child.start_kill();
                    let _ = child.wait().await;
                    return Err("cannot contain the new runtime's process".into());
                }
                #[cfg(unix)]
                let group = child.id();
                let wait = async { child.wait().await };
                #[cfg(unix)]
                let result = tokio::select! {
                    biased;
                    signal = super::super::signals::wait(group) => {
                        super::super::signals::terminate(&mut *child, signal).await;
                        return Err(format!("the update was interrupted (signal {signal})"));
                    }
                    result = super::super::signals::timeout(CHILD_TIMEOUT, wait) => result,
                };
                #[cfg(not(unix))]
                let result = tokio::time::timeout(CHILD_TIMEOUT, wait).await;
                match result {
                    Ok(Ok(status)) => {
                        // Release the kill-on-close limit only after a normal
                        // exit, so a tunnel the child started outlives it.
                        #[cfg(windows)]
                        job.complete()
                            .map_err(|_| "cannot release the new runtime's helpers".to_owned())?;
                        Ok(status.code().unwrap_or(1))
                    }
                    Ok(Err(e)) => Err(format!("waiting for it failed: {e}")),
                    Err(_) => {
                        #[cfg(windows)]
                        drop(job);
                        let _ = child.start_kill();
                        let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
                        Err(format!(
                            "it ran longer than {} hours",
                            CHILD_TIMEOUT.as_secs() / 3600
                        ))
                    }
                }
            })
        })
        .join()
        .map_err(|_| "the hand-off worker failed".to_owned())?
    }
}

#[cfg(test)]
#[path = "handoff_tests.rs"]
mod tests;
