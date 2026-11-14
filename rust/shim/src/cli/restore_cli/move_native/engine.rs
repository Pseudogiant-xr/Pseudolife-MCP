//! The move's transaction and its rollback.
//!
//! Every effect is preceded by a flag written to the move record (a set
//! flag means "may have happened"), and every target operation goes to the
//! target's helper (protocol 2), whose operations are safe to repeat. Two
//! invariants hold at every step:
//!
//! - **I1:** the target daemon never runs on the moved bank while the
//!   source daemon runs on it.
//! - **I2:** the source's database fence is lifted, and the source
//!   started, only after a fresh, positive answer from the target that its
//!   daemon is stopped and gated. A lost link or an unreadable answer is
//!   never that answer: the source then stays down, the record lists the
//!   manual rollback, and the move exits 6.
//!
//! Before the commit point (`moved.json` written into the stopped source) a
//! failure or an interrupt rolls back; after it, nothing is rolled back and
//! a failed follow-up exits 5. The design and its step numbers (N1-N22,
//! R1-R16) are in the PR's design document; Python's transaction
//! (`pseudolife_memory/move_cli.py`) is the reference.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::Value;

use super::protocol::{Answer, Op, PROTOCOL, Request, State};
use super::record::{Flags, Phase, RECORD_VERSION, Record, Undo};

pub(super) const EXIT_OK: u8 = 0;
pub(super) const EXIT_ROLLED_BACK: u8 = 1;
pub(super) const EXIT_REFUSED: u8 = 4;
pub(super) const EXIT_FOLLOW_UPS: u8 = 5;
pub(super) const EXIT_INCOMPLETE: u8 = 6;

/// How long the target may take to serve the moved bank (Python's
/// `health_timeout`, 180 s) and how often it is asked.
const IDENTITY_WAIT: Duration = Duration::from_secs(180);
const IDENTITY_POLL: Duration = Duration::from_secs(2);
/// How long the restarted source may take to answer its health check.
const SOURCE_WAIT: Duration = Duration::from_secs(120);
/// How long the rollback waits for an operation that holds the target's
/// update lock (the restore's own limit), how often it asks, and how many
/// times it retries a lost link.
const ABORT_BUSY_WAIT: Duration = Duration::from_secs(4 * 60 * 60);
const ABORT_PAUSE: Duration = Duration::from_secs(15);
const ABORT_LOST_TRIES: u32 = 3;

/// The final dump: where it is, its digest, and each table's row count.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct Dump {
    pub path: PathBuf,
    pub sha256: String,
    pub bytes: u64,
    pub counts: BTreeMap<String, i64>,
}

/// A file the move copies: where it is, its digest and size.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct Artifact {
    pub path: PathBuf,
    pub sha256: String,
    pub bytes: u64,
}

/// What the engine asks of the two hosts. The real one runs Docker here and
/// the helper over ssh; the tests' model keeps both hosts' state and checks
/// the invariants after every call.
pub(super) trait Hosts {
    /// Stop the source daemon (`docker stop -t 120`) and read back that it
    /// stopped.
    fn source_stop(&mut self) -> Result<(), String>;
    fn source_start(&mut self) -> Result<(), String>;
    /// Whether the source daemon runs, read fresh (`None`: unreadable).
    fn source_running(&mut self) -> Option<bool>;
    fn source_healthy(&mut self, wait: Duration) -> bool;
    fn source_policy(&mut self, policy: &str) -> Result<(), String>;
    /// Enable or disable this host's unattended update.
    fn schedule(&mut self, enable: bool) -> Result<(), String>;
    /// Terminate the database's other sessions, then take the verified dump.
    fn dump(&mut self, dir: &Path) -> Result<Dump, String>;
    /// `ALTER DATABASE ... ALLOW_CONNECTIONS false` (`on`) or `true`.
    fn fence(&mut self, on: bool) -> Result<(), String>;
    /// `ALTER DATABASE ... SET default_transaction_read_only = on` (`on`),
    /// or its `RESET`.
    fn read_only(&mut self, on: bool) -> Result<(), String>;
    /// The state archive (without `move.json` and `moved.json`).
    fn archive(&mut self, dir: &Path) -> Result<Artifact, String>;
    /// Write a file of the move's here (the manifest).
    fn write_file(&mut self, path: &Path, bytes: &[u8]) -> Result<Artifact, String>;
    /// Write `/data/moved.json` into the stopped source (`Some`), or
    /// remove it (`None`).
    fn moved(&mut self, body: Option<&str>) -> Result<(), String>;
    /// The move ID in the source's `/data/moved.json` (`None`: no such
    /// file); `Err` when it cannot be read.
    fn moved_marker(&mut self) -> Result<Option<String>, String>;
    /// One helper operation on the target. `Err` is a lost link or an
    /// unreadable answer: the operation may or may not have run.
    fn target(&mut self, request: &Request, payload: Option<&Path>) -> Result<Answer, String>;
    /// `GET <url>/health` from this host.
    fn target_health(&mut self, url: &str) -> Option<Value>;
    /// Point this host's clients at the target (`connect <url> --yes`).
    fn repoint(&mut self, url: &str) -> Result<(), String>;
    /// Write the record whole.
    fn save(&mut self, record: &Record) -> Result<(), String>;
    /// Whether the user asked the move to stop.
    fn interrupted(&mut self) -> bool;
    fn now(&mut self) -> String;
    fn sleep(&mut self, pause: Duration);
    /// One progress line for the user.
    fn say(&mut self, line: &str);
}

/// What the preflight found, which the transaction needs.
#[derive(Debug, Clone, Default)]
pub(super) struct Plan {
    pub to: String,
    /// The source bank as the preflight found it (kept in the record).
    pub source: super::probe::Facts,
    pub target_image: String,
    pub target_url: String,
    pub work_dir: String,
    pub source_bank: String,
    pub source_policy: String,
    /// This host's unattended update is enabled.
    pub schedule_enabled: bool,
    /// The command that re-enables it, for a person.
    pub schedule_enable: String,
    pub target_policy: String,
    /// The target's update timer is enabled.
    pub target_timer_enabled: bool,
    pub target_running: bool,
    /// This host's folder for the move's files.
    pub local_dir: PathBuf,
    /// `--resume`: the target may already hold this move's restored bank.
    pub resume: bool,
}

/// How the move ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Outcome {
    pub code: u8,
    /// The final message: what happened and, for 6, the four facts.
    pub message: String,
}

/// Why the transaction stopped before its commit point.
struct Stop {
    reason: String,
    /// The stop came before anything was changed.
    untouched: bool,
}

impl Stop {
    fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
            untouched: false,
        }
    }
}

struct Run<'a, H: Hosts> {
    hosts: &'a mut H,
    plan: &'a Plan,
    record: Record,
    /// The last fresh reading of the target.
    last_state: Option<State>,
}

impl<H: Hosts> Run<'_, H> {
    /// Sets flags and writes the record before the effect they name.
    fn mark(&mut self, change: impl FnOnce(&mut Flags)) -> Result<(), Stop> {
        change(&mut self.record.flags);
        self.save()
    }

    fn save(&mut self) -> Result<(), Stop> {
        self.record.updated_at = self.hosts.now();
        self.record.manual_rollback = manual_rollback(&self.record);
        self.hosts.save(&self.record).map_err(Stop::new)
    }

    fn check_interrupt(&mut self) -> Result<(), Stop> {
        if self.hosts.interrupted() {
            return Err(Stop::new("the move was interrupted"));
        }
        Ok(())
    }

    fn request(&self, op: Op) -> Request {
        Request {
            protocol: PROTOCOL,
            move_id: self.record.move_id.clone(),
            work_dir: self.plan.work_dir.clone(),
            rollback: false,
            op,
        }
    }

    /// One target operation that must succeed. A lost link, a refusal and
    /// an unreadable answer all stop the move.
    fn target(&mut self, op: Op, payload: Option<&Path>) -> Result<Answer, Stop> {
        let name = op.name();
        let request = self.request(op);
        match self.hosts.target(&request, payload) {
            Ok(answer) => {
                if let Some(state) = &answer.state {
                    self.last_state = Some(state.clone());
                    if !state.folder.is_empty() {
                        self.record.remote_folder = state.folder.clone();
                        self.record.remote_bytes = state.folder_bytes;
                    }
                }
                if answer.ok {
                    Ok(answer)
                } else {
                    Err(Stop::new(format!(
                        "the target refused {name}: {}",
                        answer.error
                    )))
                }
            }
            Err(lost) => Err(Stop::new(format!(
                "the target's {name} did not answer ({lost}); its outcome is unknown"
            ))),
        }
    }

    fn step(&mut self, line: &str) -> Result<(), Stop> {
        self.check_interrupt()?;
        self.hosts.say(line);
        Ok(())
    }

    /// N3 to N21: everything before the commit point.
    fn forward(&mut self) -> Result<(), Stop> {
        let plan = self.plan;
        // The claim comes first: a target another move holds is refused
        // before anything is changed.
        self.step("claim the target for this move")?;
        self.mark(|f| f.claimed = true)?;
        let request = self.request(Op::Claim {
            source_bank: plan.source_bank.clone(),
            resume: plan.resume,
        });
        match self.hosts.target(&request, None) {
            Ok(answer) if answer.ok => {}
            Ok(answer) => {
                // Refused: the claim was not taken, so nothing changed.
                self.record.flags.claimed = false;
                self.save()?;
                return Err(Stop {
                    reason: format!("the target cannot take this move: {}", answer.error),
                    untouched: true,
                });
            }
            Err(lost) => {
                return Err(Stop::new(format!(
                    "the target's claim did not answer ({lost}); its outcome is unknown"
                )));
            }
        }
        if plan.target_timer_enabled {
            self.step("pause the target's unattended update")?;
            self.mark(|f| f.target_timer_paused = true)?;
            self.target(Op::TimerPause, None)?;
        }
        if !matches!(plan.target_policy.as_str(), "" | "no") {
            self.step("pause the target daemon's restart policy")?;
            self.mark(|f| f.target_policy_paused = true)?;
            // The exact policy it replaced (`on-failure:N` keeps its count).
            let answer = self.target(Op::PolicyNo, None)?;
            if !answer.policy.is_empty() {
                self.record.target_policy = answer.policy;
                self.save()?;
            }
        }
        if plan.schedule_enabled {
            self.step("pause this host's unattended update")?;
            self.mark(|f| f.schedule_paused = true)?;
            self.hosts.schedule(false).map_err(Stop::new)?;
        }
        // Before the stop, so neither a Docker restart nor a reboot starts
        // the stopped source during the dump or against its fence.
        if !matches!(plan.source_policy.as_str(), "" | "no") {
            self.step("set the source daemon's restart policy to no")?;
            self.mark(|f| f.source_policy_changed = true)?;
            self.hosts.source_policy("no").map_err(Stop::new)?;
        }
        self.step("stop the source daemon")?;
        self.mark(|f| f.source_stopped = true)?;
        self.record.phase = Phase::Moving;
        self.save()?;
        self.hosts.source_stop().map_err(Stop::new)?;
        // No new session may write between the dump and the fence (a lease
        // break, a board audit, the Console): what it wrote would be in
        // neither the dump nor the target.
        self.step("make the source database read-only for new sessions")?;
        self.mark(|f| f.read_only = true)?;
        self.hosts.read_only(true).map_err(Stop::new)?;
        self.step("take the final backup of the stopped source")?;
        let dump = self.hosts.dump(&plan.local_dir).map_err(Stop::new)?;
        self.record.dump_counts = dump.counts.clone();
        self.save()?;
        // The fence follows the dump: it refuses pg_dump's own connection.
        self.step("fence the source database")?;
        self.mark(|f| f.fenced = true)?;
        self.hosts.fence(true).map_err(Stop::new)?;
        self.step("archive the source's state")?;
        let state = self.hosts.archive(&plan.local_dir).map_err(Stop::new)?;
        let manifest = serde_json::json!({
            "move_id": self.record.move_id,
            "source_bank": plan.source_bank,
            "dump": file_name(&dump.path),
            "dump_sha256": dump.sha256,
            "state": file_name(&state.path),
            "state_sha256": state.sha256,
            "tables": dump.counts,
        });
        let manifest = self
            .hosts
            .write_file(
                &plan.local_dir.join("manifest.json"),
                format!("{manifest:#}\n").as_bytes(),
            )
            .map_err(Stop::new)?;
        let files = [
            Artifact {
                path: dump.path.clone(),
                sha256: dump.sha256.clone(),
                bytes: dump.bytes,
            },
            state,
            manifest,
        ];
        for file in &files {
            self.step(&format!("copy {} to the target", file_name(&file.path)))?;
            let op = Op::Receive {
                name: file_name(&file.path),
                sha256: file.sha256.clone(),
                bytes: file.bytes,
            };
            // A receive is safe to repeat: one more try after a lost link.
            if self.target(op.clone(), Some(&file.path)).is_err() {
                self.target(op, Some(&file.path))?;
            }
        }
        self.step("stop the target daemon")?;
        let running = plan.target_running;
        self.mark(|f| f.target_quiesced = running)?;
        let stopped = self.target(Op::Stop, None)?;
        if stopped.state.as_ref().and_then(|s| s.running) != Some(false) {
            return Err(Stop::new("the target daemon did not read back as stopped"));
        }
        self.step("check the target's bank is still empty")?;
        let state = self.target(Op::State { tables: Vec::new() }, None)?;
        let state = state.state.unwrap_or_default();
        // A resumed move replaces its own earlier restored bank; any other
        // used bank is never overwritten.
        let own = plan.resume && state.bank.as_deref() == Some(plan.source_bank.as_str());
        if state.empty != Some(true) && !own {
            return Err(Stop::new(
                "the target's bank is no longer empty; move never overwrites a used bank",
            ));
        }
        self.step("archive the target's own state")?;
        self.target(Op::Snapshot, None)?;
        // The gate goes in before the restore, which keeps it: the restored
        // bank never exists without it.
        self.step("gate the target (/data/move.json)")?;
        self.mark(|f| f.gate_written = true)?;
        self.target(Op::Gate, None)?;
        self.step("restore the target from the copied backup")?;
        self.mark(|f| f.target_restored = true)?;
        let request = self.request(Op::Restore);
        match self.hosts.target(&request, None) {
            Ok(answer) if answer.ok => {}
            Ok(answer) => {
                if !answer.restore_dropped {
                    // Refused before its destructive step: nothing changed.
                    self.record.flags.target_restored = false;
                    self.save()?;
                }
                return Err(Stop::new(format!(
                    "the target's restore failed: {}",
                    answer.error
                )));
            }
            Err(lost) => {
                return Err(Stop::new(format!(
                    "the target's restore did not answer ({lost}); its outcome is unknown"
                )));
            }
        }
        self.step("check the restored bank")?;
        let tables = self.record.dump_counts.keys().cloned().collect();
        let state = self.target(Op::State { tables }, None)?;
        let state = state.state.unwrap_or_default();
        if state.bank.as_deref() != Some(plan.source_bank.as_str()) {
            return Err(Stop::new(format!(
                "the restored target bank is {:?}, not the source's {}",
                state.bank, plan.source_bank
            )));
        }
        let differs = differing(&self.record.dump_counts, &state.counts);
        if !differs.is_empty() {
            return Err(Stop::new(format!(
                "the restored target's row counts differ from the dump's ({})",
                differs.join(", ")
            )));
        }
        self.record.restored_counts = state.user_counts.clone();
        // What would show a write in place: without it, writes the target
        // takes while it serves could not be ruled out, so it never starts.
        let Some(mutations) = state.mutations.clone() else {
            return Err(Stop::new(
                "the restored target's write counters (pg_stat_user_tables) cannot be read, so writes it took while it served could not be found; the target was not started",
            ));
        };
        self.record.restored_mutations = Some(mutations);
        self.save()?;
        self.step("lift the target's gate and start it once")?;
        self.mark(|f| f.gate_lifting = true)?;
        self.target(Op::StartGated, None)?;
        self.mark(|f| f.target_started = true)?;
        self.step("wait for the target to serve the moved bank")?;
        self.wait_identity()?;
        // And from the target itself: its daemon runs, ungated, on this bank
        // (the URL alone could reach another daemon of the same bank).
        let state = self.target(Op::State { tables: Vec::new() }, None)?;
        let state = state.state.unwrap_or_default();
        if state.running != Some(true)
            || state.gated != Some(false)
            || state.bank.as_deref() != Some(plan.source_bank.as_str())
        {
            return Err(Stop::new(format!(
                "the target does not read back as serving the moved bank (running {:?}, gated {:?}, bank {:?})",
                state.running, state.gated, state.bank
            )));
        }
        // The commit point.
        self.check_interrupt()?;
        self.hosts.say("commit: mark the stopped source as moved");
        self.mark(|f| f.moved_written = true)?;
        // No interrupt is read from here until the commit is written: a
        // signal now waits for the commit point's outcome.
        let body = serde_json::json!({
            "moved_to": plan.to,
            "move_id": self.record.move_id,
            "bank": plan.source_bank,
            "moved_at": self.hosts.now(),
        })
        .to_string();
        self.hosts.moved(Some(&body)).map_err(Stop::new)?;
        self.record.committed = true;
        self.record.phase = Phase::Moved;
        if let Err(stop) = self.save() {
            // The commit happened; only its record failed. Say so.
            self.record.follow_ups.push(format!(
                "the move record could not be written after the commit ({}); the bank lives on {}",
                stop.reason, plan.to
            ));
        }
        Ok(())
    }

    fn wait_identity(&mut self) -> Result<(), Stop> {
        let url = format!("{}/health", self.plan.target_url);
        let mut waited = Duration::ZERO;
        loop {
            self.check_interrupt()?;
            if let Some(answer) = self.hosts.target_health(&url) {
                let bank = answer.get("bank").and_then(Value::as_str);
                let ok = answer.get("status").and_then(Value::as_str) == Some("ok")
                    && answer.get("ready").and_then(Value::as_bool) != Some(false)
                    && answer.get("auth").and_then(Value::as_bool) == Some(true);
                if ok && bank == Some(self.plan.source_bank.as_str()) {
                    return Ok(());
                }
                if ok && bank.is_some() {
                    return Err(Stop::new(format!(
                        "{} serves bank {}, not the moved bank {}",
                        self.plan.target_url,
                        bank.unwrap_or_default(),
                        self.plan.source_bank
                    )));
                }
            }
            if waited >= IDENTITY_WAIT {
                return Err(Stop::new(format!(
                    "{} did not serve the moved bank within {} seconds",
                    self.plan.target_url,
                    IDENTITY_WAIT.as_secs()
                )));
            }
            self.hosts.sleep(IDENTITY_POLL);
            waited += IDENTITY_POLL;
        }
    }

    /// N22: after the commit point. Nothing here is rolled back.
    fn after_commit(&mut self) {
        let plan = self.plan;
        let to = &plan.to;
        if self.record.flags.target_policy_paused {
            let policy = self.record.target_policy.clone();
            if self
                .target(
                    Op::PolicySet {
                        policy: policy.clone(),
                    },
                    None,
                )
                .is_err()
            {
                self.record.follow_ups.push(format!(
                    "ssh {to} docker update --restart={policy} pseudolife-mcp-daemon"
                ));
            }
        }
        if self.record.flags.target_timer_paused && self.target(Op::TimerResume, None).is_err() {
            self.record.follow_ups.push(format!(
                "ssh {to} systemctl --user enable --now pseudolife-update.timer"
            ));
        }
        // A last reading of the move's folder there, for the report.
        let _ = self.target(Op::State { tables: Vec::new() }, None);
        if self.target(Op::Release, None).is_err() {
            self.record
                .follow_ups
                .push(format!("ssh {to} rm -f {}", claim_path()));
        }
        if let Err(why) = self.hosts.repoint(&plan.target_url) {
            // The target kept its own tokens (--no-keep-tokens): a client
            // holding this host's token needs one of the target's.
            self.record.follow_ups.push(format!(
                "pseudolife-mcp connect {} --token-file <a file holding a token of {}'s> --yes   # this machine's clients still point at the old daemon ({why})",
                plan.target_url, plan.to
            ));
        }
        self.record.phase = if self.record.follow_ups.is_empty() {
            Phase::Complete
        } else {
            Phase::MovedFollowUps
        };
        let _ = self.save();
    }
}

/// Where the move's files are on both hosts and what they take: a full copy
/// of the bank each, which nothing removes, for a person to delete.
fn files_line(record: &Record) -> String {
    let size = |bytes: Option<u64>| {
        bytes.map_or_else(
            || "size unknown".to_owned(),
            |b| format!("{} MiB", b.div_ceil(1024 * 1024)),
        )
    };
    let local = super::ops::folder_bytes(Path::new(&record.local_dir));
    let remote = if record.remote_folder.is_empty() {
        format!(
            "{}/{}",
            if record.work_dir.is_empty() {
                "~/.pseudolife-mcp/moves"
            } else {
                &record.work_dir
            },
            record.move_id
        )
    } else {
        record.remote_folder.clone()
    };
    format!(
        "the move's files, each a full copy of the bank, are kept for you to remove: {} ({}) on this host and {} ({}) on {}",
        record.local_dir,
        size(local),
        remote,
        size(record.remote_bytes),
        record.target
    )
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The target's claim file, for a person.
/// The target's claim file, for a person: one place, whatever
/// --target-work-dir says.
fn claim_path() -> &'static str {
    "~/.pseudolife-mcp/moves/claim.json"
}

/// The tables whose write counters moved between the restore and now, or
/// every reason they cannot be compared (which counts as writes taken: a
/// move never replaces what it cannot rule out).
fn changed(
    before: Option<&BTreeMap<String, i64>>,
    now: Option<&BTreeMap<String, i64>>,
) -> Vec<String> {
    let (Some(before), Some(now)) = (before, now) else {
        return vec!["the target's write counters could not be compared, so writes in place cannot be ruled out".to_owned()];
    };
    let mut found: Vec<String> = now
        .iter()
        .filter_map(|(table, after)| match before.get(table) {
            Some(was) if after == was => None,
            Some(was) if after > was => Some(format!(
                "{table}: {} rows written in place or added on the target",
                after - was
            )),
            _ => Some(format!("{table}: its write counter cannot be compared")),
        })
        .collect();
    found.extend(
        before
            .keys()
            .filter(|table| !now.contains_key(*table))
            .map(|table| format!("{table}: its write counter cannot be compared")),
    );
    found
}

/// The tables whose counts differ, or that could not be counted.
fn differing(expected: &BTreeMap<String, i64>, found: &BTreeMap<String, i64>) -> Vec<String> {
    expected
        .iter()
        .filter_map(|(table, rows)| match found.get(table) {
            Some(count) if count == rows => None,
            Some(count) => Some(format!(
                "{table}: {count} on the target, {rows} in the dump"
            )),
            None => Some(format!("{table}: not counted on the target")),
        })
        .collect()
}

/// Runs the transaction for `plan` under move `id` and answers how it
/// ended.
pub(super) fn execute<H: Hosts>(hosts: &mut H, plan: &Plan, id: &str) -> Outcome {
    let record = Record {
        version: RECORD_VERSION,
        move_id: id.to_owned(),
        target: plan.to.clone(),
        target_url: plan.target_url.clone(),
        source_bank: plan.source_bank.clone(),
        target_policy: plan.target_policy.clone(),
        source_policy: plan.source_policy.clone(),
        work_dir: plan.work_dir.clone(),
        local_dir: plan.local_dir.to_string_lossy().into_owned(),
        target_was_running: plan.target_running,
        schedule_enable: plan.schedule_enable.clone(),
        source: plan.source.clone(),
        target_image: plan.target_image.clone(),
        ..Record::default()
    };
    let mut record = record;
    // A resumed move's target already holds its restored bank behind the
    // gate: the record says so from the start, so a rollback keeps that
    // bank gated, keeps the claim, and never starts it as the target's own.
    record.flags.target_restored = plan.resume;
    let mut run = Run {
        hosts,
        plan,
        record,
        last_state: None,
    };
    if let Err(stop) = run.save() {
        return Outcome {
            code: EXIT_REFUSED,
            message: format!("{}. Nothing was changed", stop.reason),
        };
    }
    match run.forward() {
        Ok(()) => {
            run.after_commit();
            let record = &run.record;
            let mut message = format!(
                "moved: the bank {} now lives on {} ({}); this host's daemon stays stopped behind /data/moved.json. Once the moved bank is checked, {}",
                plan.source_bank,
                plan.to,
                plan.target_url,
                files_line(record)
            );
            if record.follow_ups.is_empty() {
                Outcome {
                    code: EXIT_OK,
                    message,
                }
            } else {
                message.push_str("; finish by hand:\n");
                for line in &record.follow_ups {
                    message.push_str(&format!("  {line}\n"));
                }
                Outcome {
                    code: EXIT_FOLLOW_UPS,
                    message: message.trim_end().to_owned(),
                }
            }
        }
        Err(stop) if stop.untouched => {
            // Nothing changed: the record says so, and a resumed move stays
            // resumable (its target still holds the earlier restored bank).
            run.record.phase = Phase::RolledBack;
            let _ = run.save();
            Outcome {
                code: EXIT_REFUSED,
                message: format!("{}. Nothing was changed", stop.reason),
            }
        }
        Err(stop) => {
            run.record.error = Some(stop.reason.clone());
            let last = run.last_state.take();
            let mut back = Back {
                hosts: run.hosts,
                record: run.record,
                state: last,
            };
            back.run(&stop.reason)
        }
    }
}

/// Runs the rollback of a record a move left unfinished (`move --rollback`).
pub(super) fn roll_back<H: Hosts>(hosts: &mut H, record: Record) -> Outcome {
    if record.committed {
        return Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "move {} passed its commit point: the bank lives on {} and a committed move is not rolled back. Nothing was changed",
                record.move_id, record.target
            ),
        };
    }
    if record.phase == Phase::RolledBack {
        return Outcome {
            code: EXIT_OK,
            message: match &record.parked_writes {
                Some(tables) => format!(
                    "move {} was already rolled back; nothing to do. The target {} keeps writes it took while it served ({tables}) in its gated bank",
                    record.move_id, record.target
                ),
                None => format!(
                    "move {} was already rolled back; nothing to do",
                    record.move_id
                ),
            },
        };
    }
    let mut back = Back {
        hosts,
        record,
        state: None,
    };
    back.run("rolling back by request (move --rollback)")
}

/// The rollback: R1 to R16 in order, every undo attempted, I2 checked on a
/// fresh reading before the fence lifts.
struct Back<'a, H: Hosts> {
    hosts: &'a mut H,
    record: Record,
    state: Option<State>,
}

impl<H: Hosts> Back<'_, H> {
    fn save(&mut self) {
        self.record.updated_at = self.hosts.now();
        self.record.manual_rollback = manual_rollback(&self.record);
        // The rollback goes on whether or not its record can be written:
        // stopping here would leave the source down for no reason.
        let _ = self.hosts.save(&self.record);
    }

    fn attempt(&mut self, step: &str, result: Result<(), String>) -> bool {
        let ok = result.is_ok();
        self.hosts.say(&format!(
            "rollback: {step}: {}",
            result
                .as_ref()
                .map_or_else(|e| format!("FAILED ({e})"), |()| "done".to_owned())
        ));
        self.record.rollback.push(Undo {
            step: step.to_owned(),
            ok,
            detail: result.err().unwrap_or_default(),
        });
        self.save();
        ok
    }

    fn op(&mut self, op: Op) -> Result<Answer, String> {
        let request = Request {
            protocol: PROTOCOL,
            move_id: self.record.move_id.clone(),
            work_dir: self.record.work_dir.clone(),
            rollback: true,
            op,
        };
        let answer = self.hosts.target(&request, None)?;
        if let Some(state) = &answer.state {
            self.state = Some(state.clone());
            if !state.folder.is_empty() {
                self.record.remote_folder = state.folder.clone();
                self.record.remote_bytes = state.folder_bytes;
            }
        }
        if answer.ok {
            Ok(answer)
        } else {
            Err(answer.error)
        }
    }

    /// The target's `abort`, waiting while another operation holds its
    /// update lock (a restore can take hours) and retrying a lost link a few
    /// times.
    fn abort(&mut self) -> bool {
        let request = Request {
            protocol: PROTOCOL,
            move_id: self.record.move_id.clone(),
            work_dir: self.record.work_dir.clone(),
            rollback: true,
            op: Op::Abort,
        };
        let mut lost = 0;
        let mut waited = Duration::ZERO;
        loop {
            match self.hosts.target(&request, None) {
                Ok(answer) if answer.ok => {
                    self.attempt("stop the move's steps on the target", Ok(()));
                    return true;
                }
                Ok(answer) if answer.busy && waited < ABORT_BUSY_WAIT => {
                    if waited.is_zero() {
                        self.hosts.say(
                            "rollback: waiting for the step still running on the target to finish (it holds the target's update lock)",
                        );
                    }
                    self.hosts.sleep(ABORT_PAUSE);
                    waited += ABORT_PAUSE;
                }
                Err(_) if lost + 1 < ABORT_LOST_TRIES => {
                    lost += 1;
                    self.hosts.sleep(ABORT_PAUSE);
                }
                Ok(answer) => {
                    self.attempt("stop the move's steps on the target", Err(answer.error));
                    return false;
                }
                Err(why) => {
                    self.attempt("stop the move's steps on the target", Err(why));
                    return false;
                }
            }
        }
    }

    /// A fresh reading of the target, counting the dump's tables when the
    /// target may have served.
    fn read(&mut self) -> Result<State, String> {
        let served = self.record.flags.gate_lifting || self.record.flags.target_started;
        let tables = if served {
            self.record.dump_counts.keys().cloned().collect()
        } else {
            Vec::new()
        };
        // A stale reading must never stand in for this one.
        self.state = None;
        let answer = self.op(Op::State { tables })?;
        answer
            .state
            .ok_or_else(|| "the target answered without its state".to_owned())
    }

    fn run(&mut self, reason: &str) -> Outcome {
        let flags = self.record.flags.clone();
        // The commit marker may be in place although the record never said
        // so (its write failed, or the process died right after the copy):
        // the source's own /data/moved.json decides, and a move past its
        // commit point is never rolled back.
        if flags.moved_written && !self.record.committed {
            match self.hosts.moved_marker() {
                Ok(Some(id)) if id == self.record.move_id => return self.committed_after_all(),
                Ok(_) => {}
                Err(why) => {
                    self.record.phase = Phase::RollbackIncomplete;
                    self.save();
                    return self.incomplete(
                        reason,
                        &format!(
                            "whether the source holds /data/moved.json for this move cannot be read ({why}), so the move may have passed its commit point and nothing was rolled back"
                        ),
                        false,
                    );
                }
            }
        }
        let mut complete = true;
        let mut can_start = true;
        // The target may hold, or may soon hold, the moved bank: it must be
        // proved stopped and gated before the source comes back (I2).
        let needs_proof = flags.target_restored || flags.gate_lifting || flags.target_started;
        // First, no forward step of this move may run on the target any
        // more: one still running there (its ssh gone) finishes under the
        // target's update lock before the abort is written.
        let aborted = !flags.claimed || self.abort();
        if !aborted {
            complete = false;
        }
        let mut proved = !needs_proof;
        let mut parked: Option<String> = None;
        if needs_proof {
            if flags.gate_lifting || flags.target_started {
                let result = self.op(Op::Stop).map(|_| ());
                self.attempt("stop the target daemon", result);
            }
            for _ in 0..3 {
                match self.read() {
                    Ok(state) if state.running == Some(true) => {
                        let result = self.op(Op::Stop).map(|_| ());
                        self.attempt("stop the target daemon again", result);
                    }
                    Ok(state) if state.running == Some(false) => {
                        if state.gated != Some(true) {
                            let result = self.op(Op::Gate).map(|_| ());
                            self.attempt("keep the target from starting (/data/move.json)", result);
                            continue;
                        }
                        proved = true;
                        if flags.gate_lifting || flags.target_started {
                            // User rows only, as they stood before the first
                            // start: a plain daemon start adds none.
                            let mut differs = if self.record.restored_counts.is_empty() {
                                differing(&self.record.dump_counts, &state.counts)
                            } else {
                                differing(&self.record.restored_counts, &state.user_counts)
                            };
                            // Writes in place change no count, only the counters.
                            differs.extend(changed(
                                self.record.restored_mutations.as_ref(),
                                state.mutations.as_ref(),
                            ));
                            if !differs.is_empty() {
                                parked = Some(differs.join("; "));
                            }
                        }
                        break;
                    }
                    Ok(_) => {}
                    Err(why) => {
                        self.attempt("read the target's state", Err(why));
                    }
                }
            }
        }
        // Without the abort, a step still on its way could start the target
        // after this reading.
        if needs_proof && !aborted {
            proved = false;
        }
        if !proved {
            self.record.phase = Phase::RollbackIncomplete;
            self.record.rollback.push(Undo {
                step: "the source stays stopped and fenced: two daemons must never serve one bank"
                    .to_owned(),
                ok: false,
                detail: "the target could not be proved stopped and gated".to_owned(),
            });
            self.save();
            return self.incomplete(
                reason,
                "the target could not be proved stopped and gated, so the source stays stopped and fenced",
                false,
            );
        }
        if flags.target_timer_paused {
            let result = self.op(Op::TimerResume).map(|_| ());
            complete &= self.attempt("resume the target's unattended update", result);
        }
        if flags.target_policy_paused {
            let policy = self.record.target_policy.clone();
            let result = self.op(Op::PolicySet { policy }).map(|_| ());
            complete &= self.attempt("restore the target daemon's restart policy", result);
        }
        if flags.fenced {
            let result = self.hosts.fence(false);
            if !self.attempt("lift the source database's fence", result) {
                complete = false;
                can_start = false;
            }
        }
        if flags.read_only {
            let result = self.hosts.read_only(false);
            if !self.attempt("let the source database take writes again", result) {
                complete = false;
                can_start = false;
            }
        }
        if flags.moved_written {
            let result = self.hosts.moved(None);
            if !self.attempt("remove the source's /data/moved.json", result) {
                complete = false;
                can_start = false;
            }
        }
        if flags.source_policy_changed {
            let policy = self.record.source_policy.clone();
            let result = self.hosts.source_policy(&policy);
            complete &= self.attempt("restore the source daemon's restart policy", result);
        }
        if flags.schedule_paused {
            let result = self.hosts.schedule(true);
            complete &= self.attempt("resume this host's unattended update", result);
        }
        if flags.source_stopped {
            if can_start {
                let result = self.hosts.source_start();
                if self.attempt("start the source daemon", result) {
                    if !self.hosts.source_healthy(SOURCE_WAIT) {
                        // Started is not serving: a daemon that exits at once
                        // makes this rollback incomplete, not "running again".
                        if self.hosts.source_running() == Some(true) {
                            self.hosts.say(
                                "rollback: the source daemon started but has not answered its health check yet",
                            );
                        } else {
                            complete = false;
                            self.record.rollback.push(Undo {
                                step: "the source daemon serves again".to_owned(),
                                ok: false,
                                detail: "it started, then stopped: see docker logs pseudolife-mcp-daemon".to_owned(),
                            });
                        }
                    }
                } else {
                    complete = false;
                }
            } else {
                complete = false;
                self.record.rollback.push(Undo {
                    step: "the source stays stopped".to_owned(),
                    ok: false,
                    detail: "its fence or moved.json could not be removed".to_owned(),
                });
            }
        }
        // A gate on the target's own bank (the restore never ran, or was
        // refused before its destructive step) would keep it from starting.
        if flags.gate_written && !flags.target_restored && !flags.gate_lifting {
            let result = self.op(Op::Ungate).map(|_| ());
            complete &= self.attempt("remove the gate from the target's own bank", result);
        }
        if flags.target_quiesced && !flags.target_restored {
            let result = self.op(Op::Start).map(|_| ());
            complete &= self.attempt("start the target's own daemon again", result);
        }
        if flags.claimed && !flags.target_restored {
            let result = self.op(Op::Release).map(|_| ());
            complete &= self.attempt("release the target's claim", result);
        }
        if let Some(tables) = &parked {
            self.record.parked_writes = Some(tables.clone());
        }
        if complete && parked.is_none() {
            self.record.phase = Phase::RolledBack;
            self.save();
            let note = if flags.target_restored {
                format!(
                    "; the target keeps the restored bank stopped behind /data/move.json, and its claim ({} on {}). Rerun the move with --resume to finish it; or, to give {} back its own bank, remove that claim and restore its own state from {}/target-original-state.tgz and its database from the safety backup beside it",
                    claim_path(),
                    self.record.target,
                    self.record.target,
                    if self.record.remote_folder.is_empty() {
                        format!("~/.pseudolife-mcp/moves/{}", self.record.move_id)
                    } else {
                        self.record.remote_folder.clone()
                    }
                )
            } else {
                String::new()
            };
            return Outcome {
                code: EXIT_ROLLED_BACK,
                message: format!(
                    "move failed: {reason}. Rolled back: the source daemon runs again{note}. {}",
                    files_line(&self.record)
                ),
            };
        }
        if complete && let Some(tables) = &parked {
            // Finished: the source serves again. What remains is a person's
            // decision about the writes kept on the target, which no rerun
            // of this rollback can make.
            self.record.phase = Phase::RolledBack;
            self.save();
            let why = format!(
                "the target took writes while it served, which are not in the source ({tables}); they are in the stopped, gated target bank on {}, and nothing was deleted",
                self.record.target
            );
            return self.incomplete(reason, &why, true);
        }
        self.record.phase = Phase::RollbackIncomplete;
        self.save();
        let why = match &parked {
            Some(tables) => format!(
                "some undo steps failed, and the target took writes while it served, which are not in the source ({tables}); they are in the stopped, gated target bank on {}, and nothing was deleted",
                self.record.target
            ),
            None => {
                let failed: Vec<String> = self
                    .record
                    .rollback
                    .iter()
                    .filter(|undo| !undo.ok)
                    .map(|undo| {
                        if undo.detail.is_empty() {
                            undo.step.clone()
                        } else {
                            format!("{} ({})", undo.step, undo.detail)
                        }
                    })
                    .collect();
                format!("these undo steps failed: {}", failed.join("; "))
            }
        };
        self.incomplete(reason, &why, false)
    }

    /// The source holds this move's `moved.json`: the move passed its commit
    /// point, so the record says so and nothing is rolled back.
    fn committed_after_all(&mut self) -> Outcome {
        self.record.committed = true;
        self.record.phase = Phase::MovedFollowUps;
        let to = self.record.target.clone();
        let url = self.record.target_url.clone();
        let mut steps = vec![format!(
            "pseudolife-mcp connect {url} --token-file <a file holding a token of {to}'s> --yes   # point this machine's clients at the moved bank"
        )];
        if self.record.flags.target_policy_paused {
            steps.push(format!(
                "ssh {to} docker update --restart={} pseudolife-mcp-daemon",
                self.record.target_policy
            ));
        }
        if self.record.flags.target_timer_paused {
            steps.push(format!(
                "ssh {to} systemctl --user enable --now pseudolife-update.timer"
            ));
        }
        steps.push(format!("ssh {to} rm -f {}", claim_path()));
        self.record.follow_ups = steps.clone();
        self.save();
        Outcome {
            code: EXIT_FOLLOW_UPS,
            message: format!(
                "move {} passed its commit point (this host's /data/moved.json names it): the bank lives on {to} ({url}) and is not rolled back. Finish by hand:\n  {}",
                self.record.move_id,
                steps.join("\n  ")
            ),
        }
    }

    /// Exit 6, with the four facts a person needs, in order.
    fn incomplete(&mut self, reason: &str, why: &str, finished: bool) -> Outcome {
        let source = self.hosts.source_running();
        let target = self.state.as_ref().and_then(|s| s.running);
        let running = match (source, target) {
            (Some(true), Some(true)) => {
                "both daemons (stop the target now: ssh {to} docker stop pseudolife-mcp-daemon)"
                    .to_owned()
            }
            (Some(true), _) => "the source daemon on this host".to_owned(),
            (_, Some(true)) => format!("the target daemon on {}", self.record.target),
            (Some(false), Some(false)) => "neither daemon".to_owned(),
            (Some(false), None) => format!(
                "not the source daemon; the target daemon on {} could not be read",
                self.record.target
            ),
            (None, _) => "unknown: the source daemon could not be read".to_owned(),
        }
        .replace("{to}", &self.record.target);
        let served = match (source, target) {
            (Some(true), _) => "yes, by this host's daemon".to_owned(),
            (_, Some(true)) => format!("yes, at {}", self.record.target_url),
            (Some(false), Some(false)) => "no: the bank is not served anywhere".to_owned(),
            _ => "unknown".to_owned(),
        };
        let fenced = self.record.flags.fenced
            && !self
                .record
                .rollback
                .iter()
                .any(|u| u.ok && u.step == "lift the source database's fence");
        let mut data = format!(
            "this host's database ({}){}",
            if fenced { "fenced" } else { "not fenced" },
            if self.record.flags.target_restored {
                format!(
                    "; a restored copy on {}, stopped behind /data/move.json",
                    self.record.target
                )
            } else {
                String::new()
            }
        );
        data.push_str(&format!(
            "; the move's files in {} here and {} on {}",
            self.record.local_dir,
            if self.record.work_dir.is_empty() {
                "~/.pseudolife-mcp/moves"
            } else {
                &self.record.work_dir
            },
            self.record.target
        ));
        let next = if finished {
            format!(
                "look at those rows in the gated bank on {} (start it by hand on a spare host, or export them); remove that bank when they are kept or not needed",
                self.record.target
            )
        } else {
            format!("pseudolife-mcp move --rollback {}", self.record.move_id)
        };
        let headline = if finished {
            "Rolled back: the source daemon runs again, but"
        } else {
            "The rollback is INCOMPLETE:"
        };
        Outcome {
            code: EXIT_INCOMPLETE,
            message: format!(
                "move failed: {reason}. {headline} {why}.\n  running now: {running}\n  served: {served}\n  data: {data}\n  next: {next} (the record {}/record.json lists each step; its manual rollback is the last resort)",
                self.record.local_dir
            ),
        }
    }
}

/// The commands that undo what the record says was done, in rollback order,
/// for a person. They need only `docker` (and `psql` inside its container)
/// on each host.
pub(super) fn manual_rollback(record: &Record) -> Vec<String> {
    let f = &record.flags;
    let to = &record.target;
    let mut lines = Vec::new();
    if f.gate_lifting || f.target_started {
        lines.push(format!("ssh {to} docker stop pseudolife-mcp-daemon"));
    }
    if f.target_restored || f.gate_lifting {
        lines.push(format!(
            "ssh {to} docker run --rm --entrypoint touch --volumes-from pseudolife-mcp-daemon {} /data/move.json",
            record.target_image
        ));
    }
    if f.target_timer_paused {
        lines.push(format!(
            "ssh {to} systemctl --user enable --now pseudolife-update.timer"
        ));
    }
    if f.target_policy_paused {
        lines.push(format!(
            "ssh {to} docker update --restart={} pseudolife-mcp-daemon",
            record.target_policy
        ));
    }
    if f.fenced {
        lines.push(
            format!(
                "docker exec {} psql -U {} -d postgres -c \"ALTER DATABASE {} WITH ALLOW_CONNECTIONS true\"",
                record.source.pg, record.source.user, record.source.db
            ),
        );
    }
    if f.read_only {
        lines.push(format!(
            r#"docker exec {} psql -U {} -d postgres -c "ALTER DATABASE {} RESET default_transaction_read_only""#,
            record.source.pg, record.source.user, record.source.db
        ));
    }
    if f.moved_written {
        lines.push(format!(
            "docker run --rm --entrypoint rm --volumes-from {} {} -f /data/moved.json",
            record.source.daemon, record.source.image
        ));
    }
    if f.source_policy_changed {
        lines.push(format!(
            "docker update --restart={} pseudolife-mcp-daemon",
            record.source_policy
        ));
    }
    if f.schedule_paused {
        lines.push(record.schedule_enable.clone());
    }
    if f.source_stopped {
        lines.push("docker start pseudolife-mcp-daemon".to_owned());
    }
    if f.target_quiesced && !f.target_restored {
        lines.push(format!("ssh {to} docker start pseudolife-mcp-daemon"));
    }
    lines
}
