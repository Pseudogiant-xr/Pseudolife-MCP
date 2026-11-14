//! The transaction against a model of both hosts, with a fault injected
//! before or after every call it makes. After each call the model checks:
//!
//! - **I1:** the target never runs the moved bank while the source runs;
//! - **I2:** the source's fence lifts, and the source starts, only while
//!   the target is stopped and gated (or never held the moved bank);
//! - no dump is taken while the source daemon runs.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{Value, json};

use super::engine::{self, Artifact, Dump, Hosts, Plan};
use super::protocol::{Answer, Op, Request, State};
use super::record::{Phase, Record};

const ID: &str = "20261114-080000-abcdef01";
const BANK: &str = "75d0c7a836eb6af5";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    /// The user interrupts just before this call.
    Interrupt,
    /// The call never takes effect and its answer is lost.
    LostBefore,
    /// The call takes effect and its answer is lost.
    LostAfter,
    /// The call is refused (a target `ok: false`, a local error).
    Refused,
    /// The record cannot be written at this call.
    SaveFails,
    /// The move's process dies at this call: nothing after it has effect.
    Killed,
    /// A target operation whose answer is lost and whose effect lands three
    /// calls later (its ssh died; the helper ran on), unless the move was
    /// aborted on the target first.
    Late,
}

const FAULTS: [Fault; 7] = [
    Fault::Interrupt,
    Fault::LostBefore,
    Fault::LostAfter,
    Fault::Refused,
    Fault::SaveFails,
    Fault::Killed,
    Fault::Late,
];

#[derive(Debug, Clone, PartialEq, Eq)]
enum Bank {
    /// The target's own fresh bank.
    Empty,
    /// The restored copy of the source's bank, with `extra` rows written
    /// on the target while it served and one warm-up search row per start
    /// (`warmups`), which a daemon writes on its own.
    Clone {
        extra: i64,
        warmups: i64,
        /// Rows changed in place (no count moves).
        in_place: i64,
    },
}

/// Both hosts' state.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Hostsides {
    source_running: bool,
    source_policy: String,
    schedule: bool,
    fenced: bool,
    read_only: bool,
    moved: bool,
    target_running: bool,
    target_policy: String,
    target_timer: bool,
    gated: bool,
    bank: Bank,
    claim: Option<String>,
    /// The move's forward steps are refused on the target.
    aborted: bool,
    received: Vec<String>,
    clients: String,
}

impl Hostsides {
    fn start() -> Self {
        Self {
            source_running: true,
            source_policy: "unless-stopped".into(),
            schedule: true,
            fenced: false,
            read_only: false,
            moved: false,
            target_running: true,
            target_policy: "unless-stopped".into(),
            target_timer: true,
            gated: false,
            bank: Bank::Empty,
            claim: None,
            aborted: false,
            received: Vec::new(),
            clients: "http://127.0.0.1:8765".into(),
        }
    }

    /// The target daemon runs the moved bank.
    fn target_serves_clone(&self) -> bool {
        self.target_running && matches!(self.bank, Bank::Clone { .. })
    }

    /// The target holds the moved bank, and nothing keeps it from starting.
    fn target_may_start_clone(&self) -> bool {
        matches!(self.bank, Bank::Clone { .. }) && (self.target_running || !self.gated)
    }
}

struct Model {
    hosts: Hostsides,
    /// The call to fault, counted from 1, and how.
    fault: Option<(usize, Fault)>,
    /// Calls that take this fault every time they run, by name, from call
    /// `always_from` on.
    always: Vec<(&'static str, Fault)>,
    always_from: usize,
    calls: usize,
    dead: bool,
    interrupted: bool,
    /// A write lands on the target each time it is started ungated.
    target_takes_writes: bool,
    /// An update in place (no row count moves) lands each start.
    target_writes_in_place: bool,
    /// The source daemon exits as soon as it is started.
    source_crashes: bool,
    /// `--target-url` answers with the moved bank whatever the target does.
    health_lies: bool,
    /// The target daemon exits right after it is started.
    target_crashes: bool,
    /// A Docker restart on both hosts after every call.
    docker_restarts: bool,
    record: Option<Record>,
    /// A late effect: the call it lands at, and the request.
    pending: Option<(usize, Request)>,
    violations: Vec<String>,
    log: Vec<String>,
}

impl Model {
    fn new() -> Self {
        Self {
            hosts: Hostsides::start(),
            fault: None,
            always: Vec::new(),
            always_from: 0,
            calls: 0,
            dead: false,
            interrupted: false,
            target_takes_writes: false,
            target_writes_in_place: false,
            source_crashes: false,
            health_lies: false,
            target_crashes: false,
            docker_restarts: false,
            record: None,
            pending: None,
            violations: Vec::new(),
            log: Vec::new(),
        }
    }

    /// Counts a call and answers which fault, if any, it takes.
    fn enter(&mut self, what: &str) -> Option<Fault> {
        self.calls += 1;
        if let Some((at, request)) = self.pending.take() {
            if at <= self.calls {
                self.log.push(format!(
                    "{}: (late) target {}",
                    self.calls,
                    request.op.name()
                ));
                self.apply(&request);
                self.leave("a late target step");
            } else {
                self.pending = Some((at, request));
            }
        }
        self.log.push(format!("{}: {what}", self.calls));
        if self.dead {
            return Some(Fault::Killed);
        }
        if let Some((_, fault)) = self
            .always
            .iter()
            .find(|(name, _)| *name == what && self.calls >= self.always_from)
        {
            return Some(*fault);
        }
        match self.fault {
            Some((at, fault)) if at == self.calls => {
                if fault == Fault::Killed {
                    self.dead = true;
                }
                if fault == Fault::Interrupt {
                    self.interrupted = true;
                    return None;
                }
                Some(fault)
            }
            _ => None,
        }
    }

    /// After every call: a Docker restart if modelled, then the invariants.
    fn leave(&mut self, what: &str) {
        if self.docker_restarts {
            let h = &mut self.hosts;
            if h.source_policy == "always" && !h.moved {
                // A daemon against a fenced database cannot serve; it runs
                // only in the sense that its container is up.
                h.source_running = true;
            }
            if h.target_policy == "always" && !h.gated {
                h.target_running = true;
            }
        }
        let h = &self.hosts;
        if h.source_running && !h.fenced && h.target_serves_clone() {
            self.violations
                .push(format!("I1 broken after {}: {what}", self.calls));
        }
    }

    /// I2, checked before the fence lifts or the source starts.
    fn check_i2(&mut self, what: &str) {
        if self.hosts.target_may_start_clone() {
            self.violations.push(format!(
                "I2 broken at {}: {what} while the target may run the moved bank",
                self.calls
            ));
        }
    }

    fn local(
        &mut self,
        what: &str,
        effect: impl FnOnce(&mut Self) -> Result<(), String>,
    ) -> Result<(), String> {
        let fault = self.enter(what);
        let result = match fault {
            Some(Fault::Killed) => Err("the process is gone".to_owned()),
            Some(Fault::LostBefore | Fault::Refused | Fault::Late) => Err(format!("{what} failed")),
            Some(Fault::LostAfter) => effect(self).and(Err(format!("{what} timed out"))),
            _ => effect(self),
        };
        self.leave(what);
        result
    }

    fn state(&self) -> State {
        let h = &self.hosts;
        let counts = match h.bank {
            Bank::Clone { extra, warmups, .. } => BTreeMap::from([
                ("public.entries".to_owned(), 3 + extra),
                ("public.meta".to_owned(), 6),
                ("public.retrieval_events".to_owned(), 2 + warmups),
            ]),
            Bank::Empty => BTreeMap::new(),
        };
        // A daemon's own warm-up rows are not user data.
        let user_counts = match h.bank {
            Bank::Clone { extra, .. } => BTreeMap::from([
                ("public.entries".to_owned(), 3 + extra),
                ("public.meta".to_owned(), 6),
                ("public.retrieval_events".to_owned(), 2),
            ]),
            Bank::Empty => BTreeMap::new(),
        };
        State {
            running: Some(h.target_running),
            gated: Some(h.gated),
            restart_policy: h.target_policy.clone(),
            bank: matches!(h.bank, Bank::Clone { .. }).then(|| BANK.to_owned()),
            empty: Some(h.bank == Bank::Empty),
            counts,
            user_counts,
            mutations: Some(match h.bank {
                Bank::Clone {
                    extra, in_place, ..
                } => BTreeMap::from([("entries".to_owned(), 3 + extra + in_place)]),
                Bank::Empty => BTreeMap::new(),
            }),
            folder: format!("/home/dev/.pseudolife-mcp/moves/{ID}"),
            folder_bytes: Some(3 << 20),
            claim: h.claim.clone(),
            update_timer: if h.target_timer {
                "enabled"
            } else {
                "disabled"
            }
            .into(),
        }
    }

    fn apply(&mut self, request: &Request) -> Answer {
        let mut answer = Answer {
            protocol: 2,
            ok: true,
            ..Answer::default()
        };
        let op = &request.op;
        if op.changes()
            && !matches!(op, Op::Claim { .. } | Op::Release | Op::Abort)
            && self.hosts.claim.as_deref() != Some(request.move_id.as_str())
        {
            answer.ok = false;
            answer.error = "the target's claim does not name this move".into();
            return answer;
        }
        let resumes = matches!(op, Op::Claim { resume: true, .. });
        if op.changes()
            && !request.rollback
            && !resumes
            && !matches!(op, Op::Abort)
            && self.hosts.aborted
        {
            answer.ok = false;
            answer.error = "the move is being rolled back on its source".into();
            return answer;
        }
        let h = &mut self.hosts;
        match op {
            Op::Claim { resume, .. } => match &h.claim {
                Some(other) if *other != request.move_id => {
                    answer.ok = false;
                    answer.error = format!("move {other} holds the target");
                }
                _ => {
                    h.claim = Some(request.move_id.clone());
                    if *resume {
                        h.aborted = false;
                    }
                }
            },
            Op::State { .. } | Op::Check { .. } | Op::Snapshot => {}
            Op::TimerPause => h.target_timer = false,
            Op::TimerResume => h.target_timer = true,
            Op::PolicyNo => {
                answer.policy = h.target_policy.clone();
                h.target_policy = "no".into();
            }
            Op::PolicySet { policy } => h.target_policy = policy.clone(),
            Op::Receive { name, .. } => h.received.push(name.clone()),
            Op::Stop => h.target_running = false,
            Op::Start => {
                if h.gated {
                    answer.ok = false;
                    answer.error = "the daemon refused to start: /data/move.json".into();
                } else {
                    h.target_running = true;
                }
            }
            Op::Gate => h.gated = true,
            Op::Ungate => h.gated = false,
            Op::StartGated => {
                h.gated = false;
                h.target_running = !self.target_crashes;
                if let Bank::Clone {
                    extra,
                    warmups,
                    in_place,
                } = &mut h.bank
                {
                    // Every start writes its warm-up search row.
                    *warmups += 1;
                    if self.target_takes_writes {
                        *extra += 1;
                    }
                    if self.target_writes_in_place {
                        *in_place += 1;
                    }
                }
            }
            Op::Restore => {
                h.bank = Bank::Clone {
                    extra: 0,
                    warmups: 0,
                    in_place: 0,
                }
            }
            Op::Abort => h.aborted = true,
            Op::Release => {
                if h.claim.as_deref() == Some(request.move_id.as_str()) {
                    h.claim = None;
                }
            }
        }
        answer.state = Some(self.state());
        answer
    }
}

impl Hosts for Model {
    fn source_stop(&mut self) -> Result<(), String> {
        self.local("source stop", |m| {
            m.hosts.source_running = false;
            Ok(())
        })
    }
    fn source_start(&mut self) -> Result<(), String> {
        self.check_i2("source start");
        // `docker start` succeeds even when the daemon then exits at once
        // (its moved.json, a crash): only a later reading tells.
        self.local("source start", |m| {
            m.hosts.source_running = !m.hosts.moved && !m.source_crashes;
            Ok(())
        })
    }
    fn source_running(&mut self) -> Option<bool> {
        let fault = self.enter("source running?");
        self.leave("source running?");
        match fault {
            Some(_) => None,
            None => Some(self.hosts.source_running),
        }
    }
    fn source_healthy(&mut self, _wait: Duration) -> bool {
        self.hosts.source_running && !self.hosts.fenced && !self.hosts.moved
    }
    fn source_policy(&mut self, policy: &str) -> Result<(), String> {
        let policy = policy.to_owned();
        self.local("source policy", move |m| {
            m.hosts.source_policy = policy;
            Ok(())
        })
    }
    fn schedule(&mut self, enable: bool) -> Result<(), String> {
        self.local("schedule", move |m| {
            m.hosts.schedule = enable;
            Ok(())
        })
    }
    fn read_only(&mut self, on: bool) -> Result<(), String> {
        self.local("read only", move |m| {
            m.hosts.read_only = on;
            Ok(())
        })
    }
    fn dump(&mut self, dir: &Path) -> Result<Dump, String> {
        if !self.hosts.read_only && !self.dead {
            self.violations.push(format!(
                "a dump was taken while new sessions could write ({})",
                self.calls
            ));
        }
        if self.hosts.source_running && !self.dead {
            self.violations.push(format!(
                "a dump was taken while the source daemon ran ({})",
                self.calls
            ));
        }
        let path = dir.join("pseudolife_memory.sql.gz");
        self.local("dump", |_| Ok(()))?;
        Ok(Dump {
            path,
            sha256: "d".repeat(64),
            bytes: 100,
            counts: BTreeMap::from([
                ("public.entries".into(), 3),
                ("public.meta".into(), 6),
                ("public.retrieval_events".into(), 2),
            ]),
        })
    }
    fn fence(&mut self, on: bool) -> Result<(), String> {
        if !on {
            self.check_i2("fence lift");
        }
        self.local("fence", move |m| {
            m.hosts.fenced = on;
            Ok(())
        })
    }
    fn archive(&mut self, dir: &Path) -> Result<Artifact, String> {
        self.local("archive", |_| Ok(()))?;
        Ok(Artifact {
            path: dir.join("pseudolife_state.tgz"),
            sha256: "a".repeat(64),
            bytes: 10,
        })
    }
    fn write_file(&mut self, path: &Path, _bytes: &[u8]) -> Result<Artifact, String> {
        self.local("write file", |_| Ok(()))?;
        Ok(Artifact {
            path: path.to_path_buf(),
            sha256: "m".repeat(64),
            bytes: 1,
        })
    }
    fn moved_marker(&mut self) -> Result<Option<String>, String> {
        let fault = self.enter("moved marker");
        self.leave("moved marker");
        match fault {
            Some(_) => Err("docker could not be asked".into()),
            None => Ok(self.hosts.moved.then(|| ID.to_owned())),
        }
    }
    fn moved(&mut self, body: Option<&str>) -> Result<(), String> {
        let on = body.is_some();
        self.local("moved.json", move |m| {
            m.hosts.moved = on;
            Ok(())
        })
    }
    fn target(&mut self, request: &Request, _payload: Option<&Path>) -> Result<Answer, String> {
        let what = format!("target {}", request.op.name());
        let fault = self.enter(&what);
        let result = match fault {
            Some(Fault::Killed) => Err("the process is gone".to_owned()),
            Some(Fault::LostBefore) => Err("ssh lost the link".to_owned()),
            Some(Fault::LostAfter) => {
                self.apply(request);
                Err("ssh lost the link".to_owned())
            }
            Some(Fault::Late) => {
                self.pending = Some((self.calls + 3, request.clone()));
                Err("ssh lost the link".to_owned())
            }
            Some(Fault::Refused) => Ok(Answer {
                protocol: 2,
                ok: false,
                error: "refused by the model".into(),
                state: Some(self.state()),
                ..Answer::default()
            }),
            _ => Ok(self.apply(request)),
        };
        self.leave(&what);
        result
    }
    fn target_health(&mut self, _url: &str) -> Option<Value> {
        let fault = self.enter("target health");
        self.leave("target health");
        // Another daemon of the same bank at the URL answers as if it were the
        // target, whatever the target itself does.
        if self.health_lies && fault.is_none() {
            return Some(json!({"status": "ok", "ready": true, "auth": true, "bank": BANK}));
        }
        if fault.is_some() || !self.hosts.target_serves_clone() || self.hosts.gated {
            return None;
        }
        Some(json!({"status": "ok", "ready": true, "auth": true, "bank": BANK}))
    }
    fn repoint(&mut self, url: &str) -> Result<(), String> {
        let url = url.to_owned();
        self.local("repoint", move |m| {
            m.hosts.clients = url;
            Ok(())
        })
    }
    fn save(&mut self, record: &Record) -> Result<(), String> {
        let fault = self.enter("save");
        match fault {
            Some(Fault::SaveFails) => Err("cannot write the move record".into()),
            Some(Fault::Killed) => Err("the process is gone".into()),
            _ => {
                self.record = Some(record.clone());
                Ok(())
            }
        }
    }
    fn interrupted(&mut self) -> bool {
        self.interrupted
    }
    fn now(&mut self) -> String {
        "2026-11-14T08:00:00Z".into()
    }
    fn sleep(&mut self, _pause: Duration) {}
    fn say(&mut self, line: &str) {
        self.log.push(line.to_owned());
    }
}

fn plan() -> Plan {
    Plan {
        to: "dev@target".into(),
        source: super::probe::Facts {
            pg: "pseudolife-mcp-postgres".into(),
            daemon: "pseudolife-mcp-daemon".into(),
            db: "pseudolife_memory".into(),
            user: "pseudolife".into(),
            port: 5432,
            image: "pseudolife-daemon:test".into(),
            ..Default::default()
        },
        target_image: "pseudolife-daemon:test".into(),
        target_url: "http://100.64.0.2:8765".into(),
        work_dir: String::new(),
        source_bank: BANK.into(),
        source_policy: "unless-stopped".into(),
        schedule_enabled: true,
        schedule_enable: "systemctl --user enable --now pseudolife-update.timer".into(),
        target_policy: "unless-stopped".into(),
        target_timer_enabled: true,
        target_running: true,
        local_dir: PathBuf::from("/home/u/.pseudolife-mcp/moves").join(ID),
        resume: false,
    }
}

fn run(model: &mut Model) -> engine::Outcome {
    engine::execute(model, &plan(), ID)
}

/// The calls a move with no fault makes.
fn clean_calls() -> usize {
    let mut model = Model::new();
    let outcome = run(&mut model);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    model.calls
}

#[test]
fn a_move_without_faults_moves_the_bank() {
    let mut model = Model::new();
    let outcome = run(&mut model);
    assert_eq!(outcome.code, 0, "{outcome:?}\n{:#?}", model.log);
    let h = &model.hosts;
    assert!(model.violations.is_empty(), "{:?}", model.violations);
    assert!(!h.source_running && h.moved && h.fenced);
    assert!(h.target_serves_clone() && !h.gated);
    assert_eq!(h.target_policy, "unless-stopped");
    assert!(h.target_timer);
    assert_eq!(h.claim, None);
    assert_eq!(h.clients, "http://100.64.0.2:8765");
    // This host's schedule and the source's policy stay as the move left
    // them: the source must never start again.
    assert!(!h.schedule);
    assert_eq!(h.source_policy, "no");
    let record = model.record.unwrap();
    assert!(record.committed);
    assert_eq!(record.phase, Phase::Complete);
}

/// After a run that did not commit: the source serves again (exit 1), or
/// nothing changed (exit 4), or exit 6 says the four facts in order.
fn judge(model: &Model, outcome: &engine::Outcome, label: &str) {
    assert!(
        model.violations.is_empty(),
        "{label}: {:?}\n{:#?}",
        model.violations,
        model.log
    );
    let committed = model.record.as_ref().is_some_and(|r| r.committed);
    let h = &model.hosts;
    match outcome.code {
        0 | 5 => assert!(committed && h.moved, "{label}: {outcome:?}"),
        1 => {
            assert!(!committed, "{label}");
            assert!(
                h.source_running && !h.fenced && !h.read_only && !h.moved,
                "{label}: exit 1 but the source does not serve: {h:?}\n{outcome:?}"
            );
            assert!(!h.target_may_start_clone(), "{label}: {h:?}");
            assert_eq!(h.source_policy, "unless-stopped", "{label}");
            assert!(h.schedule && h.target_timer, "{label}: {h:?}");
        }
        4 => assert_eq!(*h, Hostsides::start(), "{label}: exit 4 changed something"),
        6 => {
            let at = |needle: &str| {
                outcome
                    .message
                    .find(needle)
                    .unwrap_or_else(|| panic!("{label}: no {needle:?} in {}", outcome.message))
            };
            assert!(at("running now:") < at("served:"), "{label}");
            assert!(at("served:") < at("data:"), "{label}");
            assert!(at("data:") < at("next:"), "{label}");
            assert!(outcome.message.contains("move --rollback"), "{label}");
        }
        code => panic!("{label}: unexpected exit {code}: {outcome:?}"),
    }
}

#[test]
fn a_fault_at_any_call_keeps_both_invariants_and_leaves_a_true_answer() {
    let calls = clean_calls();
    for at in 1..=calls {
        for fault in FAULTS {
            let mut model = Model::new();
            model.fault = Some((at, fault));
            let outcome = run(&mut model);
            let label = format!("{fault:?} at call {at}");
            if fault == Fault::Killed {
                // Nothing ran after the kill; the rollback is checked below.
                assert!(
                    model.violations.is_empty(),
                    "{label}: {:?}",
                    model.violations
                );
                continue;
            }
            judge(&model, &outcome, &label);
        }
    }
}

#[test]
fn after_a_kill_at_any_call_move_rollback_from_the_record_restores_service() {
    let calls = clean_calls();
    for at in 1..=calls {
        let mut model = Model::new();
        model.fault = Some((at, Fault::Killed));
        run(&mut model);
        let label = format!("killed at call {at}");
        let Some(record) = model.record.clone() else {
            // Killed before the first record: nothing was changed.
            assert_eq!(model.hosts, Hostsides::start(), "{label}");
            continue;
        };
        // A later `move --rollback <id>`, in a new process.
        let mut after = Model::new();
        after.hosts = model.hosts.clone();
        let outcome = engine::roll_back(&mut after, record.clone());
        if record.committed {
            assert_eq!(outcome.code, 4, "{label}: {outcome:?}");
            assert!(after.hosts.moved, "{label}");
            continue;
        }
        let label = format!("{label}, then --rollback");
        // Killed after moved.json reached the source but before the record
        // said so: the move passed its commit point and is finished, never
        // rolled back.
        if model.hosts.moved {
            assert_eq!(outcome.code, 5, "{label}: {outcome:?}");
            assert!(
                after.hosts.moved && !after.hosts.source_running,
                "{label}: {:?}",
                after.hosts
            );
            assert!(
                after.hosts.target_serves_clone(),
                "{label}: {:?}",
                after.hosts
            );
            assert!(
                after.record.as_ref().is_some_and(|r| r.committed),
                "{label}"
            );
            continue;
        }
        assert!(
            matches!(outcome.code, 0 | 1),
            "{label}: {outcome:?}\n{:#?}",
            after.log
        );
        if outcome.code == 1 || record.flags.source_stopped {
            assert!(
                after.hosts.source_running && !after.hosts.fenced,
                "{label}: {:?}",
                after.hosts
            );
        }
        assert!(
            after.violations.is_empty(),
            "{label}: {:?}",
            after.violations
        );
        assert!(
            !after.hosts.target_may_start_clone(),
            "{label}: {:?}",
            after.hosts
        );
    }
}

#[test]
fn a_rollback_with_a_fault_at_any_of_its_calls_keeps_both_invariants() {
    // Every point the forward run can fail at, then every fault in the
    // rollback that follows.
    let calls = clean_calls();
    for at in 1..=calls {
        let mut first = Model::new();
        first.fault = Some((at, Fault::Killed));
        run(&mut first);
        let Some(record) = first.record.clone() else {
            continue;
        };
        if record.committed {
            continue;
        }
        let mut probe = Model::new();
        probe.hosts = first.hosts.clone();
        engine::roll_back(&mut probe, record.clone());
        for back_at in 1..=probe.calls {
            for fault in [
                Fault::LostBefore,
                Fault::LostAfter,
                Fault::Refused,
                Fault::SaveFails,
            ] {
                let mut model = Model::new();
                model.hosts = first.hosts.clone();
                model.fault = Some((back_at, fault));
                let outcome = engine::roll_back(&mut model, record.clone());
                let label = format!("killed at {at}, then {fault:?} at rollback call {back_at}");
                assert!(
                    model.violations.is_empty(),
                    "{label}: {:?}",
                    model.violations
                );
                assert!(
                    matches!(outcome.code, 0 | 1 | 5 | 6),
                    "{label}: {outcome:?}"
                );
                if outcome.code == 5 {
                    // Only a move whose moved.json is in place finishes.
                    assert!(model.hosts.moved && !model.hosts.source_running, "{label}");
                }
                if outcome.code == 1 {
                    assert!(model.hosts.source_running && !model.hosts.fenced, "{label}");
                }
                if outcome.code == 6 {
                    assert!(outcome.message.contains("running now:"), "{label}");
                }
            }
        }
    }
}

#[test]
fn writes_the_target_took_before_a_rollback_are_named_and_exit_6() {
    let mut model = Model::new();
    model.target_takes_writes = true;
    // Interrupt while waiting for the target's identity: it has served.
    let health_call = call_of("target health");
    model.fault = Some((health_call, Fault::Interrupt));
    let outcome = run(&mut model);
    assert_eq!(outcome.code, 6, "{outcome:?}");
    assert!(
        outcome.message.contains("public.entries"),
        "{}",
        outcome.message
    );
    assert!(
        outcome.message.contains("nothing was deleted"),
        "{}",
        outcome.message
    );
    let h = &model.hosts;
    assert!(h.source_running && !h.fenced, "{h:?}");
    assert!(!h.target_running && h.gated, "{h:?}");
    assert!(model.record.unwrap().parked_writes.is_some());
}

#[test]
fn a_second_move_into_a_claimed_target_is_refused_before_any_change() {
    let mut model = Model::new();
    model.hosts.claim = Some("20261114-070000-00000000".into());
    let before = model.hosts.clone();
    let outcome = run(&mut model);
    assert_eq!(outcome.code, 4, "{outcome:?}");
    assert!(
        outcome.message.contains("20261114-070000-00000000"),
        "{}",
        outcome.message
    );
    assert_eq!(model.hosts, before);
}

#[test]
fn a_docker_restart_after_any_call_never_starts_the_source_during_the_dump() {
    let calls = clean_calls();
    for at in 0..=calls {
        let mut model = Model::new();
        model.hosts.source_policy = "always".into();
        model.docker_restarts = true;
        if at > 0 {
            model.fault = Some((at, Fault::Refused));
        }
        let mut plan = plan();
        plan.source_policy = "always".into();
        let outcome = engine::execute(&mut model, &plan, ID);
        assert!(
            model.violations.is_empty(),
            "fault at {at}: {:?}\n{outcome:?}",
            model.violations
        );
    }
}

#[test]
fn rollback_refuses_a_committed_record_and_closes_a_rolled_back_one() {
    let mut model = Model::new();
    run(&mut model);
    let committed = model.record.clone().unwrap();
    let mut again = Model::new();
    again.hosts = model.hosts.clone();
    let outcome = engine::roll_back(&mut again, committed);
    assert_eq!(outcome.code, 4);
    assert!(
        outcome.message.contains("not rolled back"),
        "{}",
        outcome.message
    );
    assert_eq!(again.hosts, model.hosts);

    let record = Record {
        phase: Phase::RolledBack,
        move_id: ID.into(),
        ..Record::default()
    };
    let mut model = Model::new();
    let outcome = engine::roll_back(&mut model, record);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert_eq!(model.calls, 0);
}

/// The call number of the first logged call ending in `what` in a clean run.
fn call_of(what: &str) -> usize {
    let mut probe = Model::new();
    run(&mut probe);
    probe
        .log
        .iter()
        .find(|l| {
            l.ends_with(what)
                && l.split(':')
                    .next()
                    .is_some_and(|n| n.parse::<usize>().is_ok())
        })
        .and_then(|l| l.split(':').next())
        .and_then(|n| n.parse().ok())
        .unwrap_or_else(|| panic!("no call {what:?}"))
}

#[test]
fn an_interrupt_at_the_commit_point_waits_for_the_commit() {
    let mut model = Model::new();
    model.fault = Some((call_of("moved.json"), Fault::Interrupt));
    let outcome = run(&mut model);
    assert!(matches!(outcome.code, 0 | 5), "{outcome:?}");
    assert!(model.record.unwrap().committed);
    assert!(model.hosts.moved && model.hosts.target_serves_clone());
}

#[test]
fn writes_taken_after_a_lost_start_answer_are_still_named() {
    // The start ran and its answer was lost: the target may have served.
    let mut model = Model::new();
    model.target_takes_writes = true;
    model.fault = Some((call_of("target start-gated"), Fault::LostAfter));
    let outcome = run(&mut model);
    assert_eq!(outcome.code, 6, "{outcome:?}");
    assert!(
        outcome.message.contains("public.entries"),
        "{}",
        outcome.message
    );
    assert!(model.hosts.source_running && !model.hosts.target_running && model.hosts.gated);
}

/// The four facts of an exit 6, in order.
fn assert_four_facts(outcome: &engine::Outcome, label: &str) {
    assert_eq!(outcome.code, 6, "{label}: {outcome:?}");
    let at = |needle: &str| {
        outcome
            .message
            .find(needle)
            .unwrap_or_else(|| panic!("{label}: no {needle:?} in {}", outcome.message))
    };
    assert!(at("running now:") < at("served:"), "{label}");
    assert!(at("served:") < at("data:"), "{label}");
    assert!(at("data:") < at("next:"), "{label}");
}

/// [`assert_four_facts`] for a rollback that is not finished: the next
/// command reruns it.
fn assert_unfinished(outcome: &engine::Outcome, label: &str) {
    assert_four_facts(outcome, label);
    assert!(
        outcome
            .message
            .contains(&format!("next: pseudolife-mcp move --rollback {ID}")),
        "{label}: {}",
        outcome.message
    );
    assert!(
        outcome.message.contains("The rollback is INCOMPLETE"),
        "{label}"
    );
}

/// A move that fails once the target has served (interrupted while it
/// waits for the target's identity).
fn failing_after_start(model: &mut Model) -> engine::Outcome {
    let health = call_of("target health");
    model.fault = Some((health, Fault::Interrupt));
    // The faults the test names start with the rollback.
    model.always_from = health;
    run(model)
}

#[test]
fn an_undo_that_fails_exits_6_with_the_four_facts() {
    // The fence lifts, and the source then cannot start.
    let mut model = Model::new();
    model.always = vec![("source start", Fault::Refused)];
    let outcome = failing_after_start(&mut model);
    assert_unfinished(&outcome, "source start fails");
    assert!(
        outcome.message.contains("running now: neither daemon"),
        "{}",
        outcome.message
    );
    assert!(
        outcome.message.contains("served: no"),
        "{}",
        outcome.message
    );
    assert!(model.violations.is_empty(), "{:?}", model.violations);
}

#[test]
fn a_target_that_cannot_be_proved_stopped_exits_6_with_the_source_fenced() {
    let mut model = Model::new();
    model.always = vec![("target state", Fault::Refused)];
    let outcome = failing_after_start(&mut model);
    assert_unfinished(&outcome, "state refused");
    assert!(
        outcome.message.contains("could not be proved"),
        "{}",
        outcome.message
    );
    assert!(
        outcome
            .message
            .contains("data: this host's database (fenced)"),
        "{}",
        outcome.message
    );
    let h = &model.hosts;
    assert!(!h.source_running && h.fenced, "{h:?}");
    assert!(model.violations.is_empty(), "{:?}", model.violations);
}

#[test]
fn a_link_lost_during_the_rollback_exits_6_and_keeps_the_source_down() {
    let mut model = Model::new();
    model.always = vec![
        ("target state", Fault::LostBefore),
        ("target stop", Fault::LostBefore),
    ];
    let outcome = failing_after_start(&mut model);
    assert_unfinished(&outcome, "link lost");
    assert!(
        outcome.message.contains("could not be read"),
        "{}",
        outcome.message
    );
    let h = &model.hosts;
    assert!(!h.source_running && h.fenced, "{h:?}");
    assert!(model.violations.is_empty(), "{:?}", model.violations);
}

#[test]
fn row_counts_that_differ_exit_6_with_the_four_facts() {
    let mut model = Model::new();
    model.target_takes_writes = true;
    let outcome = failing_after_start(&mut model);
    assert_four_facts(&outcome, "parked writes");
    assert!(
        outcome
            .message
            .contains("served: yes, by this host's daemon"),
        "{}",
        outcome.message
    );
    assert!(
        outcome
            .message
            .contains("Rolled back: the source daemon runs again, but"),
        "{}",
        outcome.message
    );
    // Finished, not stuck: a rerun says so, and nothing blocks or resumes it.
    let record = model.record.clone().unwrap();
    assert_eq!(record.phase, Phase::RolledBack);
    assert!(!super::command::unfinished(&record));
    assert!(super::command::resumable(std::slice::from_ref(&record), &record.target).is_none());
    let mut again = Model::new();
    again.hosts = model.hosts.clone();
    let rerun = engine::roll_back(&mut again, record);
    assert_eq!(rerun.code, 0, "{rerun:?}");
    assert!(
        rerun
            .message
            .contains("keeps writes it took while it served"),
        "{}",
        rerun.message
    );
    assert_eq!(again.calls, 0);
}

#[test]
fn a_daemons_own_warmup_rows_are_not_writes_the_target_took() {
    // Served, without any user write: only its warm-up row is new.
    let mut model = Model::new();
    let outcome = failing_after_start(&mut model);
    assert_eq!(outcome.code, 1, "{outcome:?}");
    let h = &model.hosts;
    assert!(h.source_running && !h.fenced, "{h:?}");
    assert!(matches!(h.bank, Bank::Clone { warmups: 1, .. }), "{h:?}");
}

#[test]
fn a_resumed_move_replaces_its_own_restored_bank_and_no_other() {
    // A move rolled back after its restore: the target keeps the restored
    // bank, gated, and the claim.
    let mut first = Model::new();
    first.fault = Some((call_of("target health"), Fault::Interrupt));
    let outcome = run(&mut first);
    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(first.hosts.gated && matches!(first.hosts.bank, Bank::Clone { .. }));
    assert_eq!(first.hosts.claim.as_deref(), Some(ID));
    // A new move (a new ID) is refused by the claim before any change.
    let mut again = Model::new();
    again.hosts = first.hosts.clone();
    let before = again.hosts.clone();
    let outcome = engine::execute(&mut again, &plan(), "20261114-090000-00000001");
    assert_eq!(outcome.code, 4, "{outcome:?}");
    assert_eq!(again.hosts, before);
    // A resumed run that fails before its own restore keeps that bank
    // gated and claimed, so it can be resumed again.
    let mut failed = Model::new();
    failed.hosts = first.hosts.clone();
    failed.hosts.target_running = false;
    let mut plan_resume = plan();
    plan_resume.resume = true;
    plan_resume.target_running = false;
    // Refused after the claim, before this run's own restore.
    failed.always = vec![("target stop", Fault::Refused)];
    let outcome = engine::execute(&mut failed, &plan_resume, ID);
    assert!(matches!(outcome.code, 1 | 4), "{outcome:?}");
    assert_eq!(failed.hosts.claim.as_deref(), Some(ID));
    assert!(failed.hosts.gated && !failed.hosts.target_running);
    assert!(failed.violations.is_empty(), "{:?}", failed.violations);
    // With it, the move goes through.
    let mut resumed = Model::new();
    resumed.hosts = first.hosts.clone();
    resumed.hosts.target_running = false;
    let mut plan = plan();
    plan.resume = true;
    plan.target_running = false;
    let outcome = engine::execute(&mut resumed, &plan, ID);
    assert_eq!(
        outcome.code, 0,
        "{outcome:?}
{:#?}",
        resumed.log
    );
    assert!(resumed.hosts.target_serves_clone() && resumed.hosts.moved);
    assert!(resumed.violations.is_empty(), "{:?}", resumed.violations);
}

#[test]
fn the_report_names_both_hosts_files_and_the_exact_old_restart_policy_returns() {
    let mut model = Model::new();
    model.hosts.target_policy = "on-failure:5".into();
    let outcome = run(&mut model);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(
        outcome.message.contains(&format!(
            "/home/dev/.pseudolife-mcp/moves/{ID} (3 MiB) on dev@target"
        )),
        "{}",
        outcome.message
    );
    assert!(
        outcome.message.contains("kept for you to remove"),
        "{}",
        outcome.message
    );
    assert_eq!(model.hosts.target_policy, "on-failure:5");
    // A rollback names them too.
    let mut model = Model::new();
    let outcome = failing_after_start(&mut model);
    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome.message.contains("kept for you to remove"),
        "{}",
        outcome.message
    );
}

#[test]
fn a_write_in_place_while_the_target_served_is_kept_and_never_resumed_over() {
    let mut model = Model::new();
    model.target_writes_in_place = true;
    let outcome = failing_after_start(&mut model);
    assert_four_facts(&outcome, "in place");
    assert!(
        outcome.message.contains("entries: 1 rows written in place"),
        "{}",
        outcome.message
    );
    let record = model.record.clone().unwrap();
    assert!(record.parked_writes.is_some());
    assert!(super::command::resumable(std::slice::from_ref(&record), &record.target).is_none());
    assert!(model.hosts.source_running && !model.hosts.fenced && model.hosts.gated);
}

#[test]
fn a_source_that_starts_and_dies_is_not_reported_as_running_again() {
    let mut model = Model::new();
    model.source_crashes = true;
    let outcome = failing_after_start(&mut model);
    assert_unfinished(&outcome, "source crashes");
    assert!(
        outcome.message.contains("it started, then stopped"),
        "{}",
        outcome.message
    );
    assert!(!model.hosts.source_running);
}

#[test]
fn a_refused_resume_changes_nothing_and_stays_resumable() {
    // A move rolled back after its restore: the target keeps the gated
    // bank and this move's claim.
    let mut first = Model::new();
    first.fault = Some((call_of("target health"), Fault::Interrupt));
    assert_eq!(run(&mut first).code, 1);
    // Another move took the claim meanwhile: the resume is refused.
    let mut model = Model::new();
    model.hosts = first.hosts.clone();
    model.hosts.claim = Some("20261114-100000-00000002".into());
    let before = model.hosts.clone();
    let mut plan = plan();
    plan.resume = true;
    plan.target_running = false;
    let outcome = engine::execute(&mut model, &plan, ID);
    assert_eq!(outcome.code, 4, "{outcome:?}");
    assert_eq!(model.hosts, before);
    let record = model.record.unwrap();
    assert_eq!(record.phase, Phase::RolledBack);
    assert!(!super::command::unfinished(&record));
    assert!(super::command::resumable(std::slice::from_ref(&record), &record.target).is_some());
}

#[test]
fn the_url_alone_never_commits_a_move_the_target_does_not_serve() {
    // The target starts and dies at once, and the URL reaches some other
    // daemon of the same bank: only the target's own reading tells.
    let mut model = Model::new();
    model.health_lies = true;
    model.target_crashes = true;
    let outcome = run(&mut model);
    assert!(!model.record.unwrap().committed, "{outcome:?}");
    assert!(!model.hosts.moved, "{outcome:?}");
    assert!(model.violations.is_empty(), "{:?}", model.violations);
}

#[test]
fn a_docker_restart_after_any_call_never_starts_a_target_whose_policy_is_always() {
    // The target's own policy is `always`: a restart may start it only
    // while it is ungated, and the rollback puts that policy back.
    let calls = clean_calls();
    for at in 0..=calls {
        let mut model = Model::new();
        model.hosts.target_policy = "always".into();
        model.docker_restarts = true;
        if at > 0 {
            model.fault = Some((at, Fault::Refused));
        }
        let mut plan = plan();
        plan.target_policy = "always".into();
        let outcome = engine::execute(&mut model, &plan, ID);
        let label = format!("refused at call {at}");
        assert!(
            model.violations.is_empty(),
            "{label}: {:?}\n{outcome:?}",
            model.violations
        );
        let h = &model.hosts;
        match outcome.code {
            0 | 5 => assert!(h.moved && h.target_serves_clone(), "{label}: {h:?}"),
            1 => {
                assert!(h.source_running && !h.fenced, "{label}: {h:?}");
                assert!(!h.target_may_start_clone(), "{label}: {h:?}");
                assert_eq!(h.target_policy, "always", "{label}");
            }
            4 => assert_eq!(h.target_policy, "always", "{label}"),
            6 => assert!(!h.source_running || h.fenced, "{label}: {h:?}"),
            code => panic!("{label}: unexpected exit {code}: {outcome:?}"),
        }
    }
}

#[test]
fn a_record_that_cannot_be_saved_from_the_commit_on_never_undoes_the_move() {
    // Every record write fails from the commit's own call on (a full disk,
    // say): the move still finishes, and a later rollback from the record
    // left on disk reads the source's marker and keeps the move.
    let commit = call_of("moved.json");
    for from in commit - 1..=commit + 1 {
        let mut model = Model::new();
        model.always = vec![("save", Fault::SaveFails)];
        model.always_from = from;
        let outcome = run(&mut model);
        let label = format!("saves fail from call {from}");
        assert!(
            model.violations.is_empty(),
            "{label}: {:?}",
            model.violations
        );
        let Some(record) = model.record.clone() else {
            panic!("{label}: no record was ever saved");
        };
        if !model.hosts.moved {
            // The save before the commit failed, so the move stopped first.
            judge(&model, &outcome, &label);
            continue;
        }
        assert_eq!(outcome.code, 5, "{label}: {outcome:?}");
        assert!(model.hosts.target_serves_clone(), "{label}");
        let mut after = Model::new();
        after.hosts = model.hosts.clone();
        let back = engine::roll_back(&mut after, record);
        assert!(matches!(back.code, 4 | 5), "{label}: {back:?}");
        assert!(
            after.hosts.moved && !after.hosts.source_running,
            "{label}: {:?}",
            after.hosts
        );
        assert!(
            after.hosts.target_serves_clone(),
            "{label}: {:?}",
            after.hosts
        );
        assert!(
            after.violations.is_empty(),
            "{label}: {:?}",
            after.violations
        );
    }
}
