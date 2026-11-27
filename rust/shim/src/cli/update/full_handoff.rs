//! The full hand-off (`full-update-v1`).
//!
//! A release-mode update whose target release is newer than this updater
//! runs the whole update, daemon step included, with the target's own code:
//! before any daemon, backup or engine step it downloads the target's client,
//! checks it against the release's `SHA256SUMS`, writes it to a private
//! staging folder (not a runtime: the stable launcher starts the newest
//! complete runtime, and nothing may switch the clients before the daemon
//! step), probes it, and runs `<staged> update <the user's arguments> --tag
//! <target>` under this process's update lock. The child is marked with the
//! client hand-off's variables ([`handoff::HANDOFF_ENV`] and the report
//! file), so it borrows the lock and never hands off again; its own shim
//! step installs the target as a runtime and switches the launcher.
//!
//! If the client cannot be fetched, checked or started, or predates this
//! protocol, the run says so in one line and continues on this updater's
//! code. Once the child has started, its result is the run's: this process
//! changes nothing after it.
use super::clients::handoff::{self, FullRun, Spawned};
use super::clients::{NativeRunner, Runner};
use super::{Update, UpdateLock, version_key};
use crate::cli::lease::lock as os_lock;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

/// The staging folder under the runtimes folder: `<runtimes>/.staged/<id>/`.
/// The launcher selects only six-digit runtime folders, and a binary whose
/// grandparent is not the runtimes folder runs as itself.
pub(super) const STAGED: &str = ".staged";

/// The child's command line after the executable: the user's arguments,
/// without `--result-file` (this process writes it from the child's report),
/// with `--tag <target>` pinned unless the user named one (`tagged`, as
/// `Options::parse` read it). A value-taking option's value is never read as
/// an option, as the parser does not.
pub(super) fn child_args(argv: &[String], target: &str, tagged: bool) -> Vec<String> {
    let mut args = vec!["update".to_owned()];
    let mut rest = argv.iter();
    while let Some(arg) = rest.next() {
        if arg == "--result-file" {
            rest.next();
            continue;
        }
        if arg.starts_with("--result-file=") {
            continue;
        }
        args.push(arg.clone());
        if super::options::VALUE_OPTIONS.contains(&arg.as_str())
            && let Some(value) = rest.next()
        {
            args.push(value.clone());
        }
    }
    if !tagged {
        args.extend(["--tag".to_owned(), target.to_owned()]);
    }
    args
}

/// What became of the attempt.
#[derive(Debug, PartialEq)]
pub(super) enum Attempt {
    /// Nothing was started: the one-line note, then this run's own code.
    Fallback(String),
    /// The target's updater ran: its exit code, its report when it wrote one,
    /// and why waiting for it failed when it did.
    Ran {
        code: i32,
        report: Option<Value>,
        failure: Option<String>,
    },
}

/// Everything [`attempt`] passes to the child.
pub(super) struct Child<'a> {
    pub target: &'a str,
    pub own: &'a str,
    pub folder: &'a Path,
    pub argv: &'a [String],
    pub tagged: bool,
    pub env: Vec<(String, std::ffi::OsString)>,
    pub quiet: bool,
}

/// Checks the staged client and runs it. `stage` writes the client into the
/// folder it is given and returns its path; `announce` shows the one line
/// that says the child takes over, just before it starts.
pub(super) fn attempt(
    child: Child,
    stage: impl FnOnce(&Path) -> Result<PathBuf, String>,
    probe: &impl Runner,
    spawner: &impl FullRun,
    announce: &mut dyn FnMut(String),
) -> Attempt {
    let Child {
        target,
        own,
        folder,
        ..
    } = child;
    let continues = format!("this update continues with {own}'s code");
    let executable = match stage(folder) {
        Ok(executable) => executable,
        Err(why) => {
            return Attempt::Fallback(format!(
                "could not fetch release {target}'s client to run this update with its own code ({why}); {continues}"
            ));
        }
    };
    let not_started = |why: String| {
        Attempt::Fallback(format!(
            "release {target}'s client did not start for this update ({why}); {continues}"
        ))
    };
    let started = probe.run(
        &executable,
        &["update", handoff::START_PROBE],
        None,
        handoff::PROBE_TIMEOUT,
        &[],
    );
    if started.code != 0 || started.text().trim() != handoff::START_PROBE_ANSWER {
        let text = started.text();
        let last = text.trim().lines().last().unwrap_or("").trim();
        return not_started(if started.code == handoff::WINDOWS_DLL_NOT_FOUND {
            "a system library is missing, exit 0xC0000135".into()
        } else if last.is_empty() {
            format!("exit {}", started.code)
        } else {
            format!("exit {}: {}", started.code, handoff::truncate(last, 200))
        });
    }
    let answer = probe.run(
        &executable,
        &["update", handoff::PROBE],
        None,
        handoff::PROBE_TIMEOUT,
        &[],
    );
    let text = answer.text();
    let lines: Vec<_> = text.trim().lines().map(str::trim).collect();
    if answer.code != 0 {
        return not_started(format!(
            "its protocol probe ended with exit {}",
            answer.code
        ));
    }
    if lines.first() != Some(&target) {
        return not_started(match lines.first() {
            Some(first) => format!(
                "its probe reported {:?}, not {target}",
                handoff::truncate(first, 64)
            ),
            None => "its probe printed nothing".into(),
        });
    }
    if !lines
        .get(1)
        .is_some_and(|l| l.split_whitespace().any(|p| p == handoff::FULL_UPDATE))
    {
        return Attempt::Fallback(format!(
            "release {target}'s client predates running the whole update itself; {continues}"
        ));
    }
    let args = child_args(child.argv, target, child.tagged);
    announce(format!(
        "release {target} is newer than this updater ({own}): its own updater, fetched and checked against the release's SHA256SUMS, runs this update"
    ));
    let report = || {
        fs::read(folder.join(REPORT))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .filter(Value::is_object)
    };
    match spawner.run(&executable, &args, &child.env, child.quiet) {
        Spawned::Exited(code) => Attempt::Ran {
            code,
            report: report(),
            failure: None,
        },
        // It never ran (it could not be started, or Windows refused to
        // contain it while it was still suspended): nothing changed.
        Spawned::NotStarted(why) => not_started(why),
        // It ran, and waiting for it to the end failed: its result is the
        // run's, never this code's second attempt.
        Spawned::Failed(why) => Attempt::Ran {
            code: -1,
            report: report(),
            failure: Some(why),
        },
    }
}

/// The report file the child writes, inside the staging folder.
pub(super) const REPORT: &str = "report.json";

/// The staging folder, removed on every way out of this run. A leftover from
/// a run that was killed is removed by the next run that stages, but only
/// once its own lock (`<id>.lock` beside it, held for the whole run) is free:
/// an update of another data directory can share these runtimes.
pub(super) struct Staging {
    folder: PathBuf,
    lock: os_lock::Lock,
}
impl Staging {
    pub(super) fn create(runtimes: &Path) -> Result<Self, String> {
        let private = |path: &Path| crate::board::state::private_dir(path).map_err(str::to_owned);
        private(runtimes)?;
        let root = runtimes.join(STAGED);
        private(&root)?;
        if let Ok(entries) = fs::read_dir(&root) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().is_some_and(|e| e == "lock") {
                    continue;
                }
                let lock_path = path.with_extension("lock");
                let mut lock = os_lock::Lock::new(lock_path.clone());
                if lock.acquire().unwrap_or(false) {
                    let _ = fs::remove_dir_all(&path);
                    lock.release();
                    let _ = fs::remove_file(&lock_path);
                }
            }
        }
        let folder = root.join(uuid::Uuid::new_v4().simple().to_string());
        let mut lock = os_lock::Lock::new(folder.with_extension("lock"));
        if !lock.acquire().unwrap_or(false) {
            return Err(format!(
                "could not lock a new staging folder in {}",
                root.display()
            ));
        }
        private(&folder)?;
        Ok(Self { folder, lock })
    }
    pub(super) fn path(&self) -> &Path {
        &self.folder
    }
}
impl Drop for Staging {
    fn drop(&mut self) {
        // Windows can hold a just-exited image open for a moment.
        for attempt in 0..10 {
            if fs::remove_dir_all(&self.folder).is_ok() || !self.folder.exists() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50 * (attempt + 1)));
        }
        self.lock.release();
        if !self.folder.exists() {
            let _ = fs::remove_file(self.folder.with_extension("lock"));
        }
    }
}

impl Update {
    /// The release this run hands the whole update to, or `None` to run it
    /// here: a release-mode Docker-tier update (not `--check`, a schedule
    /// change, `--clients-only`, a checkout deploy or a run handed this one)
    /// whose target is newer than this updater, on a host with no pending
    /// legacy import and no checkout engine. An unattended run with no
    /// daemon step to hand over (its daemon already runs the target or
    /// newer, or `updates.unattended_daemon` is off) stays here too, so a
    /// scheduled run does not fetch the client every night for nothing.
    pub(super) fn full_handoff_target(&mut self) -> Option<String> {
        let o = &self.o;
        if o.handoff.from.is_some()
            || o.checkout.is_some()
            || o.clients_only
            || o.check
            || o.schedule.is_some()
            || o.unschedule
            || o.shadow
        {
            return None;
        }
        let own = env!("CARGO_PKG_VERSION");
        // A named tag is checked before anything is asked of Docker; release
        // discovery (the network) only once the tier is known.
        if let Some(tag) = &o.tag
            && !newer(tag, own)
        {
            return None;
        }
        if self.detect_tier().ok()? != "docker" {
            return None;
        }
        let target = self.target_version().ok()??;
        if !newer(&target, own) {
            return None;
        }
        if !matches!(super::engine::selected_checkout(self), Ok(None)) || self.import_pending() {
            return None;
        }
        if self.o.unattended {
            let health = self.daemon_health();
            let at_target = health
                .as_ref()
                .and_then(|h| h["version"].as_str())
                .is_some_and(|running| !newer(&target, running));
            let knob_off = health
                .as_ref()
                .is_some_and(|h| h["updates"]["unattended_daemon"] != true);
            if at_target || knob_off {
                return None;
            }
        }
        Some(target)
    }

    /// Runs the whole update with release `target`'s own updater; `true` when
    /// it ran (its result is now this run's), `false` to continue here.
    pub(super) fn full_handoff(&mut self) -> bool {
        let Some(target) = self.full_handoff_target() else {
            return false;
        };
        let own = env!("CARGO_PKG_VERSION");
        // Held until the child exits; the child borrows it. A busy lock is
        // left to this run's own code, which reports it as it always has.
        let Ok(lock) = UpdateLock::acquire() else {
            return false;
        };
        let layout = crate::runtime::default_layout();
        let staging = layout
            .as_ref()
            .map_err(Clone::clone)
            .and_then(|layout| Staging::create(&layout.root));
        let staging = match staging {
            Ok(staging) => staging,
            Err(why) => {
                self.note(format!(
                    "could not prepare a folder for release {target}'s client ({why}); this update continues with {own}'s code"
                ));
                return false;
            }
        };
        // The child keeps the lock's open file even if this process is
        // killed, so no second update can start while it runs (Windows kills
        // the child with this process instead, through its job).
        lock.inherit_by_children();
        let report_file = staging.path().join(REPORT);
        let runtime_dir = layout
            .ok()
            .and_then(|layout| crate::runtime::running_runtime(&layout))
            .map(|runtime| runtime.path);
        let env = handoff::child_env(std::process::id(), &report_file, runtime_dir.as_deref());
        let argv = self.o.argv.clone();
        let child = Child {
            target: &target,
            own,
            folder: staging.path(),
            argv: &argv,
            tagged: self.o.tag.is_some(),
            env,
            quiet: self.o.as_json,
        };
        let quiet = self.o.as_json;
        let attempt = attempt(
            child,
            |folder| crate::runtime::stage_release(&target, folder),
            &NativeRunner,
            &handoff::FullSpawner,
            &mut |note| self.note(note),
        );
        let (code, report, failure) = match attempt {
            Attempt::Fallback(note) => {
                self.note(note);
                return false;
            }
            Attempt::Ran {
                code,
                report,
                failure,
            } => (code, report, failure),
        };
        drop(staging);
        drop(lock);
        self.adopt(&target, code, report, failure, quiet);
        true
    }

    /// A step that an unattended run also writes to its log: the hand-off
    /// runs before the unattended run opens it.
    fn note(&mut self, text: String) {
        if self.o.unattended {
            super::unattended::log_line(&text);
        }
        self.step(text);
    }

    /// Makes the child's report this run's: its steps after this run's, its
    /// exit code (the process's), its Codex steps.
    pub(super) fn adopt(
        &mut self,
        target: &str,
        code: i32,
        report: Option<Value>,
        failure: Option<String>,
        quiet: bool,
    ) {
        let exit = match (&failure, u8::try_from(code)) {
            (None, Ok(code)) => code,
            _ => 1,
        };
        let mut merged = match report {
            Some(report) => {
                let mut steps: Vec<Value> =
                    self.report.steps.iter().cloned().map(Value::from).collect();
                if let Some(child) = report["steps"].as_array() {
                    steps.extend(child.iter().cloned());
                }
                let mut report = report;
                report["steps"] = Value::from(steps);
                report
            }
            None => {
                let seen = if quiet {
                    "; run the update again without --json to see its output"
                } else {
                    "; its output is above"
                };
                self.warn(format!(
                    "release {target}'s updater ended with exit {code} without writing its report{seen}"
                ));
                self.report.ok = false;
                serde_json::to_value(&self.report).unwrap_or_else(|_| json!({}))
            }
        };
        if let Some(why) = failure {
            let text = format!(
                "this run stopped waiting for release {target}'s updater ({why}); what it changed is what its own steps above report"
            );
            self.warn(text.clone());
            if let Some(steps) = merged["steps"].as_array_mut() {
                steps.push(Value::from(format!("WARNING: {text}")));
            }
            merged["ok"] = json!(false);
        }
        merged["exit_code"] = json!(exit);
        if merged["ok"].is_null() {
            merged["ok"] = json!(exit == 0);
        }
        self.report.exit_code = exit;
        self.report.ok = merged["ok"] == true;
        self.report.codex_reapproval = merged["codex_reapproval"].as_str().unwrap_or("").to_owned();
        self.handed_steps = self.report.steps.len();
        self.handed = Some(merged);
    }

    /// Whether a legacy import is pending or its state cannot be read; read
    /// only (the run's own code retires a finished pointer).
    pub(super) fn import_pending(&self) -> bool {
        !matches!(self.inspect_import(), Ok(None))
    }
}

fn newer(candidate: &str, than: &str) -> bool {
    matches!((version_key(candidate), version_key(than)), (Some(a), Some(b)) if a > b)
}

#[cfg(test)]
#[path = "full_handoff_tests.rs"]
mod tests;
