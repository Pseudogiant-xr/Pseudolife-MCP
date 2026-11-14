//! The target's half of a move's transaction: the helper's protocol-2
//! operations ([`super::protocol`]).
//!
//! Each operation is safe to repeat. Each one that changes the target
//! needs the claim to name its move (`claim` makes it, `release` gives it
//! up, and is done when the move does not hold it), and is journalled to
//! `<move folder>/ops.jsonl` before and after it runs. The move's folder
//! and the claim are private to this user, as the source's record is, and
//! hold no secret: the copied dump is the bank's own data.
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::probe::{self, Facts};
use super::protocol::{Answer, Op, PROTOCOL, Request, State};
use super::record::valid_id;
use crate::cli::restore_cli::bank;
use crate::cli::restore_cli::bank::Bank;
use crate::cli::restore_cli::docker::{Docker, Input, Output, Ran};

const SHORT: Duration = Duration::from_secs(60);
const STOP: Duration = Duration::from_secs(300);
const ARCHIVE: Duration = Duration::from_secs(4 * 60 * 60);
/// The state archive of the target's own `/data`, taken once.
pub(super) const SNAPSHOT: &str = "target-original-state.tgz";
const CLAIM: &str = "claim.json";
/// Written by `abort`: the move's forward steps no longer run here.
const ABORTED: &str = "aborted";
const GATE: &str = "/data/move.json";
const TIMER: &str = "pseudolife-update.timer";

/// What the target host offers the operations.
pub(super) trait Site {
    type D: Docker;
    fn docker(&mut self) -> Result<&mut Self::D, String>;
    fn home(&self) -> Option<PathBuf>;
    fn now(&self) -> String;
    /// `systemctl --user <args>`: its stdout when it exits 0.
    fn systemctl(&mut self, args: &[&str]) -> Result<String, String>;
    /// This client's own `restore` with `args`, told the move's abort marker
    /// (which it reads under its own update lock): its exit code and output.
    fn restore(&mut self, args: &[String], abort_file: &Path) -> (i32, String);
}

/// The claim a move holds on the target.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct Claim {
    pub move_id: String,
    pub source_bank: String,
    pub at: String,
}

/// The folder that holds every move's folder and the claim.
pub(super) fn base(home: Option<&Path>, work_dir: &str) -> Result<PathBuf, String> {
    let home = || home.ok_or_else(|| "the target has no home directory".to_owned());
    Ok(if work_dir.is_empty() {
        home()?.join(".pseudolife-mcp").join("moves")
    } else if let Some(rest) = work_dir.strip_prefix("~/") {
        home()?.join(rest)
    } else {
        PathBuf::from(work_dir)
    })
}

pub(super) fn read_claim(base: &Path) -> Result<Option<Claim>, String> {
    match std::fs::read(base.join(CLAIM)) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|_| format!("{} is not a move claim", base.join(CLAIM).display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "cannot read {}: {error}",
            base.join(CLAIM).display()
        )),
    }
}

fn journal(folder: &Path, line: &serde_json::Value) {
    let path = folder.join("ops.jsonl");
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(file, "{line}");
        let _ = file.sync_all();
    }
}

fn failed(error: impl Into<String>) -> Answer {
    Answer {
        protocol: PROTOCOL,
        version: env!("CARGO_PKG_VERSION").to_owned(),
        ok: false,
        error: error.into(),
        ..Answer::default()
    }
}

fn done() -> Answer {
    Answer {
        protocol: PROTOCOL,
        version: env!("CARGO_PKG_VERSION").to_owned(),
        ok: true,
        ..Answer::default()
    }
}

fn run<D: Docker>(docker: &mut D, args: &[&str], timeout: Duration) -> Ran {
    let args: Vec<String> = args.iter().map(|a| (*a).to_owned()).collect();
    docker.run(&args, timeout)
}

fn daemon() -> &'static str {
    bank::DAEMON_CONTAINER
}

fn inspect<D: Docker>(docker: &mut D, format: &str) -> Result<String, String> {
    let ran = run(docker, &["inspect", "-f", format, daemon()], SHORT);
    if !ran.ok() {
        return Err(format!(
            "docker inspect {} failed ({})",
            daemon(),
            probe::why(&ran)
        ));
    }
    Ok(String::from_utf8_lossy(&ran.stdout).trim().to_owned())
}

/// The daemon container's image, by ID: a helper over its volumes then
/// never pulls, and never runs a retagged image (as restore's helpers).
fn image<D: Docker>(docker: &mut D) -> Result<String, String> {
    inspect(docker, "{{.Image}}").and_then(|image| {
        if image.is_empty() {
            Err(format!("{} names no image", daemon()))
        } else {
            Ok(image)
        }
    })
}

fn running<D: Docker>(docker: &mut D) -> Option<bool> {
    match inspect(docker, "{{.State.Running}}").ok()?.as_str() {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    }
}

/// The restart policy as `docker update --restart` takes it back.
fn policy<D: Docker>(docker: &mut D) -> Result<String, String> {
    let raw = inspect(
        docker,
        "{{.HostConfig.RestartPolicy.Name}} {{.HostConfig.RestartPolicy.MaximumRetryCount}}",
    )?;
    let mut parts = raw.split_whitespace();
    let name = parts.next().unwrap_or("no");
    let count = parts.next().unwrap_or("0");
    Ok(match name {
        "" => "no".to_owned(),
        "on-failure" if count != "0" => format!("on-failure:{count}"),
        other => other.to_owned(),
    })
}

fn valid_policy(policy: &str) -> bool {
    matches!(policy, "no" | "always" | "unless-stopped" | "on-failure")
        || policy
            .strip_prefix("on-failure:")
            .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

/// A helper container over the daemon's volumes, with no network.
fn helper<'a>(image: &'a str, entrypoint: &'a str, rest: &[&'a str]) -> Vec<&'a str> {
    let mut args = vec![
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        entrypoint,
        "--volumes-from",
        bank::DAEMON_CONTAINER,
        image,
    ];
    args.extend_from_slice(rest);
    args
}

fn gated<D: Docker>(docker: &mut D, image: &str) -> Option<bool> {
    let ran = run(
        docker,
        &helper(image, "sh", &["-c", &format!("test -e {GATE}")]),
        SHORT,
    );
    match (ran.fault.is_some(), ran.code) {
        (false, 0) => Some(true),
        (false, 1) => Some(false),
        _ => None,
    }
}

/// `schema.table` names only, as the dump's COPY lines give them.
fn valid_table(name: &str) -> bool {
    let mut parts = name.split('.');
    let ok = |part: Option<&str>| {
        part.is_some_and(|p| {
            !p.is_empty() && p.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
        })
    };
    ok(parts.next()) && ok(parts.next()) && parts.next().is_none()
}

fn valid_file(name: &str) -> bool {
    !name.is_empty()
        && !name.starts_with('.')
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// The bytes the files under `folder` take.
pub(super) fn folder_bytes(folder: &Path) -> Option<u64> {
    let mut total = 0u64;
    let mut stack = vec![folder.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).ok()? {
            let entry = entry.ok()?;
            let meta = entry.metadata().ok()?;
            if meta.is_dir() {
                stack.push(entry.path());
            } else {
                total = total.saturating_add(meta.len());
            }
        }
    }
    Some(total)
}

/// What is true on the target now.
fn state<S: Site>(site: &mut S, base: &Path, folder: &Path, tables: &[String]) -> State {
    let mut state = State {
        folder: folder.to_string_lossy().into_owned(),
        folder_bytes: folder_bytes(folder),
        claim: read_claim(base).ok().flatten().map(|c| c.move_id),
        update_timer: site
            .systemctl(&["is-enabled", TIMER])
            .unwrap_or_else(|_| "not-found".to_owned()),
        ..State::default()
    };
    let Ok(docker) = site.docker() else {
        return state;
    };
    state.running = running(docker);
    state.restart_policy = policy(docker).unwrap_or_default();
    if let Ok(image) = image(docker) {
        state.gated = gated(docker, &image);
    }
    let Ok(facts) = probe::gather(docker) else {
        return state;
    };
    state.bank = facts.meta_fingerprint.clone();
    state.empty = probe::used_tables(docker, &facts)
        .ok()
        .map(|used| used.is_empty());
    let tables: Vec<&String> = tables.iter().filter(|t| valid_table(t)).collect();
    if !tables.is_empty() {
        let sql = format!(
            "SELECT {}",
            tables
                .iter()
                .map(|t| {
                    let (schema, table) = t.split_once('.').unwrap_or_default();
                    format!("(SELECT count(*) FROM \"{schema}\".\"{table}\")")
                })
                .collect::<Vec<_>>()
                .join(", ")
        );
        let mut bank = Bank {
            docker,
            pg: facts.pg.clone(),
            daemon: facts.daemon.clone(),
        };
        if let Ok(row) = probe::query(&mut bank, &facts.user, facts.port, &facts.db, &sql) {
            let counts: Vec<&str> = row.split('|').collect();
            if counts.len() == tables.len() {
                state.counts = tables
                    .iter()
                    .zip(counts)
                    .filter_map(|(t, n)| Some(((*t).clone(), n.trim().parse().ok()?)))
                    .collect::<BTreeMap<_, _>>();
            }
        }
        let user_sql = format!(
            "SELECT {}",
            tables
                .iter()
                .map(|t| {
                    let (schema, table) = t.split_once('.').unwrap_or_default();
                    format!(
                        "(SELECT count(*) FROM \"{schema}\".\"{table}\"{})",
                        probe::served_filter(table)
                    )
                })
                .collect::<Vec<_>>()
                .join(", ")
        );
        if let Ok(row) = probe::query(&mut bank, &facts.user, facts.port, &facts.db, &user_sql) {
            let counts: Vec<&str> = row.split('|').collect();
            if counts.len() == tables.len() {
                state.user_counts = tables
                    .iter()
                    .zip(counts)
                    .filter_map(|(t, n)| Some(((*t).clone(), n.trim().parse().ok()?)))
                    .collect::<BTreeMap<_, _>>();
            }
        }
        // A plain daemon start changes only meta and retrieval_events
        // (measured 14 November 2026 on the first Linux dev box: one restart
        // of the 0.21.0 daemon on a 12-entry bank moved meta's updates by 2
        // and retrieval_events' inserts by 1, and no other table's).
        state.mutations = probe::query(
            &mut bank,
            &facts.user,
            facts.port,
            &facts.db,
            "SELECT coalesce(json_object_agg(relname, n_tup_ins + n_tup_upd + n_tup_del), '{}'::json) FROM pg_stat_user_tables WHERE schemaname = 'public' AND relname NOT IN ('meta', 'retrieval_events')",
        )
        .ok()
        .and_then(|row| serde_json::from_str::<BTreeMap<String, i64>>(&row).ok());
    }
    state
}

/// The files `restore` needs, as the manifest names them.
fn manifest(folder: &Path) -> Result<(PathBuf, PathBuf), String> {
    let path = folder.join("manifest.json");
    let text =
        std::fs::read(&path).map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    let doc: serde_json::Value = serde_json::from_slice(&text)
        .map_err(|_| format!("{} is not the move's manifest", path.display()))?;
    let file = |key: &str| {
        doc.get(key)
            .and_then(serde_json::Value::as_str)
            .filter(|name| valid_file(name))
            .map(|name| folder.join(name))
            .ok_or_else(|| format!("{} names no {key} file", path.display()))
    };
    Ok((file("dump")?, file("state")?))
}

/// Streams exactly `bytes` bytes from `payload` into `<name>.part`, checks
/// the digest, then renames it into place.
fn receive(
    folder: &Path,
    name: &str,
    sha256: &str,
    bytes: u64,
    payload: &mut dyn Read,
) -> Result<(), String> {
    if !valid_file(name) {
        return Err(format!("{name:?} is not a file name a move copies"));
    }
    let final_path = folder.join(name);
    let part = folder.join(format!("{name}.part"));
    let mut file = crate::cli::restore_cli::docker::private_file(&part)
        .or_else(|_| {
            let _ = std::fs::remove_file(&part);
            crate::cli::restore_cli::docker::private_file(&part)
        })
        .map_err(|error| format!("cannot create {}: {error}", part.display()))?;
    let mut digest = Sha256::new();
    let mut left = bytes;
    let mut buffer = vec![0u8; 1 << 16];
    while left > 0 {
        let want = usize::try_from(left.min(buffer.len() as u64)).unwrap_or(buffer.len());
        let got = payload
            .read(&mut buffer[..want])
            .map_err(|error| format!("the copy of {name} stopped: {error}"))?;
        if got == 0 {
            let _ = std::fs::remove_file(&part);
            return Err(format!(
                "the copy of {name} ended {left} bytes short; rerun the move"
            ));
        }
        digest.update(&buffer[..got]);
        file.write_all(&buffer[..got])
            .map_err(|error| format!("cannot write {}: {error}", part.display()))?;
        left -= got as u64;
    }
    file.sync_all()
        .map_err(|error| format!("cannot write {}: {error}", part.display()))?;
    drop(file);
    let got: String = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    if got != sha256 {
        let _ = std::fs::remove_file(&part);
        return Err(format!(
            "the copy of {name} does not match the source's SHA-256 ({got}, expected {sha256})"
        ));
    }
    std::fs::rename(&part, &final_path)
        .map_err(|error| format!("cannot move {} into place: {error}", part.display()))
}

fn snapshot<D: Docker>(docker: &mut D, folder: &Path) -> Result<(), String> {
    let final_path = folder.join(SNAPSHOT);
    if final_path.exists() {
        // Taken before this bank was replaced: never retaken (a resumed move
        // would capture the copy, not the target's own state).
        return Ok(());
    }
    let image = image(docker)?;
    let part = folder.join(format!("{SNAPSHOT}.part"));
    let _ = std::fs::remove_file(&part);
    let args: Vec<String> = helper(&image, "tar", &["czf", "-", "-C", "/data", "."])
        .into_iter()
        .map(str::to_owned)
        .collect();
    let ran = docker.run_io(&args, ARCHIVE, Input::Null, Output::File(&part));
    if !ran.ok() {
        let _ = std::fs::remove_file(&part);
        return Err(format!(
            "cannot archive the target's own state ({})",
            probe::why(&ran)
        ));
    }
    // Read it back whole before it counts.
    crate::cli::restore_cli::gzip::stream(&part, |_| {
        Ok::<(), crate::cli::restore_cli::gzip::Stop>(())
    })
    .map_err(|_| {
        let _ = std::fs::remove_file(&part);
        "the archive of the target's own state does not read back; check the target's disk, then rerun".to_owned()
    })?;
    std::fs::rename(&part, &final_path)
        .map_err(|error| format!("cannot move {} into place: {error}", part.display()))
}

fn restore<S: Site>(site: &mut S, folder: &Path) -> Answer {
    let (dump, state_archive) = match manifest(folder) {
        Ok(files) => files,
        Err(why) => return failed(why),
    };
    let facts: Facts = match site.docker().and_then(probe::gather) {
        Ok(facts) => facts,
        Err(why) => return failed(format!("cannot read the target's bank: {why}")),
    };
    let path = |p: &Path| p.to_string_lossy().into_owned();
    let args = vec![
        "restore".to_owned(),
        "--yes".to_owned(),
        "--leave-stopped".to_owned(),
        "--backup-file".to_owned(),
        path(&dump),
        "--state-archive".to_owned(),
        path(&state_archive),
        "--safety-dir".to_owned(),
        path(&folder.join("safety")),
        "--container".to_owned(),
        facts.pg.clone(),
        "--daemon-container".to_owned(),
        facts.daemon.clone(),
        "--db".to_owned(),
        facts.db.clone(),
        "--user".to_owned(),
        facts.user.clone(),
        "--db-port".to_owned(),
        facts.port.to_string(),
    ];
    let (code, output) = site.restore(&args, &folder.join(ABORTED));
    let tail: String = output
        .lines()
        .rev()
        .take(12)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n");
    let mut answer = if code == 0 {
        done()
    } else {
        failed(format!(
            "restore exited {code}: {}",
            tail.lines().last().unwrap_or("")
        ))
    };
    // Restore prints this line just before it drops the database
    // (`execute.rs`): a failure without it changed nothing.
    answer.restore_dropped = output.contains("==> Dropping + recreating");
    answer.detail = tail;
    answer
}

/// One operation. The caller holds the update lock for operations that
/// change the target, except `restore`, which takes it itself.
pub(super) fn perform<S: Site>(site: &mut S, request: &Request, payload: &mut dyn Read) -> Answer {
    if request.protocol != PROTOCOL {
        return failed(format!(
            "the target's pseudolife-mcp speaks move protocol {PROTOCOL}, and the source sent {}; update the older client (pseudolife-mcp update) so both match, then retry",
            request.protocol
        ));
    }
    if !valid_id(&request.move_id) {
        return failed(format!("{:?} is not a move ID", request.move_id));
    }
    // The target's claim lives in one place, whatever --target-work-dir
    // says: two moves into one target must meet at the same claim.
    let claims = match base(site.home().as_deref(), "") {
        Ok(claims) => claims,
        Err(why) => return failed(why),
    };
    let base = match base(site.home().as_deref(), &request.work_dir) {
        Ok(base) => base,
        Err(why) => return failed(why),
    };
    let folder = base.join(&request.move_id);
    let op = &request.op;
    if op.changes() {
        // A --target-work-dir the user named keeps its own permissions: only
        // folders this move creates are made private to this user.
        if !base.exists() && crate::board::state::private_dir(&base).is_err() {
            return failed(format!(
                "cannot create the private folder {}",
                base.display()
            ));
        }
        if crate::board::state::private_dir(&folder).is_err() {
            return failed(format!(
                "cannot create the private folder {}",
                folder.display()
            ));
        }
        if !claims.exists() && crate::board::state::private_dir(&claims).is_err() {
            return failed(format!(
                "cannot create the private folder {}",
                claims.display()
            ));
        }
        // `abort` and `release` hold nothing this move does not own.
        if !matches!(op, Op::Claim { .. } | Op::Release | Op::Abort) {
            match read_claim(&claims) {
                Ok(Some(claim)) if claim.move_id == request.move_id => {}
                Ok(Some(claim)) => {
                    return failed(format!(
                        "move {} (from bank {}, since {}) holds this target, not this move",
                        claim.move_id, claim.source_bank, claim.at
                    ));
                }
                Ok(None) => {
                    return failed(
                        "no move holds this target's claim; the move must claim it first",
                    );
                }
                Err(why) => return failed(why),
            }
        }
        // After the source began rolling this move back, no forward step of
        // it runs here (one that was in flight finished under the lock
        // before the abort was written).
        let resumes = matches!(op, Op::Claim { resume: true, .. });
        if !request.rollback
            && !resumes
            && !matches!(op, Op::Abort)
            && folder.join(ABORTED).exists()
        {
            return failed(format!(
                "move {} is being rolled back on its source; this step does not run",
                request.move_id
            ));
        }
        journal(
            &folder,
            &serde_json::json!({"at": site.now(), "op": op.name(), "phase": "begun"}),
        );
    }
    let mut answer = match op {
        Op::Check { .. } => failed("check is protocol 1's request"),
        Op::State { tables } => {
            let mut answer = done();
            answer.state = Some(state(site, &claims, &folder, tables));
            answer
        }
        Op::Claim {
            source_bank,
            resume,
        } => {
            let answer = claim(site, &claims, &request.move_id, source_bank);
            // The resumed move owns the target again: its earlier rollback's
            // abort no longer applies.
            if answer.ok && *resume {
                let _ = std::fs::remove_file(folder.join(ABORTED));
            }
            answer
        }
        Op::Release => match read_claim(&claims) {
            Ok(Some(claim)) if claim.move_id == request.move_id => {
                match std::fs::remove_file(claims.join(CLAIM)) {
                    Ok(()) => done(),
                    Err(error) => failed(format!("cannot remove the claim: {error}")),
                }
            }
            Ok(_) => done(),
            Err(why) => failed(why),
        },
        Op::TimerPause => match site.systemctl(&["disable", "--now", TIMER]) {
            Ok(_) => done(),
            Err(why) => failed(format!("cannot pause {TIMER}: {why}")),
        },
        Op::TimerResume => match site.systemctl(&["enable", "--now", TIMER]) {
            Ok(_) => done(),
            Err(why) => failed(format!("cannot resume {TIMER}: {why}")),
        },
        Op::PolicyNo | Op::PolicySet { .. } => {
            let docker = match site.docker() {
                Ok(docker) => docker,
                Err(why) => return failed(why),
            };
            let old = policy(docker).unwrap_or_default();
            let new = match op {
                Op::PolicySet { policy } if valid_policy(policy) => policy.clone(),
                Op::PolicySet { policy } => {
                    return failed(format!("{policy:?} is not a restart policy"));
                }
                _ => "no".to_owned(),
            };
            let ran = run(
                docker,
                &["update", &format!("--restart={new}"), daemon()],
                SHORT,
            );
            if ran.ok() {
                let mut answer = done();
                answer.policy = old;
                answer
            } else {
                failed(format!("docker update failed ({})", probe::why(&ran)))
            }
        }
        Op::Receive {
            name,
            sha256,
            bytes,
        } => match receive(&folder, name, sha256, *bytes, payload) {
            Ok(()) => done(),
            Err(why) => failed(why),
        },
        Op::Stop => {
            let docker = match site.docker() {
                Ok(docker) => docker,
                Err(why) => return failed(why),
            };
            let ran = run(docker, &["stop", "-t", "120", daemon()], STOP);
            match running(docker) {
                Some(false) => done(),
                _ => failed(format!(
                    "the target daemon did not stop ({})",
                    probe::why(&ran)
                )),
            }
        }
        Op::Start | Op::Gate | Op::Ungate | Op::StartGated => {
            gate_and_start(site, &folder, request)
        }
        Op::Snapshot => match site.docker().and_then(|d| snapshot(d, &folder)) {
            Ok(()) => done(),
            Err(why) => failed(why),
        },
        Op::Restore => restore(site, &folder),
        Op::Abort => match std::fs::write(folder.join(ABORTED), b"") {
            Ok(()) => done(),
            Err(error) => failed(format!("cannot mark the move as rolling back: {error}")),
        },
    };
    answer.version = env!("CARGO_PKG_VERSION").to_owned();
    if op.changes() {
        if !matches!(
            op,
            Op::Receive { .. } | Op::Claim { .. } | Op::Release | Op::Abort
        ) {
            answer.state = Some(state(site, &claims, &folder, &[]));
        }
        journal(
            &folder,
            &serde_json::json!({"at": site.now(), "op": op.name(), "phase": "done", "ok": answer.ok, "error": answer.error}),
        );
    }
    answer
}

fn claim<S: Site>(site: &mut S, base: &Path, move_id: &str, source_bank: &str) -> Answer {
    match read_claim(base) {
        Ok(Some(claim)) if claim.move_id == move_id => return done(),
        Ok(Some(claim)) => {
            return failed(format!(
                "move {} (from bank {}, since {}) holds this target; finish or roll back that move first",
                claim.move_id, claim.source_bank, claim.at
            ));
        }
        Ok(None) => {}
        Err(why) => return failed(why),
    }
    let claim = Claim {
        move_id: move_id.to_owned(),
        source_bank: source_bank.to_owned(),
        at: site.now(),
    };
    let bytes = serde_json::to_vec(&claim).unwrap_or_default();
    match crate::board::state::atomic_create(&base.join(CLAIM), &bytes) {
        Ok(()) => done(),
        // Another move claimed it between the read and the create.
        Err(_) => match read_claim(base) {
            Ok(Some(held)) if held.move_id == move_id => done(),
            Ok(Some(held)) => failed(format!(
                "move {} (from bank {}) holds this target; finish or roll back that move first",
                held.move_id, held.source_bank
            )),
            _ => failed(format!("cannot write {}", base.join(CLAIM).display())),
        },
    }
}

fn gate_and_start<S: Site>(site: &mut S, folder: &Path, request: &Request) -> Answer {
    let docker = match site.docker() {
        Ok(docker) => docker,
        Err(why) => return failed(why),
    };
    let image = match image(docker) {
        Ok(image) => image,
        Err(why) => return failed(why),
    };
    let ungate = |docker: &mut S::D| {
        let ran = run(docker, &helper(&image, "rm", &["-f", GATE]), SHORT);
        if ran.ok() {
            Ok(())
        } else {
            Err(format!("cannot remove {GATE} ({})", probe::why(&ran)))
        }
    };
    let start = |docker: &mut S::D| {
        let ran = run(docker, &["start", daemon()], SHORT);
        if ran.ok() {
            Ok(())
        } else {
            Err(format!("docker start failed ({})", probe::why(&ran)))
        }
    };
    let result = match &request.op {
        Op::Gate => {
            let body = serde_json::json!({"move_id": request.move_id, "written_at": site_now()});
            let file = folder.join("gate.json");
            match std::fs::write(&file, format!("{body}\n")) {
                Err(error) => Err(format!("cannot write {}: {error}", file.display())),
                Ok(()) => {
                    let args: Vec<String> = helper(&image, "sh", &["-c", &format!("cat > {GATE}")])
                        .into_iter()
                        .map(str::to_owned)
                        .collect();
                    let mut args = args;
                    args.insert(2, "-i".to_owned());
                    let ran = docker.run_io(&args, SHORT, Input::File(&file), Output::Capture);
                    if ran.ok() {
                        Ok(())
                    } else {
                        Err(format!("cannot write {GATE} ({})", probe::why(&ran)))
                    }
                }
            }
        }
        Op::Ungate => ungate(docker),
        Op::StartGated => ungate(docker).and_then(|()| start(docker)),
        _ => {
            // The target's own bank only: a gate means it holds the moved one.
            match gated(docker, &image) {
                Some(false) => start(docker),
                Some(true) => Err(format!(
                    "{GATE} is present: the target holds the moved bank, which only the move starts"
                )),
                None => Err(format!("cannot read whether {GATE} is present")),
            }
        }
    };
    match result {
        Ok(()) => done(),
        Err(why) => failed(why),
    }
}

fn site_now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::restore_cli::docker::script::{fail, ok};

    type Answers = Box<dyn FnMut(&[String]) -> Ran>;

    struct Loose {
        calls: Vec<Vec<String>>,
        answer: Answers,
    }

    impl Docker for Loose {
        fn run_io(
            &mut self,
            args: &[String],
            _timeout: Duration,
            _input: Input<'_>,
            output: Output<'_>,
        ) -> Ran {
            self.calls.push(args.to_vec());
            if let Output::File(path) = output {
                std::fs::write(path, b"").unwrap();
            }
            (self.answer)(args)
        }
    }

    struct Fake {
        home: PathBuf,
        docker: Loose,
        restored: Vec<Vec<String>>,
        restore_answer: (i32, String),
    }

    impl Fake {
        fn new(answer: impl FnMut(&[String]) -> Ran + 'static) -> Self {
            let home =
                std::env::temp_dir().join(format!("move-ops-{}", uuid::Uuid::new_v4().simple()));
            std::fs::create_dir_all(&home).unwrap();
            Self {
                home,
                docker: Loose {
                    calls: Vec::new(),
                    answer: Box::new(answer),
                },
                restored: Vec::new(),
                restore_answer: (0, "Restore complete".into()),
            }
        }
        fn base(&self) -> PathBuf {
            self.home.join(".pseudolife-mcp/moves")
        }
    }

    impl Drop for Fake {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.home);
        }
    }

    impl Site for Fake {
        type D = Loose;
        fn docker(&mut self) -> Result<&mut Loose, String> {
            Ok(&mut self.docker)
        }
        fn home(&self) -> Option<PathBuf> {
            Some(self.home.clone())
        }
        fn now(&self) -> String {
            "2026-11-14T08:00:00Z".into()
        }
        fn systemctl(&mut self, _args: &[&str]) -> Result<String, String> {
            Ok("enabled".into())
        }
        fn restore(&mut self, args: &[String], _abort_file: &Path) -> (i32, String) {
            self.restored.push(args.to_vec());
            self.restore_answer.clone()
        }
    }

    const ID: &str = "20261114-080000-abcdef01";
    const OTHER: &str = "20261114-070000-00000000";

    fn request(id: &str, op: Op) -> Request {
        Request {
            protocol: PROTOCOL,
            move_id: id.into(),
            work_dir: String::new(),
            rollback: false,
            op,
        }
    }

    fn quiet(args: &[String]) -> Ran {
        if args.first().is_some_and(|a| a == "inspect") {
            return ok("img:1");
        }
        fail(1, "no such container")
    }

    fn act(fake: &mut Fake, id: &str, op: Op) -> Answer {
        perform(fake, &request(id, op), &mut std::io::empty())
    }

    fn claim(bank: &str) -> Op {
        Op::Claim {
            source_bank: bank.into(),
            resume: false,
        }
    }

    #[test]
    fn one_move_holds_the_claim_and_another_is_refused() {
        let mut fake = Fake::new(quiet);
        assert!(act(&mut fake, ID, claim("aaaa")).ok);
        assert!(
            act(&mut fake, ID, claim("aaaa")).ok,
            "a resumed move claims again"
        );
        let refused = act(&mut fake, OTHER, claim("bbbb"));
        assert!(!refused.ok);
        assert!(
            refused.error.contains(ID) && refused.error.contains("aaaa"),
            "{}",
            refused.error
        );
        // Another move's release gives up nothing.
        assert!(act(&mut fake, OTHER, Op::Release).ok);
        assert!(fake.base().join(CLAIM).exists());
        assert!(act(&mut fake, ID, Op::Release).ok);
        assert!(!fake.base().join(CLAIM).exists());
        assert!(act(&mut fake, OTHER, claim("bbbb")).ok);
    }

    #[test]
    fn moves_with_different_work_directories_meet_at_one_claim() {
        let mut fake = Fake::new(quiet);
        let mut first = request(ID, claim("aaaa"));
        first.work_dir = fake.home.join("staging-a").to_string_lossy().into_owned();
        assert!(perform(&mut fake, &first, &mut std::io::empty()).ok);
        let mut second = request(OTHER, claim("bbbb"));
        second.work_dir = fake.home.join("staging-b").to_string_lossy().into_owned();
        let refused = perform(&mut fake, &second, &mut std::io::empty());
        assert!(
            !refused.ok && refused.error.contains(ID),
            "{}",
            refused.error
        );
        assert!(fake.base().join(CLAIM).exists());
    }

    #[test]
    fn a_change_without_this_moves_claim_is_refused_before_docker_runs() {
        let mut fake = Fake::new(quiet);
        for op in [
            Op::Stop,
            Op::Gate,
            Op::StartGated,
            Op::Restore,
            Op::PolicyNo,
            Op::TimerPause,
        ] {
            let answer = act(&mut fake, ID, op.clone());
            assert!(!answer.ok, "{op:?}");
            assert!(answer.error.contains("claim"), "{op:?}: {}", answer.error);
        }
        assert!(act(&mut fake, OTHER, claim("b")).ok);
        let answer = act(&mut fake, ID, Op::Stop);
        assert!(
            !answer.ok && answer.error.contains(OTHER),
            "{}",
            answer.error
        );
        assert!(fake.docker.calls.is_empty(), "{:?}", fake.docker.calls);
    }

    #[test]
    fn a_received_file_must_match_its_digest_and_length() {
        let mut fake = Fake::new(quiet);
        assert!(act(&mut fake, ID, claim("a")).ok);
        let body = b"pg dump bytes";
        let digest: String = Sha256::digest(body)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let receive = |sha: &str, bytes: u64| Op::Receive {
            name: "pseudolife_memory.sql.gz".into(),
            sha256: sha.into(),
            bytes,
        };
        let folder = fake.base().join(ID);
        let wrong = perform(
            &mut fake,
            &request(ID, receive(&"0".repeat(64), 13)),
            &mut &body[..],
        );
        assert!(
            !wrong.ok && wrong.error.contains("SHA-256"),
            "{}",
            wrong.error
        );
        let short = perform(
            &mut fake,
            &request(ID, receive(&digest, 20)),
            &mut &body[..],
        );
        assert!(
            !short.ok && short.error.contains("short"),
            "{}",
            short.error
        );
        assert!(!folder.join("pseudolife_memory.sql.gz").exists());
        assert!(!folder.join("pseudolife_memory.sql.gz.part").exists());
        let good = perform(
            &mut fake,
            &request(ID, receive(&digest, 13)),
            &mut &body[..],
        );
        assert!(good.ok, "{}", good.error);
        assert_eq!(
            std::fs::read(folder.join("pseudolife_memory.sql.gz")).unwrap(),
            body
        );
        for name in ["../x", ".hidden", "a/b", ""] {
            let op = Op::Receive {
                name: name.into(),
                sha256: digest.clone(),
                bytes: 13,
            };
            assert!(
                !perform(&mut fake, &request(ID, op), &mut &body[..]).ok,
                "{name:?}"
            );
        }
    }

    #[test]
    fn each_change_is_journalled_before_and_after() {
        let mut fake = Fake::new(quiet);
        assert!(act(&mut fake, ID, claim("a")).ok);
        act(&mut fake, ID, Op::TimerPause);
        let journal = std::fs::read_to_string(fake.base().join(ID).join("ops.jsonl")).unwrap();
        let phases: Vec<(String, String)> = journal
            .lines()
            .map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap())
            .map(|v| {
                (
                    v["op"].as_str().unwrap().to_owned(),
                    v["phase"].as_str().unwrap().to_owned(),
                )
            })
            .collect();
        let expected: Vec<(String, String)> = [
            ("claim", "begun"),
            ("claim", "done"),
            ("timer-pause", "begun"),
            ("timer-pause", "done"),
        ]
        .iter()
        .map(|(a, b)| ((*a).to_owned(), (*b).to_owned()))
        .collect();
        assert_eq!(phases, expected);
    }

    #[test]
    fn the_targets_own_start_is_refused_while_the_gate_is_present() {
        let mut fake = Fake::new(|args: &[String]| {
            if args.first().is_some_and(|a| a == "inspect") {
                return ok("img:1");
            }
            if args.iter().any(|a| a.contains("test -e /data/move.json")) {
                return ok("");
            }
            fail(1, "unexpected")
        });
        assert!(act(&mut fake, ID, claim("a")).ok);
        let answer = act(&mut fake, ID, Op::Start);
        assert!(
            !answer.ok && answer.error.contains("moved bank"),
            "{}",
            answer.error
        );
        assert!(
            !fake
                .docker
                .calls
                .iter()
                .any(|c| c.first().is_some_and(|a| a == "start"))
        );
    }

    #[test]
    fn requests_from_another_protocol_or_with_a_bad_id_are_refused() {
        let mut fake = Fake::new(quiet);
        let mut wrong = request(ID, Op::Stop);
        wrong.protocol = 3;
        let answer = perform(&mut fake, &wrong, &mut std::io::empty());
        assert!(
            !answer.ok && answer.error.contains("protocol"),
            "{}",
            answer.error
        );
        for id in ["../../etc", "", "20261114-080000-ABCDEF01"] {
            assert!(!act(&mut fake, id, Op::Stop).ok, "{id:?}");
        }
        assert!(act(&mut fake, ID, claim("a")).ok);
        let answer = act(
            &mut fake,
            ID,
            Op::PolicySet {
                policy: "always; rm -rf /".into(),
            },
        );
        assert!(
            !answer.ok && answer.error.contains("restart policy"),
            "{}",
            answer.error
        );
    }

    #[test]
    fn restore_needs_the_manifest_and_tables_are_schema_dot_name() {
        assert!(valid_table("public.entries"));
        assert!(!valid_table("public.\"x\""));
        assert!(!valid_table("entries"));
        assert!(!valid_table("a.b.c"));
        let mut fake = Fake::new(quiet);
        assert!(act(&mut fake, ID, claim("a")).ok);
        let answer = act(&mut fake, ID, Op::Restore);
        assert!(
            !answer.ok && answer.error.contains("manifest"),
            "{}",
            answer.error
        );
        assert!(fake.restored.is_empty());
    }
}

#[cfg(test)]
mod abort_tests {
    use super::*;
    use crate::cli::restore_cli::docker::script::ok;

    struct Quiet {
        home: PathBuf,
        docker: Fixed,
    }

    struct Fixed;

    impl Docker for Fixed {
        fn run_io(
            &mut self,
            args: &[String],
            _timeout: Duration,
            _input: Input<'_>,
            _output: Output<'_>,
        ) -> Ran {
            if args.first().is_some_and(|a| a == "inspect") {
                return ok("false");
            }
            ok("")
        }
    }

    impl Site for Quiet {
        type D = Fixed;
        fn docker(&mut self) -> Result<&mut Fixed, String> {
            Ok(&mut self.docker)
        }
        fn home(&self) -> Option<PathBuf> {
            Some(self.home.clone())
        }
        fn now(&self) -> String {
            "2026-11-14T08:00:00Z".into()
        }
        fn systemctl(&mut self, _args: &[&str]) -> Result<String, String> {
            Ok("enabled".into())
        }
        fn restore(&mut self, _args: &[String], _abort_file: &Path) -> (i32, String) {
            (0, String::new())
        }
    }

    const ID: &str = "20261114-080000-abcdef01";

    fn ask(site: &mut Quiet, op: Op, rollback: bool) -> Answer {
        let request = Request {
            protocol: PROTOCOL,
            move_id: ID.into(),
            work_dir: String::new(),
            rollback,
            op,
        };
        perform(site, &request, &mut std::io::empty())
    }

    #[test]
    fn after_the_abort_only_the_rollback_and_a_resume_act() {
        let home =
            std::env::temp_dir().join(format!("move-abort-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&home).unwrap();
        let mut site = Quiet {
            home: home.clone(),
            docker: Fixed,
        };
        let claim = |resume| Op::Claim {
            source_bank: "a".into(),
            resume,
        };
        assert!(ask(&mut site, claim(false), false).ok);
        assert!(ask(&mut site, Op::Abort, true).ok);
        // A late forward step of the original run does nothing.
        for op in [
            Op::Gate,
            Op::StartGated,
            Op::Restore,
            Op::TimerPause,
            claim(false),
        ] {
            let answer = ask(&mut site, op.clone(), false);
            assert!(!answer.ok, "{op:?}");
            assert!(
                answer.error.contains("being rolled back"),
                "{op:?}: {}",
                answer.error
            );
        }
        // The rollback's own steps run.
        assert!(ask(&mut site, Op::TimerResume, true).ok);
        // A resumed move takes the target back, and its steps run again.
        assert!(ask(&mut site, claim(true), false).ok);
        assert!(ask(&mut site, Op::TimerPause, false).ok);
        let _ = std::fs::remove_dir_all(&home);
    }
}
