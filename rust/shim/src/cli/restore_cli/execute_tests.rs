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

const RUN_TAG: &str = "0123456789abcdef";
const STAMP: &str = "20261114-000000-abcdef01";
const DF: &str =
    "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1 1 99999999 1% /\n";

fn gz(data: &[u8]) -> Vec<u8> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

fn dump() -> Vec<u8> {
    gz(b"SELECT 1;\n-- PostgreSQL database dump complete\n")
}

/// A one-file tar.gz (`./config.yaml`), as `tar czf - -C /data .` makes.
fn archive() -> Vec<u8> {
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

/// A temporary directory, removed on drop.
struct Dir(PathBuf);

impl Dir {
    fn new() -> Self {
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
struct Fake {
    calls: Vec<Vec<String>>,
    inputs: Vec<Option<PathBuf>>,
    fail_at: Option<usize>,
    /// Fail the first call with this label.
    fail_on: Option<&'static str>,
    interrupt_at: Option<usize>,
    interrupted: bool,
    interruptible: bool,
    /// helper id -> its script.
    helpers: HashMap<String, String>,
    /// A safety dump that lacks the completion marker.
    truncated_dump: bool,
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
                    write(&archive());
                    ok("")
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
struct FakeHost {
    info: Vec<String>,
    errors: Vec<String>,
    health: Option<Value>,
    health_calls: usize,
    /// How many runs this setup made before this one.
    run: u32,
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
    fn host_space(&mut self, _directory: &str) -> Step<()> {
        Ok(())
    }
}

struct Setup {
    dir: Dir,
    args: Args,
    plan: Plan,
    runs: std::cell::Cell<u32>,
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
            data_dir: self.data_dir(),
        };
        execute::execute(docker, host, &cx)
    }
}

fn ready_host() -> FakeHost {
    FakeHost {
        health: Some(json!({"status": "ok", "ready": true})),
        ..FakeHost::default()
    }
}

/// The happy path's Docker calls, by a short label each.
fn label(call: &[String]) -> String {
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
        ["create", .., s] if s.contains("tar --exclude") => "create extract-helper".into(),
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

const HAPPY: [&str; 24] = [
    "stale",
    "stop d",
    "safety-dump",
    "create safety-helper",
    "start-a helper1",
    "rm helper1",
    "copy-in",
    "gunzip",
    "create extract-helper",
    "cp state",
    "df /tmp",
    "df /",
    "create df-helper",
    "start-a helper3",
    "rm helper3",
    "drop",
    "create-db",
    "revoke",
    "replay",
    "start-a helper2",
    "start d",
    // cleanup
    "rm staged",
    "rm helper2",
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
        docker.inputs[6].as_deref(),
        Some(setup.args.backup_file.as_path())
    );
    let safety = Path::new(&setup.plan.safety_dir);
    assert!(
        safety
            .join(format!("pseudolife_memory-{STAMP}.sql.gz"))
            .is_file()
    );
    assert!(
        safety
            .join(format!("pseudolife_state-{STAMP}.tgz"))
            .is_file()
    );
    assert!(
        safety
            .join(format!("pseudolife_restore-{STAMP}.txt"))
            .is_file()
    );
    assert!(
        !safety
            .join(format!("pseudolife_memory-{STAMP}.sql.gz.part"))
            .exists()
    );
    assert!(!setup.journal().exists());
    let recovery = host
        .info
        .iter()
        .find(|line| line.starts_with("If this restore fails"))
        .unwrap();
    assert!(
        recovery.contains(&format!("pseudolife_memory-{STAMP}.sql.gz")),
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
        1..=14 => "stopped",
        15..=18 => "replacing",
        19 => "state",
        _ => "restored",
    }
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
    // A free-space refusal exits 2 like every refusal; other failures 1.
    let expected_code = if interrupt {
        130
    } else if matches!(index, 10 | 11 | 13) {
        2
    } else {
        1
    };
    // Clearing stale files and removing a finished helper are best effort:
    // their failure is ignored (an interrupt there stops at the next step).
    if matches!(index, 0 | 5 | 14) && !interrupt {
        assert_eq!(code, 0, "{context}");
        assert_eq!(labels, HAPPY[..HAPPY.len() - 1].to_vec(), "{context}");
        return;
    }
    assert_eq!(code, expected_code, "{context}");
    assert_eq!(labels[..=index], HAPPY[..=index], "{context}");
    let after: Vec<&str> = labels[index + 1..].iter().map(String::as_str).collect();
    let text = host.errors.join("\n");
    let staged = index >= 6;
    // The extract helper exists once its create succeeded.
    let extract = index >= 9;
    let mut expected: Vec<&str> = Vec::new();
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
            expected.push("start d");
            assert!(text.contains("The bank was not changed and the daemon d was started again. Nothing was restored."), "{context}");
            assert!(
                text.contains("retry with:\n  pseudolife-mcp restore --backup-file"),
                "{context}"
            );
            assert!(!setup.journal().exists(), "{context}");
        }
        "replacing" => {
            expected.push("terminate");
            if index == 15 {
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
    if staged {
        expected.push("rm staged");
    }
    if extract {
        expected.push("rm helper2");
    }
    // A safety helper or df helper whose start failed is still removed.
    if matches!(index, 4 | 13) {
        expected.insert(
            0,
            if index == 4 {
                "rm helper1"
            } else {
                "rm helper3"
            },
        );
    }
    assert_eq!(after, expected, "{context}");
    // Never a step past the failure: no drop after an early failure, no
    // daemon start after the drop began.
    if index < 15 {
        assert!(!labels.iter().any(|l| l == "drop"), "{context}");
    }
    if (15..20).contains(&index) {
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
        "pseudolife_{kind}-{STAMP}.{}",
        if kind == "memory" { "sql.gz" } else { "tgz" }
    ));
    execute::shell_word(&format!("--backup-file={}", path.to_string_lossy()))
}

#[test]
fn a_failure_at_every_step_stops_there_and_names_the_banks_state() {
    for index in 0..=20 {
        check_failure_at(index, false);
    }
}

#[test]
fn an_interrupt_at_every_step_stops_there_and_names_the_banks_state() {
    for index in 0..=20 {
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
    assert_eq!(labels, ["stale", "stop d", "safety-dump", "start d"]);
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
            "If you have repaired it since, this run changed nothing: start the daemon with:\n  docker start d\nthen delete {}",
            setup.journal().display()
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
    assert_eq!(
        labels[replay + 1..],
        ["terminate", "rm staged"],
        "{labels:?}"
    );
    // A run stopped before the drop restarts the daemon successfully.
    let setup = Setup::new(false);
    let mut docker = Fake {
        interrupt_at: Some(6),
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

const HAPPY_NO_STATE: [&str; 14] = [
    "stale",
    "stop d",
    "safety-dump",
    "create safety-helper",
    "start-a helper1",
    "rm helper1",
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
    let first = safety.join(format!("pseudolife_memory-{STAMP}.sql.gz"));
    let second = safety.join("pseudolife_memory-20261114-000000-abcde001.sql.gz");
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
                    .join(format!("pseudolife_memory-{stamp}.sql.gz"))
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
