//! Scripted runs of `execute`: the full happy path, then a failure and an
//! interrupt at every Docker call, each checked for the bank-state message,
//! the recovery calls that follow, and that no later step ever runs.
use std::{
    collections::HashMap,
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};

use flate2::{Compression, write::GzEncoder};
use serde_json::{Value, json};

use super::{
    args::Args,
    bank::{Plan, Step, Stop},
    docker::{Docker, Input, Output, Ran},
    execute::{self, Context, Host},
};

pub(super) const RUN_TAG: &str = "0123456789abcdef";
pub(super) const STAMP: &str = "20261114-000000-abcdef01";
const DF: &str =
    "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1 1 99999999 1% /\n";

pub(super) fn gz(data: &[u8]) -> Vec<u8> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

pub(super) fn dump() -> Vec<u8> {
    gz(b"SELECT 1;\n-- PostgreSQL database dump complete\n")
}

/// A one-file tar.gz (`./config.yaml`), as `tar czf - -C /data .` makes.
pub(super) fn archive() -> Vec<u8> {
    let mut builder = tar::Builder::new(Vec::new());
    let mut header = tar::Header::new_gnu();
    header.set_size(3);
    header.set_mode(0o644);
    header.set_cksum();
    builder
        .append_data(&mut header, "./config.yaml", &b"a: 1"[..3])
        .unwrap();
    gz(&builder.into_inner().unwrap())
}

/// What `find -printf` lists after extracting [`archive`] into the
/// staging root.
fn listing() -> Vec<u8> {
    [
        &b"d\x0066\x00100\x002\x00755\x00\x00\x00"[..],
        b"f\x0066\x00101\x001\x00644\x00config.yaml\x00\x00",
        b"END\x00",
    ]
    .concat()
}

/// A temporary directory, removed on drop.
pub(super) struct Dir(pub(super) PathBuf);

impl Dir {
    pub(super) fn new() -> Self {
        let path = std::env::temp_dir().join(format!("pl-exec-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}

impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// A `docker` that answers by argv shape, like the real one would for a
/// healthy bank, and fails or interrupts its `fail_at`-th call.
#[derive(Default)]
pub(super) struct Fake {
    pub(super) calls: Vec<Vec<String>>,
    pub(super) inputs: Vec<Option<PathBuf>>,
    pub(super) fail_at: Option<usize>,
    /// Fail the first call with this label.
    pub(super) fail_on: Option<&'static str>,
    /// Fail every call with this label.
    pub(super) fail_always: Option<&'static str>,
    pub(super) interrupt_at: Option<usize>,
    /// An interrupt that arrives just after this call succeeded.
    pub(super) interrupt_after: Option<usize>,
    pub(super) interrupted: bool,
    pub(super) interruptible: bool,
    /// helper id -> its script.
    pub(super) helpers: HashMap<String, String>,
    /// A safety dump that lacks the completion marker.
    pub(super) truncated_dump: bool,
    /// What the staging helper lists instead of [`listing`].
    pub(super) extracted: Option<Vec<u8>>,
    /// The free-space helper's mount table shows this mount below /data.
    pub(super) df_nested: Option<&'static str>,
    /// The swap helper's mount table shows this mount below /data.
    pub(super) swap_nested: Option<&'static str>,
    /// `docker inspect <name>` answers.
    pub(super) docs: HashMap<String, String>,
    /// The database-exists probe answers `f`.
    pub(super) absent_db: bool,
    /// Each state archive's helper fails once with the next of these as
    /// tar's stderr (exit 1), in order, before the archive is written.
    pub(super) archive_failures: Vec<&'static str>,
    /// The first call with this label never runs, for this reason (a
    /// spawn failure, a timeout, an output file that cannot be created).
    pub(super) fault_on: Option<(&'static str, &'static str)>,
}

impl Docker for Fake {
    fn run_io(
        &mut self,
        args: &[String],
        _timeout: Duration,
        input: Input<'_>,
        output: Output<'_>,
    ) -> Ran {
        let at = self.calls.len();
        self.calls.push(args.to_vec());
        self.inputs.push(match input {
            Input::Null => None,
            Input::File(path) => Some(path.to_path_buf()),
        });
        // The real runner creates its output file new, never over one.
        if let Output::File(path) = output
            && path.exists()
        {
            return Ran {
                code: 1,
                failed: true,
                fault: Some(format!("{} cannot be created (it exists)", path.display())),
                ..Ran::default()
            };
        }
        if let Some((wanted, fault)) = self.fault_on
            && label(args) == wanted
        {
            self.fault_on = None;
            return Ran {
                code: 1,
                failed: true,
                fault: Some(fault.to_owned()),
                ..Ran::default()
            };
        }
        let write = |bytes: &[u8]| {
            if let Output::File(path) = output {
                std::fs::write(path, bytes).unwrap();
            }
        };
        if self.interrupted && self.interruptible {
            write(b"");
            return Ran {
                code: 1,
                failed: true,
                interrupted: true,
                ..Ran::default()
            };
        }
        if Some(at) == self.interrupt_at {
            self.interrupted = true;
            write(b"");
            return Ran {
                code: 1,
                failed: true,
                interrupted: self.interruptible,
                ..Ran::default()
            };
        }
        if Some(at) == self.fail_at
            || self.fail_always.is_some_and(|wanted| label(args) == wanted)
            || self.fail_on.is_some_and(|wanted| {
                label(args) == wanted && {
                    self.fail_on = None;
                    true
                }
            })
        {
            write(b"");
            return Ran {
                code: 1,
                stderr: b"boom".to_vec(),
                ..Ran::default()
            };
        }
        if Some(at) == self.interrupt_after {
            self.interrupted = true;
        }
        let ok = |stdout: &str| Ran {
            code: 0,
            stdout: stdout.as_bytes().to_vec(),
            ..Ran::default()
        };
        let a: Vec<&str> = args.iter().map(String::as_str).collect();
        match a.as_slice() {
            ["create", .., script] => {
                let id = format!("helper{}", self.helpers.len() + 1);
                self.helpers.insert(id.clone(), (*script).to_owned());
                ok(&format!("{id}\n"))
            }
            ["start", "-a", helper] => {
                let script = self.helpers.get(*helper).cloned().unwrap_or_default();
                if script.starts_with("tar czf") {
                    if !self.archive_failures.is_empty() {
                        let stderr = self.archive_failures.remove(0);
                        write(b"partial");
                        return Ran {
                            code: 1,
                            stderr: stderr.as_bytes().to_vec(),
                            ..Ran::default()
                        };
                    }
                    write(&archive());
                    ok("")
                } else if let (Some(mount), true) = (self.df_nested, script.contains("df -Pk")) {
                    Ran {
                        code: 3,
                        stdout: format!("nested {mount}\n").into_bytes(),
                        ..Ran::default()
                    }
                } else if let (Some(mount), true) =
                    (self.swap_nested, script.contains("mv -t /data"))
                {
                    Ran {
                        code: 3,
                        stdout: format!("nested {mount}\n").into_bytes(),
                        ..Ran::default()
                    }
                } else if script.contains("-printf") {
                    Ran {
                        code: 0,
                        stdout: self.extracted.clone().unwrap_or_else(listing),
                        ..Ran::default()
                    }
                } else if script.contains("df -Pk") {
                    ok(DF)
                } else {
                    ok("")
                }
            }
            ["exec", _, "df", ..] => ok(DF),
            ["exec", _, "sh", "-c", script, ..] if script.contains("pg_dump") => {
                if self.truncated_dump {
                    write(&gz(b"SELECT 1;\n"));
                } else {
                    write(&dump());
                }
                ok("")
            }
            ["inspect", name] if self.docs.contains_key(*name) => ok(&self.docs[*name]),
            _ if a
                .iter()
                .any(|arg| arg.contains("SELECT EXISTS(SELECT 1 FROM pg_database")) =>
            {
                ok(if self.absent_db { "f\n" } else { "t\n" })
            }
            _ => ok(""),
        }
    }

    fn set_interruptible(&mut self, on: bool) {
        self.interruptible = on;
    }

    fn is_interruptible(&self) -> bool {
        self.interruptible
    }

    fn interrupted(&self) -> bool {
        self.interrupted
    }
}

#[derive(Default)]
pub(super) struct FakeHost {
    pub(super) info: Vec<String>,
    pub(super) errors: Vec<String>,
    pub(super) health: Option<Value>,
    pub(super) health_calls: usize,
    /// How many runs this setup made before this one.
    pub(super) run: u32,
    /// The host's free space is under the floor.
    pub(super) no_space: bool,
}

impl Host for FakeHost {
    fn info(&mut self, line: &str) {
        self.info.push(line.to_owned());
    }
    fn error(&mut self, line: &str) {
        self.errors.push(line.to_owned());
    }
    fn health(&mut self, _url: &str) -> Option<Value> {
        self.health_calls += 1;
        self.health.clone()
    }
    fn sleep(&mut self, _duration: Duration) {}
    fn stamp(&mut self) -> String {
        // A setup's first run gets STAMP; a later run over the same safety
        // directory a fresh one, as the real stamp's random part would be.
        if self.run == 0 {
            STAMP.into()
        } else {
            format!("20261114-000000-abcde{:03x}", self.run)
        }
    }
    fn run_tag(&mut self) -> String {
        RUN_TAG.into()
    }
    fn now(&mut self) -> String {
        "2026-11-14T00:00:00+11:00".into()
    }
    fn host_space(&mut self, directory: &str) -> Step<()> {
        if self.no_space {
            return Err(Stop(
                format!(
                    "less than 8 GiB is free on {directory}; free at least 8 GiB there, then retry. Nothing was changed"
                ),
                2,
            ));
        }
        Ok(())
    }
}

struct Setup {
    dir: Dir,
    args: Args,
    plan: Plan,
    runs: std::cell::Cell<u32>,
    /// What the state check said the archive unpacks to.
    state_size: std::cell::Cell<u128>,
}

impl Setup {
    fn new(state: bool) -> Self {
        let dir = Dir::new();
        let backup = dir.0.join("bank.sql.gz");
        std::fs::write(&backup, dump()).unwrap();
        let archive_path = dir.0.join("state.tgz");
        std::fs::write(&archive_path, archive()).unwrap();
        let safety = dir.0.join("safety");
        let args = Args {
            backup_file: backup,
            state_archive: state.then_some(archive_path),
            yes: true,
            leave_stopped: false,
            container: None,
            daemon_container: None,
            db: None,
            user: None,
            db_port: None,
            daemon_url: None,
            safety_dir: None,
        };
        let plan = Plan {
            pg: "p".into(),
            daemon: "d".into(),
            user: "u".into(),
            db: "db".into(),
            port: 5432,
            image: "img".into(),
            daemon_url: "http://127.0.0.1:9".into(),
            was_running: true,
            exists: true,
            safety_dir: safety.to_string_lossy().into_owned(),
            state_mounts: vec!["/data".into()],
            pg_paths: vec!["/".into()],
            stale_helpers: Vec::new(),
        };
        Self {
            dir,
            args,
            plan,
            runs: std::cell::Cell::new(0),
            state_size: std::cell::Cell::new(4096),
        }
    }

    fn data_dir(&self) -> PathBuf {
        self.dir.0.join("data")
    }

    fn journal(&self) -> PathBuf {
        execute::journal_file(&self.data_dir(), &self.plan)
    }

    fn run(&self, docker: &mut Fake, host: &mut FakeHost) -> u8 {
        host.run = self.runs.get();
        self.runs.set(host.run + 1);
        std::fs::create_dir_all(self.data_dir()).unwrap();
        let cx = Context {
            args: &self.args,
            plan: &self.plan,
            backup_file: self.args.backup_file.clone(),
            state_archive: self.args.state_archive.clone(),
            state_size: self.state_size.get(),
            data_dir: self.data_dir(),
        };
        execute::execute(docker, host, &cx)
    }
}

pub(super) fn ready_host() -> FakeHost {
    FakeHost {
        health: Some(json!({"status": "ok", "ready": true})),
        ..FakeHost::default()
    }
}

/// The happy path's Docker calls, by a short label each.
pub(super) fn label(call: &[String]) -> String {
    let a: Vec<&str> = call.iter().map(String::as_str).collect();
    let text = a.join(" ");
    match a.as_slice() {
        ["exec", _, "sh", "-c", s, ..] if s.starts_with("rm -f /tmp/pl_restore-*") => {
            "stale".into()
        }
        ["stop", d] => format!("stop {d}"),
        ["start", d] => format!("start {d}"),
        ["exec", _, "sh", "-c", s, ..] if s.contains("pg_dump") => "safety-dump".into(),
        ["create", .., s] if s.starts_with("tar czf") => "create safety-helper".into(),
        ["create", .., s] if s.contains("df -Pk") => "create df-helper".into(),
        ["create", .., s]
            if s.starts_with("rm -rf --one-file-system -- /data/.pseudolife-restore-[") =>
        {
            "create sweep-helper".into()
        }
        ["create", .., s]
            if s.starts_with("rm -rf --one-file-system -- /data/.pseudolife-restore-") =>
        {
            "create discard-helper".into()
        }
        ["create", .., s] if s.contains("-printf") => "create stage-helper".into(),
        ["create", .., s] if s.contains("mv -t /data") => "create swap-helper".into(),
        ["start", "-a", h] => format!("start-a {h}"),
        ["rm", "-f", h] => format!("rm {h}"),
        ["rm", "-f", "-v", h] => format!("rm {h}"),
        ["exec", "-i", ..] => "copy-in".into(),
        ["exec", _, "sh", "-c", s, ..] if s.contains("gunzip -c") => "gunzip".into(),
        ["cp", ..] => "cp state".into(),
        ["exec", _, "df", "-Pk", path] => format!("df {path}"),
        ["exec", _, "rm", "-f", ..] => "rm staged".into(),
        _ if text.contains("DROP DATABASE") => "drop".into(),
        _ if text.contains("CREATE DATABASE") => "create-db".into(),
        _ if text.contains("REVOKE CONNECT") => "revoke".into(),
        _ if text.contains("pg_terminate_backend") => "terminate".into(),
        _ if text.contains(" -f /tmp/pl_restore-") => "replay".into(),
        _ => text,
    }
}

const HAPPY: [&str; 30] = [
    "stale",
    "stop d",
    "create sweep-helper",
    "start-a helper1",
    "rm helper1",
    "safety-dump",
    "create safety-helper",
    "start-a helper2",
    "rm helper2",
    "copy-in",
    "gunzip",
    "df /tmp",
    "df /",
    "create df-helper",
    "start-a helper3",
    "rm helper3",
    "create stage-helper",
    "cp state",
    "start-a helper4",
    "rm helper4",
    "create swap-helper",
    "drop",
    "create-db",
    "revoke",
    "replay",
    "start-a helper5",
    "rm helper5",
    "start d",
    // cleanup
    "rm staged",
    "",
];

#[test]
fn a_successful_restore_runs_every_step_in_order_and_clears_its_journal() {
    let setup = Setup::new(true);
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert_eq!(labels, HAPPY[..HAPPY.len() - 1].to_vec());
    assert!(host.errors.is_empty(), "{:?}", host.errors);
    assert_eq!(
        host.info.last().unwrap(),
        "Restore complete; daemon started."
    );
    // The backup streamed in from its file, and the safety pair is final.
    assert_eq!(
        docker.inputs[9].as_deref(),
        Some(setup.args.backup_file.as_path())
    );
    let safety = Path::new(&setup.plan.safety_dir);
    assert!(
        safety
            .join(format!("pseudolife_safety_memory-{STAMP}.sql.gz"))
            .is_file()
    );
    assert!(
        safety
            .join(format!("pseudolife_safety_state-{STAMP}.tgz"))
            .is_file()
    );
    assert!(
        safety
            .join(format!("pseudolife_restore-{STAMP}.txt"))
            .is_file()
    );
    assert!(
        !safety
            .join(format!("pseudolife_safety_memory-{STAMP}.sql.gz.part"))
            .exists()
    );
    assert!(!setup.journal().exists());
    let recovery = host
        .info
        .iter()
        .find(|line| line.starts_with("If this restore fails"))
        .unwrap();
    assert!(
        recovery.contains(&format!("pseudolife_safety_memory-{STAMP}.sql.gz")),
        "{recovery}"
    );
    assert!(
        recovery.contains("--container=p --daemon-container=d --db=db --user=u --db-port=5432")
    );
}

/// Where each happy-path call index falls.
fn phase_of(index: usize) -> &'static str {
    match index {
        0 => "unchanged",
        1..=20 => "stopped",
        21..=24 => "replacing",
        25 => "state",
        _ => "restored",
    }
}

/// The calls after a failure (or an interrupt) at happy-path call `index`:
/// a helper whose step failed is removed, the staging directory goes before
/// the daemon starts again, staged files at cleanup. `None`: the failure is
/// ignored (clearing stale files, removing a finished helper) and the run
/// succeeds; an interrupt there stops at the next step.
fn after_failure(index: usize, interrupt: bool) -> Option<Vec<&'static str>> {
    let after = match (index, interrupt) {
        (0 | 4 | 8 | 15 | 19 | 26, false) | (28, _) => return None,
        (0, true) => vec![],
        (1 | 2 | 5 | 6, _) | (8, true) => vec!["start d"],
        (3, _) | (4, true) => vec!["rm helper1", "start d"],
        (7, _) => vec!["rm helper2", "start d"],
        (9..=13, _) | (15, true) => vec!["start d", "rm staged"],
        (14, _) => vec!["rm helper3", "start d", "rm staged"],
        // The staging directory may exist from here: removed (by its own
        // helper) before the daemon starts.
        (16, _) => vec![
            "create discard-helper",
            "start-a helper4",
            "rm helper4",
            "start d",
            "rm staged",
        ],
        (17..=19, _) => vec![
            "rm helper4",
            "create discard-helper",
            "start-a helper5",
            "rm helper5",
            "start d",
            "rm staged",
        ],
        (20, _) => vec![
            "create discard-helper",
            "start-a helper5",
            "rm helper5",
            "start d",
            "rm staged",
        ],
        // After the drop the daemon stays stopped; the swap helper and the
        // staged tree go at cleanup.
        (21..=24, _) => vec![
            "terminate",
            "rm staged",
            "rm helper5",
            "create discard-helper",
            "start-a helper6",
            "rm helper6",
        ],
        (25, _) => vec![
            "rm helper5",
            "rm staged",
            "create discard-helper",
            "start-a helper6",
            "rm helper6",
        ],
        (26, true) => vec!["rm staged", "rm helper5"],
        (27, _) => vec!["rm staged"],
        _ => unreachable!("{index}"),
    };
    Some(after)
}

fn check_failure_at(index: usize, interrupt: bool) {
    let setup = Setup::new(true);
    let mut docker = Fake::default();
    if interrupt {
        docker.interrupt_at = Some(index);
    } else {
        docker.fail_at = Some(index);
    }
    let mut host = ready_host();
    let code = setup.run(&mut docker, &mut host);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    let context = format!(
        "call {index} ({}), interrupt {interrupt}: {labels:?} {:?}",
        HAPPY[index], host.errors
    );
    let Some(expected) = after_failure(index, interrupt) else {
        assert_eq!(code, 0, "{context}");
        assert_eq!(
            labels[..HAPPY.len() - 1],
            HAPPY[..HAPPY.len() - 1],
            "{context}"
        );
        // A helper that could not be removed is removed at cleanup.
        let retried: &[&str] = match index {
            4 => &["rm helper1"],
            19 => &["rm helper4"],
            26 => &["rm helper5"],
            _ => &[],
        };
        assert_eq!(labels[HAPPY.len() - 1..], *retried, "{context}");
        return;
    };
    // A free-space refusal exits 2 like every refusal; other failures 1.
    let expected_code = if interrupt {
        130
    } else if matches!(index, 11 | 12 | 14) {
        2
    } else {
        1
    };
    assert_eq!(code, expected_code, "{context}");
    assert_eq!(labels[..=index], HAPPY[..=index], "{context}");
    let after: Vec<&str> = labels[index + 1..].iter().map(String::as_str).collect();
    assert_eq!(after, expected, "{context}");
    let text = host.errors.join("\n");
    match phase_of(index) {
        "unchanged" => {
            assert!(
                text.contains(
                    "The bank was not changed and the daemon was not stopped. Nothing was restored."
                ),
                "{context}"
            );
        }
        "stopped" => {
            assert!(text.contains("The bank was not changed and the daemon d was started again. Nothing was restored."), "{context}");
            // The printed command quotes the path as the platform's shell
            // needs it (a temp folder spelt `RUNNER~1` is quoted on Windows).
            let retry = format!(
                "retry with:\n  pseudolife-mcp restore {}",
                execute::shell_word(&format!(
                    "--backup-file={}",
                    setup.args.backup_file.to_string_lossy()
                ))
            );
            assert!(text.contains(&retry), "{context}");
            assert!(!setup.journal().exists(), "{context}");
        }
        "replacing" => {
            if index == 21 {
                assert!(text.contains("The DROP of the database db did not report success, so it may or may not still exist; the state volume is unchanged. The daemon d is left stopped"), "{context}");
            } else {
                assert!(text.contains("The database db was dropped and is missing or only partly restored; the state volume is unchanged. The daemon d is left stopped"), "{context}");
            }
            assert!(text.contains(&format!("Recover the bank as it was before this restore with:\n  pseudolife-mcp restore {}", shown(&setup, "memory"))), "{context}");
            assert!(setup.journal().exists(), "{context}");
        }
        "state" => {
            assert!(text.contains("holds the backup, but the state volume is only partly replaced. The daemon d is left stopped"), "{context}");
            assert!(
                text.contains("Recover the bank as it was before this restore with:"),
                "{context}"
            );
            assert!(setup.journal().exists(), "{context}");
        }
        _ => {
            assert!(
                text.contains("The database and state were restored completely."),
                "{context}"
            );
            assert!(!setup.journal().exists(), "{context}");
        }
    }
    // Never a step past the failure: no drop after an early failure, no
    // daemon start after the drop began.
    if index < 21 {
        assert!(!labels.iter().any(|l| l == "drop"), "{context}");
    }
    if (21..26).contains(&index) {
        assert!(!after.contains(&"start d"), "{context}");
    }
    assert!(text.starts_with("restore failed: "), "{context}");
    if interrupt {
        assert!(
            text.starts_with("restore failed: restore was interrupted."),
            "{context}"
        );
    }
}

fn shown(setup: &Setup, kind: &str) -> String {
    let path = Path::new(&setup.plan.safety_dir).join(format!(
        "pseudolife_safety_{kind}-{STAMP}.{}",
        if kind == "memory" { "sql.gz" } else { "tgz" }
    ));
    execute::shell_word(&format!("--backup-file={}", path.to_string_lossy()))
}

#[test]
fn a_failure_at_every_step_stops_there_and_names_the_banks_state() {
    // Every call of the happy path, cleanup included.
    for index in 0..HAPPY.len() - 1 {
        check_failure_at(index, false);
    }
}

#[test]
fn an_interrupt_at_every_step_stops_there_and_names_the_banks_state() {
    for index in 0..HAPPY.len() - 1 {
        check_failure_at(index, true);
    }
}

#[test]
fn a_daemon_that_never_reports_ready_is_reported_with_the_data_restored() {
    let setup = Setup::new(false);
    let mut docker = Fake::default();
    let mut host = FakeHost {
        health: Some(json!({"status": "ok", "ready": false})),
        ..FakeHost::default()
    };
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let text = host.errors.join("\n");
    assert!(
        text.contains("did not report ready at http://127.0.0.1:9/health within 10 minutes"),
        "{text}"
    );
    assert!(
        text.contains("The database and state were restored completely."),
        "{text}"
    );
    assert!(host.health_calls >= 300);
    // The daemon is not stopped again: it serves a complete bank.
    assert_eq!(
        docker.calls.iter().filter(|c| label(c) == "stop d").count(),
        1
    );
}

#[test]
fn an_incomplete_safety_dump_stops_before_anything_is_dropped() {
    let setup = Setup::new(false);
    let mut docker = Fake {
        truncated_dump: true,
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert_eq!(
        labels,
        [
            "stale",
            "stop d",
            "create sweep-helper",
            "start-a helper1",
            "rm helper1",
            "safety-dump",
            "start d"
        ]
    );
    let text = host.errors.join("\n");
    assert!(
        text.contains("the safety dump of the current database is incomplete"),
        "{text}"
    );
    assert!(
        text.contains("The bank was not changed and the daemon d was started again."),
        "{text}"
    );
    let safety = Path::new(&setup.plan.safety_dir);
    assert!(
        std::fs::read_dir(safety).unwrap().next().is_none(),
        "no partial file is left"
    );
}

#[test]
fn an_interrupted_restores_journal_names_its_state_archive_for_backup_rotation() {
    let setup = Setup::new(true);
    let mut docker = Fake {
        fail_on: Some("replay"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let journal: Value =
        serde_json::from_str(&std::fs::read_to_string(setup.journal()).unwrap()).unwrap();
    let archive = setup.args.state_archive.clone().unwrap();
    assert_eq!(
        journal["state_archive"].as_str(),
        Some(archive.to_string_lossy().as_ref()),
        "{journal}"
    );
    // Backup reads it back: the archive, named unlike any backup, is
    // never rotated while the journal stands.
    let protected = super::docker_backup::journal_files(&setup.data_dir()).unwrap();
    assert!(protected.contains(&archive), "{protected:?}");
}

#[test]
fn a_rerun_after_an_interrupted_replay_keeps_the_first_safety_backup() {
    let setup = Setup::new(false);
    // The first run fails during the replay: its journal stays.
    let mut docker = Fake {
        fail_on: Some("replay"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    assert!(setup.journal().exists());
    let journal = std::fs::read_to_string(setup.journal()).unwrap();
    let first: Value = serde_json::from_str(&journal).unwrap();
    // Rename the first safety pair so the second run's names differ.
    let safety = Path::new(&setup.plan.safety_dir);
    let first_dump = PathBuf::from(first["safety_dump"].as_str().unwrap());
    let moved = safety.join("first.sql.gz");
    std::fs::rename(&first_dump, &moved).unwrap();
    let mut record = first.clone();
    record["safety_dump"] = json!(moved.to_string_lossy());
    std::fs::write(setup.journal(), record.to_string()).unwrap();

    // The second run fails before the drop: the daemon stays stopped (the
    // bank is incomplete) and the recovery names the first backup.
    let mut docker = Fake {
        fail_on: Some("copy-in"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert!(!labels.iter().any(|l| l == "start d"), "{labels:?}");
    let text = host.errors.join("\n");
    assert!(
        text.contains(
            "is left stopped because the earlier restore may have left the bank incomplete"
        ),
        "{text}"
    );
    // The journal cannot tell whether the bank was repaired since, so
    // both courses are given.
    assert!(
        text.contains(
            "If you have not repaired the bank since 2026-11-14T00:00:00+11:00, it is still incomplete; recover it as it was before that restore with:"
        ),
        "{text}"
    );
    assert!(
        text.contains(&format!(
            "If you have repaired it since, this run changed nothing: start the daemon with:\n  docker start d\nthen delete {}; then retry this restore with:\n  pseudolife-mcp restore {}",
            setup.journal().display(),
            execute::shell_word(&format!(
                "--backup-file={}",
                setup.args.backup_file.to_string_lossy()
            ))
        )),
        "{text}"
    );
    assert!(
        text.contains(&execute::shell_word(&format!(
            "--backup-file={}",
            moved.to_string_lossy()
        ))),
        "{text}"
    );
    assert!(
        host.info[0].starts_with("Note: an earlier restore of db in p"),
        "{:?}",
        host.info
    );

    // A third run that succeeds clears the journal.
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    assert!(!setup.journal().exists());
}

#[test]
fn another_banks_restore_keeps_an_interrupted_banks_journal() {
    let setup = Setup::new(false);
    let mut docker = Fake {
        fail_on: Some("replay"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let journal = setup.journal();
    let first = std::fs::read(&journal).unwrap();
    // A restore of another bank (another database) under the same data
    // directory succeeds and clears only its own journal.
    let mut other = Setup::new(false);
    other.dir = Dir(setup.dir.0.clone());
    other.plan.db = "other".into();
    other.plan.safety_dir = setup.plan.safety_dir.clone();
    other.runs.set(setup.runs.get());
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(other.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    assert_ne!(other.journal(), journal);
    assert_eq!(std::fs::read(&journal).unwrap(), first);
    std::mem::forget(other.dir);
}

#[test]
fn an_unreadable_journal_is_refused_before_anything_changes() {
    let setup = Setup::new(false);
    std::fs::create_dir_all(setup.data_dir()).unwrap();
    std::fs::write(setup.journal(), b"{not json").unwrap();
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 2);
    assert!(docker.calls.is_empty(), "{:?}", docker.calls);
    assert!(
        host.errors[0].starts_with("restore failed: the restore journal "),
        "{:?}",
        host.errors
    );
    assert!(host.errors[0].ends_with("then delete the file and retry. Nothing was changed"));
}

#[test]
fn helpers_a_killed_run_left_are_removed_before_the_daemon_stops() {
    let mut setup = Setup::new(false);
    setup.plan.stale_helpers = vec!["old1".into()];
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert_eq!(labels[..3], ["stale", "rm old1", "stop d"], "{labels:?}");
    // One the engine will not remove stops the run with the daemon untouched.
    let mut docker = Fake {
        fail_on: Some("rm old1"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert!(!labels.iter().any(|l| l == "stop d"), "{labels:?}");
    assert!(
        host.errors
            .join("\n")
            .contains("run `docker rm -f old1`, then retry")
    );
}

#[test]
fn recovery_still_runs_after_an_interrupt() {
    // An interrupt during the replay: the fake kills any later call made
    // while restore still allows interrupts, so the backend termination
    // and cleanup run only because recovery turns them off.
    let setup = Setup::new(false);
    let mut docker = Fake::default();
    let mut host = ready_host();
    let replay = HAPPY_NO_STATE.iter().position(|l| *l == "replay").unwrap();
    docker.interrupt_at = Some(replay);
    assert_eq!(setup.run(&mut docker, &mut host), 130);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert_eq!(labels[..=replay], HAPPY_NO_STATE[..=replay], "{labels:?}");
    assert_eq!(
        labels[replay + 1..],
        ["terminate", "rm staged"],
        "{labels:?}"
    );
    // A run stopped before the drop restarts the daemon successfully.
    let setup = Setup::new(false);
    let copy_in = HAPPY_NO_STATE.iter().position(|l| *l == "copy-in").unwrap();
    let mut docker = Fake {
        interrupt_at: Some(copy_in),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 130);
    assert!(
        host.errors
            .join("\n")
            .contains("the daemon d was started again"),
        "{:?}",
        host.errors
    );
}

#[test]
fn an_extracted_state_that_leads_out_is_refused_with_the_bank_unchanged() {
    // The header check passed; what tar actually wrote holds a link out.
    let setup = Setup::new(true);
    let mut extracted = listing();
    // The new entry goes before the listing's `END`.
    extracted.truncate(extracted.len() - 4);
    extracted.extend_from_slice(b"l\x0066\x00102\x001\x00777\x00out\x00../x\x00END\x00");
    let mut docker = Fake {
        extracted: Some(extracted),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 2, "{:?}", host.errors);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    let stage = HAPPY.iter().position(|l| *l == "rm helper4").unwrap();
    assert_eq!(labels[..=stage], HAPPY[..=stage], "{labels:?}");
    // The staging directory goes before the daemon starts again; nothing
    // was dropped or replaced.
    assert_eq!(
        labels[stage + 1..],
        [
            "create discard-helper",
            "start-a helper5",
            "rm helper5",
            "start d",
            "rm staged"
        ],
        "{labels:?}"
    );
    let discard = docker.helpers.get("helper5").cloned().unwrap_or_default();
    assert_eq!(
        discard,
        format!("rm -rf --one-file-system -- /data/.pseudolife-restore-{RUN_TAG}")
    );
    let text = host.errors.join("\n");
    assert!(
        text.starts_with("restore failed: the state archive, once extracted, holds a symlink that leads outside the state volume (out -> ../x); re-create it"),
        "{text}"
    );
    assert!(
        text.contains(
            "The bank was not changed and the daemon d was started again. Nothing was restored."
        ),
        "{text}"
    );
    assert!(!setup.journal().exists());
}

#[test]
fn the_state_volume_must_hold_the_archive_copy_and_what_it_unpacks_to() {
    // The fake's df reports 99999999 KiB free on every mount; the floor is
    // 8 GiB; the staged state is the unpacked size plus the archive's copy.
    let free: u128 = 99_999_999 * 1024;
    let room = free - 8 * 1024 * 1024 * 1024;
    let copy = u128::try_from(archive().len()).unwrap();
    let run = |unpacked: u128| {
        let setup = Setup::new(true);
        setup.state_size.set(unpacked);
        let mut docker = Fake::default();
        let mut host = ready_host();
        let code = setup.run(&mut docker, &mut host);
        let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
        (code, host.errors.join("\n"), labels)
    };
    let (code, text, _) = run(room - copy);
    assert_eq!(code, 0, "{text}");
    // One byte more, which only the copy makes too much.
    let (code, text, labels) = run(room - copy + 1);
    assert_eq!(code, 2, "{text}");
    assert!(
        text.starts_with("restore failed: less than 8 GiB beyond the 88 GiB that staging the state archive takes (its copy and what it unpacks to) is free on d:/data; free at least 96 GiB there"),
        "{text}"
    );
    assert!(
        !labels.iter().any(|l| l == "create stage-helper"),
        "{labels:?}"
    );
}

#[test]
fn the_state_is_staged_in_data_and_swapped_in_by_rename() {
    let setup = Setup::new(true);
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    let stage = &docker.helpers["helper4"];
    let swap = &docker.helpers["helper5"];
    let dir = format!("/data/.pseudolife-restore-{RUN_TAG}");
    // A staging directory's exact name: the prefix and 16 hex digits.
    let glob = ".pseudolife-restore-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]";
    // move.json at any depth and a top-level staging directory an older
    // backup caught are left out; the listing carries modes and ends with
    // END.
    assert!(stage.contains(&format!("S={dir}; mkdir -m 700 -- $S; tar --exclude=move.json --exclude=./move.json --anchored '--exclude=./{glob}' '--exclude={glob}' -xzf /tmp/pl_state.tgz -C $S;")), "{stage}");
    assert!(
        stage.ends_with("find $S -printf '%y\\0%D\\0%i\\0%n\\0%m\\0%P\\0%l\\0'; printf 'END\\0'"),
        "{stage}"
    );
    assert!(stage.starts_with("set -e;"), "{stage}");
    // The safety backup leaves only such a directory out.
    assert_eq!(
        docker.helpers["helper2"],
        format!("tar czf - --anchored '--exclude=./{glob}' -C /data .")
    );
    assert!(swap.starts_with("set -e;"), "{swap}");
    // The whole swap: before anything is removed, a mount below /data in
    // the helper's mount table stops it with exit 3.
    assert_eq!(
        swap,
        &format!(
            r#"set -e; S={dir}; test -d $S; awk '$5 ~ /^\/data\// {{ print "nested " $5; found = 1 }} END {{ exit 3 * found }}' /proc/self/mountinfo || exit $?; find /data -mindepth 1 -maxdepth 1 ! -name move.json ! -name .pseudolife-restore-{RUN_TAG} -exec rm -rf --one-file-system -- {{}} +; find $S -mindepth 1 -maxdepth 1 -exec mv -t /data -- {{}} +; rmdir -- $S"#
        )
    );
    // Stale staging directories (that exact name only) go before the
    // safety backup reads /data.
    assert_eq!(
        docker.helpers["helper1"],
        format!("rm -rf --one-file-system -- /data/{glob}")
    );
}

#[test]
fn the_daemon_stays_stopped_while_a_helper_may_still_write_to_data() {
    // The staging copy fails and the staging helper cannot be removed: it
    // may still be extracting into /data, so the daemon is not started.
    let setup = Setup::new(true);
    let cp = HAPPY.iter().position(|l| *l == "cp state").unwrap();
    let mut docker = Fake {
        fail_at: Some(cp),
        fail_always: Some("rm helper4"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert!(!labels[cp..].iter().any(|l| l == "start d"), "{labels:?}");
    let text = host.errors.join("\n");
    assert!(
        text.contains("The bank was not changed, but the daemon d is left stopped because the helper container helper4 could not be removed and may still be writing into its /data volume: run docker rm -f helper4, then docker start d. Nothing was restored."),
        "{text}"
    );
}

#[test]
fn an_interrupt_between_the_replay_and_the_swap_says_the_database_is_restored() {
    let setup = Setup::new(true);
    let replay = HAPPY.iter().position(|l| *l == "replay").unwrap();
    let mut docker = Fake {
        interrupt_after: Some(replay),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 130);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert!(
        !labels[replay..]
            .iter()
            .any(|l| l == "start d" || l == "start-a helper5"),
        "{labels:?}"
    );
    let text = host.errors.join("\n");
    assert!(
        text.contains("The database db holds the backup, but the state volume is unchanged: it was not yet replaced. The daemon d is left stopped"),
        "{text}"
    );
    assert!(
        text.contains("or, once the cause is fixed, finish this restore with:"),
        "{text}"
    );
    assert!(setup.journal().exists());
}

#[test]
fn a_mount_below_data_the_free_space_helper_sees_is_refused_before_the_drop() {
    // A host mount below a bind-mounted /data: not in `docker inspect`, in
    // the helper's mount table.
    let setup = Setup::new(true);
    let mut docker = Fake {
        df_nested: Some("/data/models"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 2, "{:?}", host.errors);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert!(
        !labels
            .iter()
            .any(|l| l == "drop" || l == "create stage-helper"),
        "{labels:?}"
    );
    let text = host.errors.join("\n");
    assert!(
        text.starts_with("restore failed: d has a volume mounted at /data/models, inside its /data volume; restore replaces only /data and cannot replace a volume mounted inside it."),
        "{text}"
    );
    assert!(
        text.contains("The bank was not changed and the daemon d was started again."),
        "{text}"
    );
    assert!(!setup.journal().exists());
}

#[test]
fn a_mount_the_swap_finds_below_data_leaves_the_state_unchanged_and_says_so() {
    let setup = Setup::new(true);
    let mut docker = Fake {
        swap_nested: Some("/data/a\\040b"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 2, "{:?}", host.errors);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    let swap = HAPPY.iter().position(|l| *l == "start-a helper5").unwrap();
    assert_eq!(labels[..=swap], HAPPY[..=swap], "{labels:?}");
    assert!(!labels[swap..].iter().any(|l| l == "start d"), "{labels:?}");
    let text = host.errors.join("\n");
    assert!(
        text.starts_with("restore failed: d has a volume mounted at /data/a b, inside its /data volume, so the state volume could not be replaced."),
        "{text}"
    );
    assert!(
        text.contains("The database db holds the backup, but the state volume is unchanged: nothing in it was replaced, because a volume is mounted inside /data at /data/a b. The daemon d is left stopped"),
        "{text}"
    );
    // A command that can succeed: the database alone, as it was.
    let dump =
        Path::new(&setup.plan.safety_dir).join(format!("pseudolife_safety_memory-{STAMP}.sql.gz"));
    let database_back = format!(
        "Put the database back as it was before this restore (this run did not change the state volume) with:\n  pseudolife-mcp restore {} --container=p",
        execute::shell_word(&format!("--backup-file={}", dump.to_string_lossy()))
    );
    assert!(text.contains(&database_back), "{text}");
    let line = text
        .lines()
        .find(|l| l.contains("pseudolife-mcp restore") && l.contains(&*dump.to_string_lossy()))
        .unwrap();
    assert!(!line.contains("--state-archive"), "{line}");
    assert!(
        text.contains("or mount that volume outside /data and finish this restore with:"),
        "{text}"
    );
    assert!(setup.journal().exists());
}

const HAPPY_NO_STATE: [&str; 17] = [
    "stale",
    "stop d",
    "create sweep-helper",
    "start-a helper1",
    "rm helper1",
    "safety-dump",
    "create safety-helper",
    "start-a helper2",
    "rm helper2",
    "copy-in",
    "gunzip",
    "df /tmp",
    "df /",
    "drop",
    "create-db",
    "revoke",
    "replay",
];

#[test]
fn a_failure_with_an_earlier_journal_names_both_safety_backups() {
    let setup = Setup::new(false);
    // The first run dies in its replay: its journal names its pair.
    let mut docker = Fake {
        fail_on: Some("replay"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    // The second run dies in its replay too: both pairs are named, each
    // with what it restores and when it was taken.
    let mut docker = Fake {
        fail_on: Some("replay"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let text = host.errors.join(
        "
",
    );
    let safety = Path::new(&setup.plan.safety_dir);
    let first = safety.join(format!("pseudolife_safety_memory-{STAMP}.sql.gz"));
    let second = safety.join("pseudolife_safety_memory-20261114-000000-abcde001.sql.gz");
    let date = "2026-11-14T00:00:00+11:00";
    assert!(
        text.contains(&format!(
            "Recover the bank as it was before the interrupted restore of {date} with:
  pseudolife-mcp restore {}",
            execute::shell_word(&format!("--backup-file={}", first.to_string_lossy()))
        )),
        "{text}"
    );
    assert!(
        text.contains(&format!(
            "or recover the bank as it was immediately before this run ({date}; the incomplete bank the earlier restore left, unless you repaired it since) with:
  pseudolife-mcp restore {}",
            execute::shell_word(&format!("--backup-file={}", second.to_string_lossy()))
        )),
        "{text}"
    );
    // So does the note beside the second pair.
    let note =
        std::fs::read_to_string(safety.join("pseudolife_restore-20261114-000000-abcde001.txt"))
            .unwrap();
    assert!(
        note.contains("as it was before the interrupted restore of"),
        "{note}"
    );
    assert!(
        note.contains("as it was immediately before this run"),
        "{note}"
    );
}

#[test]
fn a_third_failed_run_names_the_first_incidents_pair_and_its_own() {
    let setup = Setup::new(false);
    // Three runs, each dying in its replay. The journal keeps the first
    // run's pair: the second run's is a backup of the half-restored bank.
    for _ in 0..3 {
        let mut docker = Fake {
            fail_on: Some("replay"),
            ..Fake::default()
        };
        let mut host = ready_host();
        assert_eq!(setup.run(&mut docker, &mut host), 1);
        if setup.runs.get() < 3 {
            continue;
        }
        let text = host.errors.join("\n");
        let safety = Path::new(&setup.plan.safety_dir);
        let word = |stamp: &str| {
            execute::shell_word(&format!(
                "--backup-file={}",
                safety
                    .join(format!("pseudolife_safety_memory-{stamp}.sql.gz"))
                    .to_string_lossy()
            ))
        };
        assert!(
            text.contains(&format!(
                "Recover the bank as it was before the interrupted restore of 2026-11-14T00:00:00+11:00 with:\n  pseudolife-mcp restore {}",
                word(STAMP)
            )),
            "{text}"
        );
        assert!(
            text.contains(&format!(
                "unless you repaired it since) with:\n  pseudolife-mcp restore {}",
                word("20261114-000000-abcde002")
            )),
            "{text}"
        );
        assert!(!text.contains("abcde001"), "{text}");
    }
}

#[test]
fn a_journal_that_cannot_be_removed_is_reported() {
    let dir = Dir::new();
    assert_eq!(execute::clear_journal(&dir.0.join("absent.json")), None);
    // A directory where the journal is: removal fails, and says so.
    let blocked = dir.0.join("restore-in-progress-x.json");
    std::fs::create_dir_all(&blocked).unwrap();
    let warning = execute::clear_journal(&blocked).unwrap();
    assert!(warning.contains("could not be removed"), "{warning}");
    assert!(warning.contains("delete that file by hand"), "{warning}");
}

#[test]
fn a_value_starting_with_a_dash_is_never_read_as_an_option() {
    let mut setup = Setup::new(false);
    setup.plan.db = "-x".into();
    let mut docker = Fake {
        fail_on: Some("replay"),
        ..Fake::default()
    };
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 1);
    let text = host.errors.join(
        "
",
    );
    assert!(text.contains(" --db=-x "), "{text}");
    assert!(!text.contains(" --db -x"), "{text}");
}

#[test]
fn an_absent_database_is_restored_without_a_safety_dump() {
    let mut setup = Setup::new(false);
    setup.plan.exists = false;
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    assert!(!docker.calls.iter().any(|c| label(c) == "safety-dump"));
    assert!(
        host.info
            .iter()
            .any(|l| l.contains("(destination database was absent)"))
    );
}

#[test]
fn leave_stopped_and_a_stopped_daemon_are_kept_stopped() {
    let mut setup = Setup::new(false);
    setup.plan.was_running = false;
    setup.args.leave_stopped = true;
    let mut docker = Fake::default();
    let mut host = ready_host();
    assert_eq!(setup.run(&mut docker, &mut host), 0, "{:?}", host.errors);
    let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
    assert!(
        !labels.iter().any(|l| l == "stop d" || l == "start d"),
        "{labels:?}"
    );
    assert_eq!(
        host.info.last().unwrap(),
        "Restore complete; daemon left stopped (--leave-stopped)."
    );
    assert_eq!(host.health_calls, 0);
}

#[test]
fn readiness_needs_ready_true_except_before_0_20() {
    assert!(execute::ready(&json!({"status": "ok", "ready": true})));
    assert!(!execute::ready(&json!({"status": "ok", "ready": false})));
    assert!(!execute::ready(
        &json!({"status": "starting", "ready": true})
    ));
    assert!(execute::ready(
        &json!({"status": "ok", "version": "0.19.3"})
    ));
    assert!(!execute::ready(
        &json!({"status": "ok", "version": "0.20.0"})
    ));
    assert!(!execute::ready(&json!({"status": "ok"})));
}

#[test]
fn printed_commands_quote_for_this_platform() {
    if cfg!(windows) {
        assert_eq!(execute::shell_word(r"C:\x\b.sql.gz"), r"C:\x\b.sql.gz");
        assert_eq!(execute::shell_word("it's here"), "'it''s here'");
        // PowerShell also ends a quote at U+2018..U+201B.
        assert_eq!(execute::shell_word("a\u{2019}s"), "'a\u{2019}\u{2019}s'");
    } else {
        assert_eq!(execute::shell_word("/x/b.sql.gz"), "/x/b.sql.gz");
        assert_eq!(execute::shell_word("it's here"), r"'it'\''s here'");
    }
    let _ = Stop("x".into(), 1);
}
