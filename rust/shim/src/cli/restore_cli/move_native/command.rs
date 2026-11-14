//! The move command around the transaction: what this host's records say,
//! confirmation, the update lock, and what the user is told.
use std::io::{BufRead, IsTerminal, Write};
use std::path::PathBuf;

use serde_json::json;

use super::argv::{Request, Rollback};
use super::engine::{self, Outcome};
use super::record::{Files, Flags, Phase, Record, Store, valid_id};
use super::{NOTHING, Plan, say};

/// The home this client keeps its records under.
pub(super) fn home() -> Option<PathBuf> {
    let set = |key: &str| std::env::var_os(key).filter(|value| !value.is_empty());
    set("HOME")
        .or_else(|| set("USERPROFILE"))
        .map(PathBuf::from)
}

/// Every record this host keeps, newest first.
/// Every record this host keeps, newest first, or the first that cannot be
/// read: a record nobody can read could be an unfinished move.
pub(super) fn records(store: &mut Files) -> Result<Vec<Record>, String> {
    let Ok(entries) = std::fs::read_dir(&store.moves) else {
        return Ok(Vec::new());
    };
    let mut ids: Vec<String> = entries
        .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
        .filter(|id| valid_id(id))
        .collect();
    ids.sort();
    ids.reverse();
    let mut found = Vec::new();
    for id in &ids {
        // A folder with no record is a move that never wrote one.
        if let Some(record) = store.load(id)? {
            found.push(record);
        }
    }
    Ok(found)
}

/// A record whose move neither committed nor finished its rollback.
pub(super) fn unfinished(record: &Record) -> bool {
    !record.committed
        && matches!(
            record.phase,
            Phase::Started | Phase::Moving | Phase::RollbackIncomplete
        )
        && record.flags != Flags::default()
}

/// The last effect a record says may have happened, in step order.
fn last_effect(flags: &Flags) -> &'static str {
    let steps: [(bool, &'static str); 15] = [
        (
            flags.moved_written,
            "moved.json written into the source (the commit point)",
        ),
        (flags.target_started, "the target started"),
        (flags.gate_lifting, "the target's gate lifted"),
        (flags.target_restored, "the target restored"),
        (flags.gate_written, "the target gated"),
        (flags.marker_written, "the target marked"),
        (flags.target_quiesced, "the target stopped"),
        (flags.fenced, "the source database fenced"),
        (flags.read_only, "the source database made read-only"),
        (flags.source_stopped, "the source stopped"),
        (
            flags.source_policy_changed,
            "the source's restart policy set to no",
        ),
        (
            flags.schedule_paused,
            "this host's unattended update paused",
        ),
        (
            flags.target_policy_paused,
            "the target's restart policy paused",
        ),
        (
            flags.target_timer_paused,
            "the target's unattended update paused",
        ),
        (flags.claimed, "the target claimed"),
    ];
    steps
        .iter()
        .find(|(set, _)| *set)
        .map_or("nothing", |(_, what)| what)
}

/// The refusal while an unfinished move's record stands.
pub(super) fn unfinished_refusal(record: &Record) -> String {
    if record.flags.moved_written {
        return format!(
            "move {} to {} may have passed its commit point: its record says moved.json may have reached this host's daemon, and the record was not marked committed. Run pseudolife-mcp move --rollback {}: it reads this host's /data/moved.json and finishes the move if it committed, or rolls it back if it did not",
            record.move_id, record.target, record.move_id
        );
    }
    format!(
        "move {} to {} did not finish: its record ({:?}) says the last thing it may have done is {}, and it is not committed. Roll it back first with pseudolife-mcp move --rollback {}",
        record.move_id,
        record.target,
        record.phase,
        last_effect(&record.flags),
        record.move_id
    )
}

/// The move `--resume` takes up: the newest rolled-back move to the same
/// target whose restore ran there (the target keeps its claim and that bank).
pub(super) fn resumable<'a>(records: &'a [Record], to: &str) -> Option<&'a Record> {
    records.iter().find(|r| {
        // Never one whose target kept writes it took: a resume would replace
        // them.
        !r.committed
            && r.phase == Phase::RolledBack
            && r.flags.target_restored
            && r.parked_writes.is_none()
            && r.target == to
    })
}

/// Asks the user unless `--yes`; a run without a terminal must say `--yes`.
fn confirm(yes: bool, question: &str) -> Result<(), String> {
    if yes {
        return Ok(());
    }
    if !std::io::stdin().is_terminal() {
        return Err(format!(
            "this run is not interactive: re-run with --yes. {NOTHING}"
        ));
    }
    let mut out = std::io::stderr().lock();
    let _ = write!(out, "{question} [y/N] ");
    let _ = out.flush();
    let mut answer = String::new();
    let _ = std::io::stdin().lock().read_line(&mut answer);
    if matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes") {
        Ok(())
    } else {
        Err(format!("not confirmed. {NOTHING}"))
    }
}

/// `rollback`: the outcome of `move --rollback`, where only a move found
/// past its commit point (exit 5) has moved.
fn report(json: bool, move_id: &str, outcome: &Outcome, rollback: bool) -> u8 {
    let moved = if rollback {
        outcome.code == engine::EXIT_FOLLOW_UPS
    } else {
        matches!(outcome.code, engine::EXIT_OK | engine::EXIT_FOLLOW_UPS)
    };
    if json {
        let doc = json!({
            "move_id": move_id,
            "moved": moved,
            "exit": outcome.code,
            "message": outcome.message,
        });
        say(true, &format!("{doc}\n"));
    } else if matches!(outcome.code, engine::EXIT_OK | engine::EXIT_FOLLOW_UPS) {
        say(true, &format!("{}\n", outcome.message));
    } else {
        say(false, &format!("move: {}\n", outcome.message));
    }
    outcome.code
}

fn refuse(json: bool, code: u8, why: &str) -> u8 {
    if json {
        say(
            true,
            &format!("{}\n", json!({"moved": false, "exit": code, "error": why})),
        );
    } else {
        say(false, &format!("move: {why}\n"));
    }
    code
}

/// Runs `work` under this host's update lock, with interrupts raised as a
/// flag the engine reads at its step boundaries.
fn locked(work: impl FnOnce() -> Outcome) -> Result<Outcome, String> {
    let directory = super::super::data_dir()
        .ok_or_else(|| format!("this host has no home directory for its update lock. {NOTHING}"))?;
    let _ = crate::board::state::private_dir(&directory);
    let _guard = super::super::interrupt::Guard::install();
    super::super::with_update_lock(&directory, work).map_err(|refusal| match refusal {
        super::super::LockRefusal::Busy(pid) => format!(
            "another update, restore, backup or move holds this host's update lock (pid {pid}); wait for it to finish, then retry. {NOTHING}"
        ),
        super::super::LockRefusal::Unavailable(error) => format!(
            "this host's update lock cannot be taken ({error}). {NOTHING}"
        ),
    })
}

/// The move itself, after a preflight that found `plan`.
pub(super) fn transaction(
    request: &Request,
    plan: &Plan,
    docker: super::super::docker::Real,
    ssh: super::remote::Ssh,
) -> u8 {
    let Some(home) = home() else {
        return refuse(
            request.json,
            4,
            &format!("this host has no home directory for the move record. {NOTHING}"),
        );
    };
    let store = Files::under_home(&home);
    let schedule = super::source::schedule();
    let id = if request.resume_move.is_empty() {
        super::record::new_id(chrono::Utc::now())
    } else {
        request.resume_move.clone()
    };
    let target = plan.target.facts.clone().unwrap_or_default();
    let engine_plan = engine::Plan {
        to: plan.to.clone(),
        source: plan.source.clone(),
        target_image: target.image.clone(),
        target_url: super::argv::base(&plan.target_url).to_owned(),
        work_dir: request.target_work_dir.clone().unwrap_or_default(),
        source_bank: plan.source.bank.clone().unwrap_or_default(),
        source_policy: plan.source.restart_policy.clone(),
        schedule_enabled: schedule.enabled,
        schedule_enable: schedule.enable_command.clone(),
        target_policy: target.restart_policy.clone(),
        target_timer_enabled: plan.target.update_timer == "enabled",
        target_running: target.running,
        local_dir: super::source::local_dir(&store, &id),
        resume: !request.resume_move.is_empty(),
    };
    if !request.json {
        say(true, &super::render(plan, false));
    }
    // What this host's records said when the plan was made: rechecked under
    // the lock, since another move may have run while the user confirmed.
    let mut seen = Files::under_home(&home);
    let before = records(&mut seen)
        .ok()
        .and_then(|r| r.first().map(|r| (r.move_id.clone(), r.phase, r.committed)));
    if let Err(why) = confirm(
        request.yes,
        &format!("Move bank {} to {} now?", engine_plan.source_bank, plan.to),
    ) {
        return refuse(request.json, 2, &why);
    }
    let mut live = super::source::Live {
        docker,
        ssh,
        source: plan.source.clone(),
        to: plan.to.clone(),
        store,
    };
    match locked(|| {
        let now = records(&mut seen)
            .ok()
            .and_then(|r| r.first().map(|r| (r.move_id.clone(), r.phase, r.committed)));
        if now != before {
            return engine::Outcome {
                code: engine::EXIT_REFUSED,
                message: format!(
                    "this host's move records changed while you confirmed (another move or rollback ran); run the move again so both hosts are checked afresh. {NOTHING}"
                ),
            };
        }
        engine::execute(&mut live, &engine_plan, &id)
    }) {
        Ok(outcome) => report(request.json, &id, &outcome, false),
        Err(why) => refuse(request.json, 4, &why),
    }
}

/// `move --rollback <id>`.
pub(super) fn rollback(request: &Rollback) -> u8 {
    let Some(home) = home() else {
        return refuse(
            request.json,
            4,
            &format!("this host has no home directory, so it keeps no move records. {NOTHING}"),
        );
    };
    let mut store = Files::under_home(&home);
    if !valid_id(&request.id) {
        return refuse(
            request.json,
            2,
            &format!(
                "{:?} is not a move ID (pseudolife-mcp prints it as YYYYMMDD-HHMMSS-xxxxxxxx). {NOTHING}",
                request.id
            ),
        );
    }
    let record = match store.load(&request.id) {
        Ok(Some(record)) => record,
        Ok(None) => {
            return refuse(
                request.json,
                4,
                &format!(
                    "this host has no record of move {} (under {}). {NOTHING}",
                    request.id,
                    store.moves.display()
                ),
            );
        }
        Err(why) => return refuse(request.json, 4, &format!("{why}. {NOTHING}")),
    };
    if record.committed || record.phase == Phase::RolledBack {
        // Answered from the record alone: nothing to run.
        let mut none = NoHosts;
        let id = record.move_id.clone();
        return report(
            request.json,
            &id,
            &engine::roll_back(&mut none, record),
            true,
        );
    }
    if let Err(why) = confirm(
        request.yes,
        &format!("Roll back move {} to {}?", record.move_id, record.target),
    ) {
        return refuse(request.json, 2, &why);
    }
    let docker = match super::docker_program() {
        Ok(program) => super::super::docker::Real {
            program,
            interruptible: false,
        },
        Err(why) => return refuse(request.json, 4, &format!("{why}. {NOTHING}")),
    };
    let ssh = match super::remote::Ssh::find() {
        Ok(ssh) => ssh,
        Err(why) => return refuse(request.json, 4, &format!("{why}. {NOTHING}")),
    };
    // The source as the move's preflight found it: a fenced database
    // refuses the queries that would find it again.
    let source = record.source.clone();
    if source.pg.is_empty() || source.daemon.is_empty() || source.db.is_empty() {
        return refuse(
            request.json,
            4,
            &format!(
                "the record of move {} does not name the source's containers; roll it back by hand with the steps it lists. {NOTHING}",
                record.move_id
            ),
        );
    }
    let mut live = super::source::Live {
        docker,
        ssh,
        source,
        to: record.target.clone(),
        store,
    };
    let id = record.move_id.clone();
    let mut again = Files::under_home(&home);
    match locked(|| {
        // The record as it is now, under the lock: the move it names may
        // have committed, or another rollback run, while the user confirmed.
        match again.load(&id) {
            Ok(Some(now)) if now == record => engine::roll_back(&mut live, record),
            Ok(_) => engine::Outcome {
                code: engine::EXIT_REFUSED,
                message: format!(
                    "the record of move {id} changed while you confirmed; run pseudolife-mcp move --rollback {id} again to act on what it says now. {NOTHING}"
                ),
            },
            Err(why) => engine::Outcome {
                code: engine::EXIT_REFUSED,
                message: format!("{why}. {NOTHING}"),
            },
        }
    }) {
        Ok(outcome) => report(request.json, &id, &outcome, true),
        Err(why) => refuse(request.json, 4, &why),
    }
}

/// For a rollback the record alone answers: any host call is a bug.
struct NoHosts;

impl engine::Hosts for NoHosts {
    fn source_stop(&mut self) -> Result<(), String> {
        unreachable!()
    }
    fn source_start(&mut self) -> Result<(), String> {
        unreachable!()
    }
    fn source_running(&mut self) -> Option<bool> {
        unreachable!()
    }
    fn source_healthy(&mut self, _: std::time::Duration) -> bool {
        unreachable!()
    }
    fn source_policy(&mut self, _: &str) -> Result<(), String> {
        unreachable!()
    }
    fn schedule(&mut self, _: bool) -> Result<(), String> {
        unreachable!()
    }
    fn dump(&mut self, _: &std::path::Path) -> Result<engine::Dump, String> {
        unreachable!()
    }
    fn fence(&mut self, _: bool) -> Result<(), String> {
        unreachable!()
    }
    fn archive(&mut self, _: &std::path::Path) -> Result<engine::Artifact, String> {
        unreachable!()
    }
    fn write_file(&mut self, _: &std::path::Path, _: &[u8]) -> Result<engine::Artifact, String> {
        unreachable!()
    }
    fn moved_marker(&mut self) -> Result<Option<String>, String> {
        unreachable!()
    }
    fn read_only(&mut self, _: bool) -> Result<(), String> {
        unreachable!()
    }
    fn moved(&mut self, _: Option<&str>) -> Result<(), String> {
        unreachable!()
    }
    fn target(
        &mut self,
        _: &super::protocol::Request,
        _: Option<&std::path::Path>,
    ) -> Result<super::protocol::Answer, String> {
        unreachable!()
    }
    fn target_health(&mut self, _: &str) -> Option<serde_json::Value> {
        unreachable!()
    }
    fn repoint(&mut self, _: &str) -> Result<(), String> {
        unreachable!()
    }
    fn save(&mut self, _: &Record) -> Result<(), String> {
        unreachable!()
    }
    fn interrupted(&mut self) -> bool {
        false
    }
    fn now(&mut self) -> String {
        String::new()
    }
    fn sleep(&mut self, _: std::time::Duration) {}
    fn say(&mut self, _: &str) {}
}
