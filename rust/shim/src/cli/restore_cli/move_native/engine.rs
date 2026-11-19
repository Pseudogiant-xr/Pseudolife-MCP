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
use super::record::{Baseline, Ending, Flags, Phase, RECORD_VERSION, Reading, Record, Undo};

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
/// How long a forward step waits while another operation (a backup or an
/// update there) holds the target's update lock, before the move rolls back
/// rather than keep the stopped source down longer: a design bound, not a
/// measurement.
const FORWARD_BUSY_WAIT: Duration = Duration::from_secs(30 * 60);
/// How long `--release-target` gives the source to answer its health check.
const RELEASE_SOURCE_WAIT: Duration = Duration::from_secs(10);
/// `--target-gone` asks the target this many times, this far apart, before
/// it counts as giving no answer: the give-up is final.
const GIVE_UP_ATTEMPTS: u32 = 3;
const GIVE_UP_PAUSE: Duration = Duration::from_secs(5);

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
    /// The bank fingerprint the source daemon's `/health` names, read now.
    fn source_bank(&mut self) -> Option<String>;
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
    /// What the source's `/data/moved.json` says; `Err` when this host's
    /// Docker cannot be asked.
    fn moved_marker(&mut self) -> Result<Marker, String>;
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

/// The source's `/data/moved.json`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Marker {
    Absent,
    /// It names this move ID.
    Names(String),
    /// It is there but names no move (cut short, or not a marker): no move
    /// counts its commit until a whole marker is written, so it is no
    /// move's commit point.
    NamesNone,
}

/// What the preflight found, which the transaction needs.
#[derive(Debug, Clone, Default)]
pub(super) struct Plan {
    pub to: String,
    /// The source bank as the preflight found it (kept in the record).
    pub source: super::probe::Facts,
    pub target_image: String,
    pub target_pins: Option<super::carry::TargetPins>,
    pub target_files: super::carry::TargetFiles,
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
    /// Whether the target daemon ran before the move first began (a
    /// resumed move's target is stopped behind its gate by then).
    pub original_target_running: bool,
    /// For `--resume`: the gated bank as the earlier rollback read it, which
    /// the target's bank must still match before it is replaced.
    pub resume_baseline: Option<Baseline>,
    /// These live only in this invocation. The record names the step, never the values.
    pub identities: Option<(super::carry::Identities, super::carry::TargetPins)>,
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
            pins: self.record.target_pins.clone(),
            op,
        }
    }

    /// One target operation that must succeed. A lost link, a refusal and
    /// an unreadable answer all stop the move.
    /// One request, waiting while another operation (a backup or an update
    /// there) holds the target's update lock, up to a bound.
    fn ask(
        &mut self,
        request: &Request,
        payload: Option<&Path>,
    ) -> Result<Result<Answer, String>, Stop> {
        let mut waited = Duration::ZERO;
        loop {
            match self.hosts.target(request, payload) {
                Ok(answer) if !answer.ok && answer.busy && waited < FORWARD_BUSY_WAIT => {
                    if waited.is_zero() {
                        self.hosts.say(&format!(
                            "waiting for the target's update lock before {} (another update, restore or backup holds it)",
                            request.op.name()
                        ));
                    }
                    self.check_interrupt()?;
                    self.hosts.sleep(ABORT_PAUSE);
                    waited += ABORT_PAUSE;
                }
                other => return Ok(other),
            }
        }
    }

    fn target(&mut self, op: Op, payload: Option<&Path>) -> Result<Answer, Stop> {
        let name = op.name();
        let request = self.request(op);
        let result = self.ask(&request, payload)?;
        match result {
            Ok(answer) => {
                if let Some(state) = &answer.state {
                    self.last_state = Some(state.clone());
                    remember_target_daemon(&mut self.record, state);
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
        let tables = if plan.resume {
            baseline_tables(plan.resume_baseline.as_ref(), &self.record.dump_counts)
        } else {
            Vec::new()
        };
        let state = self.target(Op::State { tables }, None)?;
        let state = state.state.unwrap_or_default();
        // A resumed move replaces its own earlier restored bank, and only
        // while that bank is still as its last rollback left it: gated, with
        // no write since. Any other used bank is never overwritten.
        let own = plan.resume && state.bank.as_deref() == Some(plan.source_bank.as_str());
        if own {
            let writes = since_baseline(plan.resume_baseline.as_ref(), &state);
            if !writes.is_empty() {
                let found = writes.join("; ");
                self.record.parked_writes = Some(found.clone());
                self.save()?;
                return Err(Stop::new(format!(
                    "the target's gated bank may have taken writes since this move's last rollback ({found}), so the resume does not replace it"
                )));
            }
        }
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
        // Written before the send: a record that says the restore was sent
        // and not whether it dropped cannot tell what the target holds.
        self.record.restore_sent = true;
        if let Err(stop) = self.mark(|f| f.target_restored = true) {
            // Not written, so not sent: the record stays as it was.
            self.record.restore_sent = false;
            self.record.flags.target_restored = plan.resume;
            return Err(stop);
        }
        let request = self.request(Op::Restore);
        let asked = match self.ask(&request, None) {
            Ok(asked) => asked,
            // Interrupted while the target answered busy: it said the restore
            // did not run, so the record says so before the rollback reads it.
            Err(stop) => {
                self.record.restore_sent = false;
                self.record.flags.target_restored = plan.resume;
                let _ = self.save();
                return Err(stop);
            }
        };
        match asked {
            Ok(answer) if answer.ok => {
                self.record.restore_dropped = true;
                self.save()?;
            }
            Ok(answer) => {
                if answer.restore_dropped {
                    self.record.restore_dropped = true;
                } else {
                    // Refused before its destructive step: nothing changed.
                    // A resumed target still holds its earlier restored bank.
                    self.record.restore_sent = false;
                    if !plan.resume {
                        self.record.flags.target_restored = false;
                    }
                }
                self.save()?;
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
        // The bank as this restore left it, kept before anything is judged
        // of it: a rollback compares the gated bank with this reading.
        self.record.restored_counts = state.user_counts.clone();
        self.record.restored_mutations = state.mutations.clone();
        self.record.restored_reading = Some(Reading::Check);
        self.save()?;
        let differs = differing(&self.record.dump_counts, &state.counts);
        if !differs.is_empty() {
            return Err(Stop::new(format!(
                "the restored target's row counts differ from the dump's ({})",
                differs.join(", ")
            )));
        }
        // What would show a write in place: without it, writes the target
        // takes while it serves could not be ruled out, so it never starts.
        if self.record.restored_mutations.is_none() {
            return Err(Stop::new(
                "the restored target's write counters (pg_stat_user_tables) cannot be read, so writes it took while it served could not be found; the target was not started",
            ));
        }
        if let Some((identities, pins)) = &plan.identities {
            self.step("apply the source's identities to the stopped target")?;
            self.record.verified_target_daemon = None;
            self.mark(|flags| flags.identities_applied = true)?;
            self.target(
                Op::CarryApply {
                    identities: identities.settings(),
                    expected_pins: pins.clone(),
                    files: self.plan.target_files.clone(),
                },
                None,
            )?;
        }
        self.step("lift the target's gate and start it once")?;
        self.mark(|f| f.gate_lifting = true)?;
        self.target(Op::StartGated, None)?;
        self.mark(|f| f.target_started = true)?;
        if let Some((identities, _)) = &plan.identities {
            self.target(
                Op::CarryVerify {
                    identities: identities.clone(),
                },
                None,
            )?;
        }
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
        // Everything the move still has to do after its commit, so a move
        // killed past the commit point still names it; struck off below as
        // each step succeeds.
        self.record.follow_ups = planned_follow_ups(&self.record);
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
        // From the planned list to what is left: each step below adds itself
        // back only if it fails.
        let failed_save: Vec<String> = self
            .record
            .follow_ups
            .iter()
            .filter(|line| line.starts_with("the move record could not be written"))
            .cloned()
            .collect();
        self.record.follow_ups = failed_save;
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
            self.record
                .follow_ups
                .push(repoint_line(&self.record, &why));
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
    if record.flags.target_restored && !record.committed && record.phase != Phase::Released {
        return format!(
            "the move's files, each a full copy of the bank, are kept: {} ({}) on this host, for you to remove, and {} ({}) on {}, which holds the target's own backup and what --resume or --release-target needs: keep it until one of them has run",
            record.local_dir,
            size(local),
            remote,
            size(record.remote_bytes),
            record.target
        );
    }
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
        target_was_running: plan.original_target_running,
        schedule_enable: plan.schedule_enable.clone(),
        source: plan.source.clone(),
        target_image: plan.target_image.clone(),
        target_pins: plan.target_pins.clone(),
        target_files: plan.target_files.clone(),
        // Kept as the earlier record had it, so a resume refused before it
        // changed anything leaves the record as resumable as it was.
        gated_baseline: plan.resume_baseline.clone(),
        carried_identities: plan.identities.is_some(),
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
                same_run: true,
            };
            back.run(&stop.reason)
        }
    }
}

/// Runs the rollback of a record a move left unfinished (`move --rollback`).
pub(super) fn roll_back<H: Hosts>(hosts: &mut H, record: Record) -> Outcome {
    if let Some(outcome) = answered_by_record(&record) {
        return outcome;
    }
    let mut back = Back {
        hosts,
        record,
        state: None,
        same_run: false,
    };
    back.run("rolling back by request (move --rollback)")
}

/// What `move --rollback` answers from a record alone (committed, rolled
/// back or released), or `None` when there is a rollback to run.
pub(super) fn answered_by_record(record: &Record) -> Option<Outcome> {
    let id = &record.move_id;
    let to = &record.target;
    if record.committed {
        if record.follow_ups.is_empty() {
            return Some(Outcome {
                code: EXIT_REFUSED,
                message: format!(
                    "move {id} passed its commit point: the bank lives on {to} and a committed move is not rolled back. Nothing was changed"
                ),
            });
        }
        // Killed past its commit point: what it had left to do, which no
        // rerun does for it.
        return Some(Outcome {
            code: EXIT_FOLLOW_UPS,
            message: format!(
                "move {id} passed its commit point: the bank lives on {to} and a committed move is not rolled back. It did not finish; finish by hand:\n  {}",
                record.follow_ups.join("\n  ")
            ),
        });
    }
    match record.phase {
        Phase::Released => Some(Outcome {
            code: EXIT_OK,
            message: match ending(record) {
                Ending::NothingThere => format!(
                    "move {id} was rolled back, and its release found {to} holding nothing of it, so nothing was changed there; nothing to do"
                ),
                _ => format!(
                    "move {id} was rolled back, and {to} was given back its own bank; nothing to do. {}",
                    kept_line(record)
                ),
            },
        }),
        Phase::GivenUp => Some(given_up_answer(record, None, Asked::Rollback)),
        Phase::Releasing => Some(Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "move {id} was rolled back, and giving {to} back its own bank did not finish; finish it with pseudolife-mcp move --release-target {id}. Nothing was changed"
            ),
        }),
        Phase::RolledBack => Some(Outcome {
            code: EXIT_OK,
            message: match (
                &record.parked_writes,
                &record.unresolved,
                record.flags.target_restored,
            ) {
                (Some(tables), _, _) => format!(
                    "move {id} was already rolled back; nothing to do. The target {to} keeps writes it took while it served ({tables}) in its gated bank. To free that target, keeping those writes in a backup there: pseudolife-mcp move --release-target {id}"
                ),
                (None, Some(cause), _) => format!(
                    "move {id} was already rolled back; nothing to do. The target {to} keeps the bank behind its gate, but {cause}, so it is not resumed over. To free that target, keeping that bank in a backup there: pseudolife-mcp move --release-target {id}"
                ),
                (None, None, true) => format!(
                    "move {id} was already rolled back; nothing to do. The target {to} keeps the restored bank behind its gate: finish the move with pseudolife-mcp move --resume, or give {to} back its own bank with pseudolife-mcp move --release-target {id}"
                ),
                (None, None, false) => format!("move {id} was already rolled back; nothing to do"),
            },
        }),
        _ => None,
    }
}

/// How a released move ended (a record from before `ending` was kept says
/// it only through its give-back).
pub(super) fn ending(record: &Record) -> Ending {
    record
        .ending
        .unwrap_or(if record.given_back || !record.kept.is_empty() {
            Ending::GivenBack
        } else {
            Ending::NothingThere
        })
}

/// Where the moved bank's copy is, after a give-back.
fn kept_line(record: &Record) -> String {
    if record.kept.is_empty() {
        return if record.phase == Phase::Released && ending(record) == Ending::GivenBack {
            format!(
                "The move's restore never replaced {}'s bank, so there was nothing of it to keep there",
                record.target
            )
        } else {
            String::new()
        };
    }
    format!(
        "The moved bank as it stood on {}, with any writes it took there, is kept in {} on {}; nothing was deleted",
        record.target,
        record.kept.join(" and "),
        record.target
    )
}

/// The unresolved causes, as the record and every answer give them.
const UNKNOWN_RESTORE_LOST: &str = "this move's restore was sent and its answer never arrived, so it cannot be told whether it ran there";
const UNKNOWN_RESTORE_UNRECORDED: &str = "this host's record shows this move's restore was sent, or about to be, and no answer to it is recorded on this host, so it cannot be told whether it ran there";
const NOT_READ_BACK: &str =
    "the target could not be read after this move's restore, so its bank cannot be shown unchanged";
const RESTORE_TARGET_IDENTITIES: &str = "restore the target's original identity settings";
const PROVE_TARGET_STOPPED: &str = "verify the target is stopped and gated";

fn remember_target_daemon(record: &mut Record, state: &State) {
    if record.target_pins.is_some() {
        record.verified_target_daemon = state
            .daemon_id
            .clone()
            .filter(|id| super::probe::container_id(id));
    }
}

/// The rollback: R1 to R16 in order, every undo attempted, I2 checked on a
/// fresh reading before the fence lifts.
struct Back<'a, H: Hosts> {
    hosts: &'a mut H,
    record: Record,
    state: Option<State>,
    /// This rollback runs in the invocation that sent the move's restore,
    /// so its reading follows that restore within the same run.
    same_run: bool,
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
            pins: self.record.target_pins.clone(),
            op,
        };
        let answer = self.hosts.target(&request, None)?;
        if let Some(state) = &answer.state {
            self.state = Some(state.clone());
            remember_target_daemon(&mut self.record, state);
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
            pins: self.record.target_pins.clone(),
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

    /// What the gated bank must still match: the first reading after this
    /// move's restore, taken by the invocation that ran it, or the reading
    /// the last rollback kept when this move sent no restore. `Ok(None)`:
    /// this is that invocation and `state` is its first reading. `Err`: no
    /// trustworthy reading exists, and why.
    fn reference(&self, state: &State) -> Result<Option<Baseline>, String> {
        let record = &self.record;
        if record.restore_sent {
            if !record.restore_dropped {
                return Err(if self.same_run {
                    UNKNOWN_RESTORE_LOST
                } else {
                    UNKNOWN_RESTORE_UNRECORDED
                }
                .to_owned());
            }
            if let Some(mutations) = &record.restored_mutations {
                return Ok(Some(Baseline {
                    user_counts: record.restored_counts.clone(),
                    mutations: mutations.clone(),
                }));
            }
            if self.same_run && state.mutations.is_some() {
                return Ok(None);
            }
            return Err(NOT_READ_BACK.to_owned());
        }
        record
            .gated_baseline
            .clone()
            .map(Some)
            .ok_or_else(|| NOT_READ_BACK.to_owned())
    }

    /// A fresh reading of the target, counting the dump's tables when the
    /// target may have served.
    fn read(&mut self) -> Result<State, String> {
        let flags = &self.record.flags;
        let counted = flags.gate_lifting
            || flags.target_started
            || flags.target_restored
            || flags.gate_written;
        let tables = if counted {
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
                Ok(Marker::Names(id)) if id == self.record.move_id => {
                    return self.committed_after_all();
                }
                Ok(Marker::Names(other)) => {
                    self.record.phase = Phase::RollbackIncomplete;
                    self.save();
                    return self.incomplete(
                        reason,
                        &format!(
                            "this host's /data/moved.json names move {other}, not this one, so nothing was rolled back"
                        ),
                        false,
                    );
                }
                Ok(Marker::NamesNone) => self.hosts.say(
                    "rollback: this host's /data/moved.json names no move (its write was cut short), so it is no commit point; the rollback goes on and removes it",
                ),
                Ok(Marker::Absent) => {}
                Err(why) => {
                    self.record.phase = Phase::RollbackIncomplete;
                    self.save();
                    return self.incomplete(
                        reason,
                        &format!(
                            "this host's Docker could not be asked whether /data/moved.json names this move ({why}), so the move may have passed its commit point and nothing was rolled back; once docker answers here (docker ps), rerun the rollback"
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
        // The target holds, or may soon hold, the moved bank: proved stopped
        // and gated before the source comes back. A gate alone (the restore
        // never sent, or refused before its drop) leaves the target its own
        // bank: it is read when it can be, to find a copy the record missed,
        // but an unreadable target does not keep the source down.
        let strict = flags.target_restored || flags.gate_lifting || flags.target_started;
        let needs_proof = strict || flags.gate_written;
        // Whether the target holds the moved bank: the record's word, or a
        // reading that finds it there although the record says it never got
        // that far.
        let mut restored = flags.target_restored;
        // First, no forward step of this move may run on the target any
        // more: one still running there (its ssh gone) finishes under the
        // target's update lock before the abort is written.
        let aborted = !flags.claimed || self.abort();
        if !aborted {
            complete = false;
        }
        let mut proved = !needs_proof;
        // Writes a resume found in the gated bank before it would replace it.
        let mut parked: Option<String> = self.record.parked_writes.clone();
        // Why the gated bank cannot be shown unchanged, once found so.
        let mut unresolved: Option<String> = self.record.unresolved.clone();
        let served = flags.gate_lifting || flags.target_started;
        // What a bank this move never served shows of someone else's writes.
        let mut found: Vec<String> = Vec::new();
        let mut identities_restored = !flags.identities_applied;
        if needs_proof {
            if flags.gate_lifting || flags.target_started {
                let result = self.op(Op::Stop).map(|_| ());
                self.attempt("stop the target daemon", result);
            }
            if flags.identities_applied && aborted {
                self.record.verified_target_daemon = None;
                let result = self.op(Op::CarryUndo).map(|_| ());
                identities_restored = self.attempt(RESTORE_TARGET_IDENTITIES, result);
            }
            for _ in 0..3 {
                let state = match self.read() {
                    Ok(state) => state,
                    Err(why) => {
                        self.attempt("read the target's state", Err(why));
                        continue;
                    }
                };
                // Whatever else it shows, a reading that finds the moved bank
                // there means the full proof, the record's word aside.
                if !restored && state.bank.as_deref() == Some(self.record.source_bank.as_str()) {
                    restored = true;
                    self.record.flags.target_restored = true;
                }
                match state.running {
                    Some(true) => {
                        if restored && !served {
                            found.push(
                                "the gated bank was found running, so it may have served writes"
                                    .to_owned(),
                            );
                        }
                        let result = self.op(Op::Stop).map(|_| ());
                        self.attempt("stop the target daemon again", result);
                    }
                    Some(false) => {
                        if state.gated != Some(true) {
                            if restored && !served {
                                found.push(
                                    "the bank was found without /data/move.json, so it may have been started"
                                        .to_owned(),
                                );
                            }
                            let result = self.op(Op::Gate).map(|_| ());
                            self.attempt("keep the target from starting (/data/move.json)", result);
                            continue;
                        }
                        proved = true;
                        if restored && !served {
                            match self.reference(&state) {
                                Ok(Some(reference)) => {
                                    found.extend(since_baseline(Some(&reference), &state));
                                }
                                // This run's own first reading after its
                                // restore: it is the reference from now on.
                                Ok(None) => {
                                    self.record.restored_counts = state.user_counts.clone();
                                    self.record.restored_mutations = state.mutations.clone();
                                    self.record.restored_reading = Some(Reading::Rollback);
                                }
                                // No trustworthy reading to compare with: the
                                // bank is kept, and no writes are claimed.
                                Err(cause) => {
                                    unresolved.get_or_insert(cause);
                                }
                            }
                            if !found.is_empty() {
                                let mut all: Vec<String> = parked.iter().cloned().collect();
                                all.extend(found.iter().cloned());
                                parked = Some(all.join("; "));
                            }
                        }
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
                        // The gated bank as this rollback leaves it: what a
                        // resume must still find before it replaces it. Never
                        // a reading that could not be compared with one.
                        if restored && parked.is_none() && unresolved.is_none() {
                            self.record.gated_baseline =
                                state.mutations.clone().map(|mutations| Baseline {
                                    user_counts: state.user_counts.clone(),
                                    mutations,
                                });
                        }
                        break;
                    }
                    None => {}
                }
            }
        }
        // Without the abort, a step still on its way could start the target
        // after this reading.
        if (strict || restored) && !aborted {
            proved = false;
        }
        // Only a gate, and no reading found the moved bank: nothing to prove.
        if !strict && !restored {
            proved = true;
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
        if !identities_restored {
            self.record.phase = Phase::RollbackIncomplete;
            self.attempt(PROVE_TARGET_STOPPED, Ok(()));
            return self.incomplete(reason, "the target's identity change could not be undone. The target was verified stopped and gated. The record lists the source recovery steps", false);
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
        // Never the moved bank's gate, nor the claim while it is there.
        let reads_moved = self.state.as_ref().and_then(|s| s.bank.as_deref())
            == Some(self.record.source_bank.as_str());
        if flags.gate_written && !restored && !flags.gate_lifting && !reads_moved {
            let result = self.op(Op::Ungate).map(|_| ());
            complete &= self.attempt("remove the gate from the target's own bank", result);
        }
        if flags.target_quiesced && !restored && !reads_moved {
            let result = self.op(Op::Start).map(|_| ());
            complete &= self.attempt("start the target's own daemon again", result);
        }
        if flags.claimed && !restored && !reads_moved {
            let result = self.op(Op::Release).map(|_| ());
            complete &= self.attempt("release the target's claim", result);
        }
        if let Some(tables) = &parked {
            self.record.parked_writes = Some(tables.clone());
        }
        if let Some(cause) = &unresolved {
            self.record.unresolved = Some(cause.clone());
        }
        if complete && parked.is_none() && unresolved.is_none() {
            self.record.phase = Phase::RolledBack;
            self.save();
            let note = if restored {
                format!(
                    "; the target keeps the restored bank stopped behind /data/move.json, and its claim ({} on {}). Rerun the move with --resume to finish it, or give {} back its own bank with pseudolife-mcp move --release-target {}",
                    claim_path(),
                    self.record.target,
                    self.record.target,
                    self.record.move_id
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
        if complete && let Some(cause) = &unresolved {
            // Finished, but the bank the target keeps cannot be shown to be
            // this host's as it stands: no resume replaces it unseen.
            self.record.phase = Phase::RolledBack;
            self.save();
            let why = format!(
                "{cause}; that bank stays stopped behind /data/move.json on {}, it is not resumed over, and nothing was deleted",
                self.record.target
            );
            return self.incomplete(reason, &why, true);
        }
        self.record.phase = Phase::RollbackIncomplete;
        self.save();
        let why = match (&parked, &unresolved) {
            (Some(tables), _) => format!(
                "some undo steps failed, and the target took writes while it served, which are not in the source ({tables}); they are in the stopped, gated target bank on {}, and nothing was deleted",
                self.record.target
            ),
            (None, Some(cause)) => format!(
                "some undo steps failed, and {cause}; that bank stays stopped behind /data/move.json on {}, and nothing was deleted",
                self.record.target
            ),
            (None, None) => {
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

    /// `--release-target`'s steps, each safe to repeat: whatever a killed
    /// run did, a rerun finishes it, and never restores over a bank the
    /// release already gave back.
    fn release(&mut self) -> Outcome {
        let id = self.record.move_id.clone();
        let to = self.record.target.clone();
        let bank = self.record.source_bank.clone();
        let unfinished = |back: &mut Self, step: &str, why: String| {
            back.record.rollback.push(Undo {
                step: step.to_owned(),
                ok: false,
                detail: why.clone(),
            });
            back.save();
            let resort = if step == "read the target's state" {
                String::new()
            } else {
                format!("\n  {}", last_resort(&back.record))
            };
            Outcome {
                code: EXIT_INCOMPLETE,
                message: format!(
                    "giving {to} back its own bank did not finish: {step} failed ({why}).\n  running now: this host's daemon, serving your bank ({bank})\n  served: yes, by this host's daemon\n  data: your bank here; on {to}, the moved copy stays stopped behind /data/move.json until its own bank is back\n  next: pseudolife-mcp move --release-target {id} (each step is safe to repeat){resort}"
                ),
            }
        };
        let state = match self.read() {
            Ok(state) => state,
            Err(why) => {
                let mut outcome = unfinished(self, "read the target's state", why);
                outcome.message.push_str(&format!(
                    "\n  if {to} will never answer again: pseudolife-mcp move --release-target {id} --target-gone (it gives {to} up on this host only, for good, while {to} gives no answer)"
                ));
                return outcome;
            }
        };
        // Only a reading Docker answered tells what the target holds: a blank
        // one is no proof that nothing of the move is there.
        let positive = state.running.is_some()
            && state.gated.is_some()
            && (state.bank.is_some() || state.empty.is_some());
        if !positive {
            return unfinished(
                self,
                "read the target's state",
                format!(
                    "{to} answered, but its Docker did not say whether the daemon runs, whether /data/move.json is there, or which bank it holds; once docker answers there (ssh {to} docker ps), rerun"
                ),
            );
        }
        let ours = state.claim.as_deref() == Some(id.as_str());
        let holds_bank = state.bank.as_deref() == Some(bank.as_str());
        // No claim and no bank identity behind a gate: a copy of this move's
        // cut short and another bank cannot be told apart, so nothing is sent.
        if state.claim.is_none() && state.gated == Some(true) && state.bank.is_none() {
            let why = format!(
                "{to} reads /data/move.json with no claim and no bank identity behind it, so it cannot be told whether that is this move's copy cut short or another bank; nothing was sent to change it"
            );
            self.record.rollback.push(Undo {
                step: "identify the target's bank".to_owned(),
                ok: false,
                detail: why.clone(),
            });
            self.save();
            return Outcome {
                code: EXIT_INCOMPLETE,
                message: format!(
                    "giving {to} back its own bank did not start: {why}.\n  running now: this host's daemon, serving your bank ({bank})\n  served: yes, by this host's daemon\n  data: your bank here; on {to}, whatever is behind /data/move.json stays as it is\n  next: look at what {to} holds; once its bank can be identified, rerun pseudolife-mcp move --release-target {id}\n  {}",
                    last_resort(&self.record)
                ),
            };
        }
        // Neither this move's claim nor its bank: nothing of this host's bank
        // is there, and nothing there is this move's to change.
        if !ours && !holds_bank {
            return self.nothing_there(&state);
        }
        self.record.phase = Phase::Releasing;
        self.save();
        // What this run changed on the target before its give-back, for an
        // answer that refuses after them.
        let mut took: Vec<&str> = Vec::new();
        // The claim is gone (removed by hand) while the target still holds
        // this move's bank: taken again, which only an unclaimed target allows.
        let reclaim = state.claim.is_none() && holds_bank;
        if reclaim {
            let claim = Op::Claim {
                source_bank: bank.clone(),
                resume: false,
            };
            if let Err(why) = self.op(claim) {
                return unfinished(self, "claim the target for this release again", why);
            }
            took.push("claimed it again for this move (its claim had been removed)");
        }
        if state.bank.as_deref() == Some(bank.as_str()) {
            if state.running != Some(false) {
                if let Err(why) = self.op(Op::Stop) {
                    return unfinished(self, "stop the target daemon", why);
                }
                took.push("stopped its daemon, which held the moved copy");
            }
            if state.gated != Some(true) {
                if let Err(why) = self.op(Op::Gate) {
                    return unfinished(
                        self,
                        "keep the target from starting (/data/move.json)",
                        why,
                    );
                }
                took.push("put /data/move.json in front of the moved copy");
            }
        }
        self.hosts
            .say("release: restore the target's own bank (its restore keeps the moved bank in a safety backup)");
        let request = Request {
            protocol: PROTOCOL,
            move_id: id.clone(),
            work_dir: self.record.work_dir.clone(),
            rollback: true,
            pins: self.record.target_pins.clone(),
            op: Op::GiveBack,
        };
        let mut waited = Duration::ZERO;
        let result = loop {
            match self.hosts.target(&request, None) {
                Ok(answer) if !answer.ok && answer.busy && waited < FORWARD_BUSY_WAIT => {
                    if waited.is_zero() {
                        self.hosts.say(
                            "release: waiting for the target's update lock (another update, restore or backup holds it)",
                        );
                    }
                    self.hosts.sleep(ABORT_PAUSE);
                    waited += ABORT_PAUSE;
                }
                other => break other,
            }
        };
        let kept = match result {
            Ok(answer) if answer.ok => answer.kept,
            // Refused before its restore began: the bank there is as it was.
            Ok(answer) if !answer.restore_dropped => {
                return self.refused_give_back(&answer.error, &took, reclaim);
            }
            Ok(answer) => return unfinished(self, "restore the target's own bank", answer.error),
            Err(lost) => {
                return unfinished(
                    self,
                    "restore the target's own bank",
                    format!("the target did not answer ({lost}); its outcome is unknown"),
                );
            }
        };
        self.record.kept = kept;
        self.record.given_back = true;
        self.save();
        self.hosts
            .say("release: lift the gate from the target's own bank");
        if let Err(why) = self.op(Op::Ungate) {
            return unfinished(self, "remove the gate from the target's own bank", why);
        }
        if self.record.target_was_running {
            let running = self.read().ok().and_then(|s| s.running);
            if running != Some(true)
                && let Err(why) = self.op(Op::Start)
            {
                return unfinished(self, "start the target's own daemon", why);
            }
        }
        if let Err(why) = self.op(Op::Release) {
            return unfinished(self, "release the target's claim", why);
        }
        self.record.phase = Phase::Released;
        self.record.ending = Some(Ending::GivenBack);
        self.save();
        Outcome {
            code: EXIT_OK,
            message: format!(
                "released: {to} has its own bank back{} and is free for a new move. Your bank is this host's ({bank}), which this host's daemon serves. {}",
                if self.record.target_was_running {
                    " and runs again"
                } else {
                    ""
                },
                kept_line(&self.record)
            ),
        }
    }

    /// The release read the target, positively, and it holds nothing of
    /// this host's bank: the record closes, and nothing there changes.
    fn nothing_there(&mut self, state: &State) -> Outcome {
        let id = self.record.move_id.clone();
        let to = self.record.target.clone();
        let bank = self.record.source_bank.clone();
        let found = format!(
            "{}, {}",
            state.claim.as_deref().map_or_else(
                || "no claim".to_owned(),
                |other| format!("move {other}'s claim")
            ),
            state.bank.as_deref().map_or_else(
                || "no bank identity".to_owned(),
                |other| format!("bank {other}")
            )
        );
        let gate = if state.gated == Some(true) {
            format!(
                " This move's gate, /data/move.json, is still there, so {to}'s daemon does not start; removing it is for whoever runs {to}, once they are sure that bank is the one it should serve: {}",
                gate_removal(&self.record)
            )
        } else {
            String::new()
        };
        let given_back = self.record.given_back || !self.record.kept.is_empty();
        self.record.phase = Phase::Released;
        self.record.ending = Some(if given_back {
            Ending::GivenBack
        } else {
            Ending::NothingThere
        });
        self.save();
        if given_back {
            return Outcome {
                code: EXIT_OK,
                message: format!(
                    "released: {to} has its own bank back and is free for a new move (it reads {found}). Your bank is this host's ({bank}), which this host's daemon serves. {}{gate}",
                    kept_line(&self.record)
                ),
            };
        }
        Outcome {
            code: EXIT_OK,
            message: format!(
                "released: {to} no longer holds anything of move {id}'s copy of this host's bank: it was read and reads {found}. This host's record of the move is closed, so it no longer holds this host, and nothing was changed on {to}. Your bank is this host's ({bank}), which this host's daemon serves.{gate}"
            ),
        }
    }

    /// The give-back refused before its restore began: what this run took
    /// on the target is said, and the claim it took is given up again.
    fn refused_give_back(&mut self, error: &str, took: &[&str], reclaimed: bool) -> Outcome {
        let id = self.record.move_id.clone();
        let to = self.record.target.clone();
        let bank = self.record.source_bank.clone();
        let mut done = String::new();
        if took.is_empty() {
            done.push_str(" Nothing was changed there.");
        } else {
            done.push_str(&format!(" This release {}", took.join(", ")));
            if reclaimed {
                match self.op(Op::Release) {
                    Ok(_) => done.push_str("; it has released that claim again."),
                    Err(why) => done.push_str(&format!(
                        "; that claim ({}) could not be released again ({why}), and stays until a rerun of this release finishes, or until it is removed by hand: ssh {to} rm -f {}.",
                        claim_path(),
                        claim_path()
                    )),
                }
            } else {
                done.push('.');
            }
        }
        self.record.phase = Phase::RolledBack;
        self.save();
        Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "{to} cannot give back its own bank: it reported {error}.{done} Your bank is this host's ({bank}), which this host's daemon serves. Fix what {to} names, then rerun pseudolife-mcp move --release-target {id}; {}",
                last_resort(&self.record)
            ),
        }
    }

    /// The source holds this move's `moved.json`: the move passed its commit
    /// point, so the record says so and nothing is rolled back.
    fn committed_after_all(&mut self) -> Outcome {
        self.record.committed = true;
        self.record.phase = Phase::MovedFollowUps;
        let to = self.record.target.clone();
        let url = self.record.target_url.clone();
        let steps = if self.record.follow_ups.is_empty() {
            planned_follow_ups(&self.record)
        } else {
            self.record.follow_ups.clone()
        };
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
            (Some(true), Some(true)) => format!(
                "both daemons. Stop the selected target now. {}",
                fill_in_container_id(format!(
                    "ssh {{to}} docker stop {}",
                    manual_target_daemon(&self.record)
                ))
            ),
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
        let next = if finished && self.record.parked_writes.is_none() {
            format!(
                "pseudolife-mcp move --release-target {} gives {} back its own bank and keeps the gated bank, whatever it holds, in a backup there that it names",
                self.record.move_id, self.record.target
            )
        } else if finished {
            format!(
                "decide about those writes: pseudolife-mcp move --release-target {} gives {} back its own bank and keeps the gated bank, writes and all, in a backup there that it names",
                self.record.move_id, self.record.target
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

/// Everything a move has left after its commit point, as commands for a
/// person: written before the commit, struck off as each step succeeds.
fn planned_follow_ups(record: &Record) -> Vec<String> {
    let to = &record.target;
    let mut steps = vec![repoint_line(
        record,
        "point this machine's clients at the moved bank",
    )];
    if record.flags.target_policy_paused {
        steps.push(format!(
            "ssh {to} docker update --restart={} pseudolife-mcp-daemon",
            record.target_policy
        ));
    }
    if record.flags.target_timer_paused {
        steps.push(format!(
            "ssh {to} systemctl --user enable --now pseudolife-update.timer"
        ));
    }
    steps.push(format!("ssh {to} rm -f {}", claim_path()));
    steps
}

fn repoint_line(record: &Record, detail: &str) -> String {
    let argument = if record.carried_identities {
        "<a file holding a carried source token>".to_owned()
    } else {
        format!("<a file holding a token of {}'s>", record.target)
    };
    format!(
        "pseudolife-mcp connect {} --token-file {argument} --yes   # {detail}",
        record.target_url
    )
}

/// The tables a resume counts on the target: its own dump's and those its
/// baseline names.
fn baseline_tables(baseline: Option<&Baseline>, dump: &BTreeMap<String, i64>) -> Vec<String> {
    let mut tables: Vec<String> = dump.keys().cloned().collect();
    if let Some(baseline) = baseline {
        tables.extend(baseline.user_counts.keys().cloned());
    }
    tables.sort();
    tables.dedup();
    tables
}

/// What a resume finds in the gated bank since its baseline: writes, or
/// every reason they cannot be ruled out (which counts as writes: a resume
/// never replaces what it cannot rule out).
pub(super) fn since_baseline(baseline: Option<&Baseline>, state: &State) -> Vec<String> {
    let mut found = Vec::new();
    if state.gated != Some(true) {
        found.push(format!(
            "the bank is {} behind /data/move.json, so it may have been started",
            if state.gated == Some(false) {
                "no longer"
            } else {
                "not known to be"
            }
        ));
    }
    let Some(baseline) = baseline else {
        found.push(
            "no reading of the gated bank was kept at this move's last rollback, so writes since cannot be ruled out"
                .to_owned(),
        );
        return found;
    };
    found.extend(differing(&baseline.user_counts, &state.user_counts));
    found.extend(changed(Some(&baseline.mutations), state.mutations.as_ref()));
    found
}

/// `move --release-target <id>`: gives the target back its own bank, for a
/// rolled-back move whose target kept the restored one. The moved bank,
/// writes and all, ends in the restore's safety backup there, which the
/// answer names; nothing is deleted.
pub(super) fn release_target<H: Hosts>(hosts: &mut H, record: Record) -> Outcome {
    let id = record.move_id.clone();
    let to = record.target.clone();
    if record.committed {
        return Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "move {id} passed its commit point: the bank lives on {to}, which is not released. Nothing was changed"
            ),
        };
    }
    match record.phase {
        Phase::Released if ending(&record) == Ending::NothingThere => {
            return Outcome {
                code: EXIT_OK,
                message: format!(
                    "move {id} is already released: its release found {to} holding nothing of it, and changed nothing there; nothing to do"
                ),
            };
        }
        Phase::Released => {
            return Outcome {
                code: EXIT_OK,
                message: format!(
                    "{to} was already given back its own bank by move {id}; nothing to do. {}",
                    kept_line(&record)
                ),
            };
        }
        // Final on this host: nothing is sent to a target given up.
        Phase::GivenUp => return given_up_answer(&record, None, Asked::Release),
        Phase::RolledBack | Phase::Releasing => {}
        _ => {
            return Outcome {
                code: EXIT_REFUSED,
                message: format!(
                    "move {id} is not rolled back (its record says {:?}); roll it back first with pseudolife-mcp move --rollback {id}. Nothing was changed",
                    record.phase
                ),
            };
        }
    }
    if !record.flags.target_restored {
        return Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "move {id} never restored into {to}, so {to} keeps its own bank and there is nothing to give back. Nothing was changed"
            ),
        };
    }
    // The bank the user keeps is this host's: it must be serving, or the
    // release could leave no bank served anywhere.
    let serving = hosts.source_running() == Some(true)
        && hosts.source_healthy(RELEASE_SOURCE_WAIT)
        && hosts.source_bank().as_deref() == Some(record.source_bank.as_str());
    if !serving {
        return Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "this host's daemon is not serving its bank, and move {id} no longer holds it stopped: your bank is this host's ({}), so {to}'s copy is not released while nothing serves it. Start this host's daemon (docker start {}) and check that its /health names that bank, then rerun. Nothing was changed",
                record.source_bank,
                record.source.daemon_selector()
            ),
        };
    }
    let mut back = Back {
        hosts,
        record,
        state: None,
        same_run: false,
    };
    back.release()
}

/// Why `--target-gone` cannot end this record, from the record alone, or
/// `None` when it can be tried.
pub(super) fn give_up_refusal(record: &Record) -> Option<Outcome> {
    let id = &record.move_id;
    let to = &record.target;
    if record.committed {
        return Some(Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "move {id} passed its commit point: the bank lives on {to}, which is not given up. Nothing was changed"
            ),
        });
    }
    match record.phase {
        Phase::Released => {
            return Some(Outcome {
                code: EXIT_OK,
                message: format!("move {id} is already released; nothing to do"),
            });
        }
        Phase::GivenUp => return Some(given_up_answer(record, None, Asked::TargetGone)),
        Phase::RolledBack | Phase::Releasing => {}
        Phase::RollbackIncomplete if record.flags.identities_applied => {}
        _ => {
            return Some(Outcome {
                code: EXIT_REFUSED,
                message: format!(
                    "move {id} is not rolled back (its record says {:?}); roll it back first with pseudolife-mcp move --rollback {id}. Nothing was changed",
                    record.phase
                ),
            });
        }
    }
    if !record.flags.target_restored {
        return Some(Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "move {id} never restored into {to}, so {to} keeps its own bank and this host is not held by it; there is nothing to give up. Nothing was changed"
            ),
        });
    }
    None
}

/// `move --release-target <id> --target-gone`: gives up a target that
/// does not answer, on this host only. It ends only a record whose source
/// already serves its own bank, never starts a fenced source, and changes
/// nothing on the target: a target that answers is given back instead.
/// The given-up record and what to say, or a refusal.
pub(super) fn give_up<H: Hosts>(
    hosts: &mut H,
    record: &Record,
) -> Result<(Record, Outcome), Outcome> {
    if let Some(refusal) = give_up_refusal(record) {
        return Err(refusal);
    }
    let id = record.move_id.clone();
    let to = record.target.clone();
    let serving = hosts.source_running() == Some(true)
        && hosts.source_healthy(RELEASE_SOURCE_WAIT)
        && hosts.source_bank().as_deref() == Some(record.source_bank.as_str());
    if !serving {
        return Err(Outcome {
            code: EXIT_REFUSED,
            message: format!(
                "this host's daemon is not serving its bank, so {to} is not given up: your bank is this host's ({}). Recover the source using move {id}'s rollback instructions and verify its /health names that bank, then rerun. Nothing was changed",
                record.source_bank
            ),
        });
    }
    let request = Request {
        protocol: PROTOCOL,
        move_id: id.clone(),
        work_dir: record.work_dir.clone(),
        rollback: true,
        pins: record.target_pins.clone(),
        op: Op::State { tables: Vec::new() },
    };
    // Any answer, a refusal included, means the target is there. A give-up
    // is final, so one failed attempt is not enough to conclude.
    let mut why = String::new();
    for attempt in 1..=GIVE_UP_ATTEMPTS {
        match hosts.target(&request, None) {
            Ok(_) => {
                return Err(Outcome {
                    code: EXIT_REFUSED,
                    message: format!(
                        "{to} answers as a move target, so it is not given up: give it back its own bank with pseudolife-mcp move --release-target {id}. Nothing was changed"
                    ),
                });
            }
            Err(failed) => why = failed,
        }
        if attempt < GIVE_UP_ATTEMPTS {
            hosts.sleep(GIVE_UP_PAUSE);
        }
    }
    let why = format!(
        "{GIVE_UP_ATTEMPTS} attempts, {} seconds apart; the last: {why}",
        GIVE_UP_PAUSE.as_secs()
    );
    let mut released = record.clone();
    released.phase = Phase::GivenUp;
    released.ending = Some(Ending::GivenUp);
    released.updated_at = hosts.now();
    released.rollback.push(Undo {
        step: format!("give up {to}: it gave no answer as a move target (--target-gone)"),
        ok: true,
        detail: why.clone(),
    });
    let writes = match (&record.parked_writes, &record.unresolved) {
        (Some(tables), _) => {
            format!(" That copy took writes there that are not in this host's bank ({tables}).")
        }
        (None, Some(cause)) => format!(" Of that copy, {cause}."),
        (None, None) => String::new(),
    };
    let outcome = Outcome {
        code: EXIT_OK,
        message: format!(
            "given up: {to} gave no answer as a move target ({why}), so move {id} is closed on this host only, and nothing was changed on {to}. Your bank is this host's ({}), which this host's daemon serves; writes made on {to} are not in it.{writes} {}",
            record.source_bank,
            given_up_line(&released, None)
        ),
    };
    Ok((released, outcome))
}

/// The move's folder on the target, as the record knows it.
fn remote_folder(record: &Record) -> String {
    if record.remote_folder.is_empty() {
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
    }
}

/// The command that removes the move's gate on the target, for a person.
fn gate_removal(record: &Record) -> String {
    gate_removal_for(record, manual_target_daemon(record))
}

fn gate_removal_for(record: &Record, daemon: &str) -> String {
    fill_in_container_id(format!(
        "ssh {} docker run --rm --entrypoint rm --volumes-from {} {} -f /data/move.json",
        record.target, daemon, record.target_image
    ))
}

fn fill_in_container_id(command: String) -> String {
    if command.contains("<verified-") {
        format!("Fill in the verified container ID before running: {command}")
    } else {
        command
    }
}

fn manual_target_daemon(record: &Record) -> &str {
    if let Some(id) = record
        .verified_target_daemon
        .as_deref()
        .filter(|id| super::probe::container_id(id))
    {
        return id;
    }
    match &record.target_pins {
        Some(_) if record.flags.identities_applied => "<verified-target-daemon-id>",
        Some(pins) => &pins.daemon,
        None => "pseudolife-mcp-daemon",
    }
}

fn manual_target_selection(record: &Record) -> Option<String> {
    let pins = record.target_pins.as_ref()?;
    let selected = manual_target_daemon(record);
    if selected.starts_with('<') {
        Some(format!(
            "On the target, substitute the daemon ID saved in {}/carry.json (pins.daemon) for <verified-target-daemon-id>. Verify that Docker returns that full ID for the expected Compose service. If the journal was never created, the selected daemon ID was {}. If a recreation began without recording its replacement, verify the original Compose project and its volumes before proceeding.",
            remote_folder(record),
            selected
        ))
    } else {
        Some(format!(
            "On the target, verify that Docker returns the selected daemon's full ID {} before running these commands.",
            pins.daemon
        ))
    }
}

/// The last way to close a record whose target keeps refusing: on this
/// host, by hand, with what it leaves on the target.
fn last_resort(record: &Record) -> String {
    format!(
        "if that cannot be put right there, the last resort is on this host: rename {} to record.json.closed, which closes this host's record of move {} so it no longer holds this host. {} then stays as it is, gated and perhaps claimed, until whoever runs it gives it back its own bank and then removes the gate ({}) and the claim (ssh {} rm -f {}); never the gate first, or the copy of this host's bank behind it would start there",
        Path::new(&record.local_dir).join("record.json").display(),
        record.move_id,
        record.target,
        gate_removal(record),
        record.target,
        claim_path()
    )
}

/// What a given-up record says: final on this host, and what the person who
/// runs the target can clear there by hand.
pub(super) fn given_up_line(record: &Record, newer: Option<&Record>) -> String {
    let to = &record.target;
    let at = &record.updated_at;
    let folder = remote_folder(record);
    let (environment, daemon) = if record.flags.identities_applied {
        (
            format!(
                "Keep the target daemon stopped and gated while restoring its configuration. Remove only this move's appended # Pseudolife move {} block from the target's environment file. In {folder}/carry.json, deployment.env_file names the file and original_bytes and original_digest identify the original prefix. Use that prefix only after verifying its digest and checking for later edits. If the file is already restored, verify its original settings. Recreate the selected daemon stopped with the verified environment, and fill in its new verified container ID before running later Docker commands. ",
                record.move_id
            ),
            "<verified-target-daemon-id>",
        )
    } else {
        (String::new(), manual_target_daemon(record))
    };
    let start = fill_in_container_id(format!("ssh {to} docker start {daemon}"));
    let gate = gate_removal_for(record, daemon);
    let claim = format!("ssh {to} rm -f {}", claim_path());
    let hands = if record.given_back || !record.kept.is_empty() {
        format!(
            "{to} already has its own bank back; what may remain there is the gate ({gate}) and the claim ({claim}), and the moved bank is kept in {}",
            if record.kept.is_empty() {
                format!("{folder}/safety")
            } else {
                record.kept.join(" and ")
            }
        )
    } else {
        format!(
            "{to} then held the move's claim ({}), its gate /data/move.json and the stopped copy of this host's bank behind it. Its own bank first, from the safety backup that {folder}/own-bank.json names in {folder}/safety (on {to}, fill in both backup paths in pseudolife-mcp restore --backup-file <that dump> --state-archive <that state archive> --leave-stopped; if own-bank.json is missing, no command restores it, so look in {folder}/safety before anything else); then the gate, never before its own bank is back, or the copy of this host's bank behind it would start there ({gate}); then the claim ({claim}); then its daemon ({start})",
            claim_path()
        )
    };
    let newer = newer.map_or_else(String::new, |later| {
        format!(
            " This host's records show a newer move to {to}, move {} ({}), so {to} may now hold a bank that move put there.",
            later.move_id,
            if later.committed {
                "committed: the bank went there".to_owned()
            } else {
                format!("its record says {:?}", later.phase)
            }
        )
    });
    let selection =
        manual_target_selection(record).map_or_else(String::new, |line| format!(" {line}"));
    format!(
        "This host gave {to} up at {at} (--target-gone), and that is final here: nothing is sent to {to} from this host for move {} again, and --release-target and --resume refuse it.{newer} The steps below describe {to} as it was at {at}, for whoever runs it. Check first: look at what {to} holds now, and if its daemon serves a bank, or a newer move has put one there, do none of this.{selection} Otherwise, in this order: {environment}{hands}",
        record.move_id
    )
}

/// Which command a given-up record answers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Asked {
    Rollback,
    Release,
    TargetGone,
}

/// The answer to a command about a given-up record, from the record and the
/// newest later move to the same target (`newer`).
pub(super) fn given_up_answer(record: &Record, newer: Option<&Record>, asked: Asked) -> Outcome {
    let id = &record.move_id;
    let to = &record.target;
    let line = given_up_line(record, newer);
    match asked {
        Asked::Rollback => Outcome {
            code: EXIT_OK,
            message: format!("move {id} was rolled back; nothing to roll back. {line}"),
        },
        Asked::Release => Outcome {
            code: EXIT_REFUSED,
            message: format!("{to} is not released from this host: {line}"),
        },
        Asked::TargetGone => Outcome {
            code: EXIT_OK,
            message: format!(
                "{to} is already given up on this host by move {id}; nothing to do. {line}"
            ),
        },
    }
}

/// The commands that undo what the record says was done, in rollback order,
/// for a person. A carried move needs its verified identity rollback
/// before the remaining steps can be performed by hand.
pub(super) fn manual_rollback(record: &Record) -> Vec<String> {
    let f = &record.flags;
    let to = &record.target;
    let mut lines = Vec::new();
    let daemon = manual_target_daemon(record);
    if (f.gate_lifting
        || f.target_started
        || f.target_restored
        || f.target_policy_paused
        || f.target_quiesced)
        && let Some(selection) = manual_target_selection(record)
    {
        lines.push(selection);
    }
    if f.gate_lifting || f.target_started {
        lines.push(format!("ssh {to} docker stop {daemon}"));
    }
    if f.target_restored || f.gate_lifting {
        lines.push(format!(
            "ssh {to} docker run --rm --entrypoint touch --volumes-from {daemon} {} /data/move.json",
            record.target_image
        ));
    }
    let identities_pending = f.identities_applied
        && !record
            .rollback
            .iter()
            .rfind(|undo| undo.step == RESTORE_TARGET_IDENTITIES)
            .is_some_and(|undo| undo.ok);
    let target_proved = record
        .rollback
        .iter()
        .any(|undo| undo.step == PROVE_TARGET_STOPPED && undo.ok);
    if identities_pending {
        lines.push(format!(
            "To finish restoring the target's original environment, restore access to its installed Compose deployment and run pseudolife-mcp move --rollback {} from the source. For manual source recovery, first verify that the target is stopped and gated, or that the target and its moved copy are permanently retired. Keep the source stopped and fenced until then.",
            record.move_id
        ));
    }
    if f.target_timer_paused && !identities_pending {
        lines.push(format!(
            "ssh {to} systemctl --user enable --now pseudolife-update.timer"
        ));
    }
    if f.target_policy_paused && !identities_pending {
        lines.push(format!(
            "ssh {to} docker update --restart={} {daemon}",
            record.target_policy
        ));
    }
    let source_steps = lines.len();
    let pg = if record.source.pg_id.is_empty() {
        record.source.pg.as_str()
    } else {
        if f.fenced || f.read_only {
            lines.push(format!(
                "Before changing the source database, verify the target is stopped and gated or permanently retired. Check the PostgreSQL container mounting the original source bank's volumes ({}) in Compose project {}. If the original container was replaced, verify the replacement mounts those same volumes and use its current full ID.",
                record.source.pg_sources.join(", "), record.source.pg_compose_project
            ));
        }
        if record.rollback.iter().any(|undo| {
            !undo.ok
                && matches!(
                    undo.step.as_str(),
                    "lift the source database's fence"
                        | "let the source database take writes again"
                )
        }) {
            "<verified-source-postgres-id>"
        } else {
            record.source.pg_id.as_str()
        }
    };
    if f.fenced {
        lines.push(format!(
            "docker exec {pg} psql -h /var/run/postgresql -p {} -U {} -d postgres -c \"ALTER DATABASE {} WITH ALLOW_CONNECTIONS true\"",
            record.source.port, record.source.user, record.source.db
        ));
    }
    if f.read_only {
        lines.push(format!(
            r#"docker exec {pg} psql -h /var/run/postgresql -p {} -U {} -d postgres -c "ALTER DATABASE {} RESET default_transaction_read_only""#,
            record.source.port, record.source.user, record.source.db
        ));
    }
    if f.moved_written {
        lines.push(format!(
            "docker run --rm --entrypoint rm --volumes-from {} {} -f /data/moved.json",
            record.source.daemon_selector(),
            record.source.image
        ));
    }
    if f.source_policy_changed {
        lines.push(format!(
            "docker update --restart={} {}",
            record.source_policy,
            record.source.daemon_selector()
        ));
    }
    if f.schedule_paused {
        lines.push(record.schedule_enable.clone());
    }
    if f.source_stopped {
        lines.push(format!("docker start {}", record.source.daemon_selector()));
    }
    if f.target_quiesced && !f.target_restored && !identities_pending {
        lines.push(format!("ssh {to} docker start {daemon}"));
    }
    if identities_pending && !target_proved {
        for line in &mut lines[source_steps..] {
            *line = format!(
                "After verifying the target is stopped and gated or permanently retired, {line}"
            );
        }
    }
    lines
        .into_iter()
        .map(|line| {
            if line.starts_with("ssh ")
                || line.starts_with("docker ")
                || line.starts_with("After verifying")
            {
                fill_in_container_id(line)
            } else {
                line
            }
        })
        .collect()
}
